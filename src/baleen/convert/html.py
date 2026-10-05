"""HTML pages -> PDF/A with LibreOffice Writer (spec §6, §6.2, §6.4, R-05, P8).

- Charset: a BOM, else a declared charset (<meta charset>, <meta http-equiv=Content-Type>,
  an XML declaration), else the §6.3 rules (strict UTF-8 is certain; otherwise
  ENCODING_UNCERTAIN under auto, or the named encoding / ENCODING_MISMATCH).
- The staging copy is the decoded page, its own charset declarations removed, passed through
  the shared §6.4 sanitiser with <base href> = the file URI of the ORIGINAL source folder,
  and written as UTF-8 with a BOM for the "HTML (StarWriter)" import filter.
- Relative images (<img src>, background=) are resolved against that same folder by Baleen,
  read-only and only inside the source root, and inlined as data: URIs: headless LibreOffice
  never renders linked images in PDF export, but it does fetch linked resources. Anything that
  could still reach the network or an SMB share (file://host/..., \\\\host\\...) or another
  scheme is replaced by visible placeholder text, so LibreOffice has nothing to fetch.
- After conversion: V-PDF-OPEN, then V-PDFA at pdfa_level in the PDF/A lane.
"""

from __future__ import annotations

import base64
import codecs
import html
import os
import re
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import IO, ClassVar

from ..model import Action, Category, CheckState, Mode, Probe
from ..paths import is_within, long_path
from ..scheduler import Lane, TaskContext
from ..verify.pdf import v_pdf_open
from .base import LoJob, LoResult, ProbeContext, Route, SourceRef, WorkItem, register
from .sanitize import placeholder, sanitize_html
from .text import (
    Encoding,
    bom_codec,
    decodes,
    finish_pdf,
    named_or_uncertain,
    note_check_messages,
    pdfa_level,
    read_file,
)

HTML_INFILTER = "HTML (StarWriter)"
SNIFF_BYTES = 65536
MAX_INLINE_FILE = 50 << 20  # one image
MAX_INLINE_TOTAL = 256 << 20  # one page

DECL_RE = re.compile(rb"""<meta\b[^>]*?\bcharset\s*=\s*["']?\s*([A-Za-z0-9._:-]+)""", re.I)
XML_DECL_RE = re.compile(rb"""^\s*<\?xml\b[^>]*\bencoding\s*=\s*["']([A-Za-z0-9._:-]+)["']""", re.I)
STRIP_DECL_RE = re.compile(r"""<meta\b[^>]*?\bcharset\s*=[^>]*>""", re.I)

# Declared labels as browsers read them (WHATWG Encoding Standard), for the common legacy
# ones; anything else goes through Python's codec registry.
WHATWG: dict[str, str] = {
    **dict.fromkeys(("us-ascii", "ascii", "iso-8859-1", "iso8859-1", "iso_8859-1", "latin1", "l1",
                     "windows-1252", "cp1252", "x-cp1252"), "cp1252"),
    **dict.fromkeys(("big5", "x-x-big5", "cn-big5", "csbig5"), "cp950"),
    **dict.fromkeys(("gb2312", "gbk", "x-gbk", "chinese", "csgb2312", "iso-ir-58", "gb18030"), "gb18030"),
    **dict.fromkeys(("shift_jis", "shift-jis", "sjis", "x-sjis", "ms_kanji", "windows-31j", "csshiftjis"),
                    "cp932"),
    **dict.fromkeys(("euc-kr", "ks_c_5601-1987", "windows-949", "cseuckr"), "cp949"),
    **dict.fromkeys(("iso-8859-9", "latin5", "windows-1254"), "cp1254"),
    **dict.fromkeys(("tis-620", "windows-874", "iso-8859-11"), "cp874"),
}
MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".jpe": "image/jpeg", ".png": "image/png",
        ".gif": "image/gif", ".bmp": "image/bmp", ".webp": "image/webp", ".svg": "image/svg+xml",
        ".tif": "image/tiff", ".tiff": "image/tiff", ".wmf": "image/x-wmf", ".emf": "image/x-emf"}
SIGNATURES = ((b"\xff\xd8\xff", "image/jpeg"), (b"\x89PNG", "image/png"), (b"GIF8", "image/gif"),
              (b"BM", "image/bmp"), (b"II*\x00", "image/tiff"), (b"MM\x00*", "image/tiff"))
INLINE_TAGS = frozenset({"img", "input", "body", "table", "td", "th", "tr"})
TAG_RE = re.compile(r"<([A-Za-z][A-Za-z0-9]*)((?:\s+[^\s=>]+(?:=\"[^\"]*\")?)*)\s*>")
ATTR_RE = re.compile(r'(\s+)([^\s=>]+)="([^"]*)"')
SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*):")
DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")


