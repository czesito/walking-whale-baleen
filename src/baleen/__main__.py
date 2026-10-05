"""Command line (spec Appendix A): serve | convert | check | doctor.

Exit codes: 0 all OK/IGNORED · 1 any NEEDS_REVIEW/UNSUPPORTED/SKIPPED · 2 any FAILED ·
3 fatal (bad arguments, lock held, invalid roots).
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import __version__

EXIT_OK, EXIT_REVIEW, EXIT_FAILED, EXIT_FATAL = 0, 1, 2, 3


def _resource_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("resource use (overrides Settings for this run)")
    g.add_argument("--processor", metavar="gentle|balanced|maximum|N")
    g.add_argument("--memory", metavar="auto|GB")
    g.add_argument("--transfers", metavar="auto|N")
    g.add_argument("--work-dir", metavar="PATH")
    g.add_argument("--no-low-priority", action="store_true")
    g.add_argument("--no-keep-awake", action="store_true")


def resource_overrides(ns: argparse.Namespace) -> dict[str, Any]:
    """Translate CLI resource flags into app-preference changes (validated by settings)."""
    ch: dict[str, Any] = {}
    if ns.processor:
        v = ns.processor.strip().lower()
        if v in ("gentle", "balanced", "maximum"):
            ch["processor_use"] = v
        elif v.isdigit() and int(v) >= 1:
            ch["processor_use"] = "custom"
            ch["processor_cores"] = int(v)
        else:
            raise ValueError("--processor expects gentle, balanced, maximum or a number of cores")
    if ns.memory:
        v = ns.memory.strip().lower()
        if v == "auto":
            ch["memory_limit"] = "auto"
        elif v.isdigit() and int(v) >= 1:
            ch["memory_limit"] = "custom"
            ch["memory_gb"] = int(v)
        else:
            raise ValueError("--memory expects auto or a number of GB")
    if ns.transfers:
        v = ns.transfers.strip().lower()
        if v == "auto":
            ch["transfer_slots"] = "auto"
        elif v.isdigit() and 1 <= int(v) <= 16:
            ch["transfer_slots"] = "custom"
            ch["transfer_count"] = int(v)
        else:
            raise ValueError("--transfers expects auto or a number from 1 to 16")
    if ns.work_dir:
        ch["work_dir"] = ns.work_dir
    if ns.no_low_priority:
        ch["low_priority"] = False
    if ns.no_keep_awake:
        ch["keep_awake"] = False
    return ch


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="baleen", description="Baleen by Walking Whale")
    p.add_argument("--version", action="version", version=f"Baleen {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="start the local web interface")
    s.add_argument("--port", type=int)
    s.add_argument("--no-browser", action="store_true")

    c = sub.add_parser("convert", help="convert a folder tree into archival formats")
    c.add_argument("source")
    c.add_argument("output")
    c.add_argument("--set", action="append", default=[], metavar="key=value", dest="sets")
    c.add_argument("--quiet", "-q", action="store_true")
    _resource_args(c)

    k = sub.add_parser("check", help="verify a folder read-only")
    k.add_argument("folder")
    k.add_argument("--report", metavar="PATH", help="also copy the report CSV here")
    k.add_argument("--quiet", "-q", action="store_true")
    _resource_args(k)

    d = sub.add_parser("doctor", help="show tool paths and versions")
    d.add_argument("--json", action="store_true")
    return p


def cmd_doctor(ns: argparse.Namespace) -> int:
    from .home import Home
    from .scheduler import Machine
    from .tools import Toolset, library_versions

    home = Home.current()
    tools = Toolset(home).detect(force=True)
    machine = Machine.detect([])
    if ns.json:
        print(json.dumps({
            "baleen_version": __version__,
            "home": str(home.root),
            "runtime": str(home.runtime_dir),
            "machine": machine.to_dict(),
            "tools": {k: t.to_dict() for k, t in tools.items()},
            "libraries": library_versions(),
        }, indent=2, ensure_ascii=False))
    else:
        print(f"Baleen {__version__}")
        print(f"  home     {home.root}")
        print(f"  runtime  {home.runtime_dir}")
        print(f"  machine  {machine.cores} logical processors · {machine.ram_gb} GB memory")
        for t in tools.values():
            mark = "ok " if t.found else "MISSING"
            print(f"  {mark:7} {t.name:13} {t.version or '-':24} {t.source:8} {t.path or t.error}")
        for k, v in library_versions().items():
            print(f"  ok      {k:13} {v.get('version', '')}")
    return EXIT_OK if all(t.found for t in tools.values()) else EXIT_REVIEW


def cmd_serve(ns: argparse.Namespace) -> int:
    from .server.app import serve

    return serve(port=ns.port, open_browser=not ns.no_browser)


def cmd_run(ns: argparse.Namespace) -> int:
    from .cli_run import run_cli

    return run_cli(ns)


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
            except Exception:
                pass
    parser = build_parser()
    ns = parser.parse_args(argv)
    try:
        if ns.cmd == "doctor":
            return cmd_doctor(ns)
        if ns.cmd == "serve":
            return cmd_serve(ns)
        return cmd_run(ns)
    except KeyboardInterrupt:
        return EXIT_FATAL


if __name__ == "__main__":
    sys.exit(main())
