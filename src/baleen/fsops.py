"""File operations that carry the safety principles: no overwrite (P2), atomic placement (P5),
and the output lock (§5.1)."""

from __future__ import annotations

import ctypes
import errno
import json
import os
import socket
import sys
from dataclasses import dataclass
from datetime import UTC, datetime

from .paths import long_path

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"


_UNSUPPORTED = (errno.ENOTSUP, errno.EINVAL, errno.ENOSYS, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP))


def _native_excl_rename(s: str, d: str) -> bool:
    """Exclusive rename via renamex_np (macOS) / renameat2 (Linux). False if unsupported here."""
    try:
        if IS_MAC:
            libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
            rc = libc.renamex_np(s.encode(), d.encode(), 0x00000004)  # RENAME_EXCL
        else:
            libc = ctypes.CDLL(None, use_errno=True)
            rc = libc.renameat2(-100, s.encode(), -100, d.encode(), 1)  # AT_FDCWD, RENAME_NOREPLACE
    except (OSError, AttributeError):
        return False
    if rc == 0:
        return True
    err = ctypes.get_errno()
    if err == errno.EEXIST:
        raise FileExistsError(errno.EEXIST, "File exists", d)
    if err in _UNSUPPORTED:
        return False
    raise OSError(err, os.strerror(err), d)


def rename_noreplace(src: str, dst: str) -> None:
    """Atomically rename src -> dst on the same volume; FileExistsError if dst exists (P2, P5)."""
    s, d = long_path(src), long_path(dst)
    if IS_WINDOWS:
        os.rename(s, d)  # MoveFileExW without MOVEFILE_REPLACE_EXISTING: fails if d exists
        return
    if _native_excl_rename(s, d):
        return
    # Filesystems without an exclusive rename: hard link + unlink (link fails if dst exists),
    # then a final check-then-rename for filesystems without hard links.
    try:
        os.link(s, d)
        os.unlink(s)
        return
    except FileExistsError:
        raise
    except OSError:
        pass
    if os.path.lexists(d):
        raise FileExistsError(errno.EEXIST, "File exists", dst)
    os.rename(s, d)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if IS_WINDOWS:
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class LockHeld(Exception):
    pass


@dataclass
class OutputLock:
    """<output>/_baleen/.lock with PID + hostname + start time (§5.1)."""

    path: str
    warning: str = ""

    def acquire(self) -> None:
        os.makedirs(long_path(os.path.dirname(self.path)), exist_ok=True)
        info = {"pid": os.getpid(), "hostname": socket.gethostname(),
                "started_at": datetime.now(UTC).isoformat(timespec="seconds")}
        for _attempt in range(2):
            try:
                fd = os.open(long_path(self.path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError:
                other = self._read()
                if other and other.get("hostname") == socket.gethostname() and not pid_alive(int(other.get("pid", 0))):
                    self.warning = (f"Replaced a stale lock left by process {other.get('pid')} "
                                    f"(started {other.get('started_at', '?')}).")
                    try:
                        os.unlink(long_path(self.path))
                    except FileNotFoundError:
                        pass
                    continue
                who = f"process {other.get('pid')} on {other.get('hostname')}" if other else "another process"
                raise LockHeld(
                    f"Another Baleen ({who}) is writing to this output folder. If that is not true, "
                    f"delete {self.path} and try again."
                ) from None
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(info, f)
            return
        raise LockHeld(f"Couldn't create the lock file {self.path}.")

    def _read(self) -> dict[str, object] | None:
        try:
            with open(long_path(self.path), encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def held_by_live_process(self) -> bool:
        """True if the lock file exists and its owner may still be running (or can't be checked)."""
        other = self._read()
        if other is None:
            return os.path.exists(long_path(self.path))
        if other.get("hostname") != socket.gethostname():
            return True  # another machine: liveness cannot be checked
        return pid_alive(int(other.get("pid", 0) or 0))

    def release(self) -> None:
        try:
            os.unlink(long_path(self.path))
        except FileNotFoundError:
            pass
