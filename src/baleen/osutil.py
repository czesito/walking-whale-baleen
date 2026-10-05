"""OS integration: worker-thread priority and keep-awake (spec §5.5), reveal in file manager.

- Lower priority: Windows THREAD_PRIORITY_BELOW_NORMAL; macOS QoS utility via
  pthread_set_qos_class_self_np. Child processes get BELOW_NORMAL_PRIORITY_CLASS / nice +10
  in proc.py. Applied per task (each task runs on a fresh thread).
- Keep awake: Windows SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED) from a
  thread that lives for the whole job; macOS `caffeinate -i -w <pid>`.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import threading
from collections.abc import Iterator

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

_k32 = None


def _kernel32():  # noqa: ANN202
    """kernel32 with explicit prototypes (handles are 64-bit; flags are unsigned 32-bit)."""
    global _k32
    if _k32 is None:
        import ctypes
        from ctypes import wintypes

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.GetCurrentThread.restype = wintypes.HANDLE
        k.GetCurrentThread.argtypes = []
        k.SetThreadPriority.restype = wintypes.BOOL
        k.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
        k.GetThreadPriority.restype = ctypes.c_int
        k.GetThreadPriority.argtypes = [wintypes.HANDLE]
        k.SetThreadExecutionState.restype = wintypes.DWORD
        k.SetThreadExecutionState.argtypes = [wintypes.DWORD]
        _k32 = k
    return _k32


def set_thread_low_priority(low: bool) -> None:
    """Set the calling thread's priority. Never raises."""
    try:
        if IS_WINDOWS:
            k32 = _kernel32()
            THREAD_PRIORITY_BELOW_NORMAL, THREAD_PRIORITY_NORMAL = -1, 0
            k32.SetThreadPriority(
                k32.GetCurrentThread(), THREAD_PRIORITY_BELOW_NORMAL if low else THREAD_PRIORITY_NORMAL
            )
        elif IS_MAC and low:
            import ctypes

            libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
            QOS_CLASS_UTILITY = 0x11
            libc.pthread_set_qos_class_self_np(QOS_CLASS_UTILITY, 0)
        elif low and hasattr(os, "setpriority"):
            # Linux: per-thread nice value (cannot be raised again without privileges, which is
            # fine because every task runs on a fresh thread).
            tid = threading.get_native_id()
            cur = os.getpriority(os.PRIO_PROCESS, tid)
            if cur < 10:
                os.setpriority(os.PRIO_PROCESS, tid, 10)
    except Exception:
        pass


def thread_priority_is_low() -> bool | None:
    """Introspection for tests (Windows only)."""
    if not IS_WINDOWS:
        return None
    k32 = _kernel32()
    return int(k32.GetThreadPriority(k32.GetCurrentThread())) < 0


class KeepAwake:
    """Prevents idle sleep while a job runs. Use as a context manager on the job thread."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self._proc: subprocess.Popen[bytes] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.held = threading.Event()
        self.state = 0  # previous execution state returned by Windows (non-zero = call succeeded)

    def __enter__(self) -> KeepAwake:
        if not self.enabled:
            return self
        try:
            if IS_WINDOWS:
                # ES_CONTINUOUS is per-thread; hold it on a dedicated thread for the job's life.
                self._thread = threading.Thread(target=self._win_hold, daemon=True, name="baleen-awake")
                self._thread.start()
            elif IS_MAC and os.path.exists("/usr/bin/caffeinate"):
                self._proc = subprocess.Popen(  # noqa: S603
                    ["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
        except Exception:
            pass
        return self

    def _win_hold(self) -> None:
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        k32 = _kernel32()
        self.state = k32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        self.held.set()
        try:
            self._stop.wait()
        finally:
            k32.SetThreadExecutionState(ES_CONTINUOUS)

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self._proc is not None:
            with contextlib.suppress(Exception):
                self._proc.terminate()
                self._proc.wait(timeout=5)


@contextlib.contextmanager
def keep_awake(enabled: bool) -> Iterator[KeepAwake]:
    with KeepAwake(enabled) as k:
        yield k


def reveal(path: str, *, select: bool = True) -> bool:
    """Open a folder, or reveal a file, in Finder / Explorer. Caller enforces SEC-7."""
    try:
        if IS_WINDOWS:
            if select and os.path.isfile(path):
                subprocess.Popen(["explorer.exe", "/select,", path])  # noqa: S603,S607
            else:
                os.startfile(path)  # type: ignore[attr-defined]  # noqa: S606
        elif IS_MAC:
            args = ["/usr/bin/open", "-R", path] if select and os.path.isfile(path) else ["/usr/bin/open", path]
            subprocess.Popen(args)  # noqa: S603
        else:
            target = os.path.dirname(path) if os.path.isfile(path) else path
            subprocess.Popen(["xdg-open", target])  # noqa: S603,S607
        return True
    except Exception:
        return False
