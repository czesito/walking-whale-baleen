"""AC-11 on this machine: a smoke run from a freshly extracted bundle creates nothing outside the
bundle and output folders except spec §14.5's allowed list.

    python scripts/ac11_local.py dist/baleen-0.1.0-win-x64.zip --work %TEMP%/baleen-test/ac11

Steps:
1. Control pair: two profile snapshots with nothing in between, giving the background noise.
2. Snapshot, then extract the zip and run the bundle's own smoke test (doctor + convert with the
   launcher's environment, build_bundle.py --smoke-only). Then start `serve --no-browser`, open a
   session with its token, and quit it through POST /api/quit. Snapshot again.
3. Diff, minus the noise. Fail on any new path or registry entry with a tool-related name.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent


def snap(out: Path, excludes: list[str]) -> None:
    subprocess.run([sys.executable, str(HERE / "snapshot_profile.py"), "snap", str(out),
                    *[a for e in excludes for a in ("--exclude", e)]], check=True)


def serve_and_quit(bundle: Path, env: dict[str, str]) -> str:
    py = bundle / "runtime" / "python" / ("python.exe" if os.name == "nt" else "bin/python3")
    p = subprocess.Popen([str(py), "-m", "baleen", "serve", "--no-browser"], cwd=bundle, env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
    url = ""
    t0 = time.time()
    assert p.stdout is not None
    while time.time() - t0 < 60:
        line = p.stdout.readline()
        m = re.search(r"(http://127\.0\.0\.1:\d+)/auth\?t=([\w-]+)", line or "")
        if m:
            url = m.group(0)
            base = m.group(1)
            break
    if not url:
        p.kill()
        return "serve did not print its URL"
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
    opener.open(url, timeout=30).read()
    opener.open(base + "/convert", timeout=30).read()
    req = urllib.request.Request(base + "/api/quit", data=b"", method="POST",
                                 headers={"Origin": base, "Referer": base + "/convert"})
    try:
        opener.open(req, timeout=30).read()
    except Exception:
        pass
    try:
        p.wait(timeout=60)
    except subprocess.TimeoutExpired:
        p.kill()
        return "serve did not exit after /api/quit"
    return f"serve exit code {p.returncode}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("zip")
    ap.add_argument("--work", required=True)
    ns = ap.parse_args()
    work = Path(ns.work)
    work.mkdir(parents=True, exist_ok=True)
    tmp_root = str(Path(os.environ.get("TEMP", "/tmp")) / "baleen-test")
    ex = [tmp_root]
    snap(work / "c1.json", ex)
    snap(work / "c2.json", ex)
    subprocess.run([sys.executable, str(HERE / "snapshot_profile.py"), "diff", str(work / "c1.json"),
                    str(work / "c2.json")], check=False, stdout=subprocess.DEVNULL)
    noise = work / "c2.diff.json"

    snap(work / "before.json", ex)
    dest = work / "Baleen AC-11 test"
    with zipfile.ZipFile(ns.zip) as z:
        z.extractall(dest)
    bundle = next(p for p in dest.iterdir() if p.is_dir() and p.name.startswith("Baleen-"))
    smoke = subprocess.run([sys.executable, str(HERE / "build_bundle.py"), "--smoke-only", str(bundle),
                            "--work", str(work / "smoke-work")], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    sys.path.insert(0, str(HERE))
    import build_bundle as bb

    env = bb.smoke_env(bundle)
    served = serve_and_quit(bundle, env)
    snap(work / "after.json", ex)
    d = subprocess.run([sys.executable, str(HERE / "snapshot_profile.py"), "diff", str(work / "before.json"),
                        str(work / "after.json"), "--baseline", str(noise), "--fail"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    result = {
        "smoke_exit": smoke.returncode, "smoke_tail": smoke.stdout[-1500:] + smoke.stderr[-500:],
        "serve": served, "diff_exit": d.returncode, "diff": d.stdout[-4000:],
    }
    (work / "ac11-result.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(json.dumps(result, indent=1))
    return 0 if smoke.returncode == 0 and d.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
