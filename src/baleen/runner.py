"""Job runner (spec §5): validate → scan → plan → stage → convert → verify → publish → report.

One job at a time per Baleen instance. Items flow through the lanes of §5.5:

    Stage    (Files)      re-stat, work-space check, copy to the work folder while hashing;
                          or, if the final path exists, the §7.5 resume lookup
    Process  (route lane) convert or check; Documents-lane routes run in soffice batches
    PDF/A    (PDF/A)      veraPDF batches, when the route asked for V-PDFA
    Publish  (Files)      <output>/.baleen-staging/<run-id>/<n>.part, V-HASH read-back,
                          exclusive atomic rename, mtime = source mtime
    Report                commit to the journal; delete the item's work and staging files

Check-only items skip Stage and Publish: the source is verified in place and hashed by
streaming. Nothing is ever written under the source root (P1).
"""

from __future__ import annotations

import contextlib
import enum
import logging
import os
import shutil
import socket
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from . import __version__, fsops, osutil
from .convert import libreoffice
from .convert.base import ProbeContext, RunContext, WorkItem, get_route
from .home import Home
from .journal import Journal
from .model import (
    REASONS,
    Action,
    Category,
    CheckResult,
    CheckState,
    ItemResult,
    Mode,
    Plan,
    PlanItem,
    Status,
    clip_message,
    combine_status,
    format_checks,
    join_reasons,
)
from .paths import free_bytes, is_network_path, join_rel, long_path, overlap, real
from .plan import Planner
from .report import (
    ResumeIndex,
    RunEntry,
    RunIndex,
    journal_name,
    mtime_iso,
    new_run_id,
    now_iso,
    read_csv,
    report_name,
    run_json_name,
    write_csv,
    write_json,
)
from .scan import ScanError, scan
from .scheduler import Budget, Lane, Machine, Scheduler, Task, TaskContext, describe_budget, resolve_budget
from .settings import RESOURCE_KEYS, SettingsStore
from .tools import Toolset, library_versions
from .verify import copy_with_hash, sha256_file, v_hash

log = logging.getLogger("baleen.runner")

GB = 1 << 30


class JobState(enum.StrEnum):
    PREPARING = "preparing"
    RUNNING = "running"
    FINISHING = "finishing"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    STOPPED = "stopped"


FINAL_STATES = (JobState.COMPLETED, JobState.CANCELLED, JobState.STOPPED)


class RootError(Exception):
    """Fatal before the run starts: invalid roots, lock held (CLI exit code 3)."""


@dataclass
class RunSpec:
    mode: Mode
    source_root: str
    output_root: str | None
    settings: dict[str, Any]
    report_copy: str | None = None

    @property
    def workflow(self) -> str:
        return self.mode.value


# --------------------------------------------------------------------------- validation (§5.1)


def validate_roots(mode: Mode, source: str, output: str | None, work_dir: str | None = None) -> list[str]:
    """Return human-readable problems; empty when the roots are usable (§5.1, SEC-7)."""
    errs: list[str] = []
    if not source or not os.path.isabs(source):
        return ["Choose a source folder (an absolute path)."] if mode == Mode.CONVERT else [
            "Choose a folder to check (an absolute path)."]
    if not os.path.isdir(long_path(source)):
        return [f"Folder not found: {source}"]
    if not os.access(long_path(source), os.R_OK):
        errs.append(f"Baleen can't read {source}.")
    if mode == Mode.CONVERT:
        if not output or not os.path.isabs(output):
            errs.append("Choose an output folder (an absolute path).")
            return errs
        rel = overlap(source, output)
        if rel == "same":
            errs.append("The output folder is the source folder. Choose a different folder.")
        elif rel == "b_in_a":
            errs.append("This folder is inside the source folder. Choose a folder outside it.")
        elif rel == "a_in_b":
            errs.append("The source folder is inside this folder. Choose a folder outside it.")
        if os.path.exists(long_path(output)) and not os.path.isdir(long_path(output)):
            errs.append(f"{output} is a file, not a folder.")
    if work_dir:
        for root, label in ((source, "source"), (output, "output")):
            if root and overlap(work_dir, root):
                errs.append(f"The work folder must be outside the {label} folder.")
        if is_network_path(work_dir):
            errs.append("The work folder must be on a local disk.")
    return errs


def check_output_writable(output: str) -> str | None:
    try:
        os.makedirs(long_path(output), exist_ok=True)
        probe = os.path.join(output, "_baleen")
        os.makedirs(long_path(probe), exist_ok=True)
        if not os.access(long_path(probe), os.W_OK):
            return f"Baleen can't write to {output}."
    except OSError as e:
        return f"Baleen can't create or write to {output}: {e.strerror or e}"
    return None


# --------------------------------------------------------------------------- job


