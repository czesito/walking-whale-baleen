"""R-05 / P8 / AC-10: LibreOffice never fetches content linked from documents or e-mails (workstream b).

A local HTTP listener (127.0.0.1:8775) stands in for the internet. Documents link images to it in
the ways Office formats allow: DOCX external image relationship and INCLUDEPICTURE field, ODT
linked image, RTF INCLUDEPICTURE, and raw HTML <img> (not sanitised, to test the profile itself).
E-mails carry a tracking pixel and remote CSS. Baleen's conversions must cause no request at all.
A control run with a fresh, unhardened profile shows that LibreOffice would fetch, so the listener
really sees attempts. Also measures batch vs one-process-per-file timings (DR-35).
"""

from __future__ import annotations

import copy
import os
import struct
import threading
import time
import zipfile
import zlib
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from baleen import proc
from baleen import settings as S
from baleen.convert import libreoffice as lo
from baleen.convert.base import LoJob, RunContext
from baleen.home import Home
from baleen.model import Mode
from baleen.report import read_csv
from baleen.runner import Engine, RunSpec
from baleen.settings import SettingsStore
from baleen.tools import Toolset

pytestmark = pytest.mark.integration
PORT = 8775


def _png() -> bytes:
    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + chunk(b"IEND", b""))


class Listener:
    def __init__(self, port: int) -> None:
        self.hits: list[str] = []
        hits = self.hits

        class H(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                hits.append(self.path)
                data = _png()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_HEAD = do_POST = do_GET

            def log_message(self, *a: object) -> None:
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", port), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture()
def listener():  # noqa: ANN201
    li = Listener(PORT)
    yield li
    li.close()


def url(name: str) -> str:
    return f"http://127.0.0.1:{PORT}/{name}.png"


def linked_docx() -> bytes:
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/'
                   'content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
                   'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName='
                   '"/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.'
                   'document.main+xml"/></Types>')
        z.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.'
                   'openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.'
                   'openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
                   "</Relationships>")
        z.writestr("word/_rels/document.xml.rels",
                   '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/'
                   'package/2006/relationships"><Relationship Id="rId9" Type="http://schemas.openxmlformats.org/'
                   f'officeDocument/2006/relationships/image" Target="{url("docx")}" TargetMode="External"/>'
                   "</Relationships>")
        z.writestr("word/document.xml",
                   '<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/'
                   'wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/'
                   'relationships" xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
                   'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:pic="http://schemas.'
                   'openxmlformats.org/drawingml/2006/picture"><w:body><w:p><w:r><w:t>Linked image:</w:t></w:r></w:p>'
                   '<w:p><w:r><w:drawing><wp:inline><wp:extent cx="952500" cy="952500"/><wp:docPr id="1" name="P"/>'
                   '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture"><pic:pic>'
                   '<pic:nvPicPr><pic:cNvPr id="1" name="P"/><pic:cNvPicPr/></pic:nvPicPr><pic:blipFill><a:blip '
                   'r:link="rId9"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill><pic:spPr><a:xfrm><a:off x="0" '
                   'y="0"/><a:ext cx="952500" cy="952500"/></a:xfrm><a:prstGeom prst="rect"/></pic:spPr></pic:pic>'
                   "</a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>"
                   '<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText xml:space="preserve"> '
                   f'INCLUDEPICTURE "{url("docxfield")}" \\d </w:instrText></w:r><w:r><w:fldChar w:fldCharType='
                   '"separate"/></w:r><w:r><w:t>x</w:t></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'
                   "</w:body></w:document>")
    return buf.getvalue()


def linked_odt() -> bytes:
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/vnd.oasis.opendocument.text")
        z.writestr("META-INF/manifest.xml",
                   '<?xml version="1.0" encoding="UTF-8"?><manifest:manifest xmlns:manifest="urn:oasis:names:tc:'
                   'opendocument:xmlns:manifest:1.0" manifest:version="1.2"><manifest:file-entry manifest:full-path='
                   '"/" manifest:media-type="application/vnd.oasis.opendocument.text"/><manifest:file-entry '
                   'manifest:full-path="content.xml" manifest:media-type="text/xml"/></manifest:manifest>')
        z.writestr("content.xml",
                   '<?xml version="1.0" encoding="UTF-8"?><office:document-content xmlns:office="urn:oasis:names:tc:'
                   'opendocument:xmlns:office:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
                   'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" xmlns:xlink="http://www.w3.org/'
                   '1999/xlink" xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" office:version='
                   '"1.2"><office:body><office:text><text:p>Linked image:</text:p><text:p><draw:frame draw:name="i1" '
                   'text:anchor-type="as-char" svg:width="2cm" svg:height="2cm">'
                   f'<draw:image xlink:href="{url("odt")}" xlink:type="simple" xlink:show="embed" '
                   'xlink:actuate="onLoad"/></draw:frame></text:p></office:text></office:body>'
                   "</office:document-content>")
    return buf.getvalue()


