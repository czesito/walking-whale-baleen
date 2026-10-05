"""Routes and rendering (spec §12, Appendix B) with fake converters and Starlette's TestClient:
setup pages, validation, options, preview, start, the run page live and finished, the results
table (pagination, filters), the inspector, the report download, history, tools, settings."""

from __future__ import annotations

import html
import json
import re
import time
from pathlib import Path

import pytest

from baleen.model import STATUS_LABEL, Status
from baleen.server import views
from baleen.server.present import duration, size, split_path, when
from tests.ui import fake_routes, harness

PORT = 8774


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    ctx = harness.build_ctx(tmp_path, PORT, monkeypatch)
    yield ctx, harness.client(ctx), tmp_path
    fake_routes.GATE.set()
    ctx.engine.cancel()
    ctx.engine.wait(30)


class _Page:
    def __init__(self, r) -> None:  # noqa: ANN001
        self.status_code = r.status_code
        self.headers = r.headers
        self.text = html.unescape(r.text)


def page(c, path: str) -> _Page:  # noqa: ANN001
    """A full page as the browser loads it (no HX-Request), text unescaped for copy checks."""
    return _Page(c.get(path, headers={"hx-request": ""}))


def start_convert(ctx, c, src: Path, out: Path, *, wait: bool = True) -> str:  # noqa: ANN001
    data = {"source_dir": str(src), "output_dir": str(out)}
    r = c.post("/api/convert", data=data)
    assert r.status_code == 200 and "Start converting?" in r.text
    assert json.loads(r.headers["hx-trigger-after-settle"]) == {"baleen:modal-open": True}
    r = c.post("/api/convert", data={**data, "confirmed": "1"})
    assert r.status_code == 204, r.text
    run_id = r.headers["hx-redirect"].rsplit("/", 1)[1]
    if wait:
        assert ctx.engine.wait(60)
    return run_id


def small_tree(root: Path) -> None:
    for rel, data in harness.PROTOTYPE_ROWS:
        harness.write(root, rel, data)


# --------------------------------------------------------------------------- formats (design §10)


def test_formats() -> None:
    assert size(980_000) == "980 KB" and size(4_820_000) == "4.82 MB" and size(1_200_000_000) == "1.2 GB"
    assert duration(18 * 60 + 42) == "18m 42s" and duration(3900) == "1h 05m" and duration(20) == "0m 20s"
    assert split_path("2004-07 Poetry evening/notes.txt") == ("2004-07 Poetry evening/", "notes.txt")
    assert split_path("C:\\Archive\\x") == ("C:\\Archive\\", "x")
    assert when(None) == "—"
    assert when("2026-10-05T12:00:00Z").count(":") == 1


# --------------------------------------------------------------------------- setup pages


def test_root_redirects_to_the_first_workflow(env) -> None:  # noqa: ANN001
    _ctx, c, _ = env
    r = c.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/convert"


def test_convert_page_is_generated_from_the_registry(env) -> None:  # noqa: ANN001
    _ctx, c, _ = env
    html = page(c, "/convert").text
    for text in ("Turn legacy files into archival formats: JPEG, TIFF, PDF/A, MP4 and M4A.",
                 "<b>Your source folder is never changed.</b>", "Source folder", "Output folder",
                 "PDF/A level", "Text encoding when unsure", "Attachments", "Hide e-mail addresses in PDFs",
                 "Video quality when re-encoding", "Audio-only output", "Also copy them to the output folder",
                 "Presets arrive in v1.1", "Choose both folders to preview.", "Choose a source and an output folder.",
                 'aria-current="page"', "Paste a folder path, or choose Browse…", "Waiting for the folder dialog…"):
        assert text in html, text
    assert re.search(r'id="ab-start"\s+disabled', html)


def test_check_page_has_the_explainer_and_no_options(env) -> None:  # noqa: ANN001
    _ctx, c, _ = env
    html = page(c, "/check").text
    assert "What Baleen checks" in html and "<b>JPEG and TIFF</b> · decode every frame fully" in html
    assert "options-card" not in html and "Choose a folder to check." in html


def test_browse_is_hidden_without_a_folder_dialog(env) -> None:  # noqa: ANN001
    ctx, c, _ = env
    ctx.picker = None
    html = page(c, "/convert").text
    assert "Browse…" not in html and "Paste a folder path" in html


