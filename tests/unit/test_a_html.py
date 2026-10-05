"""HTML route: charset (§6.4/§6.3), staging copy, inlined local images, no fetchable URLs (R-05)."""

from __future__ import annotations

import base64
import io
from pathlib import Path

import pytest
from PIL import Image

from baleen.convert import html as H
from baleen.convert.html import HtmlRoute, Localiser, settle_html
from baleen.convert.sanitize import remote_urls_left
from baleen.model import Mode

from .a_support import FakeTaskContext, probe_ctx, run_ctx, src_bytes, work_item

ROUTE = HtmlRoute()


def opener(data: bytes):  # noqa: ANN201
    return lambda: io.BytesIO(data)


def page(body: str, head: str = "") -> str:
    return f"<html><head>{head}<title>t</title></head><body>{body}</body></html>"


# --------------------------------------------------------------------------- charset


@pytest.mark.parametrize(("data", "codec", "label"), [
    (b"\xef\xbb\xbf" + page("訪談").encode(), "utf-8-sig", "UTF-8, BOM"),
    (page("繁體", '<meta charset="big5">').encode("big5"), "cp950", "big5, declared"),
    (page("繁體", '<meta http-equiv="Content-Type" content="text/html; charset=Big5">').encode("big5"),
     "cp950", "big5, declared"),
    (('<?xml version="1.0" encoding="Shift_JIS"?>' + page("ｱｲｳ")).encode("cp932"), "cp932", "shift_jis, declared"),
    (page("Café", '<meta charset="iso-8859-1">').encode("cp1252"), "cp1252", "iso-8859-1, declared"),
    (page("簡體", '<meta charset="gb2312">').encode("gb18030"), "gb18030", "gb2312, declared"),
    (page("ascii", '<meta charset="utf-16">').encode(), "utf-8", "utf-16, declared"),
    (page("訪談").encode(), "utf-8", "UTF-8"),
])
def test_charset_sources(data, codec, label) -> None:  # noqa: ANN001
    enc = settle_html(opener(data), "auto")
    assert (enc.codec, enc.label, enc.reason) == (codec, label, None)


def test_declared_charset_that_does_not_decode_falls_back_to_6_3() -> None:
    # Declares Shift_JIS but the bytes are UTF-8 (strictly valid): §6.3 makes UTF-8 certain.
    utf8 = page("訪談", '<meta charset="shift_jis">').encode()
    assert settle_html(opener(utf8), "auto").codec == "utf-8"
    # Declares an unknown charset and is Big5: uncertain, and the message says why.
    big5 = page("繁體中文", '<meta charset="x-unknown-charset">').encode("big5")
    enc = settle_html(opener(big5), "auto")
    assert enc.reason == "ENCODING_UNCERTAIN" and "unknown charset" in enc.message and "Big5" in enc.message
    assert settle_html(opener(big5), "big5").codec == "cp950"


def test_undeclared_legacy_page() -> None:
    data = page("繁體中文").encode("big5")
    assert settle_html(opener(data), "auto").reason == "ENCODING_UNCERTAIN"
    assert settle_html(opener("Café".encode("cp1252") + b"\x81"), "big5").reason == "ENCODING_MISMATCH"


def test_probe_modes(home) -> None:  # noqa: ANN001
    data = page("繁體中文").encode("big5")
    assert ROUTE.probe(probe_ctx(home), src_bytes("p.htm", data)).reasons == ["ENCODING_UNCERTAIN"]
    assert ROUTE.probe(probe_ctx(home, Mode.CHECK), src_bytes("p.htm", data)).reasons == []
    ok = ROUTE.probe(probe_ctx(home), src_bytes("p.html", page("x", '<meta charset="utf-8">').encode()))
    assert ok.data == {"codec": "utf-8", "label": "utf-8, declared"} and ok.source_format == "HTML (utf-8, declared)"


# --------------------------------------------------------------------------- localiser


@pytest.fixture()
def site(tmp_path: Path) -> Path:
    root = tmp_path / "src root 訪談"
    (root / "pages" / "img dir").mkdir(parents=True)
    Image.new("RGB", (4, 4), (1, 2, 3)).save(root / "pages" / "img dir" / "pic one.jpg", "JPEG")
    Image.new("RGB", (4, 4)).save(root / "pages" / "logo.png", "PNG")
    Image.new("RGB", (4, 4)).save(tmp_path / "outside.png", "PNG")
    return root


def run_loc(site: Path, doc: str) -> tuple[str, Localiser]:
    loc = Localiser(str(site / "pages"), str(site))
    return loc.run(doc), loc


