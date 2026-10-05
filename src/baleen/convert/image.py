"""Images (spec §6.1, DR-16): Pillow probe, byte copy of JPEG/TIFF, conversion of the rest.

- Routing is by content: Pillow's detected format decides; a mismatch with the extension
  adds the note CONTENT_MISMATCH (§6.1).
- JPEG / TIFF content: byte copy after V-IMG, never re-encoded (DR-16). A JPEG/TIFF that
  fails V-IMG is NEEDS_REVIEW SOURCE_INVALID (§8 outcome rules, new_output=False).
- Other images: more than one frame -> NEEDS_REVIEW MULTI_FRAME_IMAGE (plan time). Alpha,
  more than 8 bits per channel or CMYK -> TIFF with LZW, ICC kept. Otherwise -> RGB JPEG,
  quality 95, 4:4:4, optimised, ICC and EXIF kept.
- A source that needs conversion but can't be decoded -> FAILED SOURCE_UNREADABLE.
- Images over 32 MP reserve 2 x their decoded size in the Files lane (§5.5).
"""

from __future__ import annotations

import os
from typing import ClassVar

import PIL
from PIL import Image

from ..model import Action, Category, Probe
from ..paths import long_path
from ..scheduler import Lane, TaskContext, image_reservation_mb
from ..verify.image import JPEG_FORMATS, TIFF_FORMATS, v_img
from .base import ProbeContext, Route, SourceRef, WorkItem, register
from .formats import JPEG_EXTS, TIFF_EXTS
from .text import note_check_messages

Image.MAX_IMAGE_PIXELS = 1_000_000_000  # §6.1

PILLOW = ".".join(PIL.__version__.split(".")[:2])
JPEG_METHOD = f"Pillow {PILLOW} · JPEG q95 4:4:4"
TIFF_METHOD = f"Pillow {PILLOW} · TIFF LZW"

# Formats Pillow may report for each extension; anything else is a content mismatch.
EXT_FORMATS: dict[str, frozenset[str]] = {
    **{e: JPEG_FORMATS for e in JPEG_EXTS},
    **{e: TIFF_FORMATS for e in TIFF_EXTS},
    ".png": frozenset({"PNG"}),
    ".bmp": frozenset({"BMP", "DIB"}),
    ".gif": frozenset({"GIF"}),
    ".webp": frozenset({"WEBP"}),
}

ALPHA_MODES = frozenset({"RGBA", "LA", "PA", "RGBa", "La"})
DEEP_MODES = frozenset({"I", "F"})  # plus every "I;16*" mode
GRAY_MODES = frozenset({"1", "L", "LA", "La", "I", "F"})
# Bytes per band of the decoded image (for the memory reservation).
BAND_BYTES = {"I": 4, "F": 4}


def _is_deep(mode: str) -> bool:
    return mode.startswith("I;16") or mode in DEEP_MODES


def _has_alpha(im: Image.Image) -> bool:
    return im.mode in ALPHA_MODES or "transparency" in im.info


def _png_bit_depth(head: bytes) -> tuple[int, int] | None:
    """(bit depth, colour type) from a PNG's IHDR, or None."""
    if len(head) >= 26 and head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
        return head[24], head[25]
    return None


def _icc_space(icc: bytes | None) -> bytes:
    return icc[16:20] if icc and len(icc) >= 20 else b""


def _family_name(fmt: str) -> str:
    return "JPEG" if fmt in JPEG_FORMATS else fmt


