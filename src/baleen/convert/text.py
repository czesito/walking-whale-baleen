"""Plain text -> PDF/A with LibreOffice Writer (spec §6, §6.2, §6.3, DR-14).

Encoding (§6.3, DR-14):
- Certain: a UTF-8 or UTF-16 BOM (UTF-32 too), or a strict UTF-8 decode (pure ASCII counts).
- Not certain, txt_encoding = auto: NEEDS_REVIEW ENCODING_UNCERTAIN at plan time, nothing
  written. The message names the first of big5, gb18030, shift_jis, windows-1252 that
  decodes strictly - as a hint only.
- Not certain, a named encoding: strict decode with it; failure -> FAILED ENCODING_MISMATCH.

The staging copy is the decoded text with LF line endings, written as UTF-8 with a BOM and
imported with the "Text (encoded)" filter set to UTF-8 / LF. After conversion: V-PDF-OPEN,
V-TEXT (>= 98% of the source's non-whitespace characters, else FAILED TEXT_LOSS), then
V-PDFA at pdfa_level in the PDF/A lane.
"""

from __future__ import annotations

import codecs
import collections
from collections.abc import Callable
from dataclasses import dataclass
from typing import IO, ClassVar

from ..model import Action, Category, CheckResult, CheckState, Mode, Probe, outcome_from_checks
from ..paths import long_path
from ..scheduler import Lane, TaskContext
from ..verify.pdf import v_pdf_open, v_text, visible_chars
from .base import LoJob, LoResult, ProbeContext, Route, SourceRef, WorkItem, register

# LibreOffice "Text (encoded)" import options: charset, line end, font, language, (BOM, export
# only). Verified with CJK text on LibreOffice 26.8: UTF8 with LF imports exactly; an empty
# font keeps Writer's defaults (system fonts, DR-15).
TEXT_INFILTER = "Text (encoded):UTF8,LF,,,"

# Settings value -> Python codec. big5 and shift_jis use the Windows code pages (cp950,
# cp932), which add the vendor characters common in files written on Windows.
CODECS: dict[str, str] = {"big5": "cp950", "gb18030": "gb18030", "shift_jis": "cp932", "windows-1252": "cp1252"}
LABELS: dict[str, str] = {"big5": "Big5", "gb18030": "GB18030", "shift_jis": "Shift_JIS",
                          "windows-1252": "Windows-1252"}
HINT_ORDER: tuple[str, ...] = ("big5", "gb18030", "shift_jis", "windows-1252")

CHUNK = 1 << 20


@dataclass
class Encoding:
    """Outcome of §6.3 for one file."""

    codec: str | None  # Python codec to decode with (None when undecided)
    label: str  # e.g. "UTF-8", "UTF-16 LE, BOM", "Big5"
    reason: str | None = None  # ENCODING_UNCERTAIN | ENCODING_MISMATCH
    message: str = ""


def bom_codec(head: bytes) -> tuple[str, str] | None:
    if head.startswith(b"\xff\xfe\x00\x00"):
        return "utf-32", "UTF-32 LE, BOM"
    if head.startswith(b"\x00\x00\xfe\xff"):
        return "utf-32", "UTF-32 BE, BOM"
    if head.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig", "UTF-8, BOM"
    if head.startswith(b"\xff\xfe"):
        return "utf-16", "UTF-16 LE, BOM"
    if head.startswith(b"\xfe\xff"):
        return "utf-16", "UTF-16 BE, BOM"
    return None


def decodes(opener: Callable[[], IO[bytes]], codec: str) -> bool:
    """True if the whole stream decodes strictly with `codec` (streamed, constant memory)."""
    dec = codecs.getincrementaldecoder(codec)("strict")
    try:
        with opener() as f:
            while True:
                b = f.read(CHUNK)
                if not b:
                    break
                dec.decode(b)
        dec.decode(b"", final=True)
    except (UnicodeDecodeError, LookupError):
        return False
    return True


def is_ascii(opener: Callable[[], IO[bytes]]) -> bool:
    with opener() as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                return True
            if not b.isascii():
                return False


def hint(opener: Callable[[], IO[bytes]]) -> str | None:
    """First of big5, gb18030, shift_jis, windows-1252 that decodes strictly (§6.3 hint)."""
    for name in HINT_ORDER:
        if decodes(opener, CODECS[name]):
            return name
    return None