def declared_charset(head: bytes) -> str | None:
    m = XML_DECL_RE.search(head) or DECL_RE.search(head)
    return m.group(1).decode("ascii", "replace").strip().lower() if m else None


def codec_for_label(label: str) -> str | None:
    label = label.strip().lower()
    if label.startswith(("utf-16", "utf-32", "ucs-2", "unicode")):
        return "utf-8"  # WHATWG: a UTF-16 declaration inside ASCII-compatible bytes means UTF-8
    if label in WHATWG:
        return WHATWG[label]
    try:
        return codecs.lookup(label).name
    except LookupError:
        return None


def settle_html(opener: Callable[[], IO[bytes]], setting: str) -> Encoding:
    """Charset from the BOM or the page's declaration; otherwise §6.3."""
    with opener() as f:
        head = f.read(SNIFF_BYTES)
    bom = bom_codec(head)
    if bom and decodes(opener, bom[0]):
        return Encoding(bom[0], bom[1])
    prefix = ""
    label = declared_charset(head)
    if label:
        codec = codec_for_label(label)
        if codec and decodes(opener, codec):
            return Encoding(codec, f"{label}, declared")
        prefix = (f"The page declares charset {label}, but doesn't decode with it." if codec else
                  f"The page declares an unknown charset ({label}).")
    if decodes(opener, "utf-8"):
        return Encoding("utf-8", "UTF-8")
    if prefix and setting in (None, "", "auto"):
        prefix += " Its encoding isn't certain."
    return named_or_uncertain(opener, setting, prefix)


def _mime(path: str, data: bytes) -> str:
    for sig, mime in SIGNATURES:
        if data.startswith(sig):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return MIME.get(os.path.splitext(path)[1].lower(), "application/octet-stream")


class Localiser:
    """Inline relative images of a sanitised page as data: URIs; neutralise the rest."""

    def __init__(self, folder: str | None, source_root: str | None) -> None:
        self.folder = folder
        self.source_root = source_root
        self.budget = MAX_INLINE_TOTAL
        self.inlined: list[str] = []
        self.dropped: list[str] = []

    def _local_path(self, url: str) -> str | None:
        """Filesystem path a local reference points to, or None if it isn't resolvable here."""
        m = SCHEME_RE.match(url)
        if m and not DRIVE_RE.match(url):
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme.lower() != "file" or parsed.netloc.lower() not in ("", "localhost"):
                return None
            return urllib.request.url2pathname(parsed.path)
        if DRIVE_RE.match(url):
            return url
        if not self.folder or url.startswith(("/", "\\")):
            return None  # root-relative, or no source folder (e-mail attachments)
        rel = urllib.parse.unquote(url.split("#", 1)[0].split("?", 1)[0]).replace("\\", "/")
        return os.path.normpath(os.path.join(self.folder, *[p for p in rel.split("/") if p]))

    def resolve(self, url: str) -> tuple[str | None, str]:
        """(new URL, placeholder): new URL None means drop the reference and show the text."""
        u = url.strip()
        if not u or u.lower().startswith("data:"):
            return url, ""
        low = u.lower()
        if u.startswith(("\\\\", "//")) or (low.startswith("file:") and urllib.parse.urlsplit(u).netloc.lower()
                                             not in ("", "localhost")):
            return None, placeholder(u)  # a network share or host: never fetched (P8)
        if SCHEME_RE.match(u) and not DRIVE_RE.match(u) and not low.startswith("file:"):
            return None, placeholder(u)  # another scheme: nothing for LibreOffice to fetch
        path = self._local_path(u)
        try:
            ok = (path is not None and self.source_root is not None and os.path.isfile(long_path(path))
                  and is_within(path, self.source_root))
            size = os.path.getsize(long_path(path)) if ok and path else 0
        except OSError:
            ok, size = False, 0
        if not ok or path is None or size > MAX_INLINE_FILE or size > self.budget:
            self.dropped.append(u)
            return None, f"[local resource not archived: {u}]"
        try:
            with open(long_path(path), "rb") as f:  # read-only (P1)
                data = f.read()
        except OSError:
            self.dropped.append(u)
            return None, f"[local resource not archived: {u}]"
        self.budget -= size
        self.inlined.append(u)
        return f"data:{_mime(path, data)};base64,{base64.b64encode(data).decode('ascii')}", ""

    def run(self, doc: str) -> str:
        def tag(m: re.Match[str]) -> str:
            name = m.group(1).lower()
            if 'src="' not in m.group(2) and 'background="' not in m.group(2):
                return m.group(0)
            notes: list[str] = []

            def attr(am: re.Match[str]) -> str:
                key = am.group(2).lower()
                if key not in ("src", "background"):
                    return am.group(0)
                url = html.unescape(am.group(3))
                if name not in INLINE_TAGS and not url.strip().lower().startswith(("file:", "\\\\")):
                    return am.group(0)  # other elements keep relative links (resolved via <base href>)
                new, note = self.resolve(url)
                if new is None:
                    notes.append(note)
                    return ""
                if new == url:
                    return am.group(0)
                return f'{am.group(1)}{am.group(2)}="{html.escape(new, quote=True)}"'

            attrs = ATTR_RE.sub(attr, m.group(2))
            spans = "".join(f"<span>{html.escape(n)}</span>" for n in notes)
            if notes and name == "img":
                return spans
            return f"<{m.group(1)}{attrs}>{spans}"

        return TAG_RE.sub(tag, doc)


