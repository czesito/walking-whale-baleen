"""Scan (§5.2): recursive, read-only walk with a filesystem-independent order.

- Entries are sorted by NFC-normalised, case-folded name.
- Symlinks and junctions are never followed; they are listed as IGNORED SYMLINK.
- System files and folders are listed as IGNORED SYSTEM_FILE; folders are not entered.
- Other dotfiles are processed normally (DR-20).
"""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Callable, Iterator

from .model import ScanEntry
from .paths import fold_key, long_path

IS_WINDOWS = sys.platform == "win32"

SYSTEM_FILE_NAMES = frozenset({".ds_store", "icon\r", "thumbs.db", "ehthumbs.db", "desktop.ini"})
SYSTEM_DIR_NAMES = frozenset({
    "$recycle.bin", ".trashes", ".spotlight-v100", ".fseventsd", "@eadir", "#recycle",
    ".@__thumb", "_baleen", ".baleen-staging",
    # Proposed DR-55: Windows' per-volume system folder (always access-denied at a drive root).
    "system volume information",
})


def is_system_file(name: str) -> bool:
    low = name.lower()
    if low in SYSTEM_FILE_NAMES:
        return True
    if name.startswith("._") or name.startswith("~$"):
        return True
    return bool(name.startswith(".~lock.") and name.endswith("#"))


def is_system_dir(name: str) -> bool:
    return name.lower() in SYSTEM_DIR_NAMES


def _is_link(entry: os.DirEntry[str]) -> bool:
    try:
        if entry.is_symlink():
            return True
        if IS_WINDOWS:
            # Junctions and other reparse points are not followed either.
            st = entry.stat(follow_symlinks=False)
            attrs = getattr(st, "st_file_attributes", 0)
            if attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                return True
            is_junction = getattr(entry, "is_junction", None)
            if is_junction is not None and is_junction():
                return True
    except OSError:
        return False
    return False


class ScanError(Exception):
    pass


def scan(root: str, *, on_progress: Callable[[int], None] | None = None,
         should_stop: Callable[[], bool] | None = None) -> list[ScanEntry]:
    """Walk `root` and return entries in plan order (depth-first, sorted per directory).

    Directory order: within each directory, all entries (files and folders) are sorted
    together by fold_key(name); a folder's contents follow the folder's position.
    """
    out: list[ScanEntry] = []
    root_l = long_path(root)
    if not os.path.isdir(root_l):
        raise ScanError(f"Source folder not found: {root}")
    count = 0

    def walk(abs_dir: str, rel_dir: str) -> Iterator[ScanEntry]:
        nonlocal count
        try:
            with os.scandir(abs_dir) as it:
                entries = list(it)
        except OSError as e:
            if not rel_dir:
                raise ScanError(f"Couldn't read the folder: {e.strerror or e}") from e
            # One unreadable subfolder never stops the run (P6): it becomes a FAILED row (P9).
            yield ScanEntry(rel_dir, None, None, is_dir=True, ignore=None,
                            error=f"Couldn't read this folder: {e.strerror or e}")
            return
        entries.sort(key=lambda e: (fold_key(e.name), e.name))
        for e in entries:
            if should_stop and should_stop():
                return
            rel = f"{rel_dir}/{e.name}" if rel_dir else e.name
            if _is_link(e):
                yield ScanEntry(rel, None, None, is_dir=False, ignore="SYMLINK")
                continue
            try:
                is_dir = e.is_dir(follow_symlinks=False)
            except OSError:
                is_dir = False
            if is_dir:
                if is_system_dir(e.name):
                    yield ScanEntry(rel, None, None, is_dir=True, ignore="SYSTEM_FILE")
                    continue
                yield from walk(os.path.join(abs_dir, e.name), rel)
                continue
            try:
                st = e.stat(follow_symlinks=False)
                size, mtime_ns = st.st_size, st.st_mtime_ns
            except OSError:
                size, mtime_ns = None, None
            count += 1
            if on_progress and count % 200 == 0:
                on_progress(count)
            yield ScanEntry(rel, size, mtime_ns, ignore="SYSTEM_FILE" if is_system_file(e.name) else None)

    out.extend(walk(root_l, ""))
    if on_progress:
        on_progress(count)
    return out
