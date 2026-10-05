"""Documents route: format sniffing and password detection (§6.2), on synthetic files (workstream b).

A tiny OLE (CFB v3) writer builds DOC/XLS/PPT/encrypted-OOXML structures, small streams in the
mini stream and large ones in regular sectors, so both reader paths are covered.
"""

from __future__ import annotations

import copy
import io
import struct
import zipfile

import pytest

from baleen import settings as S
from baleen.convert import document as D
from baleen.convert.base import LoJob, LoResult, ProbeContext, SourceRef
from baleen.home import Home
from baleen.model import Mode
from baleen.tools import Toolset

ENDOFCHAIN, FREESECT, FATSECT, NOSTREAM = 0xFFFFFFFE, 0xFFFFFFFF, 0xFFFFFFFD, 0xFFFFFFFF


def make_cfb(streams: dict[str, bytes]) -> bytes:
    """A valid compound file with the given root-level streams (512-byte sectors)."""
    ss = 512
    names = sorted(streams, key=lambda n: (len(n), n.upper()))
    sectors: list[bytes] = []
    fat: list[int] = []

    def alloc(data: bytes) -> int:
        cnt = max(1, -(-len(data) // ss))
        start = len(sectors)
        for k in range(cnt):
            sectors.append(data[k * ss:(k + 1) * ss].ljust(ss, b"\0"))
            fat.append(start + k + 1 if k < cnt - 1 else ENDOFCHAIN)
        return start

    mini = bytearray()
    minifat: list[int] = []
    starts: dict[str, int] = {}
    for n in names:
        d = streams[n]
        if len(d) < 4096:
            cnt = max(1, -(-len(d) // 64))
            s0 = len(mini) // 64
            starts[n] = s0
            minifat += [s0 + k + 1 if k < cnt - 1 else ENDOFCHAIN for k in range(cnt)]
            mini += d.ljust(cnt * 64, b"\0")
    mf = b"".join(struct.pack("<I", x) for x in minifat)
    minifat_start = alloc(mf) if minifat else ENDOFCHAIN
    n_minifat = -(-len(mf) // ss) if minifat else 0
    mini_start = alloc(bytes(mini)) if mini else ENDOFCHAIN
    for n in names:
        if len(streams[n]) >= 4096:
            starts[n] = alloc(streams[n])

    def entry(name: str, kind: int, right: int, child: int, start: int, size: int) -> bytes:
        nm = (name.encode("utf-16-le") + b"\0\0") if name else b""
        return (nm.ljust(64, b"\0") + struct.pack("<HBB", len(nm), kind, 1)
                + struct.pack("<III", NOSTREAM, right, child) + b"\0" * 36 + struct.pack("<IQ", start, size))

    ents = [entry("Root Entry", 5, NOSTREAM, 1 if names else NOSTREAM, mini_start, len(mini))]
    for i, n in enumerate(names, start=1):
        ents.append(entry(n, 2, i + 1 if i < len(names) else NOSTREAM, NOSTREAM, starts[n], len(streams[n])))
    while len(ents) % 4:
        ents.append(entry("", 0, NOSTREAM, NOSTREAM, 0, 0))
    dir_start = alloc(b"".join(ents))
    n_fat = 1
    while len(sectors) + n_fat > n_fat * (ss // 4):
        n_fat += 1
    fat_start = len(sectors)
    fat += [FATSECT] * n_fat
    fat += [FREESECT] * (n_fat * (ss // 4) - len(fat))
    fb = b"".join(struct.pack("<I", x) for x in fat)
    sectors += [fb[k * ss:(k + 1) * ss] for k in range(n_fat)]
    difat = [fat_start + k for k in range(n_fat)] + [FREESECT] * (109 - n_fat)
    header = (D.OLE_MAGIC + b"\0" * 16 + struct.pack("<HHHHH", 0x3E, 3, 0xFFFE, 9, 6) + b"\0" * 6
              + struct.pack("<9I", 0, n_fat, dir_start, 0, 4096, minifat_start, n_minifat, ENDOFCHAIN, 0)
              + struct.pack("<109I", *difat))
    return header + b"".join(sectors)


def word_stream(encrypted: bool, nfib: int = 0x00C1, size: int = 4608) -> bytes:
    b = bytearray(size)
    struct.pack_into("<HH", b, 0, 0xA5EC, nfib)
    struct.pack_into("<H", b, 10, 0x0100 if encrypted else 0)
    return bytes(b)


def biff(records: list[tuple[int, bytes]]) -> bytes:
    return b"".join(struct.pack("<HH", t, len(d)) + d for t, d in records)


BOF = (0x0809, struct.pack("<HHHHII", 0x0600, 0x0005, 0, 0, 0, 0))
EOF_REC = (0x000A, b"")


def current_user(token: int) -> bytes:
    return struct.pack("<HHI", 0, 0x0FF6, 20) + struct.pack("<II", 20, token) + b"\0" * 16


def odf(encrypted: bool, package: bool = False) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        if package:
            z.writestr("encrypted-package", b"\x00" * 64)
        enc = '<manifest:encryption-data manifest:checksum-type="SHA1/1K"/>' if encrypted else ""
        z.writestr("META-INF/manifest.xml", '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:'
                   f'xmlns:manifest:1.0"><manifest:file-entry manifest:full-path="content.xml">{enc}'
                   "</manifest:file-entry></manifest:manifest>")
        z.writestr("content.xml", "<x/>")
    return buf.getvalue()


def ooxml(part: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr(part, "<x/>")
    return buf.getvalue()


def sniff(data: bytes, ext: str) -> D.Sniff:
    return D.sniff(io.BytesIO(data), ext)


@pytest.mark.parametrize(("data", "ext", "fmt", "enc"), [
    (make_cfb({"WordDocument": word_stream(False), "1Table": b"t" * 100}), ".doc", "MS Word 97–2003", None),
    (make_cfb({"WordDocument": word_stream(True), "1Table": b"t" * 100}), ".doc", "MS Word 97–2003", "password"),
    (make_cfb({"WordDocument": word_stream(True, size=1024)}), ".doc", "MS Word 97–2003", "password"),  # mini stream
    (make_cfb({"WordDocument": word_stream(False, nfib=0x65)}), ".doc", "MS Word 6.0/95", None),
    (make_cfb({"Workbook": biff([BOF, (0x0085, b"\0" * 10), EOF_REC])}), ".xls", "MS Excel 97–2003", None),
    (make_cfb({"Workbook": biff([BOF, (0x002F, b"\x01\x00" + b"\0" * 52), EOF_REC])}), ".xls", "MS Excel 97–2003",
     "maybe"),
    (make_cfb({"Book": biff([BOF, (0x002F, b"\0" * 6), EOF_REC])}), ".xls", "MS Excel 5.0/95", "maybe"),
    (make_cfb({"PowerPoint Document": b"p" * 5000, "Current User": current_user(0xE391C05F)}), ".ppt",
     "MS PowerPoint 97–2003", None),
    (make_cfb({"PowerPoint Document": b"p" * 5000, "Current User": current_user(0xF3D1C4DF)}), ".ppt",
     "MS PowerPoint 97–2003", "password"),
    (make_cfb({"EncryptionInfo": b"i" * 200, "EncryptedPackage": b"e" * 6000}), ".docx",
     "MS Word 2007+ (encrypted)", "maybe"),
    (odf(False), ".odt", "ODF Text Document", None),
    (odf(True), ".odt", "ODF Text Document (encrypted)", "password"),
    (odf(False, package=True), ".odt", "ODF document (encrypted)", "password"),
    (ooxml("word/document.xml"), ".docx", "MS Word 2007+", None),
    (ooxml("xl/workbook.xml"), ".xlsx", "MS Excel 2007+", None),
    (ooxml("ppt/presentation.xml"), ".pptx", "MS PowerPoint 2007+", None),
    (b"{\\rtf1\\ansi hello}", ".doc", "Rich Text Format", None),
    (b"\xffWPC" + b"\x10\x00\x00\x00\x01\x0a\x02\x01" + b"\x00\x00" + b"\0" * 50, ".wpd", "WordPerfect", None),
    (b"\xffWPC" + b"\x10\x00\x00\x00\x01\x0a\x02\x01" + b"\x34\x12" + b"\0" * 50, ".wpd", "WordPerfect (encrypted)",
     "password"),
    (b"<html><body><table><tr><td>1</td></tr></table></body></html>", ".xls", "HTML", None),
    (b"Plain text saved as .doc\n", ".doc", "MS Word document", None),
])
def test_sniff_formats_and_protection(data: bytes, ext: str, fmt: str, enc: str | None) -> None:
    s = sniff(data, ext)
    assert (s.source_format, s.encryption) == (fmt, enc)
    assert s.infilter is None and not s.empty


def test_damaged_ole_gets_the_extension_filter() -> None:
    import random

    rnd = random.Random(1)
    garbage = D.OLE_MAGIC + bytes(rnd.getrandbits(8) for _ in range(4000))
    s = sniff(garbage, ".doc")
    assert s.infilter == "MS Word 97" and "damaged" in s.source_format
    # a valid compound file without any document stream is damaged too
    s = sniff(make_cfb({"SomethingElse": b"x" * 100}), ".xls")
    assert s.infilter == "MS Excel 97"
    assert sniff(make_cfb({"SomethingElse": b"x"}), ".wps").infilter is None


def test_truncated_ole_and_zip() -> None:
    doc = make_cfb({"WordDocument": word_stream(False)})
    s = sniff(doc[:700], ".doc")
    assert s.infilter == "MS Word 97"
    s = sniff(ooxml("word/document.xml")[:60], ".docx")
    assert "damaged" in s.source_format and s.infilter is None


def test_cfb_reader_mini_and_regular_streams() -> None:
    data = make_cfb({"Small": b"abc" * 10, "Large": bytes(range(256)) * 40})
    cfb = D.Cfb(io.BytesIO(data))
    kids = cfb.root_children()
    assert set(kids) == {"small", "large"}
    assert cfb.read_stream(kids["small"]) == b"abc" * 10
    assert cfb.read_stream(kids["large"], 1 << 20) == bytes(range(256)) * 40


def test_cfb_loop_is_an_error_not_a_hang() -> None:
    data = bytearray(make_cfb({"Large": b"L" * 5000}))
    cfb = D.Cfb(io.BytesIO(bytes(data)))
    start = cfb.root_children()["large"].start
    # point the FAT entry of the stream's first sector at itself
    per = 512 // 4
    fat_sector = cfb.difat[start // per]
    off = (fat_sector + 1) * 512 + (start % per) * 4
    data[off:off + 4] = struct.pack("<I", start)
    cfb = D.Cfb(io.BytesIO(bytes(data)))
    with pytest.raises(D.CfbError):
        cfb.read_stream(cfb.root_children()["large"], 1 << 20)


# --------------------------------------------------------------------------- route decisions


def ctx(mode: Mode = Mode.CONVERT) -> ProbeContext:
    home = Home(__import__("pathlib").Path("."))
    return ProbeContext(mode, copy.deepcopy(S.defaults()), Toolset(home), home)


def probe(data: bytes, ext: str, mode: Mode = Mode.CONVERT):  # noqa: ANN201
    return D.DocumentRoute().probe(ctx(mode), SourceRef(name="x" + ext, ext=ext, data=data, size=len(data)))


def test_probe_plan_time_decisions() -> None:
    p = probe(make_cfb({"WordDocument": word_stream(True)}), ".doc")
    assert p.reasons == ["PASSWORD_PROTECTED"] and p.method == ""
    p = probe(b"", ".doc")
    assert p.reasons == ["SOURCE_UNREADABLE"] and "empty" in p.message
    # Excel encryption: no plan-time decision, LibreOffice is tried (default password)
    p = probe(make_cfb({"Workbook": biff([BOF, (0x002F, b"\0" * 6), EOF_REC])}), ".xls")
    assert p.reasons == [] and p.data["encryption"] == "maybe"
    # Check mode never decides conversion problems
    p = probe(make_cfb({"WordDocument": word_stream(True)}), ".doc", Mode.CHECK)
    assert p.reasons == []


def test_batch_key_includes_forced_infilter() -> None:
    from baleen.model import Action, Category, PlanItem

    it = PlanItem(1, "a.doc", "C:/a.doc", 1, 0, ".doc", Category.DOCUMENT, Action.CONVERT, "document", ".pdf", "",
                  "a.pdf", data={"infilter": "MS Word 97"})

    class W:
        plan = it
        settings = {"pdfa_level": "1b"}

    assert D.DocumentRoute().batch_key(W()) == "MS Word 97|writer_pdf_Export|1b"  # type: ignore[arg-type]
    it.data = {"infilter": None}
    assert D.DocumentRoute().batch_key(W()) == "|writer_pdf_Export|1b"  # type: ignore[arg-type]
    job = LoJob("x/doc-1.doc", "x", "writer_pdf_Export", "1b", None)
    assert job.batch_key == D.DocumentRoute().batch_key(W())  # type: ignore[arg-type]


def test_finish_maps_failed_excel_encryption_to_password_protected(tmp_path) -> None:  # noqa: ANN001
    from baleen.convert.base import RunContext, WorkItem
    from baleen.model import Action, Category, PlanItem

    home = Home(tmp_path)
    run = RunContext("r", Mode.CONVERT, home, Toolset(home), copy.deepcopy(S.defaults()), str(tmp_path), None,
                     str(tmp_path))
    for enc, expect in (("maybe", "PASSWORD_PROTECTED"), (None, "CONVERSION_ERROR")):
        it = PlanItem(1, "a.xlsx", None, 1, 0, ".xlsx", Category.DOCUMENT, Action.CONVERT, "document", ".pdf", "",
                      "a.pdf", data={"encryption": enc})
        w = WorkItem(it, run, str(tmp_path / "1"), None, False)
        job = LoJob("in.xlsx", str(tmp_path), "calc_pdf_Export", "2b")
        D.DocumentRoute().finish(None, w, LoResult(job, False, None, "CONVERSION_ERROR", "no output"))  # type: ignore[arg-type]
        assert w.reasons == [expect] and w.done
