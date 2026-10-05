"""Small helpers shared by fixture groups (synthetic content only)."""

from __future__ import annotations

import os
from pathlib import Path

from baleen.paths import long_path


def write_bytes(path: Path, data: bytes) -> Path:
    os.makedirs(long_path(path.parent), exist_ok=True)
    with open(long_path(path), "wb") as f:
        f.write(data)
    return path


def write_text(path: Path, text: str, encoding: str = "utf-8") -> Path:
    return write_bytes(path, text.encode(encoding))


def jpeg(path: Path, size: tuple[int, int] = (64, 48), color: tuple[int, int, int] = (40, 90, 140)) -> Path:
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG", quality=90)
    return write_bytes(path, buf.getvalue())


def image(path: Path, fmt: str, mode: str = "RGB", size: tuple[int, int] = (64, 48)) -> Path:
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new(mode, size, (200, 120, 40) if mode == "RGB" else None).save(buf, fmt)
    return write_bytes(path, buf.getvalue())


RTF = r"{\rtf1\ansi\deff0{\fonttbl{\f0 Arial;}}\f0\fs24 Synthetic test document for Baleen.\par}"


def rtf_text(title: str = "Synthetic test document for Baleen.") -> bytes:
    return RTF.replace("Synthetic test document for Baleen.", title).encode("ascii")


def simple_eml(subject: str = "Synthetic message", body: str = "Hello from a synthetic test message.") -> bytes:
    from email.message import EmailMessage

    m = EmailMessage()
    m["From"] = "Test Sender <sender@example.invalid>"
    m["To"] = "Archive <archive@example.invalid>"
    m["Date"] = "Mon, 02 Oct 2006 09:30:00 +0800"
    m["Subject"] = subject
    m.set_content(body)
    return bytes(m)
