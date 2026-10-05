"""Subprocess runner (SEC-8): argument lists only, explicit paths, sanitised environment,
whole-tree kill on timeout, below-normal priority (§5.5 low_priority).

Windows: each child starts suspended, is put in its own Job Object
(KILL_ON_JOB_CLOSE), then resumed - so grandchildren (soffice.exe -> soffice.bin) are in the
job too, a timeout kills the whole tree, and if Baleen itself dies the jobs close and every
child dies with it.
POSIX: each child gets its own session; the process group is killed.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

IS_WINDOWS = sys.platform == "win32"

_live_lock = threading.Lock()
_live: set[_Child] = set()


@dataclass
class ProcResult:
    args: list[str]
    returncode: int | None
    stdout: bytes = b""
    stderr: bytes = b""
    timed_out: bool = False
    killed: bool = False
    duration: float = 0.0
    error: str = ""  # spawn failure (tool missing, permission)

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.killed and not self.error

    def out(self) -> str:
        return self.stdout.decode("utf-8", "replace")

    def err(self) -> str:
        return self.stderr.decode("utf-8", "replace")


@dataclass(eq=False)
class _Child:
    popen: subprocess.Popen[bytes]
    job: int | None = None
    killed: bool = field(default=False)

    def kill_tree(self) -> None:
        self.killed = True
        if IS_WINDOWS:
            if self.job:
                _win.TerminateJobObject(self.job, 1)
            else:
                try:
                    self.popen.kill()
                except OSError:
                    pass
        else:
            try:
                os.killpg(self.popen.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    def close(self) -> None:
        if IS_WINDOWS and self.job:
            _win.CloseHandle(self.job)
            self.job = None


# --------------------------------------------------------------------------- Windows plumbing

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    class _Win:
        CREATE_SUSPENDED = 0x00000004
        CREATE_NO_WINDOW = 0x08000000
        BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
        JobObjectExtendedLimitInformation = 9
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        def __init__(self) -> None:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self.k32 = k32
            self.ntdll = ctypes.WinDLL("ntdll")
            k32.CreateJobObjectW.restype = wintypes.HANDLE
            k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            self.ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]

            class EXT(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", _Win.JOBOBJECT_BASIC_LIMIT_INFORMATION),
                    ("IoInfo", _Win.IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            self.EXT = EXT

        def new_job(self) -> int | None:
            job = self.k32.CreateJobObjectW(None, None)
            if not job:
                return None
            info = self.EXT()
            info.BasicLimitInformation.LimitFlags = self.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            self.k32.SetInformationJobObject(
                job, self.JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)
            )
            return int(job)

        def assign(self, job: int, process_handle: int) -> bool:
            return bool(self.k32.AssignProcessToJobObject(job, process_handle))

        def resume(self, process_handle: int) -> None:
            self.ntdll.NtResumeProcess(process_handle)

        def TerminateJobObject(self, job: int, code: int) -> None:  # noqa: N802
            self.k32.TerminateJobObject(job, code)

        def CloseHandle(self, h: int) -> None:  # noqa: N802
            self.k32.CloseHandle(h)

    _win = _Win()


def _nice_prefix() -> list[str]:
    for p in ("/usr/bin/nice", "/bin/nice"):
        if os.path.exists(p):
            return [p, "-n", "10"]
    return []


def spawn(
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    low_priority: bool = False,
    stdin: int | None = subprocess.DEVNULL,
    stdout: int | None = subprocess.PIPE,
    stderr: int | None = subprocess.PIPE,
) -> _Child:
    """Start a child process in its own kill-able tree. Prefer run() unless streaming."""
    if not args or not isinstance(args, list):
        raise TypeError("args must be a non-empty list (SEC-8: no shell)")
    argv = [str(a) for a in args]
    if IS_WINDOWS:
        flags = _Win.CREATE_SUSPENDED | _Win.CREATE_NO_WINDOW
        if low_priority:
            flags |= _Win.BELOW_NORMAL_PRIORITY_CLASS
        p = subprocess.Popen(  # noqa: S603
            argv, env=env, cwd=cwd, stdin=stdin, stdout=stdout, stderr=stderr,
            creationflags=flags, shell=False,
        )
        job = _win.new_job()
        handle = int(p._handle)  # type: ignore[attr-defined]
        if job is not None and not _win.assign(job, handle):
            _win.CloseHandle(job)
            job = None
        _win.resume(handle)
        child = _Child(p, job)
    else:
        if low_priority:
            argv = _nice_prefix() + argv
        p = subprocess.Popen(  # noqa: S603
            argv, env=env, cwd=cwd, stdin=stdin, stdout=stdout, stderr=stderr,
            start_new_session=True, shell=False,
        )
        child = _Child(p)
    with _live_lock:
        _live.add(child)
    return child


def _forget(child: _Child) -> None:
    with _live_lock:
        _live.discard(child)
    child.close()


def run(
    args: list[str],
    *,
    timeout: float | None,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    low_priority: bool = False,
    input_bytes: bytes | None = None,
) -> ProcResult:
    """Run a tool to completion. Never raises for tool failures; inspect the result."""
    start = time.monotonic()
    try:
        child = spawn(
            args, env=env, cwd=cwd, low_priority=low_priority,
            stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
        )
    except OSError as e:
        return ProcResult([str(a) for a in args], None, error=f"{e.__class__.__name__}: {e}",
                          duration=time.monotonic() - start)
    timed_out = False
    try:
        out, err = child.popen.communicate(input=input_bytes, timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        child.kill_tree()
        try:
            out, err = child.popen.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            out, err = b"", b""
    finally:
        rc = child.popen.poll()
        if rc is None:
            # Leave nothing behind (e.g. KeyboardInterrupt while waiting).
            child.kill_tree()
            try:
                child.popen.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        _forget(child)
    return ProcResult(
        [str(a) for a in args], child.popen.returncode, out or b"", err or b"",
        timed_out=timed_out, killed=child.killed and not timed_out,
        duration=time.monotonic() - start,
    )


def kill_all() -> int:
    """Kill every live child tree (process exit, hard stop). Returns how many were killed."""
    with _live_lock:
        children = list(_live)
    for c in children:
        c.kill_tree()
    return len(children)
