"""Fixture group 'images' (spec §16.1): JPEG with EXIF + ICC; .JPG upper-case; 2-page TIFF;
PNG RGBA; PNG 16-bit; BMP 24-bit; animated GIF (3 frames); WebP; PNG content named .jpg;
truncated JPEG. Synthetic content only (Pillow)."""

from __future__ import annotations

import io
from pathlib import Path

from .common import write_bytes


def _encode(im, fmt: str, **params) -> bytes:  # noqa: ANN001, ANN003
    buf = io.BytesIO()
    im.save(buf, fmt, **params)
    return buf.getvalue()


def _gradient(size: tuple[int, int] = (160, 120)):  # noqa: ANN202
    from PIL import Image

    w, h = size
    im = Image.new("RGB", size)
    im.putdata([(x * 255 // w, y * 255 // h, (x + y) % 256) for y in range(h) for x in range(w)])
    return im


def build(dest: Path, tools) -> None:  # noqa: ANN001
    from PIL import Image, ImageCms

    srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    exif = Image.Exif()
    exif[0x010F] = "Synthetic Camera Co."  # Make
    exif[0x0110] = "Baleen Fixture 1"  # Model
    exif[0x0132] = "2004:05:06 07:08:09"  # DateTime
    write_bytes(dest / "photo_exif.jpg",
                _encode(_gradient(), "JPEG", quality=90, icc_profile=srgb, exif=exif.tobytes()))
    write_bytes(dest / "UPPER.JPG", _encode(_gradient((96, 64)), "JPEG", quality=85))

    p1 = _gradient((120, 90))
    p2 = Image.new("RGB", (80, 60), (30, 60, 200))
    write_bytes(dest / "pages.tif", _encode(p1, "TIFF", save_all=True, append_images=[p2], compression="tiff_lzw"))

    rgba = Image.new("RGBA", (80, 60), (200, 40, 40, 255))
    rgba.paste((40, 200, 40, 90), (20, 15, 60, 45))
    write_bytes(dest / "alpha.png", _encode(rgba, "PNG"))

    deep = Image.new("I;16", (64, 48))
    deep.putdata([(x * 1000 + y * 7) % 65536 for y in range(48) for x in range(64)])
    write_bytes(dest / "deep16.png", _encode(deep, "PNG"))

    write_bytes(dest / "scan.bmp", _encode(_gradient((100, 70)), "BMP"))

    frames = [Image.new("RGB", (40, 30), c) for c in ((255, 0, 0), (0, 255, 0), (0, 0, 255))]
    gif = _encode(frames[0], "GIF", save_all=True, append_images=frames[1:], duration=200, loop=0)
    with Image.open(io.BytesIO(gif)) as g:
        assert g.n_frames == 3, g.n_frames
    write_bytes(dest / "anim.gif", gif)

    write_bytes(dest / "picture.webp", _encode(_gradient((90, 60)), "WEBP", quality=80))
    write_bytes(dest / "png_named.jpg", _encode(_gradient((50, 40)), "PNG"))

    noise = Image.effect_noise((256, 256), 64).convert("RGB")
    full = _encode(noise, "JPEG", quality=90)
    write_bytes(dest / "truncated.jpg", full[: int(len(full) * 0.6)])
