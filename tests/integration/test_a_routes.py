"""Workstream (a) integration checks beyond the golden statuses, with the real tools:

- text: CJK text survives the LibreOffice "Text (encoded)" import exactly (V-TEXT counts
  alone could not prove it);
- HTML (§16.1, R-05, AC-10): the placeholder text is in the PDF, script output is not, the
  relative image is rendered, and no network request is attempted (a local HTTP server on
  127.0.0.1:8770 sees nothing; a control run on the unsanitised page proves it would);
- R-02: real veraPDF validates a CJK-named PDF read in place (Check mode).
"""

from __future__ import annotations

import copy
import http.server
import os
import shutil
import sys
import threading
from pathlib import Path

import pytest

from baleen import proc
from baleen import settings as S
from baleen.home import Home
from baleen.model import Mode
from baleen.report import read_csv
from baleen.runner import Engine, RunSpec
from baleen.settings import SettingsStore
from baleen.tools import Toolset

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

pytestmark = pytest.mark.integration
PORT = 8770


def tools(home: Home) -> Toolset:
    return Toolset(home)


def need(home: Home, *keys: str) -> None:
    missing = [k for k in keys if tools(home).path(k) is None]
    if missing:
        pytest.skip(f"missing tools: {missing}")


def run(home: Home, src: Path, out: Path | None, mode: Mode = Mode.CONVERT, **overrides):  # noqa: ANN003, ANN201
    store = SettingsStore(home.settings_path)
    st = copy.deepcopy(store.snapshot())
    for k, v in overrides.items():
        sec, _ = S.find_field(k)
        st[sec][k] = v
    st["app"]["keep_awake"] = False
    job = Engine(home, store, tools(home)).start(RunSpec(mode, str(src), str(out) if out else None, st),
                                                 background=False)
    return job, {r.source_path: r for r in read_csv(job.report_path())}


def pdf_text(path: Path) -> str:
    from pypdf import PdfReader

    return "".join((p.extract_text() or "") for p in PdfReader(str(path)).pages)


def squash(s: str) -> str:
    return "".join(s.split())


def image_count(path: Path) -> int:
    from pypdf import PdfReader

    def walk(res, seen) -> int:  # noqa: ANN001
        xo = res.get("/XObject") if res else None
        n = 0
        for v in (xo.get_object().values() if xo else []):
            o = v.get_object()
            if id(o) in seen:
                continue
            seen.add(id(o))
            if o.get("/Subtype") == "/Image":
                n += 1
            elif o.get("/Subtype") == "/Form":
                n += walk(o.get("/Resources"), seen)
        return n

    return sum(walk(p.get("/Resources"), set()) for p in PdfReader(str(path)).pages)


@pytest.fixture()
def home(tmp_path: Path) -> Home:
    h = Home(tmp_path / "home")
    h.ensure()
    return h


def test_cjk_text_survives_import(home: Home, tmp_path: Path) -> None:
    need(home, "libreoffice")
    from tests.fixtures.groups.text import BIG5, CJK

    src = tmp_path / "src 訪談"
    src.mkdir()
    (src / "utf8.txt").write_bytes(CJK.encode())
    (src / "utf16.txt").write_bytes(CJK.replace("\n", "\r\n").encode("utf-16"))
    (src / "big5.txt").write_bytes(BIG5.encode("big5"))
    _job, rows = run(home, src, tmp_path / "out", txt_encoding="big5")
    for name, text in (("utf8.txt", CJK), ("utf16.txt", CJK), ("big5.txt", BIG5)):
        r = rows[name]
        assert "V-TEXT=pass" in r.checks, (name, r.reason, r.message)
        assert squash(pdf_text(tmp_path / "out" / r.output_path)) == squash(text), name


def test_html_placeholder_script_and_local_image(home: Home, tmp_path: Path) -> None:
    need(home, "libreoffice")
    from tests.fixtures.groups import html as fx

    src = tmp_path / "pages 訪談"
    src.mkdir()
    fx.build(src, None)
    _job, rows = run(home, src, tmp_path / "out")
    remote = tmp_path / "out" / rows["remote.html"].output_path
    text = squash(pdf_text(remote))
    assert squash("[external resource not archived: http://images.example.invalid/remote-photo.jpg]") in text
    assert squash("[external resource not archived: //cdn.example.invalid/bg.png]") in text
    assert "scriptoutput" not in text
    local = tmp_path / "out" / rows["local_image.html"].output_path
    assert image_count(local) == 1  # the relative image, read from the source folder
    assert image_count(remote) == 0


