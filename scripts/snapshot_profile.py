"""AC-11 clean-uninstall check: snapshot the user profile before and after a smoke run.

    python scripts/snapshot_profile.py snap BEFORE.json [--exclude PATH ...]
    ... run Baleen (smoke) ...
    python scripts/snapshot_profile.py snap AFTER.json  [--exclude PATH ...]
    python scripts/snapshot_profile.py diff BEFORE.json AFTER.json [--allow REGEX ...] [--fail]

Windows roots: %APPDATA%, %LOCALAPPDATA%, %TEMP%, %USERPROFILE% (top level only) and the
registry under HKCU\\Software. macOS roots: ~/Library (Preferences, Caches, Application
Support, Saved Application State, Logs), $TMPDIR and the home folder (top level only).

Background noise from other programs is expected. Run a control pair (two snapshots with no
Baleen run between them) and pass its new paths with --baseline to filter that noise out.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path


def _roots() -> list[tuple[str, int]]:
    """(path, max depth) pairs; depth -1 = unlimited."""
    home = Path.home()
    if sys.platform == "win32":
        r = [(os.environ.get("APPDATA", ""), -1), (os.environ.get("LOCALAPPDATA", ""), -1),
             (os.environ.get("TEMP", ""), -1), (str(home), 1)]
    elif sys.platform == "darwin":
        lib = home / "Library"
        r = [(str(lib / d), -1) for d in ("Preferences", "Caches", "Application Support",
                                           "Saved Application State", "Logs", "HTTPStorages", "WebKit")]
        r += [(os.environ.get("TMPDIR", "/tmp"), -1), (str(home), 1), (str(lib), 1)]
    else:
        r = [(str(home / ".config"), -1), (str(home / ".cache"), -1), (str(home / ".local"), -1),
             ("/tmp", -1), (str(home), 1)]
    return [(p, d) for p, d in r if p and os.path.isdir(p)]


def _walk(root: str, depth: int, excludes: list[str]) -> dict[str, list[float | int]]:
    out: dict[str, list[float | int]] = {}
    root_depth = root.rstrip("\\/").count(os.sep)
    ex = [os.path.normcase(os.path.abspath(e)) for e in excludes]
    for dp, dns, fns in os.walk(root, topdown=True, onerror=lambda e: None):
        ndp = os.path.normcase(os.path.abspath(dp))
        if any(ndp == e or ndp.startswith(e + os.sep) for e in ex):
            dns[:] = []
            continue
        level = dp.rstrip("\\/").count(os.sep) - root_depth
        if depth >= 0 and level >= depth:
            dns[:] = []
        for n in dns:
            out[os.path.join(dp, n) + os.sep] = [0, 0]
        for n in fns:
            p = os.path.join(dp, n)
            try:
                st = os.lstat(p)
                out[p] = [st.st_size, st.st_mtime]
            except OSError:
                continue
    return out


def _registry() -> dict[str, str]:
    if sys.platform != "win32":
        return {}
    import winreg

    out: dict[str, str] = {}

    def walk(key, path: str) -> None:  # noqa: ANN001
        try:
            i = 0
            while True:
                name, data, typ = winreg.EnumValue(key, i)
                h = hashlib.sha1(repr((data, typ)).encode("utf-8", "replace")).hexdigest()[:12]
                out[f"{path}\\[{name}]"] = h
                i += 1
        except OSError:
            pass
        try:
            i = 0
            while True:
                sub = winreg.EnumKey(key, i)
                i += 1
                try:
                    with winreg.OpenKey(key, sub) as k:
                        out[f"{path}\\{sub}\\"] = ""
                        walk(k, f"{path}\\{sub}")
                except OSError:
                    continue
        except OSError:
            pass

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Software") as k:
        walk(k, "HKCU\\Software")
    return out


def snap(path: str, excludes: list[str]) -> None:
    files: dict[str, list[float | int]] = {}
    for root, depth in _roots():
        files.update(_walk(root, depth, excludes))
    data = {"platform": sys.platform, "files": files, "registry": _registry()}
    Path(path).write_text(json.dumps(data), encoding="utf-8")
    print(f"{path}: {len(files):,} paths, {len(data['registry']):,} registry entries")


def diff(before: str, after: str, allow: list[str], baseline: str | None, fail: bool) -> int:
    a = json.loads(Path(before).read_text(encoding="utf-8"))
    b = json.loads(Path(after).read_text(encoding="utf-8"))
    noise: set[str] = set()
    if baseline:
        noise = set(json.loads(Path(baseline).read_text(encoding="utf-8")).get("new", []))
    pats = [re.compile(p, re.I) for p in allow]
    new_files = sorted(set(b["files"]) - set(a["files"]))
    changed = sorted(p for p in set(a["files"]) & set(b["files"])
                     if not p.endswith(os.sep) and a["files"][p] != b["files"][p])
    new_reg = sorted(set(b["registry"]) - set(a["registry"]))
    changed_reg = sorted(k for k in set(a["registry"]) & set(b["registry"]) if a["registry"][k] != b["registry"][k])

    def keep(p: str) -> bool:
        return p not in noise and not any(x.search(p) for x in pats)

    report = {
        "new": [p for p in new_files if keep(p)],
        "changed": [p for p in changed if keep(p)],
        "new_registry": [k for k in new_reg if keep(k)],
        "changed_registry": [k for k in changed_reg if keep(k)],
    }
    for k, v in report.items():
        print(f"== {k}: {len(v)}")
        for p in v[:200]:
            print("  " + p)
    out = Path(after).with_suffix(".diff.json")
    out.write_text(json.dumps(report, indent=1), encoding="utf-8")
    suspicious = [p for p in report["new"] + report["new_registry"]
                  if re.search(r"baleen|libreoffice|soffice|java|verapdf|ffmpeg|python|hsperf|\.java", p, re.I)]
    print(f"== suspicious (tool-related names): {len(suspicious)}")
    for p in suspicious:
        print("  " + p)
    return 1 if (fail and suspicious) else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snap")
    s.add_argument("out")
    s.add_argument("--exclude", action="append", default=[])
    d = sub.add_parser("diff")
    d.add_argument("before")
    d.add_argument("after")
    d.add_argument("--allow", action="append", default=[])
    d.add_argument("--baseline")
    d.add_argument("--fail", action="store_true")
    ns = ap.parse_args()
    if ns.cmd == "snap":
        snap(ns.out, ns.exclude)
        return 0
    return diff(ns.before, ns.after, ns.allow, ns.baseline, ns.fail)


if __name__ == "__main__":
    sys.exit(main())
