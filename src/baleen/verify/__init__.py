"""Verification (spec §8). Core helpers live here; format checks live in submodules:

    verify/image.py   V-IMG                         (Pillow)
    verify/pdf.py     V-PDF-OPEN, V-PDFA, V-TEXT    (pypdf, veraPDF batches)
    verify/media.py   V-AV-PROBE, V-AV-DUR, V-AV-DECODE (FFprobe / FFmpeg)

Every check returns a model.CheckResult; the runner applies the §8 outcome rules.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable

from ..model import CheckResult, CheckState
from ..paths import long_path

CHUNK = 1 << 20  # 1 MiB


def sha256_file(path: str, *, on_chunk: Callable[[int], None] | None = None) -> tuple[str, int]:
    """Stream a file and return (hex SHA-256, size). Read-only."""
    h = hashlib.sha256()
    size = 0
    with open(long_path(path), "rb") as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            h.update(b)
            size += len(b)
            if on_chunk:
                on_chunk(len(b))
    return h.hexdigest(), size


def copy_with_hash(src: str, dst: str) -> tuple[str, int]:
    """Copy src -> dst, computing SHA-256 of the bytes read in the same pass (§5.4 Stage).

    The destination is created exclusively (never overwrites) and fsync'ed.
    """
    h = hashlib.sha256()
    size = 0
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(long_path(dst), flags, 0o644)
    try:
        with open(long_path(src), "rb") as fin, os.fdopen(fd, "wb", closefd=True) as fout:
            fd = -1
            while True:
                b = fin.read(CHUNK)
                if not b:
                    break
                h.update(b)
                fout.write(b)
                size += len(b)
            fout.flush()
            os.fsync(fout.fileno())
    finally:
        if fd != -1:
            os.close(fd)
    return h.hexdigest(), size


def v_hash(part_path: str, expected_sha256: str) -> CheckResult:
    """V-HASH: SHA-256 read back from the .part file on the output volume (§5.4, §8)."""
    try:
        got, _ = sha256_file(part_path)
    except OSError as e:
        return CheckResult("V-HASH", CheckState.FAIL, message=f"Couldn't read back the output: {e}")
    if got != expected_sha256:
        return CheckResult("V-HASH", CheckState.FAIL, message="Read-back SHA-256 differs from the verified result.")
    return CheckResult("V-HASH", CheckState.PASS)
