"""The format matrix of spec §6: extension -> route, category and default target.

Routing starts from the extension; route probes (Pillow, FFprobe, XMP) refine it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from ..model import Action, Category, PlanItem, Probe
from ..scheduler import Lane, TaskContext
from .base import ProbeContext, Route, SourceRef, WorkItem

JPEG_EXTS = (".jpg", ".jpeg", ".jpe")
TIFF_EXTS = (".tif", ".tiff")
CONVERT_IMAGE_EXTS = (".png", ".bmp", ".gif", ".webp")
WRITER_EXTS = (".doc", ".docx", ".rtf", ".odt", ".wpd", ".wps", ".sxw")
CALC_EXTS = (".xls", ".xlsx", ".ods")
IMPRESS_EXTS = (".ppt", ".pptx", ".odp")
TEXT_EXTS = (".txt",)
HTML_EXTS = (".html", ".htm")
EMAIL_EXTS = (".eml",)
PDF_EXTS = (".pdf",)
VIDEO_EXTS = (".avi", ".mov", ".mp4", ".m4v", ".mkv", ".mpg", ".mpeg", ".vob", ".wmv", ".asf",
              ".flv", ".3gp", ".webm", ".ts", ".mts", ".m2ts", ".dv")
AUDIO_EXTS = (".wma", ".mp3", ".wav", ".aif", ".aiff", ".aac", ".m4a", ".flac", ".ogg", ".oga",
              ".opus", ".amr", ".au")

EXPORT_FILTER: dict[str, str] = {
    **{e: "writer_pdf_Export" for e in WRITER_EXTS + TEXT_EXTS + HTML_EXTS + EMAIL_EXTS},
    **{e: "calc_pdf_Export" for e in CALC_EXTS},
    **{e: "impress_pdf_Export" for e in IMPRESS_EXTS},
}


@dataclass(frozen=True)
class Format:
    route: str
    category: Category


def _table() -> dict[str, Format]:
    t: dict[str, Format] = {}
    for e in JPEG_EXTS + TIFF_EXTS + CONVERT_IMAGE_EXTS:
        t[e] = Format("image", Category.IMAGE)
    for e in WRITER_EXTS + CALC_EXTS + IMPRESS_EXTS:
        t[e] = Format("document", Category.DOCUMENT)
    for e in TEXT_EXTS:
        t[e] = Format("text", Category.TEXT)
    for e in HTML_EXTS:
        t[e] = Format("html", Category.HTML)
    for e in EMAIL_EXTS:
        t[e] = Format("email", Category.EMAIL)
    for e in PDF_EXTS:
        t[e] = Format("pdf", Category.PDF)
    for e in VIDEO_EXTS:
        t[e] = Format("media", Category.VIDEO)
    for e in AUDIO_EXTS:
        t[e] = Format("media", Category.AUDIO)
    return t


FORMATS: dict[str, Format] = _table()


def lookup(ext: str) -> Format | None:
    return FORMATS.get(ext.lower())


def default_target(ext: str, settings: dict[str, Any]) -> str | None:
    """Target extension from the extension alone (used when content probing is not possible)."""
    ext = ext.lower()
    if ext in JPEG_EXTS:
        return ".jpg"
    if ext in TIFF_EXTS:
        return ".tif"
    if ext in CONVERT_IMAGE_EXTS:
        return ".jpg"
    if ext in VIDEO_EXTS:
        return ".mp4"
    if ext in AUDIO_EXTS:
        return "." + settings["workflow"].get("audio_container", "m4a")
    f = lookup(ext)
    if f and f.route in ("document", "text", "html", "email", "pdf"):
        return ".pdf"
    return None


def is_archival_ext(ext: str) -> bool:
    """Formats that are already archival by extension (JPEG, TIFF, PDF, MP4, M4A)."""
    return ext.lower() in JPEG_EXTS + TIFF_EXTS + PDF_EXTS + (".mp4", ".m4a")


class MissingRoute(Route):
    """Placeholder for a route module that is not built yet. Items fail honestly."""

    lane: ClassVar[Lane] = Lane.FILES

    def __init__(self, key: str) -> None:
        self._key = key

    @property
    def key(self) -> str:  # type: ignore[override]
        return self._key

    def probe(self, ctx: ProbeContext, src: SourceRef) -> Probe:
        f = lookup(src.ext)
        cat = f.category if f else Category.OTHER
        return Probe(category=cat, target_ext=default_target(src.ext, ctx.settings), action=Action.CONVERT,
                     route=self._key)

    def process(self, ctx: TaskContext, work: WorkItem) -> None:
        work.fail("CONVERSION_ERROR", f"The {self._key} converter is not available in this build.")

    def expand(self, ctx: ProbeContext, item: PlanItem, src: SourceRef) -> list:  # type: ignore[override]
        return []
