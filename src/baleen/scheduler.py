"""Parallel lanes within user-set budgets (spec §5.5, DR-32).

One job runs at a time; its tasks run in four lanes (Files, Documents, Media, PDF/A). A
dispatcher thread starts the next task, in plan order, whenever its lane has room and the
task's needs fit inside three budgets:

    B  processor tokens     (processor_use)
    M  reserved memory, GB  (memory_limit)
    T  transfer slots       (transfer_slots; held only while reading source / writing output)

Budgets are re-read at every dispatch, so a changed setting applies live. Running tasks are
never interrupted; if usage is above a lowered limit, nothing new starts until it drops.
"""

from __future__ import annotations

import contextlib
import enum
import itertools
import math
import os
import sys
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from dataclasses import asdict, dataclass, field
from typing import Any

from . import osutil

GB_MB = 1024


class Lane(enum.StrEnum):
    FILES = "files"
    DOCUMENTS = "documents"
    MEDIA = "media"
    PDFA = "pdfa"


LANE_ORDER: tuple[Lane, ...] = (Lane.FILES, Lane.DOCUMENTS, Lane.MEDIA, Lane.PDFA)


# --------------------------------------------------------------------------- machine & budget


def _physical_ram_bytes() -> int:
    if sys.platform == "win32":
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):  # type: ignore[attr-defined]
            return int(st.ullTotalPhys)
        return 8 << 30
    if sys.platform == "darwin":
        import subprocess

        try:
            out = subprocess.run(["/usr/sbin/sysctl", "-n", "hw.memsize"], capture_output=True,
                                 text=True, timeout=5).stdout
            return int(out.strip())
        except Exception:
            return 8 << 30
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        return 8 << 30


@dataclass(frozen=True)
class Machine:
    cores: int  # logical processors reported by the OS
    ram_gb: int  # physical memory, rounded to whole GB
    network: bool  # source or output on a network volume

    @classmethod
    def detect(cls, paths: list[str] | tuple[str, ...] = ()) -> Machine:
        from .paths import is_network_path

        cores = os.cpu_count() or 1
        ram_gb = max(1, math.floor(_physical_ram_bytes() / (1 << 30) + 0.5))
        network = any(is_network_path(p) for p in paths if p)
        return cls(cores, ram_gb, network)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Budget:
    B: int  # processor tokens
    M: int  # memory, GB
    T: int  # transfer slots
    K: int  # LibreOffice instances
    F: int  # FFmpeg encodes at once
    threads: int  # tokens per FFmpeg task (-threads)

    @property
    def files(self) -> int:
        return self.B

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


def _round_half_up(x: float) -> int:
    return math.floor(x + 0.5)