def linked_rtf() -> bytes:
    return ("{\\rtf1\\ansi\\deff0{\\fonttbl{\\f0 Arial;}}\\f0 Linked image: "
            "{\\field{\\*\\fldinst INCLUDEPICTURE \"" + url("rtf") + "\" \\\\d}{\\fldrslt x}}\\par}").encode()


def raw_html() -> bytes:
    """Unsanitised HTML with remote images: the profile alone must stop these. (A remote
    <link rel="stylesheet"> is fetched even with the hardened profile; only sanitising, §6.4,
    removes it, so Baleen never gives LibreOffice unsanitised HTML.)"""
    return (f'<html><body><p>Remote image:</p><img src="{url("html")}"><table background="{url("tbg")}"><tr><td>x'
            f'</td></tr></table><div style="background:url({url("bg")})">x</div></body></html>').encode()


def html_saved_as_doc() -> bytes:
    return (f'<html><head><meta charset="utf-8"><link rel="stylesheet" href="{url("webdoc-css")}"><style>'
            f'@import url("{url("webdoc-import")}");</style></head><body><h1>Web page saved as .doc</h1>'
            f'<img src="{url("webdoc-img")}"></body></html>').encode()


def tracking_email() -> bytes:
    m = EmailMessage()
    m["From"] = "Shop <shop@example.invalid>"
    m["To"] = "Archive <archive@example.invalid>"
    m["Subject"] = "Offer"
    m["Date"] = "Mon, 02 Oct 2006 09:30:00 +0800"
    m.set_content("plain")
    m.add_alternative(f'<html><head><style>body{{background:url("{url("mailbg")}")}}</style>'
                      f'<link rel="stylesheet" href="{url("mailcss")}"></head><body><p>Offer</p>'
                      f'<img src="{url("pixel")}" width="1" height="1"><img src="//127.0.0.1:{PORT}/proto.png">'
                      "</body></html>", subtype="html")
    return bytes(m)


def _tools(home: Home) -> Toolset:
    ts = Toolset(home)
    if ts.path("libreoffice") is None:
        pytest.skip("LibreOffice not available")
    return ts


def test_hardened_profile_fetches_nothing_and_control_does(tmp_path: Path, listener: Listener) -> None:
    home = Home(tmp_path / "home")
    home.ensure()
    ts = _tools(home)
    run = RunContext("r", Mode.CONVERT, home, ts, copy.deepcopy(S.defaults()), str(tmp_path), None,
                     str(tmp_path / "work"))
    items = tmp_path / "items"
    items.mkdir()
    files = {"doc-1.docx": linked_docx(), "doc-2.odt": linked_odt(), "doc-3.rtf": linked_rtf()}
    for n, d in files.items():
        (items / n).write_bytes(d)
    (items / "page-4.html").write_bytes(raw_html())
    ctx = SimpleNamespace(low_priority=True)
    writer = [LoJob(str(items / n), str(items), "writer_pdf_Export", "2b") for n in files]
    html = [LoJob(str(items / "page-4.html"), str(items), "writer_pdf_Export", "2b", "HTML (StarWriter)")]
    # The second and third calls start after ensure_profile() has rewritten the settings file.
    res = lo.convert_batch(ctx, run, writer) + lo.convert_batch(ctx, run, html)  # type: ignore[arg-type]
    (items / "page-5.html").write_bytes(raw_html())
    res += lo.convert_batch(ctx, run, [LoJob(str(items / "page-5.html"), str(items), "writer_pdf_Export", "2b",  # type: ignore[arg-type]
                                             "HTML (StarWriter)")])
    assert all(r.ok for r in res), [r.message for r in res]
    time.sleep(0.5)
    assert listener.hits == [], f"R-05: LibreOffice fetched {listener.hits}"

    # Control: a fresh profile without Baleen's settings fetches (so the listener sees attempts).
    plain = tmp_path / "plain-profile"
    soffice = ts.path("libreoffice")
    assert soffice
    r = proc.run([*lo.base_args(soffice, plain), "--convert-to", "pdf", "--outdir", str(tmp_path / "ctl"),
                  str(items / "doc-1.docx")], timeout=180, env=ts.env())
    r2 = proc.run([*lo.base_args(soffice, plain), "--infilter=HTML (StarWriter)", "--convert-to", "pdf", "--outdir",
                   str(tmp_path / "ctl"), str(items / "page-4.html")], timeout=180, env=ts.env())
    time.sleep(0.5)
    assert r.ok or r2.ok
    assert listener.hits, "control: an unhardened profile was expected to fetch the linked images"