class _Recorder(http.server.BaseHTTPRequestHandler):
    hits: list[str] = []

    def do_GET(self) -> None:  # noqa: N802
        _Recorder.hits.append(self.path)
        self.send_response(404)
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture()
def recorder():  # noqa: ANN201
    _Recorder.hits = []
    srv = http.server.HTTPServer(("127.0.0.1", PORT), _Recorder)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield _Recorder.hits
    srv.shutdown()
    srv.server_close()


NET_PAGE = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>net</title>
<link rel="stylesheet" href="http://127.0.0.1:{PORT}/style.css">
<style>@import url("http://127.0.0.1:{PORT}/import.css"); body {{ background: url(http://127.0.0.1:{PORT}/bg.png) }}</style>
<script src="http://127.0.0.1:{PORT}/script.js"></script></head>
<body background="http://127.0.0.1:{PORT}/body.png"><p>Offline page.</p>
<img src="http://127.0.0.1:{PORT}/img.png"><img src="//127.0.0.1:{PORT}/proto.png">
<img srcset="http://127.0.0.1:{PORT}/1x.png 1x, http://127.0.0.1:{PORT}/2x.png 2x">
<iframe src="http://127.0.0.1:{PORT}/frame.html"></iframe><object data="http://127.0.0.1:{PORT}/obj.swf"></object>
<embed src="http://127.0.0.1:{PORT}/embed.swf"><video poster="http://127.0.0.1:{PORT}/poster.png"></video>
<table background="http://127.0.0.1:{PORT}/table.png"><tr>
<td style="background-image:url('http://127.0.0.1:{PORT}/td.png')">x</td></tr></table>
</body></html>"""


def test_no_network_fetch(home: Home, tmp_path: Path, recorder: list[str]) -> None:
    need(home, "libreoffice")
    src = tmp_path / "src"
    src.mkdir()
    (src / "net.html").write_text(NET_PAGE, encoding="utf-8")
    _job, rows = run(home, src, tmp_path / "out")
    assert rows["net.html"].output_path == "net.pdf", rows["net.html"].message
    assert recorder == [], f"R-05: LibreOffice attempted network requests: {recorder}"

    # Control: the same page, unsanitised, straight into LibreOffice does reach the server -
    # so an empty recorder above means "no attempt", not "the probe can't see attempts".
    ctl = tmp_path / "control"
    ctl.mkdir()
    shutil.copyfile(src / "net.html", ctl / "net.html")
    soffice = tools(home).path("libreoffice")
    profile = (tmp_path / "ctl-profile").resolve().as_uri()
    proc.run([soffice, f"-env:UserInstallation={profile}", "--headless", "--invisible", "--nologo", "--norestore",
              "--nolockcheck", "--nodefault", "--infilter=HTML (StarWriter)", "--convert-to", "pdf",
              "--outdir", str(ctl), str(ctl / "net.html")], timeout=180, env=tools(home).env())
    assert recorder, "control run made no request; the network probe would not detect fetches"


def _pdfa(home: Home, tmp_path: Path) -> bytes:
    soffice = tools(home).path("libreoffice")
    work = tmp_path / "gen"
    work.mkdir()
    (work / "doc.html").write_text("<html><body><p>Synthetic PDF/A-2b, 訪談.</p></body></html>", encoding="utf-8")
    profile = (tmp_path / "gen-profile").resolve().as_uri()
    proc.run([soffice, f"-env:UserInstallation={profile}", "--headless", "--invisible", "--nologo", "--norestore",
              "--nolockcheck", "--nodefault", "--infilter=HTML (StarWriter)", "--convert-to",
              'pdf:writer_pdf_Export:{"SelectPdfVersion":{"type":"long","value":"2"}}', "--outdir", str(work),
              str(work / "doc.html")], timeout=180, env=tools(home).env())
    return (work / "doc.pdf").read_bytes()


def test_verapdf_with_cjk_and_space_paths_in_place(home: Home, tmp_path: Path) -> None:
    need(home, "libreoffice", "verapdf", "java")
    data = _pdfa(home, tmp_path)
    src = tmp_path / "檔案 資料夾 100% & co"
    (src / "子資料夾").mkdir(parents=True)
    names = ["訪談 紀錄.pdf", "子資料夾/中文名稱.pdf", "plain name.pdf"]
    for n in names:
        (src / n).write_bytes(data)
    before = {n: os.stat(src / n).st_nlink for n in names}
    _job, rows = run(home, src, None, mode=Mode.CHECK)
    for n in names:
        r = rows[n]
        assert (r.status, r.checks) == ("OK", "V-PDF-OPEN=pass;V-PDFA=pass(2b)"), (n, r.reason, r.message)
    assert {n: os.stat(src / n).st_nlink for n in names} == before  # sources never hard-linked (P1)
