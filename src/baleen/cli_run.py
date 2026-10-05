"""`baleen convert` and `baleen check` (spec Appendix A)."""

from __future__ import annotations

import argparse
import copy
import os
import sys
import threading
import time

from . import logs
from .home import Home
from .model import Mode, Status
from .runner import Engine, JobState, RootError, RunSpec
from .settings import SettingsStore, apply_overrides
from .tools import Toolset

EXIT_OK, EXIT_REVIEW, EXIT_FAILED, EXIT_FATAL = 0, 1, 2, 3


def exit_code(counts: dict[str, int]) -> int:
    if counts.get(Status.FAILED.value):
        return EXIT_FAILED
    if any(counts.get(s.value) for s in (Status.NEEDS_REVIEW, Status.UNSUPPORTED, Status.SKIPPED)):
        return EXIT_REVIEW
    return EXIT_OK


def run_cli(ns: argparse.Namespace) -> int:
    from .__main__ import resource_overrides

    home = Home.current()
    home.ensure()
    logs.setup(home)
    store = SettingsStore.open(home.settings_path)
    for w in store.warnings:
        print(f"warning: {w}", file=sys.stderr)
    settings = copy.deepcopy(store.snapshot())
    try:
        if ns.cmd == "convert":
            settings = apply_overrides(settings, ns.sets)
        for k, v in resource_overrides(ns).items():
            settings = apply_overrides(settings, [f"{k}={v}"])
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_FATAL
    if ns.cmd == "convert":
        source, output = os.path.abspath(ns.source), os.path.abspath(ns.output)
        spec = RunSpec(Mode.CONVERT, source, output, settings)
    else:
        source = os.path.abspath(ns.folder)
        spec = RunSpec(Mode.CHECK, source, None, settings,
                       report_copy=os.path.abspath(ns.report) if ns.report else None)
    tools = Toolset(home)
    engine = Engine(home, store, tools)
    engine.recover_all()
    frozen = settings["app"]
    try:
        job = engine.start(spec, background=True, prefs=lambda: frozen)
    except RootError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_FATAL
    quiet = getattr(ns, "quiet", False)
    stop = threading.Event()

    def ticker() -> None:
        last = ""
        while not stop.wait(2.0):
            p = job.progress()
            line = (f"{p['state']}: {p['done']:,} of {p['total']:,} ({p['percent']}%)"
                    if p["state"] != "preparing" else f"preparing: {p['scanned']:,} files scanned")
            if line != last and not quiet:
                print(line, file=sys.stderr, flush=True)
                last = line

    t = threading.Thread(target=ticker, daemon=True)
    t.start()
    try:
        while not job.done_event.wait(0.5):
            pass
    except KeyboardInterrupt:
        print("Cancelling: finishing the files in progress…", file=sys.stderr)
        engine.cancel()
        job.done_event.wait()
    stop.set()
    time.sleep(0)
    if job.exit_error and job.journal is None:
        print(f"error: {job.exit_error}", file=sys.stderr)
        return EXIT_FATAL
    for w in job.warnings:
        print(f"warning: {w}", file=sys.stderr)
    if not quiet:
        counts = ", ".join(f"{k} {v:,}" for k, v in sorted(job.counts.items()))
        print(f"Run {job.id} {job.state.value}: {job.total:,} items · {counts}")
        print(f"Report: {job.report_path()}")
        if job.stop_reason:
            print(f"Stopped: {job.stop_reason}", file=sys.stderr)
    code = exit_code(job.counts)
    if job.state == JobState.STOPPED and code == EXIT_OK:
        code = EXIT_REVIEW
    return code
