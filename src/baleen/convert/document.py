"""Documents route (spec §6.2): Writer, Calc and Impress formats -> PDF/A with LibreOffice.

Documents lane, batched (DR-35): prepare() writes a uniquely named staging input, the lane
converts up to 8 files per soffice call (convert/libreoffice.py), finish() runs V-PDF-OPEN and
asks for V-PDFA at `pdfa_level`.

Content sniffing (read-only, bounded reads, never the whole file):
- source_format: e.g. "MS Word 97–2003", "MS Excel 2007+", "ODF Text Document", "Rich Text Format".
- Password protection (§6.2 PASSWORD_PROTECTED):
    DOC   FIB flag fEncrypted (WordDocument stream)          -> decided at plan time
    PPT   CurrentUserAtom headerToken 0xF3D1C4DF ("Current User") -> decided at plan time
    ODF   manifest:encryption-data, or an encrypted-package entry  -> decided at plan time
    WPD   non-zero encryption key in the WordPerfect header    -> decided at plan time
    XLS   FILEPASS record; OOXML: an OLE file with an EncryptedPackage stream
          -> LibreOffice is tried first, because Excel encrypts some workbooks with its default
             password ("VelvetSweatshop") and those open without one (measured on 26.8). If no
             PDF comes out, the result is PASSWORD_PROTECTED.
- Damaged OLE files: when an OLE (CFB) file has no readable application stream, LibreOffice
  is told the import filter for the extension. Without that, its type detection falls back to
  plain-text import and turns the damaged file into a PDF of garbage (measured, R-12).
- An empty file is SOURCE_UNREADABLE at plan time (LibreOffice would make an empty page).
"""

from __future__ import annotations

import io
import os
import struct
import zipfile
from dataclasses import dataclass
from typing import IO, ClassVar

from ..model import Action, Category, CheckState, Mode, Probe
from ..paths import long_path
from ..scheduler import Lane, TaskContext
from .base import LoJob, LoResult, ProbeContext, Route, SourceRef, WorkItem, register
from .formats import CALC_EXTS, EXPORT_FILTER, IMPRESS_EXTS

OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
ZIP_MAGIC = b"PK\x03\x04"

# Import filter to force for a damaged OLE file, by extension (see module docstring).
OLE_FILTER_BY_EXT = {".doc": "MS Word 97", ".xls": "MS Excel 97", ".ppt": "MS PowerPoint 97"}

GENERIC_FORMAT = {
    ".doc": "MS Word document", ".docx": "MS Word 2007+", ".rtf": "Rich Text Format",
    ".odt": "ODF Text Document", ".wpd": "WordPerfect", ".wps": "MS Works", ".sxw": "OpenOffice.org 1.x Writer",
    ".xls": "MS Excel workbook", ".xlsx": "MS Excel 2007+", ".ods": "ODF Spreadsheet",
    ".ppt": "MS PowerPoint presentation", ".pptx": "MS PowerPoint 2007+", ".odp": "ODF Presentation",
}

ODF_MIMETYPES = {
    "application/vnd.oasis.opendocument.text": "ODF Text Document",
    "application/vnd.oasis.opendocument.spreadsheet": "ODF Spreadsheet",
    "application/vnd.oasis.opendocument.presentation": "ODF Presentation",
    "application/vnd.oasis.opendocument.text-master": "ODF Master Document",
    "application/vnd.sun.xml.writer": "OpenOffice.org 1.x Writer",
    "application/vnd.sun.xml.calc": "OpenOffice.org 1.x Calc",
    "application/vnd.sun.xml.impress": "OpenOffice.org 1.x Impress",
}

ENC_PASSWORD = "password"  # certainly needs a password
ENC_MAYBE = "maybe"  # encrypted, but possibly with Excel's default password: let LibreOffice try


# --------------------------------------------------------------------------- OLE compound files


class CfbError(Exception):
    pass


ENDOFCHAIN = 0xFFFFFFFE
FREESECT = 0xFFFFFFFF
MAX_SECTORS_WALKED = 1 << 20


@dataclass
class DirEntry:
    sid: int
    name: str
    kind: int  # 1 storage, 2 stream, 5 root
    left: int
    right: int
    child: int
    start: int
    size: int