@dataclass
class Job:
    id: str
    spec: RunSpec
    state: JobState = JobState.PREPARING
    started_at: str = field(default_factory=now_iso)
    started_mono: float = field(default_factory=time.monotonic)
    finished_at: str = ""
    finished_mono: float | None = None
    total: int = 0
    done: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    scanned: int = 0
    prepare_note: str = "Scanning the source folder…"
    plan: Plan | None = None
    journal: Journal | None = None
    reports_dir: str = ""
    cancel_requested: bool = False
    stop_reason: str = ""
    warnings: list[str] = field(default_factory=list)
    resource_log: list[dict[str, Any]] = field(default_factory=list)
    machine: Machine | None = None
    scheduler: Scheduler | None = None
    run: RunContext | None = None
    started_items: set[int] = field(default_factory=set)
    finished_items: set[int] = field(default_factory=set)
    exit_error: str = ""
    report_sha256: str = ""
    done_event: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def mode(self) -> Mode:
        return self.spec.mode

    @property
    def workflow(self) -> str:
        return self.spec.workflow

    def elapsed(self) -> float:
        end = self.finished_mono if self.finished_mono is not None else time.monotonic()
        return max(0.0, end - self.started_mono)

    @property
    def in_flight(self) -> int:
        return len(self.started_items - self.finished_items)

    def report_path(self) -> str:
        return os.path.join(self.reports_dir, report_name(self.id))

    def run_json_path(self) -> str:
        return os.path.join(self.reports_dir, run_json_name(self.id))

    def journal_path(self) -> str:
        return os.path.join(self.reports_dir, journal_name(self.id))

    def progress(self) -> dict[str, Any]:
        """Live state for the run page (UI-R2) and the rail card (UI-G2)."""
        snap = self.scheduler.snapshot() if self.scheduler else None
        in_progress: list[dict[str, Any]] = []
        if snap:
            for r in snap.running:
                for label, item in zip(r.labels, r.items, strict=False):
                    in_progress.append({"label": label, "action": r.action, "category": r.category,
                                        "item": item, "lane": r.lane.value})
        pct = int(self.done * 100 / self.total) if self.total else (100 if self.state in FINAL_STATES else 0)
        budget = snap.budget if snap else None
        return {
            "id": self.id,
            "workflow": self.workflow,
            "state": self.state.value,
            "total": self.total,
            "done": self.done,
            "percent": pct,
            "elapsed_s": self.elapsed(),
            "scanned": self.scanned,
            "prepare_note": self.prepare_note,
            "counts": dict(self.counts),
            "in_progress": in_progress,
            "in_flight": self.in_flight,
            "cancel_requested": self.cancel_requested,
            "stop_reason": self.stop_reason,
            "budget": budget.to_dict() if budget else None,
            "budget_text": describe_budget(budget, self.machine) if budget and self.machine else None,
        }


# --------------------------------------------------------------------------- engine