def resolve_budget(prefs: dict[str, Any], machine: Machine) -> Budget:
    """Spec §5.5 budget rules. Mirrors the design prototype's resolve() exactly (test oracle)."""
    c, ram = machine.cores, machine.ram_gb
    pu = prefs.get("processor_use", "balanced")
    if pu == "gentle":
        b = max(1, c // 4)
    elif pu == "maximum":
        b = max(1, c - 1)
    elif pu == "custom":
        b = min(c, max(1, int(prefs.get("processor_cores", 1))))
    else:
        b = max(2, c // 2)
    b = min(b, c)
    if prefs.get("memory_limit", "auto") == "custom":
        m = min(ram - 2, max(1, int(prefs.get("memory_gb", 1))))
        m = max(1, m)
    else:
        m = max(1, min(_round_half_up(ram * 0.25), ram - 4))
    if prefs.get("transfer_slots", "auto") == "custom":
        t = min(16, max(1, int(prefs.get("transfer_count", 1))))
    else:
        t = 4 if machine.network else 8
    k = min(4, max(1, b // 2), max(1, m // 1))
    f = 2 if b >= 8 else 1
    th = max(1, b // f)
    return Budget(b, m, t, k, f, th)


def lane_capacity(lane: Lane, b: Budget) -> int:
    return {Lane.FILES: b.B, Lane.DOCUMENTS: b.K, Lane.MEDIA: b.F, Lane.PDFA: 1}[lane]


def lane_tokens(lane: Lane, b: Budget) -> int:
    return b.threads if lane == Lane.MEDIA else 1


def lane_memory_mb(lane: Lane) -> int:
    return {Lane.FILES: 0, Lane.DOCUMENTS: GB_MB, Lane.MEDIA: 512, Lane.PDFA: GB_MB}[lane]


def image_reservation_mb(width: int, height: int, bands: int, bytes_per_band: int = 1) -> int:
    """Files-lane memory for big images: images over 32 MP reserve 2 x their decoded size."""
    if width * height <= 32_000_000:
        return 0
    return math.ceil(2 * width * height * bands * bytes_per_band / (1 << 20))


def describe_budget(b: Budget, machine: Machine) -> dict[str, Any]:
    """Plain-language texts for UI-S2 'With these settings' and UI-R2 (no 'tokens'/'threads')."""
    def plural(n: int, one: str, many: str) -> str:
        return f"{n:,} {one if n == 1 else many}"

    lines = [
        ("files", plural(b.B, "file task", "file tasks") + " at once"),
        ("document", plural(b.K, "document converter", "document converters")),
        ("video", f"{plural(b.F, 'video encoder', 'video encoders')} using "
                  f"{plural(b.threads, 'core', 'cores')} each" if b.F > 1 else
                  f"1 video encoder using {plural(b.threads, 'core', 'cores')}"),
        ("pdf", "PDF/A checks in batches"),
        ("transfer", plural(b.T, "file transfer", "file transfers")),
        ("memory", f"up to {b.M} GB memory"),
    ]
    return {
        "lines": lines,
        "summary": " · ".join(t for _, t in lines),
        "using": f"Using {b.B} of {machine.cores} cores · up to {b.M} GB memory · "
                 f"{plural(b.T, 'transfer', 'transfers')}",
        "cores_label": f"Uses {b.B} of {machine.cores} cores",
    }


# --------------------------------------------------------------------------- tasks


@dataclass(frozen=True)
class BatchPolicy:
    max_size: int
    wait_s: float  # how long the head task may wait for companions (0 = take what is queued)


DEFAULT_BATCH: dict[Lane, BatchPolicy] = {
    Lane.DOCUMENTS: BatchPolicy(8, 0.0),  # DR-35: up to 8 files per soffice call
    Lane.PDFA: BatchPolicy(25, 10.0),  # DR-35: up to 25 files per JVM, close after 10 s
}

_seq = itertools.count()


@dataclass(eq=False)
class Task:
    """A unit of work. Unbatched tasks carry `fn(ctx)`; batched ones carry `batch_fn(ctx, payloads)`.

    order: plan order key (item n, step); lower starts first within a lane.
    """

    lane: Lane
    order: tuple[int, ...]
    fn: Callable[[TaskContext], Any] | None = None
    batch_fn: Callable[[TaskContext, list[Any]], list[Any]] | None = None
    payload: Any = None
    batch_key: str | None = None
    tokens: int | None = None  # None: lane default
    memory_mb: int | None = None  # None: lane default
    label: str = ""  # file name for "In progress"
    action: str = ""  # e.g. "Converting", "Checking"
    category: str = ""
    item: int | None = None
    kind: str = ""  # stage | process | pdfa | publish | hash | probe ...
    future: Future[Any] = field(default_factory=Future)
    enqueued: float = field(default_factory=time.monotonic)
    seq: int = field(default_factory=lambda: next(_seq))

    def sort_key(self) -> tuple[Any, ...]:
        return (self.order, self.seq)


@dataclass
class TaskContext:
    """Handed to a running task: what it was granted, and access to transfers."""

    scheduler: Scheduler
    lane: Lane
    tokens: int
    memory_mb: int
    low_priority: bool
    tasks: list[Task]

    @contextlib.contextmanager
    def transfer(self) -> Iterator[None]:
        """Hold one transfer slot while reading the source or writing the output (§5.5)."""
        with self.scheduler.transfer():
            yield


@dataclass
class RunningTask:
    id: int
    lane: Lane
    labels: list[str]
    action: str
    category: str
    items: list[int | None]
    tokens: int
    memory_mb: int
    started: float


@dataclass
class Snapshot:
    budget: Budget
    tokens_in_use: int
    memory_in_use_mb: int
    transfers_in_use: int
    running: list[RunningTask]
    queued: dict[str, int]
    peak_tokens: int
    peak_memory_mb: int
    peak_transfers: int


class Scheduler:
    """Dispatcher with lanes and budgets. Thread-safe."""

    def __init__(
        self,
        budget: Callable[[], Budget],
        *,
        low_priority: Callable[[], bool] = lambda: True,
        batch: dict[Lane, BatchPolicy] | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self._budget_fn = budget
        self._low_priority_fn = low_priority
        self._batch = dict(DEFAULT_BATCH if batch is None else batch)
        self._on_event = on_event
        self._cond = threading.Condition()
        self._queues: dict[Lane, list[Task]] = {lane: [] for lane in LANE_ORDER}
        self._running: dict[int, RunningTask] = {}
        self._running_per_lane: dict[Lane, int] = {lane: 0 for lane in LANE_ORDER}
        self._tokens = 0
        self._mem = 0
        self._transfers = 0
        self._peak_tokens = 0
        self._peak_mem = 0
        self._peak_transfers = 0
        self._ids = itertools.count(1)
        self._closed = False
        self._paused = False
        self._kick = False
        self._thread = threading.Thread(target=self._loop, name="baleen-dispatch", daemon=True)
        self._thread.start()

    # ---------------------------------------------------------------- public API

    def budget(self) -> Budget:
        return self._budget_fn()

    def submit(self, task: Task) -> Future[Any]:
        with self._cond:
            if self._closed:
                raise RuntimeError("scheduler is closed")
            if task.batch_fn is not None and task.lane not in self._batch:
                raise ValueError(f"lane {task.lane} has no batch policy")
            q = self._queues[task.lane]
            q.append(task)
            q.sort(key=Task.sort_key)
            self._cond.notify_all()
        return task.future

    def wake(self) -> None:
        """Re-evaluate budgets now (call after a live settings change)."""
        with self._cond:
            self._kick = True
            self._cond.notify_all()

    def cancel_queued(self, predicate: Callable[[Task], bool] = lambda t: True) -> list[Task]:
        """Remove queued (not running) tasks matching predicate; their futures are cancelled."""
        removed: list[Task] = []
        with self._cond:
            for lane in LANE_ORDER:
                keep: list[Task] = []
                for t in self._queues[lane]:
                    (removed if predicate(t) else keep).append(t)
                self._queues[lane] = keep
            self._cond.notify_all()
        for t in removed:
            t.future.cancel()
        return removed

    def idle(self) -> bool:
        with self._cond:
            return not self._running and not any(self._queues.values())

    def wait_idle(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while self._running or any(self._queues.values()):
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._cond.wait(timeout=0.5 if remaining is None else min(0.5, remaining))
        return True

    def close(self, wait: bool = True) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()
        if wait:
            self._thread.join(timeout=5)

    def snapshot(self) -> Snapshot:
        with self._cond:
            return Snapshot(
                budget=self._budget_fn(),
                tokens_in_use=self._tokens,
                memory_in_use_mb=self._mem,
                transfers_in_use=self._transfers,
                running=[RunningTask(**asdict(r)) for r in self._running.values()],
                queued={lane.value: len(q) for lane, q in self._queues.items()},
                peak_tokens=self._peak_tokens,
                peak_memory_mb=self._peak_mem,
                peak_transfers=self._peak_transfers,
            )

    @contextlib.contextmanager
    def transfer(self) -> Iterator[None]:
        with self._cond:
            while self._transfers >= max(1, self._budget_fn().T):
                self._cond.wait(timeout=0.5)
            self._transfers += 1
            self._peak_transfers = max(self._peak_transfers, self._transfers)
            self._emit("transfer", {"in_use": self._transfers})
        try:
            yield
        finally:
            with self._cond:
                self._transfers -= 1
                self._cond.notify_all()

    # ---------------------------------------------------------------- dispatcher

    def _emit(self, kind: str, data: dict[str, Any]) -> None:
        if self._on_event:
            try:
                self._on_event(kind, data)
            except Exception:
                pass

    def _others_idle(self, lane: Lane) -> bool:
        """True when nothing outside `lane`'s queue can still produce work for it."""
        if self._running:
            return False
        return all(not q for ln, q in self._queues.items() if ln != lane)

    def _pick(self, lane: Lane, now: float) -> tuple[list[Task], float | None]:
        """Tasks to start together from `lane` (a batch or a single task), or [] with a deadline."""
        q = self._queues[lane]
        head = q[0]
        if head.batch_fn is None:
            return [head], None
        pol = self._batch[lane]
        group = [t for t in q if t.batch_key == head.batch_key and t.batch_fn is head.batch_fn]
        group = group[: pol.max_size]
        if len(group) < pol.max_size and pol.wait_s > 0:
            waited = now - head.enqueued
            if waited < pol.wait_s and not self._others_idle(lane):
                return [], head.enqueued + pol.wait_s
        return group, None

    def _try_dispatch(self) -> float | None:
        """Start everything that fits. Returns the earliest batch deadline, if any."""
        next_deadline: float | None = None
        while True:
            b = self._budget_fn()
            now = time.monotonic()
            heads = [
                (lane, self._queues[lane][0])
                for lane in LANE_ORDER
                if self._queues[lane] and self._running_per_lane[lane] < lane_capacity(lane, b)
            ]
            heads.sort(key=lambda lt: lt[1].sort_key())
            started = False
            for lane, _head in heads:
                group, deadline = self._pick(lane, now)
                if not group:
                    if deadline is not None:
                        next_deadline = deadline if next_deadline is None else min(next_deadline, deadline)
                    continue
                head = group[0]
                tokens = head.tokens if head.tokens is not None else lane_tokens(lane, b)
                mem = head.memory_mb if head.memory_mb is not None else lane_memory_mb(lane)
                fits = self._tokens + tokens <= b.B and self._mem + mem <= b.M * GB_MB
                oversize = tokens > b.B or mem > b.M * GB_MB
                nothing_running = not self._running
                if fits or (oversize and nothing_running):
                    self._start(lane, group, tokens, mem)
                    started = True
                    break  # budgets changed; re-evaluate from the top in plan order
            if not started:
                return next_deadline

    def _start(self, lane: Lane, group: list[Task], tokens: int, mem: int) -> None:
        q = self._queues[lane]
        for t in group:
            q.remove(t)
        rid = next(self._ids)
        head = group[0]
        self._running[rid] = RunningTask(
            rid, lane, [t.label for t in group], head.action, head.category,
            [t.item for t in group], tokens, mem, time.time(),
        )
        self._running_per_lane[lane] += 1
        self._tokens += tokens
        self._mem += mem
        self._peak_tokens = max(self._peak_tokens, self._tokens)
        self._peak_mem = max(self._peak_mem, self._mem)
        low = bool(self._low_priority_fn())
        self._emit("start", {"lane": lane.value, "tokens": tokens, "memory_mb": mem,
                             "tokens_in_use": self._tokens, "memory_in_use_mb": self._mem,
                             "items": [t.item for t in group]})
        ctx = TaskContext(self, lane, tokens, mem, low, group)
        th = threading.Thread(target=self._run, args=(rid, lane, group, ctx), daemon=True,
                              name=f"baleen-{lane.value}-{rid}")
        th.start()

    def _run(self, rid: int, lane: Lane, group: list[Task], ctx: TaskContext) -> None:
        osutil.set_thread_low_priority(ctx.low_priority)
        try:
            for t in group:
                t.future.set_running_or_notify_cancel()
            head = group[0]
            if head.batch_fn is not None:
                try:
                    results = head.batch_fn(ctx, [t.payload for t in group])
                    if len(results) != len(group):
                        raise RuntimeError("batch handler returned a wrong number of results")
                    pairs = list(zip(group, results, strict=True))
                    for t, r in pairs:
                        if isinstance(r, BaseException):
                            t.future.set_exception(r)
                        else:
                            t.future.set_result(r)
                except BaseException as e:  # noqa: BLE001 - delivered to every waiting future
                    for t in group:
                        if not t.future.done():
                            t.future.set_exception(e)
            else:
                try:
                    head.future.set_result(head.fn(ctx))  # type: ignore[misc]
                except BaseException as e:  # noqa: BLE001
                    head.future.set_exception(e)
        finally:
            with self._cond:
                r = self._running.pop(rid)
                self._running_per_lane[lane] -= 1
                self._tokens -= r.tokens
                self._mem -= r.memory_mb
                self._emit("finish", {"lane": lane.value, "tokens_in_use": self._tokens,
                                      "memory_in_use_mb": self._mem})
                self._cond.notify_all()

    def _loop(self) -> None:
        with self._cond:
            while True:
                if self._closed and not self._running and not any(self._queues.values()):
                    return
                deadline = self._try_dispatch()
                timeout = 0.5
                if deadline is not None:
                    timeout = max(0.01, min(timeout, deadline - time.monotonic()))
                self._kick = False
                self._cond.wait(timeout=timeout)
