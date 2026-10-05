"""Local server security (spec §13 SEC-1…SEC-7, DR-25, AC-12) with Starlette's TestClient."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from baleen.journal import Journal
from baleen.model import ItemResult
from baleen.report import RunEntry, journal_name
from baleen.server import routes as routes_mod
from baleen.server import security
from tests.ui import harness

PORT = 8771
CSP = "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
PAGES = ("/convert", "/check", "/runs", "/tools", "/settings", "/about")


@pytest.fixture()
def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    return harness.build_ctx(tmp_path, PORT, monkeypatch)


def plain(ctx, **kw):  # noqa: ANN001, ANN003, ANN201
    return harness.client(ctx, **kw)


# --------------------------------------------------------------------------- SEC-2 / SEC-3


def test_launch_token_is_256_bit_and_secret_values_differ() -> None:
    s = security.Secrets.generate()
    assert len(s.token) >= 43 and len(s.session) >= 43  # 32 random bytes, urlsafe base64
    assert s.token != s.session
    assert security.Secrets.generate().token != s.token


def test_auth_sets_session_cookie_and_redirects(ctx) -> None:  # noqa: ANN001
    c = plain(ctx, cookie=False)
    r = c.get(f"/auth?t={ctx.secrets.token}")
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"baleen_session={ctx.secrets.session};")
    parts = {p.strip().split("=")[0].lower() for p in cookie.split(";")[1:]}
    assert {"httponly", "samesite", "path"} <= parts
    assert "samesite=strict" in cookie.lower() and "path=/" in cookie.lower()
    assert "expires" not in parts and "max-age" not in parts  # session lifetime


@pytest.mark.parametrize("query", ["", "?t=", "?t=wrong", "?t=" + "A" * 43])
def test_auth_with_a_wrong_token_is_403(ctx, query: str) -> None:  # noqa: ANN001
    r = plain(ctx, cookie=False).get("/auth" + query)
    assert r.status_code == 403
    assert "Open Baleen from its launcher" in r.text
    assert "set-cookie" not in r.headers


@pytest.mark.parametrize("path", [*PAGES, "/", "/job", "/runs/20260101-000000", "/settings/plan", "/stopped"])
def test_no_cookie_is_403_page(ctx, path: str) -> None:  # noqa: ANN001
    """AC-12: no cookie → 403, with the page that explains the launcher (W-21)."""
    r = plain(ctx, cookie=False).get(path)
    assert r.status_code == 403
    assert "Open Baleen from its launcher" in r.text
    assert "Double-click <b>Start Baleen</b>" in r.text


def test_wrong_cookie_is_403(ctx) -> None:  # noqa: ANN001
    c = plain(ctx, cookie=False)
    c.cookies.set("baleen_session", ctx.secrets.session[:-1] + ("A" if ctx.secrets.session[-1] != "A" else "B"))
    assert c.get("/convert").status_code == 403
    c.cookies.set("baleen_session", ctx.secrets.token)  # the URL token is not the cookie
    assert c.get("/convert").status_code == 403


def test_no_cookie_post_is_403(ctx) -> None:  # noqa: ANN001
    r = plain(ctx, cookie=False).post("/api/settings", data={"pdfa_level": "1b"})
    assert r.status_code == 403
    assert ctx.store.workflow()["pdfa_level"] == "2b"


def test_static_needs_no_cookie_but_gets_the_headers(ctx) -> None:  # noqa: ANN001
    r = plain(ctx, cookie=False).get("/static/baleen.css")
    assert r.status_code == 200 and "--ww-abyss" in r.text
    assert r.headers["content-security-policy"] == CSP
    assert r.headers["cache-control"] == "no-cache"
    assert plain(ctx, cookie=False).get("/static/../templates/base.html").status_code in (403, 404)


def test_constant_time_compare_is_used() -> None:
    assert security.same("abc", "abc")
    assert not security.same("abd", "abc")
    assert not security.same(None, "abc")
    assert not security.same("", "abc")


# --------------------------------------------------------------------------- SEC-4


@pytest.mark.parametrize("host", ["evil.example", f"evil.example:{PORT}", "127.0.0.1", f"127.0.0.1:{PORT + 1}",
                                  f"192.168.1.10:{PORT}", f"[::1]:{PORT}", f"localhost.evil.example:{PORT}", ""])
def test_wrong_host_is_rejected(ctx, host: str) -> None:  # noqa: ANN001
    """AC-12 / SEC-4: DNS rebinding defence; even with a valid cookie."""
    r = plain(ctx).get("/convert", headers={"host": host})
    assert r.status_code == 400
    assert r.headers["content-security-policy"] == CSP


@pytest.mark.parametrize("host", [f"127.0.0.1:{PORT}", f"localhost:{PORT}", f"LOCALHOST:{PORT}"])
def test_loopback_hosts_are_accepted(ctx, host: str) -> None:  # noqa: ANN001
    assert plain(ctx).get("/convert", headers={"host": host}).status_code == 200


def test_non_loopback_client_is_rejected(ctx) -> None:  # noqa: ANN001
    from starlette.testclient import TestClient

    from baleen.server.app import create_app

    c = TestClient(create_app(ctx), base_url=ctx.origin, client=("192.168.1.20", 5000))
    c.cookies.set("baleen_session", ctx.secrets.session)
    assert c.get("/convert").status_code == 403


# --------------------------------------------------------------------------- SEC-5


def test_cross_origin_post_is_403(ctx) -> None:  # noqa: ANN001
    """AC-12: cross-origin POST → 403, even with the session cookie."""
    c = plain(ctx, origin=False)
    for origin in ("http://evil.example", "null", f"http://127.0.0.1:{PORT + 1}", f"https://127.0.0.1:{PORT}",
                   f"http://localhost:{PORT}"):
        r = c.post("/api/settings", data={"pdfa_level": "1b"}, headers={"origin": origin})
        assert r.status_code == 403, origin
    assert ctx.store.workflow()["pdfa_level"] == "2b"


def test_origin_must_match_the_host_used(ctx) -> None:  # noqa: ANN001
    c = plain(ctx, origin=False)
    r = c.post("/api/settings", data={"pdfa_level": "1b"},
               headers={"host": f"localhost:{PORT}", "origin": f"http://localhost:{PORT}"})
    assert r.status_code == 204
    assert ctx.store.workflow()["pdfa_level"] == "1b"


def test_without_origin_the_referer_must_match(ctx) -> None:  # noqa: ANN001
    c = plain(ctx, origin=False)
    assert c.post("/api/settings", data={"pdfa_level": "1b"}).status_code == 403  # neither header
    assert c.post("/api/settings", data={"pdfa_level": "1b"},
                  headers={"referer": "http://evil.example/convert"}).status_code == 403
    assert c.post("/api/settings", data={"pdfa_level": "1b"},
                  headers={"referer": f"http://127.0.0.1:{PORT}.evil.example/"}).status_code == 403
    assert ctx.store.workflow()["pdfa_level"] == "2b"
    ok = c.post("/api/settings", data={"pdfa_level": "3b"}, headers={"referer": f"{ctx.origin}/convert"})
    assert ok.status_code == 204
    assert ctx.store.workflow()["pdfa_level"] == "3b"


@pytest.mark.parametrize("path", ["/api/settings", "/api/validate-path", "/api/pick-folder", "/api/preview",
                                  "/api/convert", "/api/check", "/api/job/cancel", "/api/tools/recheck", "/api/open",
                                  "/api/quit"])
def test_state_changing_endpoints_are_post_only(ctx, path: str) -> None:  # noqa: ANN001
    r = plain(ctx).get(path)
    assert r.status_code == 405
    assert not ctx.quit.requested.is_set()


def test_other_methods_need_the_origin_check_too(ctx) -> None:  # noqa: ANN001
    c = plain(ctx, origin=False)
    for method in ("PUT", "DELETE", "PATCH"):
        assert c.request(method, "/api/settings").status_code == 403


# --------------------------------------------------------------------------- SEC-6


@pytest.mark.parametrize("path", [*PAGES, "/job", "/settings/plan", "/nope"])
def test_security_headers_on_every_dynamic_response(ctx, path: str) -> None:  # noqa: ANN001
    r = plain(ctx).get(path)
    assert r.headers["content-security-policy"] == CSP
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "same-origin"
    assert r.headers["cache-control"] == "no-store"


def test_headers_on_rejections_too(ctx) -> None:  # noqa: ANN001
    for r in (plain(ctx, cookie=False).get("/convert"), plain(ctx).get("/convert", headers={"host": "x"})):
        assert r.headers["content-security-policy"] == CSP
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["cache-control"] == "no-store"


class _InlineFinder(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.problems: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        names = {k for k, _ in attrs}
        if tag == "script" and "src" not in names:
            self.problems.append("inline <script>")
        if tag == "style":
            self.problems.append("<style> element")
        if "style" in names:
            self.problems.append(f"style attribute on <{tag}>")
        for k in names:
            if k.startswith("on"):
                self.problems.append(f"{k} handler on <{tag}>")
        for k, v in attrs:
            if k in ("href", "src") and v and v.strip().lower().startswith(("javascript:", "data:")):
                self.problems.append(f"{k}={v[:20]}")


@pytest.mark.parametrize("path", [*PAGES, "/runs/unknown"])
def test_no_inline_scripts_or_styles(ctx, path: str) -> None:  # noqa: ANN001
    """SEC-6: nothing the CSP would block; htmx's indicator styles are off."""
    html = plain(ctx, hx=False).get(path).text
    f = _InlineFinder()
    f.feed(html)
    assert not f.problems, f.problems
    assert '"includeIndicatorStyles":false' in html
    assert '"allowEval":false' in html


