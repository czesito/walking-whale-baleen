"""Image route (§6.1, DR-16) and V-IMG (§8)."""

from __future__ import annotations

import hashlib
import struct
import zlib
from pathlib import Path

import pytest
from PIL import Image, ImageCms, ImageFile, JpegImagePlugin

from baleen.convert.image import ImageRoute, convert_image
from baleen.model import Action, CheckState
from baleen.verify.image import v_img

from .a_support import FakeTaskContext, encode_image, probe_ctx, run_ctx, src_bytes, work_item

SRGB = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
ROUTE = ImageRoute()


def gradient(size=(64, 48), mode="RGB"):  # noqa: ANN001, ANN201
    w, h = size
    im = Image.new("RGB", size)
    im.putdata([(x * 255 // w, y * 255 // h, 90) for y in range(h) for x in range(w)])
    return im.convert(mode) if mode != "RGB" else im


def jpeg_with_meta() -> bytes:
    exif = Image.Exif()
    exif[0x010F] = "Synthetic Camera"
    return encode_image(gradient(), "JPEG", quality=90, icc_profile=SRGB, exif=exif.tobytes())


def png16_rgb(w: int = 4, h: int = 3) -> bytes:
    raw = b"".join(b"\x00" + struct.pack(">" + "H" * 3 * w, *([40000, 1000, 65535] * w)) for _ in range(h))

    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 16, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def truncated_jpeg() -> bytes:
    full = encode_image(Image.effect_noise((128, 128), 64).convert("RGB"), "JPEG", quality=90)
    return full[: int(len(full) * 0.6)]


# --------------------------------------------------------------------------- probe


@pytest.mark.parametrize(("name", "data", "target", "action", "fmt"), [
    ("photo.jpg", jpeg_with_meta(), ".jpg", Action.COPY, "JPEG"),
    ("photo.jpeg", jpeg_with_meta(), ".jpg", Action.COPY, "JPEG"),
    ("scan.tif", encode_image(gradient(), "TIFF", save_all=True, append_images=[gradient((10, 10))]), ".tif",
     Action.COPY, "TIFF (2 pages)"),
    ("rgba.png", encode_image(gradient(mode="RGBA"), "PNG"), ".tif", Action.CONVERT, "PNG (RGBA)"),
    ("deep.png", encode_image(Image.new("I;16", (8, 8), 40000), "PNG"), ".tif", Action.CONVERT, "PNG (I;16)"),
    ("deep_rgb.png", png16_rgb(), ".tif", Action.CONVERT, "PNG (RGB, 16-bit)"),
    ("grey.png", encode_image(gradient(mode="L"), "PNG"), ".jpg", Action.CONVERT, "PNG (L)"),
    ("scan.bmp", encode_image(gradient(), "BMP"), ".jpg", Action.CONVERT, "BMP (RGB)"),
    ("pic.webp", encode_image(gradient(), "WEBP"), ".jpg", Action.CONVERT, "WEBP (RGB)"),
])
def test_probe_targets(home, name, data, target, action, fmt) -> None:  # noqa: ANN001
    pr = ROUTE.probe(probe_ctx(home), src_bytes(name, data))
    assert (pr.target_ext, pr.action, pr.source_format) == (target, action, fmt)
    assert pr.reasons == [] and pr.notes == [] and pr.route == "image"


def test_probe_palette_with_transparency_goes_to_tiff(home) -> None:  # noqa: ANN001
    data = encode_image(Image.new("P", (8, 8), 0), "PNG", transparency=0)
    pr = ROUTE.probe(probe_ctx(home), src_bytes("p.png", data))
    assert pr.target_ext == ".tif" and pr.source_format == "PNG (P, transparency)"


def test_probe_sixteen_bit_colour_png_is_explained(home) -> None:  # noqa: ANN001
    pr = ROUTE.probe(probe_ctx(home), src_bytes("deep_rgb.png", png16_rgb()))
    assert "8 bits per channel" in pr.message


def test_probe_multi_frame_is_decided_at_plan_time(home) -> None:  # noqa: ANN001
    frames = [Image.new("RGB", (8, 8), c) for c in ((255, 0, 0), (0, 255, 0), (0, 0, 255))]
    data = encode_image(frames[0], "GIF", save_all=True, append_images=frames[1:])
    pr = ROUTE.probe(probe_ctx(home), src_bytes("anim.gif", data))
    assert pr.reasons == ["MULTI_FRAME_IMAGE"] and pr.method == ""
    assert pr.source_format == "GIF (P, 3 frames)" and "3 frames" in pr.message


def test_probe_content_mismatch_routes_by_content(home) -> None:  # noqa: ANN001
    png_as_jpg = ROUTE.probe(probe_ctx(home), src_bytes("x.jpg", encode_image(gradient(), "PNG")))
    assert png_as_jpg.notes == ["CONTENT_MISMATCH"] and png_as_jpg.action == Action.CONVERT
    assert png_as_jpg.target_ext == ".jpg" and png_as_jpg.source_format == "PNG (RGB)"
    jpg_as_png = ROUTE.probe(probe_ctx(home), src_bytes("x.png", jpeg_with_meta()))
    assert jpg_as_png.notes == ["CONTENT_MISMATCH"] and jpg_as_png.action == Action.COPY
    assert jpg_as_png.target_ext == ".jpg"
    tif_as_bmp = ROUTE.probe(probe_ctx(home), src_bytes("x.bmp", encode_image(gradient(), "TIFF")))
    assert tif_as_bmp.target_ext == ".tif" and tif_as_bmp.action == Action.COPY


def test_probe_unreadable(home) -> None:  # noqa: ANN001
    conv = ROUTE.probe(probe_ctx(home), src_bytes("broken.png", b"not an image at all"))
    assert conv.reasons == ["SOURCE_UNREADABLE"]
    # JPEG/TIFF by extension: V-IMG decides at run time (SOURCE_INVALID takes precedence, §8).
    arch = ROUTE.probe(probe_ctx(home), src_bytes("broken.jpg", b"not an image at all"))
    assert arch.reasons == [] and arch.action == Action.COPY and arch.target_ext == ".jpg"


def test_memory_reservation_for_big_images(home, tmp_path: Path) -> None:  # noqa: ANN001
    src = tmp_path / "src" / "big.png"
    src.parent.mkdir(parents=True)
    src.write_bytes(encode_image(gradient(), "PNG"))
    pr = ROUTE.probe(probe_ctx(home), src_bytes("big.png", src.read_bytes()))
    wi = work_item(run_ctx(home, tmp_path), src, pr, stage=False)
    assert ROUTE.memory_mb(wi) is None
    wi.plan.data.update({"w": 8000, "h": 5000, "bands": 3, "bpb": 1})  # 40 MP > 32 MP
    assert ROUTE.memory_mb(wi) == 229  # 2 x decoded size, MiB


# --------------------------------------------------------------------------- process


def _work(home, tmp_path: Path, name: str, data: bytes, **kw):  # noqa: ANN001, ANN003, ANN202
    src = tmp_path / "src" / name
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(data)
    pr = ROUTE.probe(probe_ctx(home), src_bytes(name, data))
    return work_item(run_ctx(home, tmp_path), src, pr, **kw)


def test_jpeg_is_byte_copied_never_reencoded(home, tmp_path: Path) -> None:  # noqa: ANN001
    data = jpeg_with_meta()
    wi = _work(home, tmp_path, "photo.jpg", data)
    ROUTE.process(FakeTaskContext(), wi)
    assert wi.result_path == wi.staged and not wi.new_output and wi.method == "byte copy"
    assert hashlib.sha256(Path(wi.result_path).read_bytes()).digest() == hashlib.sha256(data).digest()
    assert [c.report_form() for c in wi.checks] == ["V-IMG=pass"]
    assert ROUTE.decide(wi) == []


def test_truncated_jpeg_is_source_invalid(home, tmp_path: Path) -> None:  # noqa: ANN001
    wi = _work(home, tmp_path, "bad.jpg", truncated_jpeg())
    ROUTE.process(FakeTaskContext(), wi)
    assert wi.checks[0].state == CheckState.FAIL and "truncated" in wi.checks[0].message
    assert ROUTE.decide(wi) == ["SOURCE_INVALID"] and wi.messages


def test_check_only_reads_in_place(home, tmp_path: Path) -> None:  # noqa: ANN001
    wi = _work(home, tmp_path, "photo.jpg", jpeg_with_meta(), check_only=True, action=Action.CHECK)
    ROUTE.process(FakeTaskContext(), wi)
    assert wi.staged is None and wi.result_path is None and wi.method == "checked in place"
    assert wi.checks[0].state == CheckState.PASS


def test_convert_to_jpeg_keeps_icc_and_exif(home, tmp_path: Path) -> None:  # noqa: ANN001
    im = gradient()
    exif = Image.Exif()
    exif[0x0132] = "2004:05:06 07:08:09"
    data = encode_image(im, "PNG", icc_profile=SRGB, exif=exif.tobytes())
    wi = _work(home, tmp_path, "photo.png", data)
    ROUTE.process(FakeTaskContext(), wi)
    assert wi.new_output and wi.result_path and wi.result_path.endswith(".jpg")
    assert wi.method.endswith("JPEG q95 4:4:4") and wi.checks[0].state == CheckState.PASS
    with Image.open(wi.result_path) as out:
        assert out.format == "JPEG" and out.mode == "RGB"
        assert JpegImagePlugin.get_sampling(out) == 0  # 4:4:4
        assert out.info.get("icc_profile") == SRGB
        assert out.getexif().get(0x0132) == "2004:05:06 07:08:09"


def test_convert_to_tiff_lzw_keeps_alpha_and_icc(home, tmp_path: Path) -> None:  # noqa: ANN001
    data = encode_image(gradient(mode="RGBA"), "PNG", icc_profile=SRGB)
    wi = _work(home, tmp_path, "alpha.png", data)
    ROUTE.process(FakeTaskContext(), wi)
    assert wi.result_path and wi.result_path.endswith(".tif") and wi.method.endswith("TIFF LZW")
    with Image.open(wi.result_path) as out:
        assert out.format == "TIFF" and out.mode == "RGBA"
        assert out.info.get("compression") == "tiff_lzw" and out.info.get("icc_profile") == SRGB


@pytest.mark.parametrize(("mode", "params", "expect"), [
    ("P", {"transparency": 0}, "RGBA"), ("LA", {}, "LA"), ("I;16", {}, "I;16"), ("L", {"transparency": 0}, "LA"),
])
def test_tiff_modes(home, tmp_path: Path, mode, params, expect) -> None:  # noqa: ANN001
    im = gradient(mode="LA") if mode == "LA" else Image.new(mode, (8, 8))
    wi = _work(home, tmp_path, "x.png", encode_image(im, "PNG", **params))
    ROUTE.process(FakeTaskContext(), wi)
    with Image.open(wi.result_path) as out:
        assert out.mode == expect
    assert wi.checks[0].state == CheckState.PASS


@pytest.mark.parametrize(("mode", "expect"), [("PA", "RGBA"), ("La", "LA"), ("RGBa", "RGBA"), ("CMYK", "CMYK")])
def test_tiff_mode_mapping_in_memory(mode, expect) -> None:  # noqa: ANN001
    from baleen.convert.image import _tiff_image

    im = Image.new("RGBA", (4, 4), (1, 2, 3, 100)).convert(mode) if mode != "CMYK" else Image.new("CMYK", (4, 4))
    assert _tiff_image(im).mode == expect


def test_grey_with_gray_icc_stays_grey(tmp_path: Path) -> None:
    gray_icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("LAB")).tobytes()  # any profile...
    gray_icc = gray_icc[:16] + b"GRAY" + gray_icc[20:]  # ...marked as a GRAY colour space
    src = tmp_path / "g.png"
    src.write_bytes(encode_image(gradient(mode="L"), "PNG", icc_profile=gray_icc))
    convert_image(str(src), str(tmp_path / "g.jpg"), ".jpg")
    with Image.open(tmp_path / "g.jpg") as out:
        assert out.mode == "L" and out.info.get("icc_profile") == gray_icc
    plain = tmp_path / "p.png"
    plain.write_bytes(encode_image(gradient(mode="L"), "PNG"))
    convert_image(str(plain), str(tmp_path / "p.jpg"), ".jpg")
    with Image.open(tmp_path / "p.jpg") as out:
        assert out.mode == "RGB"  # §6.1: RGB JPEG


def test_cmyk_tiff_conversion(tmp_path: Path) -> None:
    src = tmp_path / "c.tif"
    src.write_bytes(encode_image(Image.new("CMYK", (8, 8), (10, 20, 30, 40)), "TIFF"))
    convert_image(str(src), str(tmp_path / "c_out.tif"), ".tif")
    with Image.open(tmp_path / "c_out.tif") as out:
        assert out.mode == "CMYK" and out.info.get("compression") == "tiff_lzw"


def test_undecodable_conversion_source_is_source_unreadable(home, tmp_path: Path) -> None:  # noqa: ANN001
    full = encode_image(Image.effect_noise((128, 128), 64).convert("RGB"), "PNG")
    wi = _work(home, tmp_path, "cut.png", full[: len(full) // 2])  # header fine, data truncated
    assert wi.plan.reasons == []
    ROUTE.process(FakeTaskContext(), wi)
    assert wi.done and wi.reasons == ["SOURCE_UNREADABLE"] and wi.result_path is None
    assert not list(Path(wi.work_dir).glob("output.*"))


def test_resume_rechecks_existing_output(home, tmp_path: Path) -> None:  # noqa: ANN001
    wi = _work(home, tmp_path, "photo.png", encode_image(gradient(), "PNG"), stage=False)
    existing = tmp_path / "out" / "photo.jpg"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(truncated_jpeg())
    wi.resume, wi.existing_output = True, str(existing)
    ROUTE.process(FakeTaskContext(), wi)
    assert wi.result_path is None and ROUTE.decide(wi) == ["SOURCE_INVALID"]  # runner: OUTPUT_INVALID


# --------------------------------------------------------------------------- V-IMG


def test_v_img_pass_and_extension_family(tmp_path: Path) -> None:
    p = tmp_path / "a.bin"
    p.write_bytes(jpeg_with_meta())
    assert v_img(str(p), ".jpg").state == CheckState.PASS
    assert v_img(str(p), ".JPEG").state == CheckState.PASS
    r = v_img(str(p), ".tif")
    assert r.state == CheckState.FAIL and "JPEG" in r.message
    assert v_img(str(p), ".png").state == CheckState.FAIL


def test_v_img_rejects_other_content(tmp_path: Path) -> None:
    p = tmp_path / "x.jpg"
    p.write_bytes(encode_image(gradient(), "PNG"))
    r = v_img(str(p), ".jpg")
    assert r.state == CheckState.FAIL and "PNG" in r.message
    p.write_bytes(b"\x00" * 100)
    assert v_img(str(p), ".jpg").state == CheckState.FAIL


def test_v_img_truncated_jpeg_fails_though_verify_passes(tmp_path: Path) -> None:
    p = tmp_path / "t.jpg"
    p.write_bytes(truncated_jpeg())
    with Image.open(p) as im:
        im.verify()  # Pillow's verify() does not notice
    r = v_img(str(p), ".jpg")
    assert r.state == CheckState.FAIL and "truncated" in r.message


def test_v_img_loads_every_tiff_page(tmp_path: Path) -> None:
    p1 = Image.effect_noise((64, 64), 50).convert("RGB")
    p2 = Image.effect_noise((64, 64), 50).convert("RGB")
    data = encode_image(p1, "TIFF", save_all=True, append_images=[p2])
    good = tmp_path / "good.tif"
    good.write_bytes(data)
    assert v_img(str(good), ".tif").state == CheckState.PASS
    # Corrupt only the second page's pixel data: the first page still decodes.
    with Image.open(good) as im:
        assert im.n_frames == 2
        im.seek(1)
        offset = im.tag_v2[273][0]  # StripOffsets of page 2
        length = sum(im.tag_v2[279])
    bad = tmp_path / "bad.tif"
    bad.write_bytes(data[:offset + length // 2])
    with Image.open(bad) as im:
        im.load()  # page 1 is fine
    r = v_img(str(bad), ".tif")
    assert r.state == CheckState.FAIL and "frame 2" in r.message


def test_v_img_never_accepts_truncated_mode(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    p = tmp_path / "t.jpg"
    p.write_bytes(truncated_jpeg())
    monkeypatch.setattr(ImageFile, "LOAD_TRUNCATED_IMAGES", True)
    assert v_img(str(p), ".jpg").state == CheckState.UNAVAILABLE


def test_max_image_pixels_raised() -> None:
    import baleen.convert.image  # noqa: F401

    assert Image.MAX_IMAGE_PIXELS == 1_000_000_000
