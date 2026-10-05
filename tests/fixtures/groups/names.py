"""Fixture group 'names' (spec §16.1): CJK folders and files, spaces, emoji, near-limit lengths,
and the clash cases of §7.3. Uses quick formats (JPEG, BMP, text, RTF-in-.doc, e-mail)."""

from __future__ import annotations

import os
from pathlib import Path

from .common import image, jpeg, rtf_text, simple_eml, write_bytes, write_text


def build(dest: Path, tools) -> None:  # noqa: ANN001
    jpeg(dest / "訪談記錄" / "照片 01.jpg")
    jpeg(dest / "with spaces" / "a photo.jpg")
    jpeg(dest / "🐋 whale.jpg")
    # §7.3 clash cases
    write_bytes(dest / "clash" / "report.doc", rtf_text("report"))
    jpeg(dest / "clash" / "IMG_0001.JPG")
    write_bytes(dest / "clash" / "訪談.doc", rtf_text("interview"))
    write_bytes(dest / "clash" / "訪談.eml", simple_eml("訪談"))
    image(dest / "clash" / "photo.bmp", "BMP")
    jpeg(dest / "clash" / "photo.jpg")
    write_text(dest / "clash" / "notes.txt", "Synthetic notes.\n")
    write_bytes(dest / "clash" / "notes.pdf", _plain_pdf())
    # near-limit lengths: 251 + ".jpg" = 255 code units (fits); a clash pushes past 255
    jpeg(dest / "long" / ("n" * 251 + ".jpg"))
    image(dest / "long" / ("c" * 250 + ".bmp"), "BMP")
    jpeg(dest / "long" / ("c" * 250 + ".jpg"))
    if not _case_insensitive(dest):
        jpeg(dest / "case" / "a.JPG", color=(1, 2, 3))
        jpeg(dest / "case" / "a.jpg", color=(3, 2, 1))


def _plain_pdf() -> bytes:
    from io import BytesIO

    from pypdf import PdfWriter

    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    buf = BytesIO()
    w.write(buf)
    return buf.getvalue()


def _case_insensitive(d: Path) -> bool:
    probe = d / "CaseProbe.tmp"
    probe.write_text("x")
    try:
        return os.path.exists(d / "caseprobe.tmp")
    finally:
        probe.unlink()