def test_validate_path_messages_and_overlap(env) -> None:  # noqa: ANN001
    ctx, c, tmp = env
    src = tmp / "src"
    src.mkdir()
    r = c.post("/api/validate-path", data={"wf": "convert", "field": "source_dir", "source_dir": f'"{src}"'})
    assert 'data-state="ok"' in r.text and "Readable · read-only for Baleen" in r.text
    assert ctx.store.workflow()["source_dir"] == str(src)  # saved as last used (§11), quotes stripped
    r = c.post("/api/validate-path", data={"wf": "convert", "field": "output_dir", "source_dir": str(src),
                                            "output_dir": str(src / "converted")})
    assert "This folder is inside the source folder. Choose a folder outside it." in r.text
    assert 'id="ab-start" disabled' in r.text  # action bar out of band
    r = c.post("/api/validate-path", data={"wf": "convert", "field": "output_dir", "source_dir": str(src),
                                            "output_dir": str(tmp)})
    assert "The source folder is inside this folder." in r.text
    r = c.post("/api/validate-path", data={"wf": "convert", "field": "source_dir", "source_dir": str(tmp / "nope")})
    assert "Folder not found" in r.text
    r = c.post("/api/validate-path", data={"wf": "convert", "field": "source_dir", "source_dir": "relative"})
    assert "full path" in r.text
    r = c.post("/api/validate-path", data={"wf": "convert", "field": "output_dir", "source_dir": str(src),
                                            "output_dir": str(tmp / "out")})
    assert "Writable · " in r.text and "Ready. A preview is optional but recommended." in r.text
    assert c.post("/api/validate-path", data={"wf": "convert", "field": "nope"}).status_code == 400


def test_options_save_and_mark_the_preset_modified(env) -> None:  # noqa: ANN001
    ctx, c, _ = env
    r = c.post("/api/settings", data={"ui": "options:convert", "pdfa_level": "1b"})
    assert ctx.store.workflow()["pdfa_level"] == "1b"
    assert "Default (modified)" in r.text and 'id="opt-pdfa_level-1b" aria-pressed="true"' in r.text
    r = c.post("/api/settings", data={"ui": "options:convert", "reset": "workflow"})
    assert ctx.store.workflow()["pdfa_level"] == "2b" and "Default (modified)" not in r.text
    assert c.post("/api/settings", data={"pdfa_level": "9z"}).status_code == 400


def test_preview_runs_polls_and_goes_stale(env) -> None:  # noqa: ANN001
    ctx, c, tmp = env
    src, out = tmp / "src", tmp / "out"
    small_tree(src)
    r = c.post("/api/preview", data={"wf": "convert", "source_dir": str(src), "output_dir": str(out)})
    assert 'data-preview="running"' in r.text or 'data-preview="done"' in r.text
    for _ in range(200):
        r = c.post("/api/preview", data={"wf": "convert", "poll": "1"})
        if 'data-preview="done"' in r.text:
            break
        time.sleep(0.05)
    assert "Files found" in r.text and "Renamed to avoid clashes" in r.text and "Known before starting" in r.text
    assert "Preview ready: 25 files" in r.text  # polite announcement (§12.10)
    assert "preview up to date" in r.text
    r = c.post("/api/settings", data={"ui": "options:convert", "video_quality": "standard"})
    assert 'data-preview="stale"' in r.text and "Preview is out of date" in r.text
    assert not list(out.glob("**/*"))  # nothing written


def test_start_requires_valid_folders_then_confirms_then_runs(env) -> None:  # noqa: ANN001
    ctx, c, tmp = env
    src = tmp / "src"
    small_tree(src)
    r = c.post("/api/convert", data={"source_dir": str(src), "output_dir": str(src / "x")})
    assert r.headers.get("hx-reswap") == "none" and "inside the source folder" in r.text
    run_id = start_convert(ctx, c, src, tmp / "out")
    html = page(c, f"/runs/{run_id}").text
    assert "Convert run" in html and "Completed · 8 to look at" in html
    assert "8 of 25 files need your attention" in html
    assert "PDF/A-2b" in html and "Copy archival: on" in html  # settings chips
    assert "Download report" in html and "Open output folder" in html


def test_one_job_at_a_time(env) -> None:  # noqa: ANN001
    ctx, c, tmp = env
    src = tmp / "src"
    harness.make_batch(src, n=6, hold_from=1)
    fake_routes.GATE.clear()
    run_id = start_convert(ctx, c, src, tmp / "out", wait=False)
    html = page(c, "/convert").text
    assert "A run is in progress" in html and f'href="/runs/{run_id}"' in html
    assert "Wait for the current run to finish before starting another." in html
    assert re.search(r'id="ab-start"\s+disabled', html)
    check = page(c, "/check").text
    assert "Wait for the current run to finish." in check
    fake_routes.GATE.set()
    assert ctx.engine.wait(30)


