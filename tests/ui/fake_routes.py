"""Fake converters for every route key, so UI tests never depend on the real tools.

Content markers in source files decide the outcome (like tests/unit/fakes.py):

    b"HOLD"     at the start  -> processing waits on GATE (deterministic running states)
    b"BAD"      at the start  -> the item's check fails (V-IMG / V-AV-DUR)
    b"MULTI"    at the start  -> image with several frames (MULTI_FRAME_IMAGE, plan time)
    b"BIG5"     at the start  -> text encoding not certain (ENCODING_UNCERTAIN, plan time)
    b"CHARSET"  at the start  -> e-mail with undecodable characters (CHARSET_ERRORS)
    b"NOTPDFA"  anywhere      -> PDF without a PDF/A claim (NOT_PDFA, plan time)
    b"BADPDFA"  anywhere      -> PDF/A claim that fails validation (PDFA_INVALID)

FAULT simulates a disconnected output drive at the two places the runner observes it: the
publish copy raises OSError and the output root stops being a directory (UI-R7, W-11).
"""

from __future__ import annotations

import os
import shutil
import threading
import types
from typing import Any, ClassVar

from baleen import runner
from baleen.convert import base, libreoffice
from baleen.convert.base import LoJob, LoResult, ProbeContext, Route, SourceRef, WorkItem
from baleen.model import Action, Category, CheckResult, CheckState, Probe
from baleen.paths import long_path
from baleen.scheduler import Lane, TaskContext

GATE = threading.Event()
GATE.set()
HELD: set[int] = set()
_held_lock = threading.Lock()

LO_METHOD = "LibreOffice 26.8.1 → PDF/A-2b"
FF_METHOD = "FFmpeg 7.1.1 · libx264 crf18 · aac 192k"


def _read(path: str) -> bytes:
    with open(long_path(path), "rb") as f:
        return f.read()


def hold(work: WorkItem, data: bytes) -> None:
    if data.startswith(b"HOLD"):
        with _held_lock:
            HELD.add(work.n)
        GATE.wait(timeout=300)
        with _held_lock:
            HELD.discard(work.n)


class UIImage(Route):
    key: ClassVar[str] = "image"
    lane: ClassVar[Lane] = Lane.FILES

    def probe(self, ctx: ProbeContext, src: SourceRef) -> Probe:
        head = src.read_head(16)
        if head.startswith(b"MULTI"):
            return Probe(Category.IMAGE, None, Action.CONVERT, "GIF", reasons=["MULTI_FRAME_IMAGE"],
                         message="Animated GIF, 12 frames.", route="image", final=True)
        if src.ext in (".jpg", ".jpeg", ".jpe"):
            return Probe(Category.IMAGE, ".jpg", Action.COPY, "JPEG", method="verified JPEG · byte copy",
                         route="image")
        if src.ext in (".tif", ".tiff"):
            return Probe(Category.IMAGE, ".tif", Action.COPY, "TIFF", method="verified TIFF · byte copy",
                         route="image")
        return Probe(Category.IMAGE, ".jpg", Action.CONVERT, src.ext.upper().lstrip("."),
                     method="Pillow · JPEG q95 4:4:4", route="image")

    def process(self, ctx: TaskContext, work: WorkItem) -> None:
        data = _read(work.input_path())
        hold(work, data)
        if work.action == Action.CONVERT and not work.resume and not work.check_only:
            out = work.out("out.jpg")
            with open(long_path(out), "wb") as f:
                f.write(b"jpeg:" + data)
            work.result_path = out
            work.new_output = True
        elif work.action == Action.COPY:
            work.result_path = work.staged
        work.checks.append(CheckResult("V-IMG", CheckState.FAIL if data.startswith(b"BAD") else CheckState.PASS))


