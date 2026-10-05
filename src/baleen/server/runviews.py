"""One view of a run, live or from history (UI-R1…R8, UI-H1).

A live run comes from the engine (Job.progress(), job.journal). A finished run comes from the
run index (data/runs.json), its run JSON and its journal; a missing journal is rebuilt from the
CSV report (§10.4, UI-R8), so run pages survive restarts.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass, field
from typing import Any

from ..journal import Journal
from ..model import Status
from ..report import RunEntry, journal_name, read_json, report_name, run_json_name
from ..runner import FINAL_STATES, Job
from ..workflows import Workflow
from ..workflows import get as get_workflow
from .context import ServerContext
from .present import span

LIVE_STATES = ("preparing", "running", "finishing")


@dataclass
class RunView:
    id: str
    workflow_id: str
    wf: Workflow | None
    state: str  # preparing | running | finishing | completed | cancelled | stopped
    source_root: str
    output_root: str | None
    started_at: str
    finished_at: str
    elapsed_s: float | None
    total: int
    counts: dict[str, int]  # by status value (OK, NEEDS_REVIEW, ...)
    settings: dict[str, Any]  # workflow settings of the run
    stop_reason: str = ""
    reports_dir: str = ""
    cancel_requested: bool = False
    job: Job | None = None
    entry: RunEntry | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def live(self) -> bool:
        return self.state in LIVE_STATES

    @property
    def name(self) -> str:
        return self.wf.name if self.wf else self.workflow_id.capitalize()

    def count(self, status: Status) -> int:
        return int(self.counts.get(status.value, 0))

    @property
    def issues(self) -> int:
        return self.count(Status.FAILED) + self.count(Status.NEEDS_REVIEW) + self.count(Status.UNSUPPORTED)

    @property
    def processed(self) -> int:
        """Files finished before a cancel or stop: everything not SKIPPED."""
        return max(0, sum(self.counts.values()) - self.count(Status.SKIPPED))

    @property
    def report_path(self) -> str:
        return os.path.join(self.reports_dir, report_name(self.id))

    def key_counts(self) -> dict[str, int]:
        from .present import status_key

        out = {k: 0 for k in ("failed", "review", "unsupported", "ok", "ignored", "skipped")}
        for st, n in self.counts.items():
            out[status_key(st)] = out.get(status_key(st), 0) + int(n)
        return out


def _workflow_settings(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Run JSON stores flat settings; journals store {workflow, app, advanced}."""
    if not raw:
        return {}
    if isinstance(raw.get("workflow"), dict):
        return dict(raw["workflow"])
    return dict(raw)


def from_job(job: Job) -> RunView:
    state = job.state.value
    elapsed = job.elapsed()
    return RunView(
        id=job.id, workflow_id=job.workflow, wf=get_workflow(job.workflow), state=state,
        source_root=job.spec.source_root, output_root=job.spec.output_root, started_at=job.started_at,
        finished_at=job.finished_at, elapsed_s=elapsed, total=job.total, counts=dict(job.counts),
        settings=_workflow_settings(job.spec.settings), stop_reason=job.stop_reason,
        reports_dir=job.reports_dir, cancel_requested=job.cancel_requested, job=job,
        warnings=list(job.warnings),
    )


def from_entry(entry: RunEntry) -> RunView:
    settings: dict[str, Any] = {}
    stop_reason = ""
    warnings: list[str] = []
    rj_path = os.path.join(entry.reports_dir, run_json_name(entry.id))
    with contextlib.suppress(OSError, ValueError):
        rj = read_json(rj_path)
        settings = _workflow_settings(rj.get("settings"))
        stop_reason = rj.get("stop_reason") or ""
        warnings = list(rj.get("warnings") or [])
    if not settings:
        jp = os.path.join(entry.reports_dir, journal_name(entry.id))
        if os.path.exists(jp):
            with contextlib.suppress(Exception):
                settings = _workflow_settings(Journal.open(jp).meta().get("settings"))
    return RunView(
        id=entry.id, workflow_id=entry.workflow, wf=get_workflow(entry.workflow), state=entry.state,
        source_root=entry.source_root, output_root=entry.output_root, started_at=entry.started_at,
        finished_at=entry.finished_at, elapsed_s=span(entry.started_at, entry.finished_at), total=entry.total,
        counts=dict(entry.counts), settings=settings, stop_reason=stop_reason, reports_dir=entry.reports_dir,
        entry=entry, warnings=warnings,
    )


def find_run(ctx: ServerContext, run_id: str) -> RunView | None:
    job = ctx.engine.get(run_id)
    if job is not None:
        return from_job(job)
    entry = ctx.engine.index.get(run_id)
    if entry is None:
        return None
    if entry.state == "running":
        # Not this process's job: an interrupted run that recovery has not finalised yet.
        entry.state = "stopped"
    return from_entry(entry)


def journal_of(ctx: ServerContext, run: RunView) -> Journal | None:
    if run.job is not None and run.job.journal is not None:
        return run.job.journal
    if run.live:
        return None
    return ctx.journal_for(run.id, run.reports_dir)


def current_job(ctx: ServerContext) -> Job | None:
    """The running job, if any (state not final)."""
    job = ctx.engine.current()
    if job is None or job.state in FINAL_STATES:
        return None
    return job