# --------------------------------------------------------------------------- run page


def test_live_run_page_progress_and_cancel(env) -> None:  # noqa: ANN001
    ctx, c, tmp = env
    src = tmp / "src"
    harness.make_batch(src, n=24, hold_from=10)
    fake_routes.GATE.clear()
    run_id = start_convert(ctx, c, src, tmp / "out", wait=False)
    deadline = time.monotonic() + 20
    while len(fake_routes.HELD) < 1 and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.3)
    html = page(c, f"/runs/{run_id}").text
    assert "Running" in html and "of 24 files" in html and "In progress (" in html
    assert 'id="run-processor"' in html and "Using <b>4 of 8</b> cores" in html
    assert "Cancel run" in html and 'id="tpl-cancel"' in html and "Keep running" in html
    assert re.search(r"<title>\d+% · Convert · Baleen</title>", html)  # UI-G2
    frag = c.get(f"/runs/{run_id}/progress").text
    assert 'id="live-progress"' in frag and 'hx-swap-oob="true"' in frag and 'id="res-row"' not in frag
    job = c.get("/job").text
    assert "Converting" in job and 'data-state="running"' in job and 'hx-trigger="baleen:poll"' in job
    r = c.post("/api/settings", data={"ui": f"runres:{run_id}", "processor_use": "gentle"})
    assert json.loads(r.headers["hx-trigger"]) == {"baleen:toast": "Applies to new tasks right away"}
    assert "Using <b>2 of 8</b> cores" in r.text  # gentle on 8 cores (§5.5)
    r = c.post("/api/job/cancel")
    assert r.status_code == 204 and "baleen:modal-close" in r.headers["hx-trigger"]
    assert "Stopping after" in c.get(f"/runs/{run_id}/progress").text
    fake_routes.GATE.set()
    assert ctx.engine.wait(30)
    final = c.get(f"/runs/{run_id}/progress").text
    assert '<div id="run-body" hx-swap-oob="true">' in final and "Run cancelled after" in final
    assert "Run finished" in final and 'data-state="cancelled"' in final  # rail card: finished
    assert "hx-get=\"/runs/" in final and "baleen:poll" not in final.split('id="run-body"')[1].split("</div>")[0]


def test_results_table_pagination_and_filters(env) -> None:  # noqa: ANN001
    ctx, c, tmp = env
    src = tmp / "src"
    harness.make_archive(src, total=250, converts=5)
    run_id = start_convert(ctx, c, src, tmp / "out")
    p1 = c.get(f"/runs/{run_id}/rows").text
    assert "1–100 of 250" in p1 and p1.count('<tr tabindex="0"') == 100
    assert 'id="rows-prev" aria-label="Previous page" disabled' in p1
    p3 = c.get(f"/runs/{run_id}/rows?page=3")
    assert "201–250 of 250" in p3.text and p3.text.count('<tr tabindex="0"') == 50
    assert p3.headers["hx-push-url"] == f"/runs/{run_id}?page=3"
    assert re.search(r'id="rows-next" aria-label="Next page" disabled', p3.text)
    # problems first: Failed, then Needs review, then Unsupported, then OK, Ignored
    order = re.findall(r'<span class="badge (\w+)">', p1)
    ranks = ["failed", "review", "unsupported", "ok", "ignored", "skipped"]
    assert [ranks.index(k) for k in order] == sorted(ranks.index(k) for k in order)
    review = c.get(f"/runs/{run_id}/rows?status=review")
    assert "1–5 of 5" in review.text and review.text.count('<span class="badge review">') == 5
    assert 'id="tile-review" aria-pressed="true"' in review.text  # tiles out of band
    assert review.headers["hx-push-url"] == f"/runs/{run_id}?status=review"
    q = c.get(f"/runs/{run_id}/rows", params={"q": "poetry"})
    assert "1–1 of 1" in q.text and "notes.txt" in q.text
    none = c.get(f"/runs/{run_id}/rows", params={"q": "zzz-nothing"})
    assert "No files match this filter." in none.text and "0 of 0" in none.text
    # Reason codes carry their human label (UI-R4); paths keep the name visible (D-15).
    assert 'title="Text encoding not certain">ENCODING_UNCERTAIN' in c.get(
        f"/runs/{run_id}/rows", params={"q": "notes"}).text
    assert '<span class="dir">2004-07 Poetry evening/</span><span class="name">notes.txt</span>' in q.text
    deep = page(c, f"/runs/{run_id}?status=failed")
    assert 'id="tile-failed" aria-pressed="true"' in deep.text and "1–1 of 1" in deep.text