class ImageRoute(Route):
    key: ClassVar[str] = "image"
    lane: ClassVar[Lane] = Lane.FILES

    # ------------------------------------------------------------------ plan time

    def probe(self, ctx: ProbeContext, src: SourceRef) -> Probe:
        ext = src.ext.lower()
        expected = EXT_FORMATS.get(ext, frozenset())
        try:
            with src.open() as f:
                head = f.read(64)
                f.seek(0)
                with Image.open(f) as im:
                    fmt = im.format or ""
                    mode = im.mode
                    w, h = im.size
                    bands = len(im.getbands())
                    frames = int(getattr(im, "n_frames", 1) or 1)
                    alpha = _has_alpha(im)
        except Exception as e:
            if ext in JPEG_EXTS or ext in TIFF_EXTS:
                # Archival by extension: V-IMG decides at run time (SOURCE_INVALID, §8).
                target = ".jpg" if ext in JPEG_EXTS else ".tif"
                return Probe(Category.IMAGE, target, Action.COPY, source_format="unreadable",
                             method="byte copy", route=self.key,
                             message=f"Pillow can't read this file: {e.__class__.__name__}: {e}")
            return Probe(Category.IMAGE, ".jpg", Action.CONVERT, source_format="unreadable", route=self.key,
                         reasons=["SOURCE_UNREADABLE"],
                         message=f"Pillow can't read this image: {e.__class__.__name__}: {e}")

        notes = [] if fmt in expected else ["CONTENT_MISMATCH"]
        data = {"w": w, "h": h, "bands": bands, "bpb": BAND_BYTES.get(mode, 2 if _is_deep(mode) else 1),
                "format": fmt, "mode": mode}
        message = ""
        if notes:
            message = f"The content is {_family_name(fmt)}, not what the extension {src.ext} says."

        if fmt in JPEG_FORMATS or fmt in TIFF_FORMATS:
            target = ".jpg" if fmt in JPEG_FORMATS else ".tif"
            label = "JPEG (MPO)" if fmt == "MPO" else fmt
            if fmt in TIFF_FORMATS and frames > 1:
                label += f" ({frames} pages)"
            return Probe(Category.IMAGE, target, Action.COPY, source_format=label, method="byte copy",
                         notes=notes, message=message, route=self.key, data=data)

        deep_colour = False
        if fmt == "PNG":
            depth = _png_bit_depth(head)
            # Pillow reads 16-bit colour PNGs as 8 bits per channel (mode RGB/RGBA).
            deep_colour = bool(depth and depth[0] == 16 and mode in ("RGB", "RGBA"))
        to_tiff = alpha or _is_deep(mode) or mode == "CMYK" or deep_colour
        target = ".tif" if to_tiff else ".jpg"
        extra = (", 16-bit" if deep_colour else "") + (", transparency" if alpha and mode not in ALPHA_MODES else "")
        label = f"{fmt} ({mode}{extra})"
        if deep_colour:
            message = " ".join(m for m in (message, "Pillow reads 16-bit colour PNGs at 8 bits per channel; "
                                           "the TIFF keeps 8 bits per channel.") if m)
        reasons: list[str] = []
        method = TIFF_METHOD if to_tiff else JPEG_METHOD
        if frames > 1:
            label = f"{fmt} ({mode}, {frames} frames)"
            reasons = ["MULTI_FRAME_IMAGE"]
            method = ""  # decided at plan time: nothing is converted
            message = " ".join(m for m in (message, f"The image has {frames} frames.") if m)
        data["to_tiff"] = to_tiff
        return Probe(Category.IMAGE, target, Action.CONVERT, source_format=label, method=method, reasons=reasons,
                     notes=notes, message=message, route=self.key, data=data)

    def decide(self, work: WorkItem) -> list[str]:
        note_check_messages(work)
        return super().decide(work)

    def memory_mb(self, work: WorkItem) -> int | None:
        d = work.plan.data
        try:
            mb = image_reservation_mb(int(d["w"]), int(d["h"]), int(d.get("bands", 3)), int(d.get("bpb", 1)))
        except (KeyError, TypeError, ValueError):
            return None
        return mb or None

    # ------------------------------------------------------------------ run time

    def process(self, ctx: TaskContext, work: WorkItem) -> None:
        target = work.plan.target_ext or ".jpg"
        if work.resume:
            work.checks.append(v_img(work.input_path(), target))
            return
        if work.action in (Action.COPY, Action.CHECK):
            # DR-16: never re-encoded. Check the bytes that will be published (or the source in place).
            work.checks.append(v_img(work.input_path(), target))
            if work.action == Action.COPY and not work.check_only and work.staged:
                work.result_path = work.staged
                work.method = "byte copy"
            else:
                work.method = "checked in place"
            return
        out = work.out("output" + target)
        try:
            convert_image(work.input_path(), out, target)
        except _DecodeError as e:
            work.fail("SOURCE_UNREADABLE", f"Pillow couldn't decode the image: {e}")
            return
        except Exception as e:
            work.fail("CONVERSION_ERROR", f"Pillow couldn't write the {target} file: {e.__class__.__name__}: {e}")
            return
        work.new_output = True
        work.result_path = out
        work.method = TIFF_METHOD if target == ".tif" else JPEG_METHOD
        work.checks.append(v_img(out, target))


class _DecodeError(Exception):
    pass


def _exif_bytes(im: Image.Image) -> bytes | None:
    raw = im.info.get("exif")
    if isinstance(raw, bytes) and raw:
        return raw if raw.startswith(b"Exif\x00\x00") else b"Exif\x00\x00" + raw
    try:
        ex = im.getexif()
        if len(ex):
            return ex.tobytes()
    except Exception:
        return None
    return None


def _tiff_image(im: Image.Image) -> Image.Image:
    """A mode Pillow writes as a standard TIFF, keeping alpha, depth and CMYK."""
    mode = im.mode
    transparent = "transparency" in im.info
    if mode in ("RGBa", "PA") or (mode == "P" and transparent) or (mode == "RGB" and transparent):
        return im.convert("RGBA")
    if mode == "La" or (mode in ("L", "1") and transparent):
        return im.convert("LA")
    if mode == "P":
        return im.convert("RGB")
    if mode in ("RGBA", "LA", "CMYK", "RGB", "L", "1", "I", "F") or mode.startswith("I;16"):
        return im
    return im.convert("RGBA" if _has_alpha(im) else "RGB")


def _jpeg_image(im: Image.Image, icc: bytes | None) -> Image.Image:
    """RGB for the JPEG (§6.1). A grey image whose ICC profile is GRAY stays grey, so the
    kept profile still describes the pixels."""
    if im.mode in ("L", "1") and _icc_space(icc) == b"GRAY":
        return im.convert("L") if im.mode != "L" else im
    return im if im.mode == "RGB" else im.convert("RGB")


def convert_image(src: str, out: str, target: str) -> None:
    """Decode `src` fully and write `out` (.jpg or .tif) per §6.1. Raises _DecodeError when
    the source can't be decoded; other exceptions are write errors."""
    try:
        im = Image.open(long_path(src))
        im.load()
    except Exception as e:
        raise _DecodeError(f"{e.__class__.__name__}: {e}") from e
    try:
        icc = im.info.get("icc_profile") or None
        if target == ".tif":
            t = _tiff_image(im)
            params: dict[str, object] = {"compression": "tiff_lzw"}
            if icc:
                params["icc_profile"] = icc
            t.save(long_path(out), "TIFF", **params)
        else:
            exif = _exif_bytes(im)
            j = _jpeg_image(im, icc)
            params = {"quality": 95, "subsampling": 0, "optimize": True}
            if icc:
                params["icc_profile"] = icc
            if exif:
                params["exif"] = exif
            j.save(long_path(out), "JPEG", **params)
    except Exception:
        try:
            os.unlink(long_path(out))
        except OSError:
            pass
        raise
    finally:
        im.close()


register(ImageRoute())
