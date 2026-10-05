"""V-IMG (spec §8): the detected format is JPEG/TIFF and matches the extension, and every
frame decodes fully (every TIFF page is loaded).

`Image.verify()` is not enough: it passes truncated JPEGs. Only a full `load()` of every
frame proves the pixels are there. Truncated data is never accepted
(`ImageFile.LOAD_TRUNCATED_IMAGES` stays False).
"""

from __future__ import annotations

from PIL import Image, ImageFile

from ..model import CheckResult, CheckState
from ..paths import long_path

# §6.1: large archival scans must open. Sources are local files the user chose.
Image.MAX_IMAGE_PIXELS = 1_000_000_000

JPEG_FORMATS = frozenset({"JPEG", "MPO"})  # MPO: camera JPEGs with extra (preview/stereo) frames
TIFF_FORMATS = frozenset({"TIFF"})

_FAMILY = {".jpg": ("JPEG", JPEG_FORMATS), ".jpeg": ("JPEG", JPEG_FORMATS), ".jpe": ("JPEG", JPEG_FORMATS),
           ".tif": ("TIFF", TIFF_FORMATS), ".tiff": ("TIFF", TIFF_FORMATS)}


def v_img(path: str, ext: str) -> CheckResult:
    """Check the file at `path` as a `ext` (.jpg / .tif) file. Read-only.

    `ext` is the extension the file is (or will be) published under, which may differ from
    the staged copy's name (a JPEG named .png is published as .jpg).
    """
    family = _FAMILY.get(ext.lower())
    if family is None:
        return CheckResult("V-IMG", CheckState.FAIL, message=f"{ext} is not a JPEG or TIFF extension.")
    name, formats = family
    if ImageFile.LOAD_TRUNCATED_IMAGES:  # never accept partial pixels, whoever set this
        return CheckResult("V-IMG", CheckState.UNAVAILABLE,
                           message="Pillow is configured to accept truncated images; V-IMG can't run.")
    frame = 0
    try:
        with Image.open(long_path(path)) as im:
            if im.format not in formats:
                return CheckResult("V-IMG", CheckState.FAIL,
                                   message=f"The content is {im.format or 'not a known image format'}, "
                                           f"not {name} as the extension {ext} says.")
            n = int(getattr(im, "n_frames", 1) or 1)
            for frame in range(n):
                im.seek(frame)
                im.load()
    except MemoryError:
        return CheckResult("V-IMG", CheckState.UNAVAILABLE,
                           message="Not enough memory to decode the image; it could not be checked.")
    except Image.UnidentifiedImageError:
        return CheckResult("V-IMG", CheckState.FAIL, message=f"Pillow doesn't recognise this file as {name}.")
    except Exception as e:  # OSError (truncated), SyntaxError, ValueError, DecompressionBombError, ...
        where = f" (frame {frame + 1})" if frame else ""
        return CheckResult("V-IMG", CheckState.FAIL,
                           message=f"The image doesn't decode fully{where}: {e.__class__.__name__}: {e}")
    return CheckResult("V-IMG", CheckState.PASS)