class Engine:
    """Owns the one running job of this Baleen instance."""

    def __init__(self, home: Home, store: SettingsStore, tools: Toolset) -> None:
        self.home = home
        self.store = store
        self.tools = tools
        self.index = RunIndex(home.runs_index_path)
        self._lock = threading.Lock()
        self._job: Job | None = None
        self._last: Job | None = None
        self.listeners: list[Callable[[Job], None]] = []

    # ------------------------------------------------------------------ queries

    def current(self) -> Job | None:
        with self._lock:
            return self._job

    def last(self) -> Job | None:
        with self._lock:
            return self._job or self._last

    def running(self) -> bool:
        j = self.current()
        return j is not None and j.state not in FINAL_STATES

    def get(self, run_id: str) -> Job | None:
        with self._lock:
            for j in (self._job, self._last):
                if j is not None and j.id == run_id:
                    return j
        return None

    # ------------------------------------------------------------------ preview (UI-C4)

    def make_probe_ctx(self, mode: Mode, settings: dict[str, Any]) -> ProbeContext:
        return ProbeContext(mode=mode, settings=settings, tools=self.tools, home=self.home,
                            low_priority=bool(settings["app"].get("low_priority", True)))

    def preview(self, mode: Mode, source: str, output: str | None, settings: dict[str, Any],
                on_progress: Callable[[int, int], None] | None = None) -> Plan:
        """Scan and plan without writing anything."""
        errs = validate_roots(mode, source, output)
        if errs:
            raise RootError(errs[0])
        entries = scan(source)
        machine = Machine.detect([source, output or ""])
        b = resolve_budget(settings["app"], machine)
        planner = Planner(self.make_probe_ctx(mode, settings), source, output, workers=b.B, on_progress=on_progress)
        return planner.build(entries)

    # ------------------------------------------------------------------ start / cancel

    def start(self, spec: RunSpec, *, background: bool = True,
              prefs: Callable[[], dict[str, Any]] | None = None) -> Job:
        """Validate, then run the job (in a thread when background=True). Raises RootError."""
        work_dir = str(self.home.resolve_work_dir(spec.settings["app"].get("work_dir", "data/work")))
        errs = validate_roots(spec.mode, spec.source_root, spec.output_root, work_dir)
        if spec.mode == Mode.CONVERT and not errs and spec.output_root:
            werr = check_output_writable(spec.output_root)
            if werr:
                errs.append(werr)
        if errs:
            raise RootError(errs[0])
        with self._lock:
            if self._job is not None and self._job.state not in FINAL_STATES:
                raise RootError("A run is already in progress. Baleen runs one job at a time.")
            self.home.ensure()
            if spec.mode == Mode.CONVERT:
                assert spec.output_root
                reports_dir = os.path.join(spec.output_root, "_baleen")
            else:
                reports_dir = str(self.home.reports_dir)
            os.makedirs(long_path(reports_dir), exist_ok=True)
            run_id = new_run_id([reports_dir, str(self.home.reports_dir)], taken=self.index.ids())
            job = Job(run_id, spec, reports_dir=reports_dir)
            self._job = job
        lock: fsops.OutputLock | None = None
        if spec.mode == Mode.CONVERT:
            lock = fsops.OutputLock(os.path.join(reports_dir, ".lock"))
            try:
                lock.acquire()
            except fsops.LockHeld as e:
                with self._lock:
                    self._job = None
                raise RootError(str(e)) from e
            if lock.warning:
                job.warnings.append(lock.warning)
            try:
                self.recover_dir(reports_dir, skip={run_id})
            except Exception:  # recovery problems never block a new run
                log.exception("recovery failed in %s", reports_dir)
        prefs_fn = prefs or self.store.app
        if background:
            t = threading.Thread(target=self._run_job, args=(job, work_dir, prefs_fn, lock), daemon=True,
                                 name=f"baleen-job-{run_id}")
            t.start()
        else:
            self._run_job(job, work_dir, prefs_fn, lock)
        return job

    def cancel(self) -> bool:
        """Cancel (§5.6): stop dispatching; files in progress finish; the rest become SKIPPED."""
        job = self.current()
        if job is None or job.state in FINAL_STATES:
            return False
        job.cancel_requested = True
        if job.scheduler is not None:
            job.scheduler.cancel_queued(lambda t: t.kind in ("stage", "hash") and t.item not in job.started_items)
        return True

    def wait(self, timeout: float | None = None) -> bool:
        job = self.last()
        return True if job is None else job.done_event.wait(timeout)

    # ------------------------------------------------------------------ the job thread

    def _notify(self, job: Job) -> None:
        for fn in list(self.listeners):
            with contextlib.suppress(Exception):
                fn(job)

    def _run_job(self, job: Job, work_dir: str, prefs: Callable[[], dict[str, Any]],
                 lock: fsops.OutputLock | None) -> None:
        spec = job.spec
        unsubscribe: Callable[[], None] | None = None
        work_root = os.path.join(work_dir, job.id)
        try:
            with osutil.keep_awake(bool(prefs().get("keep_awake", True))):
                osutil.set_thread_low_priority(bool(prefs().get("low_priority", True)))
                machine = Machine.detect([spec.source_root, spec.output_root or ""])
                job.machine = machine
                self.index.upsert(self._entry(job))
                self._notify(job)

                # Scan + plan
                def on_scan(n: int) -> None:
                    job.scanned = n

                entries = scan(spec.source_root, on_progress=on_scan,
                               should_stop=lambda: job.cancel_requested)
                job.prepare_note = "Planning…"
                b0 = resolve_budget(prefs(), machine)
                planner = Planner(self.make_probe_ctx(spec.mode, spec.settings), spec.source_root,
                                  spec.output_root, workers=b0.B, should_stop=lambda: job.cancel_requested)
                plan = planner.build(entries)
                job.plan = plan
                items = [it for it in plan.items if it.materialise]
                job.total = len(items)

                os.makedirs(long_path(work_root), exist_ok=True)
                run = RunContext(
                    run_id=job.id, mode=spec.mode, home=self.home, tools=self.tools, settings=spec.settings,
                    source_root=spec.source_root, output_root=spec.output_root, work_root=work_root,
                    app_prefs=prefs,
                )
                job.run = run
                meta = self._meta(job, plan, b0)
                job.journal = Journal.create(job.journal_path(), meta, plan.items)
                job.resource_log.append({"at": now_iso(), "settings": _resource_settings(prefs()),
                                         "resolved": b0.to_dict()})

                def budget() -> Budget:
                    return resolve_budget(prefs(), machine)

                sched = Scheduler(budget, low_priority=lambda: bool(prefs().get("low_priority", True)))
                job.scheduler = sched

                def on_settings(snapshot: dict[str, Any]) -> None:
                    b = resolve_budget(snapshot["app"], machine)
                    job.resource_log.append({"at": now_iso(), "settings": _resource_settings(snapshot["app"]),
                                             "resolved": b.to_dict()})
                    sched.wake()

                if prefs == self.store.app:
                    unsubscribe = self.store.subscribe(on_settings)
                job.state = JobState.RUNNING
                self._notify(job)

                ex = _Execution(self, job, run, plan)
                ex.start()
                ex.wait()
                sched.close()
                job.state = JobState.FINISHING
                self._finalise(job, work_root)
        except ScanError as e:
            job.stop_reason = str(e)
            job.exit_error = str(e)
            self._finalise_failed_start(job)
        except Exception as e:
            log.exception("job %s crashed", job.id)
            job.stop_reason = f"Baleen stopped unexpectedly: {e.__class__.__name__}: {e}"
            job.exit_error = job.stop_reason
            try:
                if job.journal is not None:
                    job.state = JobState.FINISHING
                    self._finalise(job, work_root)
                else:
                    self._finalise_failed_start(job)
            except Exception:
                log.exception("finalising job %s failed", job.id)
                job.state = JobState.STOPPED
        finally:
            if unsubscribe:
                unsubscribe()
            if lock is not None:
                lock.release()
            job.finished_mono = job.finished_mono or time.monotonic()
            job.finished_at = job.finished_at or now_iso()
            with self._lock:
                self._last = job
                self._job = None
            job.done_event.set()
            self._notify(job)

    # ------------------------------------------------------------------ records

    def _meta(self, job: Job, plan: Plan, b0: Budget) -> dict[str, Any]:
        return {
            "run_id": job.id, "mode": job.mode.value, "workflow": job.workflow,
            "started_at": job.started_at, "source_root": job.spec.source_root,
            "output_root": job.spec.output_root, "settings": job.spec.settings,
            "baleen_version": __version__, "total": job.total,
            "machine": job.machine.to_dict() if job.machine else None, "budget": b0.to_dict(),
        }

    def _entry(self, job: Job) -> RunEntry:
        return RunEntry(
            id=job.id, workflow=job.workflow, source_root=job.spec.source_root,
            output_root=job.spec.output_root, started_at=job.started_at, finished_at=job.finished_at,
            state="running" if job.state not in FINAL_STATES else job.state.value,
            counts=dict(job.counts), total=job.total, reports_dir=job.reports_dir,
        )

    def _run_json(self, job: Job) -> dict[str, Any]:
        tools = {k: {"path": t.path, "version": t.version, "source": t.source}
                 for k, t in self.tools.detect().items()}
        tools.update(library_versions())
        counts = job.journal.counts() if job.journal else {"by_status": {}, "by_category": {}, "by_action": {}}
        m = job.machine
        import platform

        return {
            "schema_version": 1,
            "run_id": job.id,
            "mode": job.mode.value,
            "baleen_version": __version__,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "cancelled": job.state == JobState.CANCELLED,
            "state": job.state.value,
            "stop_reason": job.stop_reason,
            "host": {"os": {"Darwin": "macOS"}.get(platform.system(), platform.system()),
                     "os_version": platform.mac_ver()[0] or platform.version(),
                     "arch": platform.machine(), "hostname": socket.gethostname()},
            "source_root": job.spec.source_root,
            "output_root": job.spec.output_root,
            "settings": _flat_settings(job.spec.settings),
            "resources": {
                "machine": m.to_dict() if m else None,
                "settings": job.resource_log[0]["settings"] if job.resource_log else None,
                "resolved": job.resource_log[0]["resolved"] if job.resource_log else None,
                "changes": job.resource_log[1:],
                "peak": _peaks(job.scheduler),
            },
            "tools": tools,
            "counts": counts,
            "warnings": job.warnings,
            "report_file": report_name(job.id),
            "report_sha256": job.report_sha256,
        }

    def _finalise(self, job: Job, work_root: str) -> None:
        """Write the CSV (plan order) from the journal, the run JSON, and the run index."""
        assert job.journal is not None and job.plan is not None
        done = job.journal.finished_ns()
        skip_reason = "CANCELLED" if job.cancel_requested else "INTERRUPTED"
        for it in job.plan.items:
            if it.materialise and it.n not in done:
                r = _skipped_result(job.id, it, skip_reason)
                job.journal.commit(r)
                job.counts[r.status] = job.counts.get(r.status, 0) + 1
                job.done += 1
        results = job.journal.results_in_plan_order()
        report = job.report_path()
        try:
            job.report_sha256 = write_csv(report, results)
        except OSError as e:
            # The output folder vanished (UI-R7): keep the report in data/reports instead.
            fallback = str(self.home.reports_dir)
            os.makedirs(fallback, exist_ok=True)
            job.warnings.append(f"Couldn't write the report to {job.reports_dir} ({e.strerror or e}); "
                                f"saved it in {fallback}.")
            job.reports_dir = fallback
            job.report_sha256 = write_csv(job.report_path(), results)
            with contextlib.suppress(Exception):
                Journal.rebuild_from_rows(job.journal_path(), job.journal.meta(), results).close()
        if job.stop_reason:
            job.state = JobState.STOPPED
        elif job.cancel_requested:
            job.state = JobState.CANCELLED
        else:
            job.state = JobState.COMPLETED
        job.finished_mono = time.monotonic()
        job.finished_at = now_iso()
        job.journal.set_meta("state", job.state.value)
        job.journal.set_meta("finished_at", job.finished_at)
        with contextlib.suppress(OSError):
            write_json(job.run_json_path(), self._run_json(job))
        job.journal.close()
        if job.spec.report_copy:
            with contextlib.suppress(OSError):
                shutil.copyfile(job.report_path(), job.spec.report_copy)
        self.index.upsert(self._entry(job))
        # Clean up Baleen-owned temporary folders.
        shutil.rmtree(long_path(work_root), ignore_errors=True)
        if job.spec.output_root:
            staging = os.path.join(job.spec.output_root, ".baleen-staging")
            shutil.rmtree(long_path(os.path.join(staging, job.id)), ignore_errors=True)
            with contextlib.suppress(OSError):
                os.rmdir(long_path(staging))

    def _finalise_failed_start(self, job: Job) -> None:
        job.state = JobState.STOPPED
        job.finished_mono = time.monotonic()
        job.finished_at = now_iso()
        self.index.upsert(self._entry(job))

    # ------------------------------------------------------------------ crash recovery (§5.6)

    def recover_dir(self, reports_dir: str, skip: set[str] | None = None) -> list[str]:
        """Finalise runs that have a journal but no report: SKIPPED INTERRUPTED, state Stopped."""
        fixed: list[str] = []
        try:
            names = os.listdir(long_path(reports_dir))
        except OSError:
            return fixed
        for name in sorted(names):
            if not (name.startswith("journal-") and name.endswith(".sqlite")):
                continue
            run_id = name[len("journal-"):-len(".sqlite")]
            if skip and run_id in skip:
                continue
            if os.path.exists(long_path(os.path.join(reports_dir, report_name(run_id)))):
                continue
            try:
                j = Journal.open(os.path.join(reports_dir, name))
                meta = j.meta()
                done = j.finished_ns()
                for it in j.plan_items():
                    if it.materialise and it.n not in done:
                        j.commit(_skipped_result(run_id, it, "INTERRUPTED"))
                results = j.results_in_plan_order()
                sha = write_csv(os.path.join(reports_dir, report_name(run_id)), results)
                j.set_meta("state", "stopped")
                j.close()
                counts: dict[str, int] = {}
                for r in results:
                    counts[r.status] = counts.get(r.status, 0) + 1
                rj = {
                    "schema_version": 1, "run_id": run_id, "mode": meta.get("mode"),
                    "baleen_version": meta.get("baleen_version"), "started_at": meta.get("started_at"),
                    "finished_at": now_iso(), "cancelled": False, "state": "stopped",
                    "stop_reason": "Baleen stopped unexpectedly; finalised on the next start.",
                    "source_root": meta.get("source_root"), "output_root": meta.get("output_root"),
                    "settings": _flat_settings(meta.get("settings") or {}),
                    "counts": {"by_status": counts}, "report_file": report_name(run_id), "report_sha256": sha,
                }
                write_json(os.path.join(reports_dir, run_json_name(run_id)), rj)
                self.index.upsert(RunEntry(
                    id=run_id, workflow=meta.get("workflow") or meta.get("mode") or "convert",
                    source_root=meta.get("source_root") or "", output_root=meta.get("output_root"),
                    started_at=meta.get("started_at") or "", finished_at=rj["finished_at"], state="stopped",
                    counts=counts, total=len(results), reports_dir=reports_dir,
                ))
                out = meta.get("output_root")
                if out:
                    shutil.rmtree(long_path(os.path.join(out, ".baleen-staging", run_id)), ignore_errors=True)
                    with contextlib.suppress(OSError):
                        os.rmdir(long_path(os.path.join(out, ".baleen-staging")))
                work = self.home.resolve_work_dir(
                    ((meta.get("settings") or {}).get("app") or {}).get("work_dir", "data/work"))
                shutil.rmtree(long_path(os.path.join(str(work), run_id)), ignore_errors=True)
                fixed.append(run_id)
            except Exception:
                log.exception("couldn't recover run %s in %s", run_id, reports_dir)
        return fixed

    def recover_all(self) -> list[str]:
        """On start: finalise interrupted runs known to the run index, and check runs."""
        fixed: list[str] = []
        dirs = {str(self.home.reports_dir)}
        for e in self.index.load():
            if e.state == "running":
                dirs.add(e.reports_dir)
        for d in dirs:
            fixed += self.recover_dir(d)
        return fixed


