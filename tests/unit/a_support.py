"""Helpers for workstream (a) unit tests: contexts and work items without the engine."""

from __future__ import annotations

import contextlib
import io
import os
import shutil
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from baleen import settings as S
from baleen.convert.base import ProbeContext, RunContext, SourceRef, WorkItem
from baleen.home import Home
from baleen.model import Action, Category, Mode, PlanItem, Probe
from baleen.tools import Toolset


def settings(**workflow: Any) -> dict[str, Any]:
    st = S.defaults()
    for k, v in workflow.items():
        sec, _ = S.find_field(k)
        st[sec][k] = v
    return st


def probe_ctx(home: Home, mode: Mode = Mode.CONVERT, **workflow: Any) -> ProbeContext:
    return ProbeContext(mode=mode, settings=settings(**workflow), tools=Toolset(home), home=home, low_priority=False)


def src_bytes(name: str, data: bytes) -> SourceRef:
    ext = os.path.splitext(name)[1].lower()
    return SourceRef(name=name, ext=ext, data=data, size=len(data))


def src_file(path: Path) -> SourceRef:
    return SourceRef(name=path.name, ext=path.suffix.lower(), path=str(path), size=path.stat().st_size)


@dataclass
class FakeTaskContext:
    """What routes use from a TaskContext: transfer slots, token count, priority."""

    tokens: int = 1
    low_priority: bool = False
    transfers: list[int] = field(default_factory=list)

    @contextlib.contextmanager
    def transfer(self) -> Iterator[None]:
        self.transfers.append(1)
        yield


def run_ctx(home: Home, tmp: Path, mode: Mode = Mode.CONVERT, source_root: Path | None = None,
            **workflow: Any) -> RunContext:
    work_root = tmp / "work" / "run1"
    work_root.mkdir(parents=True, exist_ok=True)
    return RunContext(run_id="run1", mode=mode, home=home, tools=Toolset(home), settings=settings(**workflow),
                      source_root=str(source_root or tmp / "src"), output_root=str(tmp / "out"),
                      work_root=str(work_root))


def work_item(run: RunContext, source: Path, probe: Probe, *, n: int = 1, stage: bool = True,
              check_only: bool = False, action: Action | None = None) -> WorkItem:
    ext = source.suffix.lower()
    item = PlanItem(
        n=n, source_path=source.name, abs_path=str(source), size=source.stat().st_size,
        mtime_ns=source.stat().st_mtime_ns, ext=ext, category=probe.category or Category.OTHER,
        action=action or probe.action, route=probe.route, target_ext=probe.target_ext, out_dir="",
        output_path=source.stem + (probe.target_ext or ""), data=dict(probe.data),
        source_format=probe.source_format, method=probe.method,
    )
    wi = WorkItem(plan=item, run=run, work_dir=os.path.join(run.work_root, str(n)), source_abs=str(source),
                  check_only=check_only, category=item.category, action=item.action, method=item.method,
                  source_format=item.source_format)
    if stage and not check_only:
        os.makedirs(wi.work_dir, exist_ok=True)
        staged = os.path.join(wi.work_dir, "input" + ext)
        shutil.copyfile(source, staged)
        wi.staged = staged
    return wi


def encode_image(im: Any, fmt: str, **params: Any) -> bytes:
    buf = io.BytesIO()
    im.save(buf, fmt, **params)
    return buf.getvalue()