def uncertain_message(opener: Callable[[], IO[bytes]], prefix: str = "") -> str:
    h = hint(opener)
    lead = prefix or "There is no byte-order mark and the text isn't valid UTF-8, so its encoding isn't certain."
    if h:
        return (f"{lead} It decodes as {LABELS[h]} (a hint, not a guess). If that is right, choose "
                f"{LABELS[h]} under Options → Text encoding when unsure, then run again.")
    return (f"{lead} It doesn't decode as Big5, GB18030, Shift_JIS or Windows-1252 either; check where the "
            "file came from.")


def first_error(opener: Callable[[], IO[bytes]], codec: str) -> str:
    with opener() as f:
        data = f.read(16 * CHUNK)
    try:
        data.decode(codec)
    except UnicodeDecodeError as e:
        return f" (byte 0x{data[e.start]:02X} at offset {e.start:,})"
    return ""


def settle(opener: Callable[[], IO[bytes]], setting: str) -> Encoding:
    """§6.3 for plain text."""
    with opener() as f:
        head = f.read(4)
    bom = bom_codec(head)
    if bom and decodes(opener, bom[0]):
        return Encoding(bom[0], bom[1])
    if decodes(opener, "utf-8"):
        return Encoding("utf-8", "ASCII" if is_ascii(opener) else "UTF-8")
    return named_or_uncertain(opener, setting)


def named_or_uncertain(opener: Callable[[], IO[bytes]], setting: str, prefix: str = "") -> Encoding:
    """The 'not certain' half of §6.3."""
    if setting in (None, "", "auto") or setting not in CODECS:
        return Encoding(None, "encoding not certain", "ENCODING_UNCERTAIN", uncertain_message(opener, prefix))
    codec = CODECS[setting]
    label = LABELS[setting]
    if decodes(opener, codec):
        return Encoding(codec, f"{label}, chosen in Options")
    return Encoding(None, f"not {label}", "ENCODING_MISMATCH",
                    f"The text doesn't decode as {label}, the encoding chosen in Options"
                    f"{first_error(opener, codec)}. Pick another encoding, then run again.")


def normalise_text(text: str) -> str:
    """LF line endings, no leading BOM character (§6.3)."""
    if text.startswith("﻿"):
        text = text[1:]
    return text.replace("\r\n", "\n").replace("\r", "\n")


def read_file(path: str) -> bytes:
    with open(long_path(path), "rb") as f:
        return f.read()


def pdfa_level(work: WorkItem) -> str:
    return str(work.settings.get("pdfa_level", "2b"))


def note_check_messages(work: WorkItem) -> None:
    """Copy the messages of checks that did not pass into the report message (§10.1)."""
    for c in work.checks:
        if c.state != CheckState.PASS and c.message and c.message not in work.messages:
            work.messages.append(c.message)


def finish_pdf(work: WorkItem, result: LoResult, label: str) -> bool:
    """Common to the Documents-lane text/HTML routes: record the result and run V-PDF-OPEN.
    Returns True when the PDF opened and further checks should run."""
    if result.method:
        work.method = result.method + (f" · {label}" if label else "")
    if not result.ok or not result.output:
        work.fail(result.reason or "CONVERSION_ERROR", result.message or "LibreOffice produced no PDF.")
        return False
    work.action = Action.CONVERT
    work.new_output = True
    work.result_path = result.output
    c = v_pdf_open(result.output)
    work.checks.append(c)
    return c.state == CheckState.PASS


