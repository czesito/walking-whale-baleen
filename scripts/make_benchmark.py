"""Generate the synthetic benchmark set of spec AC-16 (and AC-13's 2,000 JPEGs).

    python scripts/make_benchmark.py DEST [--jpegs 2000] [--docs 200] [--videos 40] [--jpeg-kb 500]

- JPEGs: noise-textured photos of about --jpeg-kb each (2,000 x 500 KB = 1 GB, AC-13).
- Documents: half DOCX, half RTF, several pages each (pure Python; no tools needed).
- Videos: 10 s 640x480 clips via FFmpeg lavfi: MJPEG AVI and MPEG-1 MPG (transcode) and
  H.264 MP4 (copy).
Synthetic only (spec §16.1).
"""

from __future__ import annotations

import argparse
import io
import os
import random
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

WORDS = ("archive event documentation interview exhibition catalogue artist performance gallery "
         "photograph recording letter minutes agenda budget programme review press release").split()


def _para(rnd: random.Random, n: int = 60) -> str:
    return " ".join(rnd.choice(WORDS) for _ in range(n)).capitalize() + "."


def make_jpeg(path: Path, kb: int, seed: int) -> None:
    from PIL import Image, ImageFilter

    rnd = random.Random(seed)
    w, h = 1600, 1200
    base = Image.frombytes("RGB", (w // 8, h // 8), rnd.randbytes((w // 8) * (h // 8) * 3))
    img = base.resize((w, h), Image.BILINEAR).filter(ImageFilter.GaussianBlur(1))
    noise = Image.frombytes("L", (w, h), rnd.randbytes(w * h)).convert("RGB")
    img = Image.blend(img, noise, 0.18)
    q = 88
    for _ in range(4):
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=q)
        size = buf.tell()
        if abs(size - kb * 1024) < kb * 1024 * 0.25:
            break
        q = max(40, min(97, int(q + (8 if size < kb * 1024 else -8))))
    path.write_bytes(buf.getvalue())


DOCX_CT = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/'
           'package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
           'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/'
           'document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"'
           '/></Types>')
DOCX_RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.'
             'openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.'
             'openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
             '</Relationships>')


def make_docx(path: Path, seed: int, paras: int = 40) -> None:
    from xml.sax.saxutils import escape

    rnd = random.Random(seed)
    body = "".join(f"<w:p><w:r><w:t>{escape(_para(rnd))}</w:t></w:r></w:p>" for _ in range(paras))
    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="http://schemas.'
           f'openxmlformats.org/wordprocessingml/2006/main"><w:body>{body}</w:body></w:document>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", DOCX_CT)
        z.writestr("_rels/.rels", DOCX_RELS)
        z.writestr("word/document.xml", doc)


def make_rtf(path: Path, seed: int, paras: int = 40) -> None:
    rnd = random.Random(seed)
    body = "".join(_para(rnd) + r"\par " for _ in range(paras))
    path.write_bytes((r"{\rtf1\ansi\deff0{\fonttbl{\f0 Arial;}}\f0\fs22 " + body + "}").encode("ascii"))


def make_video(ffmpeg: str, env: dict[str, str], path: Path, kind: str, seed: int) -> None:
    from baleen import proc

    src = f"testsrc2=size=640x480:rate=25:duration=10,hue=h={seed * 37 % 360}"
    aud = f"sine=frequency={220 + seed * 7}:duration=10"
    codec = {"avi": ["-c:v", "mjpeg", "-q:v", "4", "-c:a", "pcm_s16le"],
             "mpg": ["-c:v", "mpeg1video", "-b:v", "2M", "-c:a", "mp2", "-f", "mpeg"],
             "mp4": ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac"]}[kind]
    args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "lavfi", "-i", src,
            "-f", "lavfi", "-i", aud, "-shortest", *codec, str(path)]
    r = proc.run(args, timeout=300, env=env)
    if not r.ok:
        raise RuntimeError(r.err())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dest")
    ap.add_argument("--jpegs", type=int, default=2000)
    ap.add_argument("--docs", type=int, default=200)
    ap.add_argument("--videos", type=int, default=40)
    ap.add_argument("--jpeg-kb", type=int, default=500)
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 4)
    ns = ap.parse_args()
    dest = Path(ns.dest)
    (dest / "photos").mkdir(parents=True, exist_ok=True)
    (dest / "documents").mkdir(parents=True, exist_ok=True)
    (dest / "video").mkdir(parents=True, exist_ok=True)
    for i in range(0, ns.jpegs, 100):
        (dest / "photos" / f"event{i // 100:02d}").mkdir(exist_ok=True)

    def jpeg_job(i: int) -> None:
        make_jpeg(dest / "photos" / f"event{i // 100:02d}" / f"IMG_{i:04d}.jpg", ns.jpeg_kb, i)

    with ThreadPoolExecutor(ns.workers) as ex:
        list(ex.map(jpeg_job, range(ns.jpegs)))
        for i in range(ns.docs):
            if i % 2 == 0:
                make_docx(dest / "documents" / f"doc{i:03d}.docx", i)
            else:
                make_rtf(dest / "documents" / f"doc{i:03d}.rtf", i)
        if ns.videos:
            from baleen.home import Home
            from baleen.tools import Toolset

            home = Home(dest.parent / f".{dest.name}-home")
            ts = Toolset(home)
            ffmpeg = ts.path("ffmpeg")
            if not ffmpeg:
                print("ffmpeg not found; skipping videos")
            else:
                env = ts.env()
                kinds = ["avi", "mpg", "mp4", "avi"]
                list(ex.map(lambda i: make_video(ffmpeg, env, dest / "video" / f"clip{i:02d}.{kinds[i % 4]}",
                                                 kinds[i % 4], i), range(ns.videos)))
    total = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file())
    print(f"benchmark set in {dest}: {total / 1e9:.2f} GB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