def test_relative_images_are_inlined(site: Path) -> None:
    out, loc = run_loc(site, '<p><img src="img%20dir/pic%20one.jpg?v=2#x" alt="a"><img src="img dir\\pic one.jpg">'
                             '<img src="./logo.png"></p><table background="logo.png"><tr><td>x</td></tr></table>')
    assert out.count('src="data:image/jpeg;base64,') == 2 and 'src="data:image/png;base64,' in out
    assert 'background="data:image/png;base64,' in out and 'alt="a"' in out
    assert len(loc.inlined) == 4 and not loc.dropped
    b64 = out.split('src="data:image/jpeg;base64,', 1)[1].split('"', 1)[0]
    assert base64.b64decode(b64) == (site / "pages" / "img dir" / "pic one.jpg").read_bytes()


@pytest.mark.parametrize(("src", "text"), [
    ("../../outside.png", "[local resource not archived: ../../outside.png]"),  # outside the source root
    ("missing.png", "[local resource not archived: missing.png]"),
    ("/abs/root.png", "[local resource not archived: /abs/root.png]"),
    ("file://nas.example.invalid/share/x.png", "[external resource not archived: file://nas.example.invalid/share/x.png]"),
    ("\\\\nas\\share\\x.png", "[external resource not archived: \\\\nas\\share\\x.png]"),
    ("vnd.sun.star.expand:$BRAND/x.png", "[external resource not archived: vnd.sun.star.expand:$BRAND/x.png]"),
])
def test_unresolvable_references_become_text(site: Path, src: str, text: str) -> None:
    import html

    out, _ = run_loc(site, f'<p><img src="{html.escape(src, quote=True)}"></p>')
    assert "<img" not in out and html.escape(text) in out


def test_local_file_uri_inside_root_is_inlined(site: Path) -> None:
    uri = (site / "pages" / "logo.png").as_uri()
    out, _ = run_loc(site, f'<img src="{uri}">')
    assert 'src="data:image/png;base64,' in out


def test_data_uris_and_other_elements_untouched(site: Path) -> None:
    doc = '<img src="data:image/png;base64,AAAA"><link rel="stylesheet" href="local.css"><a href="x.html">x</a>'
    out, _ = run_loc(site, doc)
    assert out == doc


def test_size_cap(site: Path, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(H, "MAX_INLINE_FILE", 10)
    out, loc = run_loc(site, '<img src="logo.png">')
    assert "[local resource not archived: logo.png]" in out and loc.dropped == ["logo.png"]


def test_attachment_pages_resolve_nothing_locally(tmp_path: Path) -> None:
    out = Localiser(None, None).run('<img src="logo.png">')
    assert "[local resource not archived: logo.png]" in out


# --------------------------------------------------------------------------- prepare


def test_prepare_staging_copy(home, site: Path, tmp_path: Path) -> None:  # noqa: ANN001
    markup = page(
        '<h1>繁體</h1><img src="img dir/pic one.jpg"><img src="http://example.invalid/r.jpg">'
        '<script>alert(1)</script><iframe src="https://example.invalid/"></iframe>'
        '<p style="background:url(//cdn.example.invalid/b.png)">x</p>',
        '<meta http-equiv="Content-Type" content="text/html; charset=big5">'
        '<link rel="stylesheet" href="https://example.invalid/s.css">')
    src = site / "pages" / "index.htm"
    src.write_bytes(markup.encode("big5"))
    pr = ROUTE.probe(probe_ctx(home), src_bytes(src.name, src.read_bytes()))
    run = run_ctx(home, tmp_path, source_root=site)
    wi = work_item(run, src, pr, n=12)
    ctx = FakeTaskContext()
    job = ROUTE.prepare(ctx, wi)
    assert Path(job.input_path).name == "lo_12.html" and job.infilter == "HTML (StarWriter)"
    raw = Path(job.input_path).read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    doc = raw[3:].decode("utf-8")
    assert "繁體" in doc and "big5" not in doc.lower()
    assert f'<base href="{(site / "pages").as_uri()}/">' in doc
    assert "<script" not in doc and "<iframe" not in doc and remote_urls_left(doc) == []
    assert "[external resource not archived: http://example.invalid/r.jpg]" in doc
    assert 'src="data:image/jpeg;base64,' in doc
    assert ctx.transfers  # the source folder was read under a transfer slot
    assert ROUTE.batch_key(wi) == "HTML (StarWriter)|writer_pdf_Export|2b"


def test_source_folder_is_never_written(home, site: Path, tmp_path: Path) -> None:  # noqa: ANN001
    src = site / "pages" / "p.html"
    src.write_bytes(page('<img src="logo.png">', '<meta charset="utf-8">').encode())
    before = sorted(p.name for p in site.rglob("*"))
    pr = ROUTE.probe(probe_ctx(home), src_bytes(src.name, src.read_bytes()))
    wi = work_item(run_ctx(home, tmp_path, source_root=site), src, pr)
    ROUTE.prepare(FakeTaskContext(), wi)
    assert sorted(p.name for p in site.rglob("*")) == before