class TextRoute(Route):
    key: ClassVar[str] = "text"
    lane: ClassVar[Lane] = Lane.DOCUMENTS
    batched: ClassVar[bool] = True
    converter_tools: ClassVar[tuple[str, ...]] = ("libreoffice",)
    infilter: ClassVar[str | None] = TEXT_INFILTER

    def probe(self, ctx: ProbeContext, src: SourceRef) -> Probe:
        method = "LibreOffice · Text (encoded) UTF-8 → PDF/A-" + str(ctx.workflow.get("pdfa_level", "2b"))
        enc = settle(src.open, str(ctx.workflow.get("txt_encoding", "auto")))
        if ctx.mode == Mode.CHECK:
            # Check has no options: a text file is simply not archival yet (NOT_ARCHIVAL_FORMAT).
            return Probe(Category.TEXT, ".pdf", Action.CONVERT, source_format=f"Plain text ({enc.label})",
                         method=method, route=self.key)
        if enc.reason:  # decided at plan time: nothing is converted, so no method
            return Probe(Category.TEXT, ".pdf", Action.CONVERT, source_format="Plain text",
                         reasons=[enc.reason], message=enc.message, route=self.key)
        return Probe(Category.TEXT, ".pdf", Action.CONVERT, source_format=f"Plain text ({enc.label})",
                     method=method, route=self.key, data={"codec": enc.codec, "label": enc.label})

    # ------------------------------------------------------------------ Documents lane

    def _decode(self, work: WorkItem, path: str) -> str | None:
        codec = str(work.plan.data.get("codec") or "utf-8")
        try:
            return normalise_text(read_file(path).decode(codec))
        except UnicodeDecodeError as e:
            work.fail("ENCODING_MISMATCH", f"The text no longer decodes as {work.plan.data.get('label', codec)}: {e}")
        except OSError as e:
            work.fail("SOURCE_UNREADABLE", f"Can't read the text: {e.strerror or e}")
        return None

    def prepare(self, ctx: TaskContext, work: WorkItem) -> LoJob | None:
        text = self._decode(work, work.input_path())
        if text is None:
            return None
        work.extra["source_chars"] = visible_chars(text)
        staging = work.out(f"lo_{work.n}.txt")  # unique stem per batch: outputs map back
        with open(long_path(staging), "wb") as f:
            f.write(codecs.BOM_UTF8 + text.encode("utf-8"))
        return LoJob(staging, work.work_dir, "writer_pdf_Export", pdfa_level(work), infilter=self.infilter)

    def finish(self, ctx: TaskContext, work: WorkItem, result: LoResult) -> None:
        label = f"text decoded as {work.plan.data.get('label', 'UTF-8')}"
        if not finish_pdf(work, result, label):
            return
        assert result.output
        self._text_and_pdfa(work, result.output, work.extra.get("source_chars"))

    def _text_and_pdfa(self, work: WorkItem, pdf: str, source: collections.Counter[str] | None) -> None:
        if source is None:
            t = CheckResult("V-TEXT", CheckState.UNAVAILABLE,
                            message="The source text couldn't be read for comparison.")
        else:
            t = v_text(pdf, source)
        work.checks.append(t)
        if t.state == CheckState.FAIL:
            return  # TEXT_LOSS is decided; validating PDF/A would change nothing
        work.pdfa = (pdf, pdfa_level(work))

    def _source_chars(self, work: WorkItem) -> collections.Counter[str] | None:
        """The source's characters, decoded in place (read-only) for a §7.5 re-check."""
        if not work.source_abs:
            return None
        try:
            return visible_chars(normalise_text(read_file(work.source_abs).decode(str(work.plan.data["codec"]))))
        except (OSError, KeyError, UnicodeDecodeError, LookupError):
            return None

    def check_existing(self, ctx: TaskContext, work: WorkItem) -> None:
        """§7.5 resume: V-PDF-OPEN, V-TEXT against the source, V-PDFA."""
        pdf = work.input_path()
        c = v_pdf_open(pdf)
        work.checks.append(c)
        if c.state == CheckState.PASS:
            self._text_and_pdfa(work, pdf, self._source_chars(work))

    def process(self, ctx: TaskContext, work: WorkItem) -> None:
        work.fail("CONVERSION_ERROR", "Text files are converted in the Documents lane.")

    def decide(self, work: WorkItem) -> list[str]:
        """§6.3: a new PDF failing V-TEXT is FAILED TEXT_LOSS, not VERIFY_FAILED."""
        note_check_messages(work)
        codes = outcome_from_checks(work.checks, new_output=work.new_output)
        if work.new_output and any(c.check == "V-TEXT" and c.state == CheckState.FAIL for c in work.checks):
            others = any(c.check != "V-TEXT" and c.state == CheckState.FAIL for c in work.checks)
            codes = ["TEXT_LOSS", *(["VERIFY_FAILED"] if others else [])]
        return codes


register(TextRoute())
