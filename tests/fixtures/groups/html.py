"""Fixture group 'html' (spec §16.1): a page with a remote <img> and <script> (no network,
placeholder text in the PDF); a page with a relative local image. Plus a Big5 page that
declares its charset (OK) and one that doesn't (ENCODING_UNCERTAIN). Synthetic only; remote
URLs use the reserved .invalid domain."""

from __future__ import annotations

from pathlib import Path

from .common import jpeg, write_bytes

REMOTE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Remote resources</title>
<link rel="stylesheet" href="https://cdn.example.invalid/site.css">
<script src="https://cdn.example.invalid/tracker.js"></script>
<script>document.write("script output must not appear");</script>
</head><body>
<h1>Synthetic page with remote resources</h1>
<p>The image below lives on a remote server and must not be fetched.</p>
<img src="http://images.example.invalid/remote-photo.jpg" alt="remote photo">
<p style="background-image:url(//cdn.example.invalid/bg.png)">Background from a remote server.</p>
<p><a href="https://www.example.invalid/">An anchor link stays a link.</a></p>
</body></html>
"""

LOCAL = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Local image</title></head><body>
<h1>Synthetic page with a relative image</h1>
<p>The photo beside this page must appear in the PDF.</p>
<img src="img/photo.jpg" width="160" height="120" alt="local photo">
</body></html>
"""

BIG5_PAGE = """<html><head>{meta}<title>Big5</title></head><body>
<p>繁體中文網頁，合成測試內容。</p></body></html>
"""


def build(dest: Path, tools) -> None:  # noqa: ANN001
    write_bytes(dest / "remote.html", REMOTE.encode("utf-8"))
    write_bytes(dest / "local_image.html", LOCAL.encode("utf-8"))
    jpeg(dest / "img" / "photo.jpg", size=(160, 120), color=(30, 120, 200))
    meta = '<meta http-equiv="Content-Type" content="text/html; charset=big5">'
    write_bytes(dest / "big5_meta.html", BIG5_PAGE.format(meta=meta).encode("big5"))
    write_bytes(dest / "nometa_big5.htm", BIG5_PAGE.format(meta="").encode("big5"))
