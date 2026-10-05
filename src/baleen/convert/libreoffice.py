"""LibreOffice batches for the Documents lane (spec §5.5, §6.2, DR-35).

- Each Documents-lane instance uses its own profile, data/lo-profile-<k>.
- One `soffice --convert-to` call converts up to 8 files that share an import filter, export
  filter and PDF/A level (the batch key). Inputs have unique stems, so outputs map back.
- Batch timeout 60 s + 30 s per file. A failed or timed-out batch is retried one file per
  process with the per-file limit (lo_timeout_s, 300 s), so one bad file cannot sink the rest.
- Arguments are a list (no shell); the whole process tree is killed on timeout (SEC-8).
"""

from __future__ import annotations

import itertools
import os
import shutil
import threading
from pathlib import Path

from .. import proc
from ..paths import long_path
from ..scheduler import TaskContext
from .base import LoJob, LoResult, RunContext

PDFA_VERSION = {"1b": "1", "2b": "2", "3b": "3"}
BATCH_BASE_S = 60
BATCH_PER_FILE_S = 30


class ProfilePool:
    """Hands out profile indexes 0..n so concurrent instances never share a profile."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._busy: set[int] = set()

    def acquire(self) -> int:
        with self._lock:
            k = 0
            while k in self._busy:
                k += 1
            self._busy.add(k)
            return k

    def release(self, k: int) -> None:
        with self._lock:
            self._busy.discard(k)


POOL = ProfilePool()
_batch_ids = itertools.count(1)


def convert_args(soffice: str, profile_dir: Path, jobs: list[LoJob], outdir: str) -> list[str]:
    head = jobs[0]
    level = PDFA_VERSION[head.pdfa_level]
    args = [
        soffice,
        f"-env:UserInstallation={profile_dir.resolve().as_uri()}",
        "--headless", "--invisible", "--nologo", "--norestore", "--nolockcheck", "--nodefault",
    ]
    if head.infilter:
        args.append(f"--infilter={head.infilter}")
    args += [
        "--convert-to",
        f'pdf:{head.export_filter}:{{"SelectPdfVersion":{{"type":"long","value":"{level}"}}}}',
        "--outdir", outdir,
    ]
    args += [j.input_path for j in jobs]
    return args


def _method(run: RunContext, job: LoJob) -> str:
    ver = run.tools.get("libreoffice").version
    return f"LibreOffice {ver} · {job.export_filter} · PDF/A-{job.pdfa_level}"


def _collect(jobs: list[LoJob], outdir: str) -> dict[int, str]:
    """Map job index -> produced PDF (moved into the job's out_dir)."""
    got: dict[int, str] = {}
    for i, j in enumerate(jobs):
        produced = os.path.join(outdir, Path(j.input_path).stem + ".pdf")
        if os.path.exists(long_path(produced)) and os.path.getsize(long_path(produced)) > 0:
            os.makedirs(long_path(j.out_dir), exist_ok=True)
            dest = j.expected_output
            if os.path.abspath(produced) != os.path.abspath(dest):
                os.replace(long_path(produced), long_path(dest))
            got[i] = dest
    return got


def _run(run: RunContext, ctx: TaskContext, jobs: list[LoJob], timeout: float, outdir: str
         ) -> tuple[dict[int, str], proc.ProcResult]:
    soffice = run.tools.path("libreoffice")
    assert soffice
    k = POOL.acquire()
    try:
        profile = run.home.lo_profile(k)
        profile.mkdir(parents=True, exist_ok=True)
        os.makedirs(long_path(outdir), exist_ok=True)
        args = convert_args(soffice, profile, jobs, outdir)
        res = proc.run(args, timeout=timeout, env=run.tools_env(), low_priority=ctx.low_priority,
                       cwd=outdir)
        return _collect(jobs, outdir), res
    finally:
        POOL.release(k)


def convert_batch(ctx: TaskContext, run: RunContext, jobs: list[LoJob]) -> list[LoResult]:
    """Convert jobs that share one batch key. Always returns one LoResult per job, in order."""
    if not jobs:
        return []
    if run.tools.path("libreoffice") is None:
        return [LoResult(j, False, None, "TOOL_MISSING", "LibreOffice is not available.") for j in jobs]
    keys = {j.batch_key for j in jobs}
    if len(keys) != 1:
        raise ValueError(f"jobs in one batch must share a batch key, got {keys}")
    bid = next(_batch_ids)
    outdir = os.path.join(run.work_root, f"lo-batch-{bid}")
    results: list[LoResult | None] = [None] * len(jobs)
    try:
        if len(jobs) > 1:
            got, res = _run(run, ctx, jobs, BATCH_BASE_S + BATCH_PER_FILE_S * len(jobs), outdir)
            for i, path in got.items():
                results[i] = LoResult(jobs[i], True, path, method=_method(run, jobs[i]))
        # Files not converted by the batch (or a single file): one process per file.
        per_file = float(run.advanced.get("lo_timeout_s", 300))
        for i, j in enumerate(jobs):
            if results[i] is not None:
                continue
            got, res = _run(run, ctx, [j], per_file, outdir)
            if 0 in got:
                results[i] = LoResult(j, True, got[0], method=_method(run, j), retried_alone=len(jobs) > 1)
            elif res.timed_out:
                results[i] = LoResult(j, False, None, "TIMEOUT",
                                      f"LibreOffice took longer than {int(per_file)} s.", method=_method(run, j),
                                      retried_alone=len(jobs) > 1)
            else:
                msg = (res.err() or res.out() or res.error).strip()
                results[i] = LoResult(j, False, None, "CONVERSION_ERROR",
                                      msg or f"LibreOffice produced no PDF (exit code {res.returncode}).",
                                      method=_method(run, j), retried_alone=len(jobs) > 1)
    finally:
        shutil.rmtree(long_path(outdir), ignore_errors=True)
    return [r for r in results if r is not None]