def test_engine_run_with_linked_documents_and_tracking_email_is_offline(tmp_path: Path, listener: Listener) -> None:
    home = Home(tmp_path / "home")
    home.ensure()
    _tools(home)
    src = tmp_path / "src"
    src.mkdir()
    (src / "linked.docx").write_bytes(linked_docx())
    (src / "linked.odt").write_bytes(linked_odt())
    (src / "linked.rtf").write_bytes(linked_rtf())
    (src / "offer.eml").write_bytes(tracking_email())
    (src / "web.doc").write_bytes(html_saved_as_doc())
    store = SettingsStore(home.settings_path)
    st = copy.deepcopy(store.snapshot())
    st["app"]["keep_awake"] = False
    job = Engine(home, store, Toolset(home)).start(RunSpec(Mode.CONVERT, str(src), str(tmp_path / "out"), st),
                                                  background=False)
    rows = {r.source_path: r for r in read_csv(job.report_path())}
    assert set(rows) == {"linked.docx", "linked.odt", "linked.rtf", "offer.eml", "web.doc"}
    assert rows["web.doc"].category == "html" and "CONTENT_MISMATCH" in rows["web.doc"].reason
    for r in rows.values():
        assert r.status in ("OK", "NEEDS_REVIEW") and r.output_path, (r.source_path, r.reason, r.message)
    time.sleep(0.5)
    assert listener.hits == [], f"P8: fetched {listener.hits}"
    from pypdf import PdfReader

    text = PdfReader(str(tmp_path / "out" / "offer.pdf")).pages[0].extract_text()
    assert f"[external resource not archived: {url('pixel')}]" in text.replace("\n", "")


@pytest.mark.slow
def test_dr35_batch_vs_one_process_per_file(tmp_path: Path) -> None:
    """DR-35: 12 documents in one soffice call vs one process each (timings printed)."""
    home = Home(tmp_path / "home")
    home.ensure()
    ts = _tools(home)
    run = RunContext("r", Mode.CONVERT, home, ts, copy.deepcopy(S.defaults()), str(tmp_path), None,
                     str(tmp_path / "work"))
    ctx = SimpleNamespace(low_priority=False)
    rtf = rb"{\rtf1\ansi\deff0{\fonttbl{\f0 Arial;}}\f0 Synthetic document %d. " + b"Body text. " * 200 + rb"\par}"

    def jobs(tag: str) -> list[LoJob]:
        d = tmp_path / tag
        d.mkdir()
        out = []
        for i in range(12):
            p = d / f"doc-{i}.rtf"
            p.write_bytes(rtf.replace(b"%d", str(i).encode()))
            out.append(LoJob(str(p), str(d), "writer_pdf_Export", "2b"))
        return out

    lo.convert_batch(ctx, run, jobs("warm"))  # profile creation and first start are not measured
    t = time.monotonic()
    res = lo.convert_batch(ctx, run, jobs("batch"))
    batch = time.monotonic() - t
    t = time.monotonic()
    single = []
    for j in jobs("single"):
        single += lo.convert_batch(ctx, run, [j])
    each = time.monotonic() - t
    assert all(r.ok for r in res + single)
    print(f"\nDR-35 on {os.cpu_count()} threads: 12 docs, 8+4 per call {batch:.2f} s ({batch / 12:.2f} s/doc); "
          f"one process each {each:.2f} s ({each / 12:.2f} s/doc); {each / batch:.1f}x")
    assert batch < each