class UIDocument(Route):
    key: ClassVar[str] = "document"
    lane: ClassVar[Lane] = Lane.DOCUMENTS
    batched: ClassVar[bool] = True
    converter_tools: ClassVar[tuple[str, ...]] = ("libreoffice",)
    fmt: ClassVar[str] = "MS Word 97–2003"
    category: ClassVar[Category] = Category.DOCUMENT

    def probe(self, ctx: ProbeContext, src: SourceRef) -> Probe:
        return Probe(self.category, ".pdf", Action.CONVERT, self.fmt, method=LO_METHOD, route=self.key)

    def prepare(self, ctx: TaskContext, work: WorkItem) -> LoJob | None:
        inp = work.out(f"lo_{work.n}{work.plan.ext}")
        shutil.copyfile(long_path(work.input_path()), long_path(inp))
        return LoJob(inp, work.work_dir, "writer_pdf_Export", work.settings["pdfa_level"])

    def finish(self, ctx: TaskContext, work: WorkItem, result: LoResult) -> None:
        if not result.ok:
            work.fail(result.reason or "CONVERSION_ERROR", result.message)
            return
        work.result_path = result.output
        work.new_output = True
        work.action = Action.CONVERT
        work.method = LO_METHOD
        work.checks.append(CheckResult("V-PDF-OPEN", CheckState.PASS))
        work.pdfa = (result.output or "", work.settings["pdfa_level"])
        self.after(work)

    def after(self, work: WorkItem) -> None:
        return None

    def process(self, ctx: TaskContext, work: WorkItem) -> None:  # check mode: never converts
        work.checks.append(CheckResult("V-PDF-OPEN", CheckState.PASS))

    def check_existing(self, ctx: TaskContext, work: WorkItem) -> None:
        work.checks.append(CheckResult("V-PDF-OPEN", CheckState.PASS))
        work.pdfa = (work.input_path(), work.settings["pdfa_level"])


class UIText(UIDocument):
    key: ClassVar[str] = "text"
    fmt: ClassVar[str] = "Plain text"
    category: ClassVar[Category] = Category.TEXT

    def probe(self, ctx: ProbeContext, src: SourceRef) -> Probe:
        if src.read_head(16).startswith(b"BIG5"):
            return Probe(Category.TEXT, ".pdf", Action.CONVERT, "Plain text", reasons=["ENCODING_UNCERTAIN"],
                         message="Not valid UTF-8 and no BOM. Decodes cleanly as big5.", route="text", final=True)
        return super().probe(ctx, src)


class UIHtml(UIDocument):
    key: ClassVar[str] = "html"
    fmt: ClassVar[str] = "HTML"
    category: ClassVar[Category] = Category.HTML


class UIEmail(UIDocument):
    key: ClassVar[str] = "email"
    fmt: ClassVar[str] = "E-mail (RFC 822)"
    category: ClassVar[Category] = Category.EMAIL

    def after(self, work: WorkItem) -> None:
        if _read(work.input_path()).startswith(b"CHARSET"):
            work.reasons.append("CHARSET_ERRORS")
            work.messages.append("Declared charset big5; 3 bytes undecodable (shown as �).")

    def expand(self, ctx: ProbeContext, item: Any, src: SourceRef) -> list:
        return []


class UIPdf(Route):
    key: ClassVar[str] = "pdf"
    lane: ClassVar[Lane] = Lane.FILES

    def probe(self, ctx: ProbeContext, src: SourceRef) -> Probe:
        if b"NOTPDFA" in src.read_head(4096):
            return Probe(Category.PDF, None, Action.CHECK, "PDF 1.4", reasons=["NOT_PDFA"],
                         message="No pdfaid entry in XMP metadata.", route="pdf", final=True)
        return Probe(Category.PDF, ".pdf", Action.COPY, "PDF/A-1b", method="veraPDF 1.28 · PDF/A-1b", route="pdf")

    def process(self, ctx: TaskContext, work: WorkItem) -> None:
        data = _read(work.input_path())
        hold(work, data)
        if work.action == Action.COPY:
            work.result_path = work.staged
        work.checks.append(CheckResult("V-PDF-OPEN", CheckState.PASS))
        work.pdfa = (work.input_path(), "1b")

    def decide(self, work: WorkItem) -> list[str]:
        fail = next((c for c in work.checks if c.check == "V-PDFA" and c.state == CheckState.FAIL), None)
        if fail is not None:
            work.messages.append(f"Claims PDF/A-1b. {fail.message}")
            return ["PDFA_INVALID"]
        return super().decide(work)