class HtmlRoute(Route):
    key: ClassVar[str] = "html"
    lane: ClassVar[Lane] = Lane.DOCUMENTS
    batched: ClassVar[bool] = True
    converter_tools: ClassVar[tuple[str, ...]] = ("libreoffice",)
    infilter: ClassVar[str | None] = HTML_INFILTER

    def probe(self, ctx: ProbeContext, src: SourceRef) -> Probe:
        method = "LibreOffice · HTML (StarWriter) → PDF/A-" + str(ctx.workflow.get("pdfa_level", "2b"))
        enc = settle_html(src.open, str(ctx.workflow.get("txt_encoding", "auto")))
        if ctx.mode == Mode.CHECK:
            return Probe(Category.HTML, ".pdf", Action.CONVERT, source_format=f"HTML ({enc.label})", method=method,
                         route=self.key)
        if enc.reason:  # decided at plan time: nothing is converted, so no method
            return Probe(Category.HTML, ".pdf", Action.CONVERT, source_format="HTML",
                         reasons=[enc.reason], message=enc.message, route=self.key)
        return Probe(Category.HTML, ".pdf", Action.CONVERT, source_format=f"HTML ({enc.label})", method=method,
                     route=self.key, data={"codec": enc.codec, "label": enc.label})

    def prepare(self, ctx: TaskContext, work: WorkItem) -> LoJob | None:
        codec = str(work.plan.data.get("codec") or "utf-8")
        try:
            markup = read_file(work.input_path()).decode(codec)
        except UnicodeDecodeError as e:
            work.fail("ENCODING_MISMATCH", f"The page no longer decodes as {work.plan.data.get('label', codec)}: {e}")
            return None
        markup = STRIP_DECL_RE.sub("", markup.lstrip("﻿"))
        folder = os.path.dirname(work.source_abs) if work.source_abs else None
        base_href = Path(folder).as_uri() + "/" if folder else None
        stem = os.path.splitext(work.plan.source_path.rsplit("#", 1)[-1].rsplit("/", 1)[-1])[0]
        doc = sanitize_html(markup, base_href=base_href, title=stem)
        loc = Localiser(folder, work.run.source_root if work.source_abs else None)
        with ctx.transfer():  # relative images are read from the source folder
            doc = loc.run(doc)
        if loc.dropped:
            shown = ", ".join(loc.dropped[:3]) + (f" (+{len(loc.dropped) - 3} more)" if len(loc.dropped) > 3 else "")
            work.messages.append(f"Local resources not found inside the source folder: {shown}.")
        staging = work.out(f"lo_{work.n}.html")  # unique stem per batch: outputs map back
        with open(long_path(staging), "wb") as f:
            f.write(codecs.BOM_UTF8 + doc.encode("utf-8"))
        return LoJob(staging, work.work_dir, "writer_pdf_Export", pdfa_level(work), infilter=self.infilter)

    def finish(self, ctx: TaskContext, work: WorkItem, result: LoResult) -> None:
        if finish_pdf(work, result, f"page decoded as {work.plan.data.get('label', 'UTF-8')}"):
            assert result.output
            work.pdfa = (result.output, pdfa_level(work))

    def check_existing(self, ctx: TaskContext, work: WorkItem) -> None:
        pdf = work.input_path()
        c = v_pdf_open(pdf)
        work.checks.append(c)
        if c.state == CheckState.PASS:
            work.pdfa = (pdf, pdfa_level(work))

    def process(self, ctx: TaskContext, work: WorkItem) -> None:
        work.fail("CONVERSION_ERROR", "Web pages are converted in the Documents lane.")

    def decide(self, work: WorkItem) -> list[str]:
        note_check_messages(work)
        return super().decide(work)


register(HtmlRoute())