def test_inspector_and_report_download(env) -> None:  # noqa: ANN001
    ctx, c, tmp = env
    src = tmp / "src"
    small_tree(src)
    run_id = start_convert(ctx, c, src, tmp / "out")
    rows = c.get(f"/runs/{run_id}/rows", params={"q": "notes.txt"}).text
    n = re.search(r'data-n="(\d+)"', rows).group(1)
    insp = html.unescape(c.get(f"/runs/{run_id}/rows/{n}").text)
    for text in ("What happened", "Text encoding not certain", "ENCODING_UNCERTAIN", "What to do",
                 "Baleen couldn't tell how this text file is encoded", "No output was written.",
                 "Reveal source", "Copy row", "No checks ran for this file."):
        assert text in insp, text
    assert "Reveal output" not in insp
    rows = c.get(f"/runs/{run_id}/rows", params={"q": "DSCF0128"}).text
    n = re.search(r'data-n="(\d+)"', rows).group(1)
    ok = c.get(f"/runs/{run_id}/rows/{n}").text
    assert "V-AV-DUR" in ok and "58.04 s vs 58.00 s" in ok and "V-HASH" in ok and "Reveal output" in ok
    assert "What to do" not in ok  # OK rows need nothing
    assert c.get(f"/runs/{run_id}/rows/9999").status_code == 404
    rep = c.get(f"/runs/{run_id}/report.csv")
    assert rep.status_code == 200 and rep.headers["content-type"].startswith("text/csv")
    assert f"report-{run_id}.csv" in rep.headers["content-disposition"]
    assert rep.content.startswith(b"\xef\xbb\xbfrun_id,source_path")


