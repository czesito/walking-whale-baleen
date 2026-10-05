"""The small smoke set for the bundle smoke test (spec §14.6): one file per route, plus
an unsupported and an ignored file. Expected statuses: tests/fixtures/smoke-expected.csv."""

from __future__ import annotations

import io
import math
import struct
import wave
from pathlib import Path

from .common import image, jpeg, rtf_text, simple_eml, write_bytes, write_text


def _wav(seconds: float = 1.0, rate: int = 22050) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate)))
                          for i in range(int(seconds * rate)))
        w.writeframes(frames)
    return buf.getvalue()


def build(dest: Path, tools) -> None:  # noqa: ANN001
    jpeg(dest / "photos" / "photo.jpg")
    image(dest / "photos" / "scan.png", "PNG")
    write_text(dest / "texts" / "notes.txt", "Synthetic smoke-test notes.\n訪談 — UTF-8 text.\n")
    write_bytes(dest / "texts" / "letter.rtf", rtf_text("Synthetic letter"))
    write_bytes(dest / "mail" / "message.eml", simple_eml())
    write_bytes(dest / "audio" / "tone.wav", _wav())
    if tools.ffmpeg:
        (dest / "video").mkdir(parents=True, exist_ok=True)
        tools.ffmpeg_run(["-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=1",
                          "-c:v", "mjpeg", "-q:v", "5", str(dest / "video" / "clip.avi")])
    write_bytes(dest / "other" / "archive.zip", b"PK\x05\x06" + b"\x00" * 18)
    write_bytes(dest / "other" / "Thumbs.db", b"\x00" * 32)