class UIMedia(Route):
    key: ClassVar[str] = "media"
    lane: ClassVar[Lane] = Lane.MEDIA

    def probe(self, ctx: ProbeContext, src: SourceRef) -> Probe:
        video = src.ext not in (".wma", ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg")
        cat = Category.VIDEO if video else Category.AUDIO
        target = ".mp4" if video else ".m4a"
        if src.ext in (".mp4", ".m4a"):
            return Probe(cat, target, Action.COPY, "MP4", method="verified MP4 · byte copy", route="media")
        if src.ext == ".mov":
            return Probe(cat, target, Action.REMUX, "QuickTime (h264 / aac)",
                         method="FFmpeg 7.1.1 · stream copy (h264/aac)", route="media")
        method = FF_METHOD if video else "FFmpeg 7.1.1 · aac 192k"
        return Probe(cat, target, Action.CONVERT, f"{src.ext.upper().lstrip('.')} (mjpeg / pcm_s16le)",
                     method=method, route="media")

    def process(self, ctx: TaskContext, work: WorkItem) -> None:
        data = _read(work.input_path())
        hold(work, data)
        bad = data.startswith(b"BAD")
        if work.action in (Action.CONVERT, Action.REMUX) and not work.check_only:
            out = work.out("out" + (work.plan.target_ext or ".mp4"))
            with open(long_path(out), "wb") as f:
                f.write(b"mp4:" + data)
            work.result_path = out
            work.new_output = True
        elif work.action == Action.COPY:
            work.result_path = work.staged
        work.checks.append(CheckResult("V-AV-PROBE", CheckState.PASS))
        if bad:
            work.checks.append(CheckResult("V-AV-DUR", CheckState.FAIL, "41.2 s vs 58.0 s"))
            work.messages.append("V-AV-DUR: output 41.2 s, source 58.0 s (limit ±1.0 s).\n"
                                 "ffmpeg: [mjpeg] error count: 37")
        else:
            work.checks.append(CheckResult("V-AV-DUR", CheckState.PASS, "58.04 s vs 58.00 s"))
        work.checks.append(CheckResult("V-AV-DECODE", CheckState.PASS))


def fake_convert_batch(ctx: TaskContext, run: Any, jobs: list[LoJob]) -> list[LoResult]:
    out = []
    for j in jobs:
        data = _read(j.input_path)
        if data.startswith(b"HOLD"):
            GATE.wait(timeout=300)
        dest = j.expected_output
        with open(long_path(dest), "wb") as f:
            f.write(b"%PDF-fake\n" + data)
        out.append(LoResult(j, True, dest, method=LO_METHOD))
    return out


def fake_verapdf_batch(ctx: TaskContext, run: Any, items: list[tuple[str, str]]) -> list[CheckResult]:
    res = []
    for path, flavour in items:
        data = _read(path)
        if b"BADPDFA" in data:
            res.append(CheckResult("V-PDFA", CheckState.FAIL, flavour,
                                   "veraPDF: 6.2.11.4.1 font not embedded (2 occurrences)."))
        else:
            res.append(CheckResult("V-PDFA", CheckState.PASS, flavour))
    return res


# --------------------------------------------------------------------------- output-drive fault


class Fault:
    on = False
    out_root: str | None = None


def _faulty_copy(real):  # noqa: ANN001, ANN202
    def copy_with_hash(src: str, dst: str):  # noqa: ANN202
        if Fault.on and str(dst).endswith(".part"):
            raise OSError(64, "The specified network name is no longer available")
        return real(src, dst)

    return copy_with_hash


class _PathProxy:
    def __init__(self, real: Any) -> None:
        self._real = real

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def isdir(self, p: Any) -> bool:
        if Fault.on and Fault.out_root and os.path.normcase(str(p)).endswith(os.path.normcase(Fault.out_root)):
            return False
        return self._real.isdir(p)


def install(mp: Any) -> None:
    """Install the fakes with a pytest MonkeyPatch (or MonkeyPatch.context())."""
    from baleen.verify import pdf as vpdf

    mp.setattr(base, "_registry", {r.key: r for r in (UIImage(), UIDocument(), UIText(), UIHtml(), UIEmail(),
                                                      UIPdf(), UIMedia())})
    mp.setattr(base, "_loaded", True)
    mp.setattr(libreoffice, "convert_batch", fake_convert_batch)
    mp.setattr(vpdf, "verapdf_batch", fake_verapdf_batch)
    mp.setattr(runner, "copy_with_hash", _faulty_copy(runner.copy_with_hash))
    os_proxy = types.SimpleNamespace(**{k: getattr(os, k) for k in dir(os) if not k.startswith("__")})
    os_proxy.path = _PathProxy(os.path)
    mp.setattr(runner, "os", os_proxy)
    GATE.set()
    Fault.on = False