# --------------------------------------------------------------------------- helpers


def _resource_settings(app: dict[str, Any]) -> dict[str, Any]:
    return {k: app.get(k) for k in RESOURCE_KEYS}


def _flat_settings(settings: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for sec in ("workflow", "app", "advanced"):
        out.update(settings.get(sec, {}) or {})
    return out


def _peaks(s: Scheduler | None) -> dict[str, int] | None:
    if s is None:
        return None
    snap = s.snapshot()
    return {"tokens": snap.peak_tokens, "memory_mb": snap.peak_memory_mb, "transfers": snap.peak_transfers}


def _base_result(run_id: str, it: PlanItem) -> ItemResult:
    return ItemResult(
        n=it.n, run_id=run_id, source_path=it.source_path, source_size=it.size,
        source_mtime=mtime_iso(it.mtime_ns), source_sha256=str(it.data.get("sha256", "")) if it.parent else "",
        source_format=it.source_format, category=it.category.value, action=Action.NONE.value,
    )


def _skipped_result(run_id: str, it: PlanItem, reason: str) -> ItemResult:
    r = _base_result(run_id, it)
    r.status = Status.SKIPPED.value
    r.reason = reason
    r.action = Action.NONE.value
    return r


class _Execution:
    """Drives every item of one job through the lanes."""

    def __init__(self, engine: Engine, job: Job, run: RunContext, plan: Plan) -> None:
        self.engine = engine
        self.job = job
        self.run = run
        self.plan = plan
        self.sched = job.scheduler
        assert self.sched is not None
        self.out_root = job.spec.output_root
        self.resume = ResumeIndex(os.path.join(self.out_root, "_baleen")) if self.out_root else None
        self.items: dict[int, WorkItem] = {}
        self.children: dict[int, list[int]] = {}
        self._pending = 0
        self._cond = threading.Condition()
        for it in plan.items:
            if it.parent is not None:
                self.children.setdefault(it.parent, []).append(it.n)

    # ------------------------------------------------------------------ setup

    def _work_item(self, it: PlanItem) -> WorkItem:
        check_only = self.job.mode == Mode.CHECK or it.action == Action.CHECK
        wi = WorkItem(
            plan=it, run=self.run, work_dir=os.path.join(self.run.work_root, str(it.n)),
            source_abs=it.abs_path, check_only=check_only, category=it.category, action=it.action,
            method=it.method, source_format=it.source_format, reasons=[], notes=list(it.notes),
        )
        if it.message:
            wi.messages.append(it.message)
        return wi

    def start(self) -> None:
        mats = [it for it in self.plan.items if it.materialise]
        with self._cond:
            self._pending = len(mats)
        for it in mats:
            self.items[it.n] = self._work_item(it)
        for it in mats:
            wi = self.items[it.n]
            for cn in self.children.get(it.n, []):
                if cn in self.items:
                    wi.children.append(self.items[cn])
        for it in mats:
            if it.parent is not None:
                continue  # launched by the parent's stage step
            self._launch(self.items[it.n])

    def wait(self) -> None:
        idle_checks = 0
        with self._cond:
            while self._pending > 0:
                self._cond.wait(timeout=0.5)
                if self.sched is None or not self.sched.idle() or self._pending <= 0:
                    idle_checks = 0
                    continue
                if self.job.cancel_requested or self.job.stop_reason:
                    # Everything still pending was never started (cancelled before staging).
                    break
                idle_checks += 1
                if idle_checks >= 6:
                    # Nothing queued or running but items unreported: a bug. Never hang; the
                    # finaliser reports them as SKIPPED INTERRUPTED so every item is accounted (P9).
                    log.error("run %s: %d items never reported; finalising", self.job.id, self._pending)
                    break

    # ------------------------------------------------------------------ dispatch helpers

    def _submit(self, wi: WorkItem, lane: Lane, kind: str, fn: Callable[[TaskContext, WorkItem], None], *,
                step: int, memory_mb: int | None = None, action: str = "") -> None:
        def run_task(ctx: TaskContext) -> None:
            self.job.started_items.add(wi.n)
            try:
                fn(ctx, wi)
            except Exception as e:
                log.error("item %s (%s) %s step failed:\n%s", wi.n, wi.plan.source_path, kind,
                          traceback.format_exc())
                if not wi.done:
                    wi.fail("CONVERSION_ERROR", f"Unexpected error in Baleen ({kind}): {e.__class__.__name__}: {e}")
                self._report(wi)

        name = wi.plan.source_path.rsplit("#", 1)[-1].rsplit("/", 1)[-1]
        assert self.sched is not None
        self.sched.submit(Task(
            lane=lane, order=(wi.n, step), fn=run_task, label=name, action=action or kind.capitalize(),
            category=wi.category.value, item=wi.n, kind=kind, memory_mb=memory_mb,
        ))

    def _launch(self, wi: WorkItem) -> None:
        it = wi.plan
        if it.is_dir or set(it.reasons) & {"SYSTEM_FILE", "SYMLINK"}:
            self._report(wi)
            return
        if it.final or wi.check_only:
            self._submit(wi, Lane.FILES, "hash", self._hash_step, step=0, action="Checking")
        else:
            self._submit(wi, Lane.FILES, "stage", self._stage_step, step=0, action="Copying")

    # ------------------------------------------------------------------ steps

    def _restat(self, wi: WorkItem) -> bool:
        it = wi.plan
        assert it.abs_path is not None
        try:
            st = os.stat(long_path(it.abs_path))
        except OSError as e:
            wi.fail("SOURCE_UNREADABLE", f"Can't read the source: {e.strerror or e}")
            return False
        if st.st_size != it.size or st.st_mtime_ns != it.mtime_ns:
            wi.fail("SOURCE_CHANGED", "The file's size or modification time changed after the scan.")
            return False
        return True

    def _hash_step(self, ctx: TaskContext, wi: WorkItem) -> None:
        """Check-only and plan-time-final items: re-stat and hash by streaming (read-only)."""
        it = wi.plan
        if not self._restat(wi):
            self._report(wi)
            return
        assert it.abs_path is not None
        try:
            with ctx.transfer():
                wi.source_sha256, wi.source_size = sha256_file(it.abs_path)
        except OSError as e:
            wi.fail("SOURCE_UNREADABLE", f"Can't read the source: {e.strerror or e}")
            self._report(wi)
            return
        if it.final:
            self._stage_children_then(wi, lambda c: self._report(wi), ctx)
            return
        self._process(wi)

    def _stage_step(self, ctx: TaskContext, wi: WorkItem) -> None:
        it = wi.plan
        if not self._restat(wi):
            self._report(wi)
            return
        assert it.abs_path is not None and self.out_root is not None
        final = join_rel(self.out_root, it.output_path or "")
        if it.output_path and os.path.lexists(long_path(final)):
            self._existing_output(ctx, wi, final, source=it.abs_path)
            return
        if not self._wait_for_space(wi, it.size or 0):
            self._report(wi)
            return
        os.makedirs(long_path(wi.work_dir), exist_ok=True)
        staged = os.path.join(wi.work_dir, "input" + (it.ext or ""))
        try:
            with ctx.transfer():
                wi.source_sha256, wi.source_size = copy_with_hash(it.abs_path, staged)
        except OSError as e:
            wi.fail("SOURCE_UNREADABLE", f"Can't read the source: {e.strerror or e}")
            self._report(wi)
            return
        if wi.source_size != it.size:
            wi.fail("SOURCE_CHANGED", "The file changed while it was being copied.")
            self._report(wi)
            return
        wi.staged = staged
        self._stage_children_then(wi, lambda c: self._process(wi), ctx)

    def _child_step(self, ctx: TaskContext, wi: WorkItem) -> None:
        """An attachment staged by its e-mail: check for an existing output, then process."""
        it = wi.plan
        if wi.done or it.final:
            # Decided already; a nested e-mail may still release attachments of its own.
            self._stage_children_then(wi, lambda c: self._report(wi), ctx)
            return
        if self.job.mode == Mode.CONVERT and it.output_path and self.out_root and not wi.check_only:
            final = join_rel(self.out_root, it.output_path)
            if os.path.lexists(long_path(final)):
                self._existing_output(ctx, wi, final, source=None)
                return
        self._stage_children_then(wi, lambda c: self._process(wi), ctx)

    def _existing_output(self, ctx: TaskContext, wi: WorkItem, final: str, source: str | None) -> None:
        """§7.5: the final path exists. Resume if a prior report proves it is ours; else OUTPUT_OCCUPIED."""
        it = wi.plan
        try:
            with ctx.transfer():
                if source is not None:
                    wi.source_sha256, wi.source_size = sha256_file(source)
                existing_sha, _ = sha256_file(final)
        except OSError as e:
            wi.fail("SOURCE_UNREADABLE", f"Can't read: {e.strerror or e}")
            self._report(wi)
            return
        assert self.resume is not None and it.output_path
        if os.path.isfile(long_path(final)) and self.resume.matches(it.source_path, wi.source_sha256,
                                                                    it.output_path, existing_sha):
            wi.resume = True
            wi.existing_output = final
            wi.action = Action.CHECK
            wi.extra["existing_sha256"] = existing_sha
            self._stage_children_then(wi, lambda c: self._process(wi), ctx)
            return
        wi.reasons.append("OUTPUT_OCCUPIED")
        wi.messages.append(f"{it.output_path} already exists and no earlier Baleen report shows it was "
                           "made from this source.")
        wi.done = True
        self._stage_children_then(wi, lambda c: self._report(wi), ctx)

    def _stage_children_then(self, wi: WorkItem, then: Callable[[TaskContext], None], ctx: TaskContext) -> None:
        """E-mails: write attachments into their work folders and launch them, then continue."""
        kids = [c for c in wi.children if c.n not in self.job.finished_items]
        if kids:
            route = get_route(wi.plan.route or "email")
            for c in kids:
                os.makedirs(long_path(c.work_dir), exist_ok=True)
            try:
                route.stage_children(ctx, wi)
            except Exception as e:
                log.exception("staging attachments of %s failed", wi.plan.source_path)
                for c in kids:
                    if not c.staged and not c.done:
                        c.fail("SOURCE_UNREADABLE", f"Couldn't extract the attachment: {e}")
            for c in kids:
                if not c.staged and not c.done and not c.plan.final:
                    c.fail("SOURCE_UNREADABLE", "The attachment could not be extracted.")
                if c.plan.final and not c.source_sha256:
                    c.source_sha256 = str(c.plan.data.get("sha256", ""))
                    c.source_size = c.plan.size
                if (c.done or c.plan.final) and not c.children:
                    self.job.started_items.add(c.n)
                    self._report(c)
                else:
                    self._submit(c, Lane.FILES, "child", self._child_step, step=0, action="Checking")
        then(ctx)

    def _wait_for_space(self, wi: WorkItem, size: int) -> bool:
        need = 2 * size + GB
        while free_bytes(self.run.work_root) < need:
            snap = self.sched.snapshot() if self.sched else None
            others = (len(snap.running) - 1) if snap else 0
            if others <= 0:
                wi.fail("NO_WORK_SPACE", f"The work folder needs {need / 1e9:.1f} GB free for this file.")
                return False
            time.sleep(1.0)
        return True

    def _process(self, wi: WorkItem) -> None:
        if wi.done:
            self._report(wi)
            return
        route = get_route(wi.plan.route or "")
        if wi.resume:
            self._submit(wi, route.lane if not route.batched else Lane.FILES, "process",
                         lambda ctx, w: self._run_route(ctx, w, resume=True), step=1, action="Checking")
            return
        if route.batched and not wi.check_only:
            assert self.sched is not None
            name = wi.plan.source_path.rsplit("#", 1)[-1].rsplit("/", 1)[-1]
            self.sched.submit(Task(
                lane=Lane.DOCUMENTS, order=(wi.n, 1), batch_fn=self._documents_batch, payload=wi,
                batch_key=route.batch_key(wi), label=name, action="Converting", category=wi.category.value,
                item=wi.n, kind="process",
            ))
            return
        lane = route.lane if not (route.batched and wi.check_only) else Lane.FILES
        mem = route.memory_mb(wi) if lane == Lane.FILES else None
        verb = "Checking" if wi.check_only or wi.action == Action.COPY else "Converting"
        self._submit(wi, lane, "process", lambda ctx, w: self._run_route(ctx, w, resume=False), step=1,
                     memory_mb=mem, action=verb)

    def _run_route(self, ctx: TaskContext, wi: WorkItem, *, resume: bool) -> None:
        route = get_route(wi.plan.route or "")
        if resume:
            route.check_existing(ctx, wi)
        else:
            route.process(ctx, wi)
        self._after_process(wi)

    def _documents_batch(self, ctx: TaskContext, wis: list[WorkItem]) -> list[None]:
        pairs = []
        for wi in wis:
            self.job.started_items.add(wi.n)
            route = get_route(wi.plan.route or "")
            try:
                job = route.prepare(ctx, wi)
            except Exception as e:
                log.exception("prepare failed for %s", wi.plan.source_path)
                wi.fail("CONVERSION_ERROR", f"Couldn't prepare the file: {e.__class__.__name__}: {e}")
                job = None
            if job is not None and not wi.done:
                pairs.append((wi, route, job))
        if pairs:
            try:
                results = libreoffice.convert_batch(ctx, self.run, [p[2] for p in pairs])
            except Exception as e:
                log.exception("LibreOffice batch failed")
                results = [libreoffice.LoResult(p[2], False, None, "CONVERSION_ERROR", str(e)) for p in pairs]
            for (wi, route, _job), res in zip(pairs, results, strict=True):
                try:
                    route.finish(ctx, wi, res)
                except Exception as e:
                    log.exception("finish failed for %s", wi.plan.source_path)
                    wi.fail("CONVERSION_ERROR", f"{e.__class__.__name__}: {e}")
        for wi in wis:
            try:
                self._after_process(wi)
            except Exception:
                log.exception("after-process failed for %s", wi.plan.source_path)
                self._report(wi)
        return [None] * len(wis)

    def _after_process(self, wi: WorkItem) -> None:
        if wi.done:
            self._report(wi)
            return
        if wi.pdfa is not None:
            path, flavour = wi.pdfa
            assert self.sched is not None
            name = wi.plan.source_path.rsplit("#", 1)[-1].rsplit("/", 1)[-1]
            self.sched.submit(Task(
                lane=Lane.PDFA, order=(wi.n, 2), batch_fn=self._pdfa_batch, payload=wi,
                batch_key=flavour, label=name, action="Validating PDF/A", category=wi.category.value,
                item=wi.n, kind="pdfa",
            ))
            return
        self._decide(wi)

    def _pdfa_batch(self, ctx: TaskContext, wis: list[WorkItem]) -> list[None]:
        from .verify import pdf as vpdf

        try:
            results = vpdf.verapdf_batch(ctx, self.run, [w.pdfa for w in wis])  # type: ignore[misc]
        except Exception as e:
            log.exception("veraPDF batch failed")
            results = [CheckResult("V-PDFA", CheckState.UNAVAILABLE, message=f"veraPDF failed: {e}")
                       for _ in wis]
        for wi, res in zip(wis, results, strict=True):
            wi.checks.append(res)
            try:
                self._decide(wi)
            except Exception:
                log.exception("decide failed for %s", wi.plan.source_path)
                self._report(wi)
        return [None] * len(wis)

    def _decide(self, wi: WorkItem) -> None:
        """§8 outcome rules, then publish or report."""
        route = get_route(wi.plan.route or "")
        codes = route.decide(wi)
        if wi.resume:
            fails = [c for c in codes if REASONS[c].status == Status.FAILED
                     or c in ("SOURCE_INVALID", "VERIFY_FAILED", "PDFA_INVALID")]
            unavailable = [c for c in codes if c in ("VALIDATOR_MISSING", "VALIDATOR_ERROR")]
            wi.reasons += ["OUTPUT_INVALID"] if fails else ["RESUMED", *unavailable]
            wi.done = True
            self._report(wi)
            return
        for c in codes:
            if c not in wi.reasons:
                wi.reasons.append(c)
        written_ok = all(REASONS[c].written != "no" for c in wi.reasons)
        status = combine_status(wi.reasons)
        if wi.check_only or not wi.result_path or not written_ok or status == Status.FAILED:
            wi.done = True
            self._report(wi)
            return
        self._submit(wi, Lane.FILES, "publish", self._publish_step, step=3, action="Saving")

    def _publish_step(self, ctx: TaskContext, wi: WorkItem) -> None:
        """P5: .part on the output volume, V-HASH read-back, exclusive atomic rename, mtime."""
        assert self.out_root is not None and wi.result_path and wi.plan.output_path
        it = wi.plan
        staging = os.path.join(self.out_root, ".baleen-staging", self.run.run_id)
        part = os.path.join(staging, f"{it.n}.part")
        final = join_rel(self.out_root, it.output_path)
        try:
            os.makedirs(long_path(staging), exist_ok=True)
            with ctx.transfer():
                local_sha, size = copy_with_hash(wi.result_path, part)
            with ctx.transfer():
                hc = v_hash(part, local_sha)
            wi.checks.append(hc)
            if hc.state != CheckState.PASS:
                wi.fail("VERIFY_FAILED", hc.message)
                _unlink(part)
                self._report(wi)
                return
            os.makedirs(long_path(os.path.dirname(final)), exist_ok=True)
            try:
                fsops.rename_noreplace(part, final)
            except FileExistsError:
                _unlink(part)
                wi.checks.pop()
                wi.fail("OUTPUT_OCCUPIED", f"{it.output_path} appeared while Baleen was working.")
                self._report(wi)
                return
            if it.mtime_ns is not None:
                with contextlib.suppress(OSError):
                    os.utime(long_path(final), ns=(time.time_ns(), it.mtime_ns))
            wi.extra["published"] = {"path": it.output_path, "size": size, "sha256": local_sha}
            wi.done = True
            self._report(wi)
        except OSError as e:
            _unlink(part)
            if not os.path.isdir(long_path(self.out_root)):
                self._stop(f"The output folder is no longer available ({self.out_root}).")
                wi.fail("VERIFY_FAILED", "The output folder disappeared while saving.")
            else:
                wi.fail("VERIFY_FAILED", f"Couldn't save the output: {e.strerror or e}")
            self._report(wi)

    def _stop(self, reason: str) -> None:
        """A fatal condition mid-run (UI-R7): stop dispatching; remaining items are INTERRUPTED."""
        if not self.job.stop_reason:
            self.job.stop_reason = reason
            log.error("run %s stopped: %s", self.job.id, reason)
        if self.sched is not None:
            self.sched.cancel_queued(lambda t: t.kind in ("stage", "hash") and t.item not in self.job.started_items)

    # ------------------------------------------------------------------ report

    def _report(self, wi: WorkItem) -> None:
        job = self.job
        with job._lock:
            if wi.n in job.finished_items:
                return
            job.finished_items.add(wi.n)
        r = self._result(wi)
        assert job.journal is not None
        job.journal.commit(r)
        with job._lock:
            job.done += 1
            job.counts[r.status] = job.counts.get(r.status, 0) + 1
        # Delete the item's work and staging files (§5.4 Report).
        shutil.rmtree(long_path(wi.work_dir), ignore_errors=True)
        with self._cond:
            self._pending -= 1
            self._cond.notify_all()

    def _result(self, wi: WorkItem) -> ItemResult:
        it = wi.plan
        r = _base_result(self.run.run_id, it)
        reasons = list(it.reasons) if it.final else []
        for c in wi.reasons:
            if c not in reasons:
                reasons.append(c)
        status = combine_status(reasons)
        r.status = status.value
        r.reason = join_reasons(reasons + list(wi.notes))
        r.source_sha256 = wi.source_sha256 or r.source_sha256
        if wi.source_size is not None:
            r.source_size = wi.source_size
        r.source_format = wi.source_format or it.source_format
        r.category = (wi.category or it.category or Category.OTHER).value
        if it.final or status in (Status.IGNORED, Status.SKIPPED):
            r.action = (Action.CHECK if self.job.mode == Mode.CHECK and status not in (
                Status.IGNORED, Status.SKIPPED) and r.source_sha256 else Action.NONE).value
        else:
            r.action = wi.action.value
        r.method = wi.method
        pub = wi.extra.get("published")
        if pub:
            r.output_path, r.output_size, r.output_sha256 = pub["path"], pub["size"], pub["sha256"]
        elif wi.resume and wi.existing_output and it.output_path:
            r.output_path = it.output_path
            r.output_sha256 = str(wi.extra.get("existing_sha256", ""))
            with contextlib.suppress(OSError):
                r.output_size = os.path.getsize(long_path(wi.existing_output))
        r.checks = format_checks(wi.checks)
        r.message = clip_message(" · ".join(m for m in wi.messages if m))
        return r


def _unlink(p: str) -> None:
    with contextlib.suppress(OSError):
        os.unlink(long_path(p))


def load_run_results(reports_dir: str, run_id: str) -> tuple[Journal | None, list[ItemResult]]:
    """For UI-R8: open (or rebuild from the CSV) the journal of a finished run."""
    jpath = os.path.join(reports_dir, journal_name(run_id))
    if os.path.exists(jpath):
        j = Journal.open(jpath)
        return j, []
    csv_path = os.path.join(reports_dir, report_name(run_id))
    if os.path.exists(csv_path):
        rows = read_csv(csv_path)
        try:
            j = Journal.rebuild_from_rows(jpath, {"run_id": run_id, "rebuilt_from_csv": True}, rows)
            return j, rows
        except OSError:
            return None, rows
    return None, []


def resolve_real(p: str) -> str:
    return real(p)


def utc_now() -> datetime:
    return datetime.now(UTC)