def test_forbidden_page_has_no_inline_code(ctx) -> None:  # noqa: ANN001
    f = _InlineFinder()
    f.feed(plain(ctx, cookie=False).get("/convert").text)
    assert not f.problems


def test_vendored_css_has_no_data_uris() -> None:
    css = (Path(__file__).resolve().parents[2] / "src" / "baleen" / "static" / "baleen.css").read_text("utf-8")
    assert "data:" not in css.split("*/", 1)[1]  # the header comment mentions it
    assert not re.search(r"@import|url\(\s*[\"']?https?:", css)


# --------------------------------------------------------------------------- SEC-7 /api/open


def _fake_run(ctx, tmp: Path, output_path: str) -> str:  # noqa: ANN001
    src, out = tmp / "src", tmp / "out"
    (src / "a").mkdir(parents=True)
    (src / "a" / "x.doc").write_bytes(b"x")
    (out / "_baleen").mkdir(parents=True)
    (out / "a").mkdir()
    (out / "a" / "x.pdf").write_bytes(b"pdf")
    (tmp / "secret.txt").write_text("no")
    run_id = "20260101-000000"
    j = Journal.create(str(out / "_baleen" / journal_name(run_id)), {"run_id": run_id}, [])
    j.commit(ItemResult(n=1, run_id=run_id, source_path="a/x.doc", output_path="a/x.pdf", status="OK"))
    j.commit(ItemResult(n=2, run_id=run_id, source_path="../secret.txt", output_path=output_path, status="OK"))
    j.close()
    ctx.engine.index.upsert(RunEntry(run_id, "convert", str(src), str(out), "2026-01-01T00:00:00Z",
                                     "2026-01-01T00:00:01Z", "completed", {"OK": 2}, 2, str(out / "_baleen")))
    return run_id