class Cfb:
    """Minimal read-only reader for OLE compound files (MS-CFB). Reads only what it needs."""

    def __init__(self, f: IO[bytes]) -> None:
        self.f = f
        f.seek(0, os.SEEK_END)
        self.file_size = f.tell()
        f.seek(0)
        h = f.read(512)
        if len(h) < 512 or h[:8] != OLE_MAGIC:
            raise CfbError("not an OLE compound file")
        (self.major,) = struct.unpack_from("<H", h, 26)
        (shift,) = struct.unpack_from("<H", h, 30)
        (mini_shift,) = struct.unpack_from("<H", h, 32)
        if shift not in (9, 12) or mini_shift != 6:
            raise CfbError("bad sector size")
        self.ssize = 1 << shift
        self.mini_size = 1 << mini_shift
        (self.n_fat, self.dir_start, _txn, self.mini_cutoff, self.minifat_start, self.n_minifat,
         self.difat_start, self.n_difat) = struct.unpack_from("<IIIIIIII", h, 44)
        self.difat = list(struct.unpack_from("<109I", h, 76))
        self._fat_cache: dict[int, tuple[int, ...]] = {}
        self._difat_loaded = False
        self.entries = self._read_directory()
        if not self.entries or self.entries[0].kind != 5:
            raise CfbError("no root entry")
        self._minifat: list[int] | None = None
        self._root_secs: list[int] | None = None

    # sectors and chains
    def _sector(self, n: int) -> bytes:
        off = (n + 1) * self.ssize
        if n >= 0xFFFFFFFA or off >= self.file_size:
            raise CfbError(f"sector {n} out of range")
        self.f.seek(off)
        return self.f.read(self.ssize)

    def _load_difat(self) -> None:
        if self._difat_loaded:
            return
        self._difat_loaded = True
        sec, seen = self.difat_start, 0
        per = self.ssize // 4 - 1
        while sec not in (ENDOFCHAIN, FREESECT) and seen < self.n_difat and seen < 4096:
            vals = struct.unpack(f"<{per + 1}I", self._sector(sec))
            self.difat.extend(vals[:per])
            sec = vals[per]
            seen += 1

    def _fat_entry(self, n: int) -> int:
        per = self.ssize // 4
        idx = n // per
        if idx >= 109:
            self._load_difat()
        if idx >= len(self.difat) or self.difat[idx] in (ENDOFCHAIN, FREESECT):
            raise CfbError("FAT sector missing")
        block = self._fat_cache.get(idx)
        if block is None:
            block = struct.unpack(f"<{per}I", self._sector(self.difat[idx]))
            if len(self._fat_cache) < 4096:
                self._fat_cache[idx] = block
        return block[n % per]

    def _chain(self, start: int, limit_bytes: int) -> bytes:
        out = bytearray()
        sec, seen = start, set()
        while sec not in (ENDOFCHAIN, FREESECT) and len(out) < limit_bytes:
            if sec in seen or len(seen) > MAX_SECTORS_WALKED:
                raise CfbError("sector chain loops")
            seen.add(sec)
            out += self._sector(sec)
            sec = self._fat_entry(sec)
        return bytes(out[:limit_bytes])

    def _read_directory(self) -> list[DirEntry]:
        raw = self._chain(self.dir_start, 128 * 20000)
        out: list[DirEntry] = []
        for i in range(len(raw) // 128):
            e = raw[i * 128:(i + 1) * 128]
            (nlen,) = struct.unpack_from("<H", e, 64)
            kind = e[66]
            if kind not in (1, 2, 5):
                out.append(DirEntry(i, "", 0, FREESECT, FREESECT, FREESECT, 0, 0))
                continue
            name = e[:max(0, min(nlen, 64) - 2)].decode("utf-16-le", "replace")
            left, right, child = struct.unpack_from("<III", e, 68)
            start, size = struct.unpack_from("<IQ", e, 116)
            if self.major == 3:
                size &= 0xFFFFFFFF
            out.append(DirEntry(i, name, kind, left, right, child, start, size))
        return out

    def root_children(self) -> dict[str, DirEntry]:
        """Direct children of the root storage, by case-folded name (red-black tree walk)."""
        found: dict[str, DirEntry] = {}
        stack, seen = [self.entries[0].child], set()
        while stack:
            sid = stack.pop()
            if sid >= len(self.entries) or sid in seen:
                continue
            seen.add(sid)
            e = self.entries[sid]
            if e.kind:
                found.setdefault(e.name.casefold(), e)
            stack += [e.left, e.right]
        return found

    def read_stream(self, e: DirEntry, limit: int = 65536) -> bytes:
        n = min(e.size, limit)
        if e.size < self.mini_cutoff:
            return self._read_mini(e.start, n)
        return self._chain(e.start, n)

    def _root_sectors(self) -> list[int]:
        """Sector numbers of the mini stream container (the root entry's regular chain)."""
        if self._root_secs is None:
            secs: list[int] = []
            sec = self.entries[0].start
            want = (self.entries[0].size + self.ssize - 1) // self.ssize
            while sec not in (ENDOFCHAIN, FREESECT) and len(secs) < want:
                if len(secs) > MAX_SECTORS_WALKED or sec in secs[-4:]:
                    raise CfbError("root chain loops")
                secs.append(sec)
                sec = self._fat_entry(sec)
            self._root_secs = secs
        return self._root_secs

    def _read_mini(self, start: int, n: int) -> bytes:
        if self._minifat is None:
            raw = self._chain(self.minifat_start, 4 * self.n_minifat * (self.ssize // 4)) if self.n_minifat else b""
            self._minifat = list(struct.unpack(f"<{len(raw) // 4}I", raw))
        roots = self._root_sectors()
        out = bytearray()
        sec, seen = start, set()
        while sec not in (ENDOFCHAIN, FREESECT) and len(out) < n:
            if sec in seen or sec >= len(self._minifat):
                raise CfbError("bad mini stream chain")
            seen.add(sec)
            off = sec * self.mini_size
            big = off // self.ssize
            if big >= len(roots):
                raise CfbError("mini sector outside the mini stream")
            self.f.seek((roots[big] + 1) * self.ssize + off % self.ssize)
            out += self.f.read(self.mini_size)
            sec = self._minifat[sec]
        return bytes(out[:n])


# --------------------------------------------------------------------------- sniffing


@dataclass
class Sniff:
    source_format: str
    encryption: str | None = None  # ENC_PASSWORD | ENC_MAYBE | None
    infilter: str | None = None  # import filter to force (damaged OLE)
    empty: bool = False
    detail: str = ""


def _word_info(cfb: Cfb, ent: DirEntry) -> tuple[str, bool]:
    fib = cfb.read_stream(ent, 64)
    if len(fib) < 12:
        raise CfbError("short FIB")
    ident, nfib = struct.unpack_from("<HH", fib, 0)
    (flags,) = struct.unpack_from("<H", fib, 10)
    encrypted = bool(flags & 0x0100)  # fEncrypted (MS-DOC FibBase)
    if ident not in (0xA5EC, 0xA5DC):
        raise CfbError("bad FIB")
    fmt = "MS Word 97–2003" if nfib >= 0x00C1 else "MS Word 6.0/95"
    return fmt, encrypted


def _excel_filepass(data: bytes) -> bool:
    """True if the workbook globals contain a FILEPASS record (MS-XLS 2.4.117)."""
    pos, n = 0, 0
    while pos + 4 <= len(data) and n < 4000:
        rtype, rlen = struct.unpack_from("<HH", data, pos)
        if rtype == 0x002F:
            return True
        if rtype in (0x000A, 0x0085) and n > 0:  # EOF, or BoundSheet8: FILEPASS comes earlier
            return False
        pos += 4 + rlen
        n += 1
    return False


def _sniff_ole(f: IO[bytes], ext: str) -> Sniff:
    try:
        cfb = Cfb(f)
        kids = cfb.root_children()
    except (CfbError, struct.error, OSError) as e:
        return Sniff(GENERIC_FORMAT.get(ext, ext.upper().lstrip(".")) + " (damaged)",
                     infilter=OLE_FILTER_BY_EXT.get(ext), detail=str(e))
    if "encryptedpackage" in kids:
        base = GENERIC_FORMAT.get(ext, "Office 2007+ document")
        return Sniff(f"{base} (encrypted)", encryption=ENC_MAYBE)
    try:
        if "worddocument" in kids:
            fmt, enc = _word_info(cfb, kids["worddocument"])
            return Sniff(fmt, ENC_PASSWORD if enc else None)
        if "workbook" in kids or "book" in kids:
            ent = kids.get("workbook") or kids["book"]
            fmt = "MS Excel 97–2003" if "workbook" in kids else "MS Excel 5.0/95"
            return Sniff(fmt, ENC_MAYBE if _excel_filepass(cfb.read_stream(ent, 65536)) else None)
        if "powerpoint document" in kids:
            enc = False
            cu = kids.get("current user")
            if cu is not None:
                data = cfb.read_stream(cu, 64)
                if len(data) >= 16:
                    (token,) = struct.unpack_from("<I", data, 12)
                    enc = token == 0xF3D1C4DF  # MS-PPT CurrentUserAtom: encrypted document
            return Sniff("MS PowerPoint 97–2003", ENC_PASSWORD if enc else None)
        if "matost" in kids or "contents" in kids:
            return Sniff("MS Works")
    except (CfbError, struct.error, OSError) as e:
        return Sniff(GENERIC_FORMAT.get(ext, ext.upper().lstrip(".")) + " (damaged)",
                     infilter=OLE_FILTER_BY_EXT.get(ext), detail=str(e))
    # An OLE file without any stream LibreOffice can read as this kind of document.
    return Sniff(GENERIC_FORMAT.get(ext, ext.upper().lstrip(".")) + " (damaged)", infilter=OLE_FILTER_BY_EXT.get(ext),
                 detail="no document stream")


def _sniff_zip(f: IO[bytes], ext: str) -> Sniff:
    try:
        z = zipfile.ZipFile(f)
        names = set(z.namelist())
        if "encrypted-package" in names:  # ODF 1.3 OpenPGP-encrypted package
            return Sniff("ODF document (encrypted)", ENC_PASSWORD)
        if "mimetype" in names:
            mt = z.read("mimetype")[:200].decode("ascii", "replace").strip()
            fmt = ODF_MIMETYPES.get(mt, GENERIC_FORMAT.get(ext, "ODF document"))
            enc = None
            if "META-INF/manifest.xml" in names and z.getinfo("META-INF/manifest.xml").file_size < (8 << 20):
                if b"encryption-data" in z.read("META-INF/manifest.xml"):
                    enc = ENC_PASSWORD
            return Sniff(fmt + (" (encrypted)" if enc else ""), enc)
        if "word/document.xml" in names:
            return Sniff("MS Word 2007+")
        if "xl/workbook.xml" in names or "xl/workbook.bin" in names:
            return Sniff("MS Excel 2007+")
        if "ppt/presentation.xml" in names:
            return Sniff("MS PowerPoint 2007+")
    except (zipfile.BadZipFile, OSError, KeyError, EOFError, ValueError) as e:
        return Sniff(GENERIC_FORMAT.get(ext, ext.upper().lstrip(".")) + " (damaged)", detail=str(e))
    return Sniff(GENERIC_FORMAT.get(ext, ext.upper().lstrip(".")))


def sniff(f: IO[bytes], ext: str) -> Sniff:
    """Identify a document's format and protection. `f` must be seekable; nothing is written."""
    head = f.read(64)
    f.seek(0)
    if not head:
        return Sniff(GENERIC_FORMAT.get(ext, ext.upper().lstrip(".")), empty=True)
    if head.startswith(OLE_MAGIC):
        return _sniff_ole(f, ext)
    if head.startswith(ZIP_MAGIC):
        return _sniff_zip(f, ext)
    if head.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"{\\rtf"):
        return Sniff("Rich Text Format")
    if head.startswith(b"\xffWPC"):
        enc = len(head) >= 14 and struct.unpack_from("<H", head, 12)[0] != 0
        return Sniff("WordPerfect" + (" (encrypted)" if enc else ""), ENC_PASSWORD if enc else None)
    low = head.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if low.startswith((b"<!doctype html", b"<html", b"<table", b"<head", b"<body")):
        return Sniff("HTML")
    if low.startswith(b"<?xml"):
        return Sniff("XML")
    return Sniff(GENERIC_FORMAT.get(ext, ext.upper().lstrip(".")))


def sniff_path(path: str, ext: str) -> Sniff:
    with open(long_path(path), "rb") as f:
        return sniff(f, ext)


def sniff_ref(src: SourceRef) -> Sniff:
    if src.data is not None:
        return sniff(io.BytesIO(src.data), src.ext)
    assert src.path is not None
    return sniff_path(src.path, src.ext)


def export_filter(ext: str) -> str:
    return EXPORT_FILTER.get(ext, "writer_pdf_Export")


def app_label(ext: str) -> str:
    return "Calc" if ext in CALC_EXTS else "Impress" if ext in IMPRESS_EXTS else "Writer"


# --------------------------------------------------------------------------- the route


def staging_input(work: WorkItem, prefix: str = "doc") -> str:
    """A uniquely named copy (hard link when possible) of the staged input: <prefix>-<n><ext>.

    The stem must be unique within a soffice batch (outputs map back by stem, §5.5).
    """
    src = work.input_path()
    dst = work.out(f"{prefix}-{work.n}{work.plan.ext}")
    if os.path.lexists(long_path(dst)):
        os.unlink(long_path(dst))
    try:
        os.link(long_path(src), long_path(dst))
    except OSError:
        import shutil

        shutil.copyfile(long_path(src), long_path(dst))
    return dst


def pdf_finish(work: WorkItem, result: LoResult) -> bool:
    """Shared finish for LibreOffice outputs: result path, V-PDF-OPEN, V-PDFA request."""
    from ..verify.pdf import v_pdf_open

    if result.method:
        work.method = result.method
    if not result.ok or not result.output:
        return False
    work.result_path = result.output
    work.new_output = True
    work.action = Action.CONVERT
    chk = v_pdf_open(result.output)
    work.checks.append(chk)
    if chk.state != CheckState.PASS:
        if chk.message:
            work.messages.append(chk.message)
        return True
    work.pdfa = (result.output, str(work.settings.get("pdfa_level", "2b")))
    return True


class DocumentRoute(Route):
    key: ClassVar[str] = "document"
    lane: ClassVar[Lane] = Lane.DOCUMENTS
    converter_tools: ClassVar[tuple[str, ...]] = ("libreoffice",)
    batched: ClassVar[bool] = True

    def probe(self, ctx: ProbeContext, src: SourceRef) -> Probe:
        ext = src.ext
        export = export_filter(ext)
        method = f"LibreOffice · {export} · PDF/A-{ctx.workflow.get('pdfa_level', '2b')}"
        try:
            sn = sniff_ref(src)
        except OSError as e:
            return Probe(Category.DOCUMENT, ".pdf", Action.CONVERT, GENERIC_FORMAT.get(ext, ""), method=method,
                         reasons=["SOURCE_UNREADABLE"], message=f"Can't read the source: {e.strerror or e}",
                         route=self.key, final=True)
        pr = Probe(Category.DOCUMENT, ".pdf", Action.CONVERT, sn.source_format, method=method, route=self.key,
                   data={"infilter": sn.infilter, "encryption": sn.encryption})
        if ctx.mode != Mode.CONVERT:
            return pr
        if sn.empty:
            pr.reasons = ["SOURCE_UNREADABLE"]
            pr.message = "The file is empty (0 bytes)."
        elif sn.encryption == ENC_PASSWORD:
            pr.reasons = ["PASSWORD_PROTECTED"]
            pr.message = "The document is encrypted and needs a password to open."
        return pr

    def batch_key(self, work: WorkItem) -> str:
        infilter = work.plan.data.get("infilter") or ""
        return f"{infilter}|{export_filter(work.plan.ext)}|{work.settings.get('pdfa_level', '2b')}"

    def prepare(self, ctx: TaskContext, work: WorkItem) -> LoJob | None:
        work.category = Category.DOCUMENT
        inp = staging_input(work, "doc")
        return LoJob(input_path=inp, out_dir=work.work_dir, export_filter=export_filter(work.plan.ext),
                     pdfa_level=str(work.settings.get("pdfa_level", "2b")),
                     infilter=work.plan.data.get("infilter") or None)

    def finish(self, ctx: TaskContext, work: WorkItem, result: LoResult) -> None:
        if pdf_finish(work, result):
            return
        reason = result.reason or "CONVERSION_ERROR"
        message = result.message
        if reason == "CONVERSION_ERROR" and work.plan.data.get("encryption") == ENC_MAYBE:
            reason = "PASSWORD_PROTECTED"
            message = "The document is encrypted and needs a password to open."
        work.fail(reason, message)

    def check_existing(self, ctx: TaskContext, work: WorkItem) -> None:
        """§7.5 resume: V-PDF-OPEN on the existing output, then V-PDFA at pdfa_level."""
        from ..verify.pdf import v_pdf_open

        path = work.existing_output
        assert path
        chk = v_pdf_open(path)
        work.checks.append(chk)
        if chk.state == CheckState.PASS:
            work.pdfa = (path, str(work.settings.get("pdfa_level", "2b")))


ROUTE = register(DocumentRoute())