def test_run_page_survives_a_restart(env, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    """UI-R8: a finished run is read back from the run index, run JSON and journal / CSV."""
    ctx, c, tmp = env
    src = tmp / "src"
    small_tree(src)
    run_id = start_convert(ctx, c, src, tmp / "out")
    entry = ctx.engine.index.get(run_id)
    for _ in range(100):  # Windows: a virus scanner may still hold the just-closed file
        try:
            Path(entry.journal_path).unlink()  # rebuilt from the CSV below
            break
        except PermissionError:
            time.sleep(0.1)
    fresh = harness.build_ctx(tmp, PORT, monkeypatch)
    html = page(harness.client(fresh), f"/runs/{run_id}").text
    assert "Completed · 8 to look at" in html and "PDF/A-2b" in html
    # Rebuilt in Baleen's own data folder: viewing a page never writes into the output (SEC-5).
    assert not Path(entry.journal_path).exists()
    assert (fresh.home.data_dir / "cache" / "journals" / Path(entry.journal_path).name).exists()


def test_unknown_run_and_page(env) -> None:  # noqa: ANN001
    _ctx, c, _ = env
    r = page(c, "/runs/20200101-000000")
    assert r.status_code == 404 and "This run isn't in the history" in r.text
    r = page(c, "/no/such/page")
    assert r.status_code == 404 and "This page doesn't exist" in r.text


# --------------------------------------------------------------------------- history & system pages


def test_runs_history_and_filters(env) -> None:  # noqa: ANN001
    ctx, c, tmp = env
    assert "No runs yet" in page(c, "/runs").text
    src = tmp / "src"
    small_tree(src)
    start_convert(ctx, c, src, tmp / "out")
    r = c.post("/api/check", data={"check_dir": str(src)})
    assert r.status_code == 204
    assert ctx.engine.wait(30)
    html = page(c, "/runs").text
    assert "2 runs · the 100 most recent are kept" in html and "8 to look at" in html
    assert 'class="count">2<' in html
    only_check = c.get("/runs?wf=check", headers={"hx-target": "runs-table"}).text
    assert only_check.startswith('<section class="card tbl-card" id="runs-table">')
    assert only_check.count('id="run-row-') == 1
    assert c.get("/runs", params={"q": "nowhere"}, headers={"hx-target": "runs-table"}).text.count(
        'id="run-row-') == 0


def test_tools_page_and_recheck(env) -> None:  # noqa: ANN001
    ctx, c, _ = env
    assert "All 5 tools found" in page(c, "/tools").text
    ctx.tools.missing_set = {"verapdf"}
    r = c.post("/api/tools/recheck")
    assert "1 tool is missing" in r.text and "Affects:" in r.text and "Fix:" in r.text
    assert 'id="rail-tools-dot" title="A tool is missing" hx-swap-oob="true"' in r.text
    assert json.loads(r.headers["hx-trigger"]) == {"baleen:toast": "All tools re-checked"}
    conv = page(c, "/convert").text
    assert "veraPDF isn't available" in conv and 'class="dot bad"' in conv
    ctx.tools.missing_set = {"libreoffice"}
    assert "LibreOffice isn't available" in page(c, "/convert").text
    assert "LibreOffice isn't available" not in page(c, "/check").text  # Check never converts


def test_settings_page_order_and_prefs(env) -> None:  # noqa: ANN001
    ctx, c, _ = env
    html = page(c, "/settings").text
    order = [html.index(f'class="card-title"{t}') for t in (' id="resT">Resource use<', ">When a run finishes<",
                                                             ">Data<", ">Advanced<")]
    assert order == sorted(order)  # UI-S1
    assert "8 processor cores" in html and "16 GB memory" in html and "Folders on a network drive" in html
    assert "http://127.0.0.1:8774" in html and "data/logs/baleen.log · stays on this computer" in html
    r = c.post("/api/settings", data={"ui": "prefs", "notify_on_finish": "true"})
    assert ctx.store.app()["notify_on_finish"] is True
    assert json.loads(r.headers["hx-trigger"]) == {"baleen:prefs": {"notify": True, "title": True}}
    r = c.post("/api/settings", data={"ui": "reset", "reset": "workflow"})
    assert r.status_code == 204 and "Options reset" in r.headers["hx-trigger"]


def test_work_folder_validation(env) -> None:  # noqa: ANN001
    ctx, c, tmp = env
    src = tmp / "src"
    src.mkdir()
    ctx.store.update({"source_dir": str(src)})
    r = c.post("/api/settings", data={"ui": "resource", "work_dir": str(src / "work")})
    assert "The work folder must be outside the source and output folders." in r.text
    assert ctx.store.app()["work_dir"] == "data/work"
    r = c.post("/api/settings", data={"ui": "resource", "work_dir": "relative"})
    assert "full path" in r.text
    good = tmp / "work"
    r = c.post("/api/settings", data={"ui": "resource", "work_dir": str(good)})
    assert ctx.store.app()["work_dir"] == str(good) and "local disk" in r.text
    c.post("/api/settings", data={"ui": "resource", "work_dir": str(ctx.home.default_work_dir)})
    assert ctx.store.app()["work_dir"] == "data/work"


def test_folder_dialog_endpoint(env) -> None:  # noqa: ANN001
    ctx, c, tmp = env
    src = tmp / "picked"
    src.mkdir()
    harness.FakePicker.path = None
    r = c.post("/api/pick-folder", data={"wf": "convert", "field": "source_dir"})
    assert r.status_code == 204  # cancelled: nothing changes (UI-C6)
    harness.FakePicker.path = str(src)
    r = c.post("/api/pick-folder", data={"wf": "convert", "field": "source_dir"})
    assert "Readable · read-only for Baleen" in r.text
    assert json.loads(r.headers["hx-trigger"]) == {"baleen:set-value": {"id": "f-source_dir", "value": str(src)}}
    assert ctx.store.workflow()["source_dir"] == str(src)
    j = c.post("/api/pick-folder", data={"wf": "convert", "field": "source_dir"}, headers={"hx-request": ""})
    assert j.json() == {"path": str(src)}
    ctx.picker = None
    assert c.post("/api/pick-folder", data={"wf": "convert", "field": "source_dir"}).status_code == 404


def test_status_vocabulary_matches_the_design() -> None:
    assert [STATUS_LABEL[s] for s in (Status.OK, Status.NEEDS_REVIEW, Status.FAILED, Status.UNSUPPORTED,
                                      Status.IGNORED, Status.SKIPPED)] == [
        "OK", "Needs review", "Failed", "Unsupported", "Ignored", "Skipped"]
    assert views.finish_text("Convert", "completed", 1670, 8, 1670) == "Convert finished · 1,670 files · 8 to look at"
