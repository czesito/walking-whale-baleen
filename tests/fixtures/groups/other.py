"""Fixture group 'other' (spec §16.1): unsupported and ignored files, symlinks (POSIX)."""

from __future__ import annotations

import io
import os
import sys
import zipfile
from pathlib import Path

from .common import write_bytes


def build(dest: Path, tools) -> None:  # noqa: ANN001
    write_bytes(dest / "message.msg", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"synthetic not-an-outlook-file" * 4)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("inside.txt", "synthetic")
    write_bytes(dest / "bundle.zip", buf.getvalue())
    write_bytes(dest / "song.mid", b"MThd\x00\x00\x00\x06\x00\x00\x00\x01\x00\x60")
    write_bytes(dest / "Thumbs.db", b"\x00" * 64)
    write_bytes(dest / ".DS_Store", b"\x00\x00\x00\x01Bud1" + b"\x00" * 32)
    write_bytes(dest / "desktop.ini", b"[.ShellClassInfo]\r\n")
    write_bytes(dest / "folder" / "._resource.jpg", b"\x00\x05\x16\x07")
    write_bytes(dest / "@eaDir" / "thumb.jpg", b"synthetic")
    write_bytes(dest / "README", b"no extension: unsupported")
    if sys.platform != "win32":
        os.symlink("bundle.zip", dest / "link-to-zip.zip")
        os.symlink("folder", dest / "link-to-folder")
