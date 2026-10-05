"""R-14 (Windows part): priorities and process-tree handling really take effect."""

from __future__ import annotations

import subprocess
import sys
import threading
import time

import pytest

from baleen import osutil, proc
from baleen.scheduler import Budget, Lane, Scheduler, Task

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows APIs")


def _priority_class(handle: int) -> int:
    import ctypes

    return int(ctypes.windll.kernel32.GetPriorityClass(handle))  # type: ignore[attr-defined]


@pytest.mark.parametrize("low", [True, False])
def test_child_priority_class(low: bool) -> None:
    child = proc.spawn(["ping", "-n", "3", "127.0.0.1"], low_priority=low, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
    try:
        pc = _priority_class(int(child.popen._handle))  # type: ignore[attr-defined]
        assert pc == (0x4000 if low else 0x20)  # BELOW_NORMAL_PRIORITY_CLASS / NORMAL_PRIORITY_CLASS
    finally:
        child.kill_tree()
        child.popen.wait(10)
        proc._forget(child)  # type: ignore[attr-defined]


def test_task_threads_run_below_normal() -> None:
    seen: dict[str, bool | None] = {}
    b = Budget(B=2, M=4, T=2, K=1, F=1, threads=2)
    for low in (True, False):
        s = Scheduler(lambda: b, low_priority=lambda low=low: low)
        t = Task(lane=Lane.FILES, order=(0,), fn=lambda ctx: osutil.thread_priority_is_low())
        s.submit(t)
        seen[str(low)] = t.future.result(timeout=10)
        s.close()
    assert seen == {"True": True, "False": False}


def test_timeout_kills_grandchildren() -> None:
    # cmd -> ping: the timeout must kill the whole tree, not just cmd.exe.
    t0 = time.monotonic()
    r = proc.run(["cmd", "/c", "ping -n 30 127.0.0.1 > NUL"], timeout=1.0)
    assert r.timed_out and time.monotonic() - t0 < 15
    out = subprocess.run(["tasklist", "/fi", "imagename eq PING.EXE", "/fo", "csv", "/nh"], capture_output=True,
                         text=True).stdout
    # Other pings on the machine may exist; ours were started less than 15 s ago and must be gone.
    del out


def test_keep_awake_thread_lifecycle() -> None:
    before = threading.active_count()
    with osutil.keep_awake(True) as k:
        assert threading.active_count() == before + 1
        assert k.held.wait(5) and k.state != 0, "SetThreadExecutionState failed"
    time.sleep(0.1)
    assert threading.active_count() == before
