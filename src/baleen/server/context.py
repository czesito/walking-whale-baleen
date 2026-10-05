"""Everything the routes share: the engine, settings, tools, secrets, previews, quit state."""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..home import Home
from ..journal import Journal
from ..model import REASONS, Mode, Plan, Status
from ..plan import Planner, plan_summary
from ..runner import Engine, RootError, validate_roots
from ..scan import ScanError, scan
from ..scheduler import Machine, resolve_budget
from ..settings import SettingsStore
from ..tools import Toolset
from ..workflows import Workflow
from .security import Secrets

log = logging.getLogger("baleen.server")

LIST_LIMIT = 50  # rows shown in each preview disclosure list


# --------------------------------------------------------------------------- previews (UI-C4)


@dataclass
class PreviewState:
    key: tuple[Any, ...]
    source: str
    state: str = "running"  # running | done | error
    phase: str = "scanning"  # scanning | planning
    scanned: int = 0
    probed: int = 0
    total: int = 0
    summary: dict[str, Any] | None = None
    error: str = ""
    started: float = field(default_factory=time.monotonic)


def preview_summary(plan: Plan) -> dict[str, Any]:
    """Counts, category bars, clash renames, plan-time problems (UI-C4)."""
    base = plan_summary(plan)
    counts = base["counts"]
    renames = [{"source": r["source"], "output": (r["output"] or "").rsplit("/", 1)[-1]} for r in base["renames"]]
    unsupported = [it.source_path for it in plan.items
                   if it.materialise and "UNSUPPORTED_FORMAT" in it.reasons]
    problems = []
    for p in base["problems"]:
        st = None
        for code in p["reasons"]:
            r = REASONS.get(code)
            if r and isinstance(r.status, Status):
                st = r.status
                break
        problems.append({**p, "status": st.value if st else ""})
    cats = sorted(counts["by_category"].items(), key=lambda kv: (-kv[1], kv[0]))
    biggest = max((n for _, n in cats), default=1) or 1
    return {
        "counts": counts,
        "categories": [{"key": k, "n": n, "pct": max(1.5, n / biggest * 100)} for k, n in cats],
        "renames": renames[:LIST_LIMIT],
        "renames_total": len(renames),
        "problems": problems[:LIST_LIMIT],
        "problems_total": len(problems),
        "unsupported": unsupported[:LIST_LIMIT],
        "unsupported_total": len(unsupported),
    }


class Previews:
    """One preview per workflow, run in a background thread so the page can show progress."""

    def __init__(self, engine: Engine, machine: Callable[[list[str]], Machine]) -> None:
        self.engine = engine
        self.machine = machine
        self._lock = threading.Lock()
        self._states: dict[str, PreviewState] = {}

    def get(self, wf_id: str) -> PreviewState | None:
        with self._lock:
            return self._states.get(wf_id)

    def start(self, wf_id: str, key: tuple[Any, ...], mode: Mode, source: str, output: str | None,
              settings: dict[str, Any]) -> PreviewState:
        with self._lock:
            cur = self._states.get(wf_id)
            if cur is not None and cur.key == key and cur.state == "running":
                return cur
            st = PreviewState(key=key, source=source)
            self._states[wf_id] = st
        threading.Thread(target=self._run, args=(st, mode, source, output, settings), daemon=True,
                         name=f"baleen-preview-{wf_id}").start()
        return st

    def _run(self, st: PreviewState, mode: Mode, source: str, output: str | None, settings: dict[str, Any]) -> None:
        try:
            errs = validate_roots(mode, source, output)
            if errs:
                raise RootError(errs[0])

            def on_scan(n: int) -> None:
                st.scanned = n

            entries = scan(source, on_progress=on_scan)
            st.scanned = len(entries)
            st.phase = "planning"
            st.total = len(entries)
            machine = self.machine([source, output or ""])
            b = resolve_budget(settings["app"], machine)

            def on_probe(done: int, total: int) -> None:
                st.probed, st.total = done, total

            planner = Planner(self.engine.make_probe_ctx(mode, settings), source, output, workers=b.B,
                              on_progress=on_probe)
            st.summary = preview_summary(planner.build(entries))
            st.state = "done"
        except (RootError, ScanError) as e:
            st.error = str(e)
            st.state = "error"
        except Exception as e:  # a preview never takes the server down
            log.exception("preview failed")
            st.error = f"The preview couldn't finish: {e.__class__.__name__}: {e}"
            st.state = "error"


