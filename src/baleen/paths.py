"""Path helpers: extended-length paths, comparison keys, overlap, containment, volumes.

Spec §5.1 (overlap), §7.7 (extended-length paths, PATH_TOO_LONG), SEC-7 (containment),
§5.5 (network volumes decide the transfer budget).
"""

from __future__ import annotations

import ctypes
import os
import shutil
import sys
import unicodedata
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

MAX_COMPONENT_UNITS = 255  # UTF-16 code units per path component (§7.7)


def utf16_units(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def component_too_long(name: str) -> bool:
    return utf16_units(name) > MAX_COMPONENT_UNITS


def any_component_too_long(rel: str) -> bool:
    return any(component_too_long(c) for c in rel.split("/") if c)


def fold_key(s: str) -> str:
    """Comparison key: NFC-normalised and case-folded (§5.2 scan order, §7.3 clash key)."""
    return unicodedata.normalize("NFC", s).casefold()


def long_path(p: str | os.PathLike[str]) -> str:
    """Return an extended-length path on Windows (\\\\?\\ prefix); unchanged elsewhere.

    Every file operation on source, output and work paths goes through this, so deep
    archive trees work without LongPathsEnabled (§7.7).
    """
    s = os.fspath(p)
    if not IS_WINDOWS:
        return s
    if s.startswith("\\\\?\\") or s.startswith("\\\\.\\"):
        return s
    s = os.path.abspath(s)
    if s.startswith("\\\\"):
        return "\\\\?\\UNC\\" + s[2:]
    return "\\\\?\\" + s


def strip_long(p: str) -> str:
    """Inverse of long_path, for display and for tools that do not accept the prefix."""
    if p.startswith("\\\\?\\UNC\\"):
        return "\\\\" + p[8:]
    if p.startswith("\\\\?\\"):
        return p[4:]
    return p


def join_rel(root: str | os.PathLike[str], rel: str) -> str:
    """Join a '/'-separated relative path onto an OS root path."""
    parts = [c for c in rel.split("/") if c]
    return os.path.join(os.fspath(root), *parts) if parts else os.fspath(root)


def real(p: str | os.PathLike[str]) -> str:
    """Resolved real path (symlinks and junctions resolved), without the long-path prefix."""
    return strip_long(os.path.realpath(long_path(p)))


def _cmp_key(p: str) -> str:
    s = os.path.normpath(p)
    if IS_WINDOWS or IS_MAC:
        return fold_key(s)
    return unicodedata.normalize("NFC", s)


def is_within(child: str | os.PathLike[str], root: str | os.PathLike[str], *, resolve: bool = True) -> bool:
    """True if `child` equals `root` or lies inside it (real paths; case-insensitive on mac/Windows)."""
    c = real(child) if resolve else os.path.abspath(os.fspath(child))
    r = real(root) if resolve else os.path.abspath(os.fspath(root))
    ck, rk = _cmp_key(c), _cmp_key(r)
    if ck == rk:
        return True
    sep = os.sep
    rk_sep = rk if rk.endswith(sep) else rk + sep
    return ck.startswith(rk_sep)


def overlap(a: str | os.PathLike[str], b: str | os.PathLike[str]) -> str | None:
    """Overlap rule (§5.1). Returns None, or 'same' | 'a_in_b' | 'b_in_a'."""
    ra, rb = real(a), real(b)
    if _cmp_key(ra) == _cmp_key(rb):
        return "same"
    if is_within(ra, rb, resolve=False):
        return "a_in_b"
    if is_within(rb, ra, resolve=False):
        return "b_in_a"
    return None


def free_bytes(p: str | os.PathLike[str]) -> int:
    """Free space on the volume holding p (walks up to an existing parent)."""
    q = Path(os.fspath(p))
    while not q.exists() and q.parent != q:
        q = q.parent
    try:
        return shutil.disk_usage(long_path(q) if IS_WINDOWS else q).free
    except OSError:
        return 0


def is_network_path(p: str | os.PathLike[str]) -> bool:
    """Network volume detection for the transfer budget (§5.5) and UI-S2."""
    s = os.path.abspath(os.fspath(p))
    if IS_WINDOWS:
        if s.startswith("\\\\"):
            return True
        drive = os.path.splitdrive(s)[0]
        if not drive:
            return False
        try:
            DRIVE_REMOTE = 4
            return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == DRIVE_REMOTE  # type: ignore[attr-defined]
        except Exception:
            return False
    if IS_MAC:
        return _mac_fstype(s) in {"smbfs", "afpfs", "nfs", "webdav", "cifs"}
    return _linux_fstype(s) in {"cifs", "smb3", "smbfs", "nfs", "nfs4", "fuse.sshfs", "9p"}


def _mac_fstype(path: str) -> str:
    try:
        import subprocess

        # `mount` lines look like: "//user@nas/share on /Volumes/share (smbfs, nodev, ...)"
        mounts = subprocess.run(["/sbin/mount"], capture_output=True, text=True, timeout=5).stdout
        best, fstype = "", ""
        for line in mounts.splitlines():
            if " on " not in line or "(" not in line:
                continue
            mp = line.split(" on ", 1)[1].rsplit(" (", 1)[0]
            ft = line.rsplit("(", 1)[1].split(",")[0].strip()
            if (path == mp or path.startswith(mp.rstrip("/") + "/")) and len(mp) > len(best):
                best, fstype = mp, ft
        return fstype
    except Exception:
        return ""


def _linux_fstype(path: str) -> str:
    try:
        best, fstype = "", ""
        with open("/proc/mounts", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mp = parts[1].replace("\\040", " ")
                if (path == mp or path.startswith(mp.rstrip("/") + "/")) and len(mp) > len(best):
                    best, fstype = mp, parts[2]
        return fstype
    except OSError:
        return ""


def same_volume(a: str | os.PathLike[str], b: str | os.PathLike[str]) -> bool:
    try:
        return os.stat(long_path(a)).st_dev == os.stat(long_path(b)).st_dev
    except OSError:
        return False