def test_open_reveals_only_inside_the_roots(ctx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    revealed: list[str] = []
    monkeypatch.setattr(routes_mod.osutil, "reveal", lambda p, **kw: revealed.append(p) or True)
    run_id = _fake_run(ctx, tmp_path / "w", "../../secret.txt")
    c = plain(ctx)
    ok = c.post("/api/open", data={"run": run_id, "n": "1", "what": "output"})
    assert ok.status_code == 204 and "Revealed in" in ok.headers["hx-trigger"]
    assert c.post("/api/open", data={"run": run_id, "n": "1", "what": "source"}).status_code == 204
    assert c.post("/api/open", data={"run": run_id, "what": "output_root"}).status_code == 204
    # Records that point outside the roots are refused, whatever the journal says.
    assert c.post("/api/open", data={"run": run_id, "n": "2", "what": "output"}).status_code == 403
    assert c.post("/api/open", data={"run": run_id, "n": "2", "what": "source"}).status_code == 403
    # The client never names a path; unknown runs and fields are refused.
    assert c.post("/api/open", data={"run": run_id, "what": "path", "path": str(tmp_path)}).status_code == 400
    assert c.post("/api/open", data={"run": "nope", "what": "output_root"}).status_code == 404
    assert c.post("/api/open", data={"what": "data"}).status_code == 204
    assert c.post("/api/open", data={"what": "logs"}).status_code == 204
    assert all("secret" not in p for p in revealed)
    assert len(revealed) == 5


def test_settings_accept_absolute_folder_paths_only(ctx) -> None:  # noqa: ANN001
    r = plain(ctx).post("/api/settings", data={"source_dir": "relative/path"})
    assert r.status_code == 400
    assert ctx.store.workflow()["source_dir"] == ""
    assert plain(ctx).post("/api/settings", data={"no_such_key": "1"}).status_code == 400