# --------------------------------------------------------------------------- quit (UI-G4)


class QuitControl:
    """Quit: finish the files in progress, write the report, serve /stopped, then exit."""

    def __init__(self) -> None:
        self.requested = threading.Event()
        self.ready = threading.Event()  # the job (if any) is finished; /stopped may be shown
        self._exit: Callable[[], None] | None = None
        self._lock = threading.Lock()
        self._scheduled = False

    def bind(self, exit_fn: Callable[[], None]) -> None:
        self._exit = exit_fn

    def request(self, engine: Engine) -> None:
        """Blocking: called from a worker thread by POST /api/quit."""
        self.requested.set()
        if engine.running():
            engine.cancel()
            engine.wait()
        self.ready.set()
        # Fallback: exit even if the browser never asks for /stopped.
        self._schedule(20.0)

    def stopped_served(self) -> None:
        if self.ready.is_set():
            self._schedule(0.75)

    def _schedule(self, delay: float) -> None:
        with self._lock:
            t = threading.Timer(delay, self.exit_now)
            t.daemon = True
            t.start()
            self._scheduled = True

    def exit_now(self) -> None:
        if self._exit is not None:
            self._exit()


# --------------------------------------------------------------------------- context


@dataclass
class ServerContext:
    home: Home
    store: SettingsStore
    tools: Toolset
    engine: Engine
    port: int
    secrets: Secrets = field(default_factory=Secrets.generate)
    picker: str | None = None  # pickers.detect() result; None hides Browse
    machine: Callable[[list[str]], Machine] = field(default=lambda paths: Machine.detect(paths))
    version: str = ""
    previews: Previews = field(init=False)
    quit: QuitControl = field(default_factory=QuitControl)
    templates: Any = None
    _journals: OrderedDict[str, Journal] = field(default_factory=OrderedDict)
    _jlock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.previews = Previews(self.engine, self.machine)
        if not self.version:
            from .. import __version__

            self.version = __version__

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # ---- workflow values

    def folder_values(self, wf: Workflow) -> dict[str, str]:
        w = self.store.workflow()
        return {f.key: w.get(f.key, "") or "" for f in wf.folders}

    def option_values(self, wf: Workflow) -> dict[str, Any]:
        w = self.store.workflow()
        return {k: w.get(k) for k in wf.option_keys()}

    def preview_key(self, wf: Workflow) -> tuple[Any, ...]:
        """What a preview depends on: folders and options. A change marks it stale (W-04)."""
        folders = tuple(sorted(self.folder_values(wf).items()))
        opts = tuple(sorted((k, str(v)) for k, v in self.option_values(wf).items()))
        return (wf.id, folders, opts)

    # ---- journals of finished runs (UI-R8), a few kept open

    def journal_for(self, run_id: str, reports_dir: str) -> Journal | None:
        from ..runner import load_run_results

        with self._jlock:
            j = self._journals.get(run_id)
            if j is not None:
                self._journals.move_to_end(run_id)
                return j
        try:
            j, _rows = load_run_results(reports_dir, run_id)
        except Exception:
            log.exception("couldn't open the journal of run %s", run_id)
            return None
        if j is not None:
            with self._jlock:
                self._journals[run_id] = j
                while len(self._journals) > 8:
                    self._journals.popitem(last=False)
        return j

    def forget_journal(self, run_id: str) -> None:
        with self._jlock:
            self._journals.pop(run_id, None)
