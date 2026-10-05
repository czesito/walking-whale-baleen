"""Benchmarks for AC-13, AC-15 and AC-16 (Windows: exact CPU accounting via a Job Object).

    python scripts/bench.py SOURCE --out RESULTS.json [--profiles gentle,balanced,maximum,8,1]
                            [--low-priority both|on|off] [--repeat 1] [--check]

Each run starts `python -m baleen convert SOURCE <tmp-output> --processor P` in its own Job
Object (proc.spawn). Every process Baleen starts (soffice, ffmpeg, java) is in a nested job,
so the job's accounting includes all of them, even after they exit. Sampled every 0.5 s:
CPU seconds -> busiest 30 s window in cores (AC-15: <= B + 0.5).
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from baleen import proc  # noqa: E402
from baleen.scheduler import Machine, resolve_budget  # noqa: E402


class BASIC_ACCOUNTING(ctypes.Structure):  # noqa: N801
    _fields_ = [("TotalUserTime", ctypes.c_longlong), ("TotalKernelTime", ctypes.c_longlong),
                ("ThisPeriodTotalUserTime", ctypes.c_longlong), ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD)]


def job_cpu_seconds(job: int) -> tuple[float, int]:
    info = BASIC_ACCOUNTING()
    k32 = ctypes.WinDLL("kernel32")
    k32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                              ctypes.c_void_p]
    ok = k32.QueryInformationJobObject(job, 1, ctypes.byref(info), ctypes.sizeof(info), None)
    if not ok:
        return 0.0, 0
    return (info.TotalUserTime + info.TotalKernelTime) / 1e7, int(info.TotalProcesses)


def job_peak_memory_mb(job: int) -> float:
    w = proc._win  # type: ignore[attr-defined]
    ext = w.EXT()
    k32 = ctypes.WinDLL("kernel32")
    k32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                              ctypes.c_void_p]
    if k32.QueryInformationJobObject(job, 9, ctypes.byref(ext), ctypes.sizeof(ext), None):
        return ext.PeakJobMemoryUsed / (1 << 20)
    return 0.0


def busiest_window(samples: list[tuple[float, float]], window: float = 30.0) -> float:
    """Max average cores over any `window` seconds (linear interpolation between samples)."""
    if len(samples) < 2:
        return 0.0
    best = 0.0
    j = 0
    for i in range(len(samples)):
        t0, c0 = samples[i]
        while j < len(samples) - 1 and samples[j][0] - t0 < window:
            j += 1
        t1, c1 = samples[j]
        span = t1 - t0
        if span >= min(window, samples[-1][0] - samples[0][0]) * 0.999 and span > 0:
            best = max(best, (c1 - c0) / span)
    return best


def run_once(source: str, profile: str, low_priority: bool, workdir: Path, mode: str) -> dict:
    out = workdir / f"out-{profile}-{'lp' if low_priority else 'np'}-{int(time.time())}"
    home = workdir / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["BALEEN_HOME"] = str(home)
    args = [sys.executable, "-m", "baleen"]
    args += ["check", source] if mode == "check" else ["convert", source, str(out)]
    args += ["--processor", profile, "--no-keep-awake", "-q"]
    if not low_priority:
        args.append("--no-low-priority")
    t0 = time.monotonic()
    child = proc.spawn(args, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    samples: list[tuple[float, float]] = []
    while child.popen.poll() is None:
        cpu, _n = job_cpu_seconds(child.job) if child.job else (0.0, 0)
        samples.append((time.monotonic() - t0, cpu))
        time.sleep(0.5)
    wall = time.monotonic() - t0
    cpu, nproc = job_cpu_seconds(child.job) if child.job else (0.0, 0)
    samples.append((wall, cpu))
    peak_mem = job_peak_memory_mb(child.job) if child.job else 0.0
    err = child.popen.stderr.read().decode("utf-8", "replace") if child.popen.stderr else ""
    rc = child.popen.returncode
    proc._forget(child)  # type: ignore[attr-defined]
    run_json = None
    rdir = (out / "_baleen") if mode != "check" else (home / "data" / "reports")
    for p in sorted(rdir.glob("run-*.json")) if rdir.exists() else []:
        run_json = json.loads(p.read_text(encoding="utf-8"))
    res = {
        "profile": profile, "low_priority": low_priority, "mode": mode, "exit_code": rc, "wall_s": round(wall, 2),
        "cpu_s": round(cpu, 2), "avg_cores": round(cpu / wall, 2) if wall else 0,
        "busiest_30s_cores": round(busiest_window(samples), 2), "processes": nproc,
        "peak_job_memory_mb": round(peak_mem), "stderr_tail": err[-400:],
    }
    if run_json:
        r = run_json.get("resources") or {}
        res["resolved"] = r.get("resolved")
        res["peak_reserved"] = r.get("peak")
        res["counts"] = (run_json.get("counts") or {}).get("by_status")
    keep = workdir / "kept" / f"{profile}-{'lp' if low_priority else 'np'}-{mode}"
    if rdir.exists():
        keep.mkdir(parents=True, exist_ok=True)
        for f in rdir.glob("*"):
            if f.suffix in (".json", ".csv", ".sqlite"):
                shutil.copy2(f, keep / f.name)
        res["kept"] = str(keep)
    shutil.rmtree(out, ignore_errors=True)
    return res


def main() -> int:
    if sys.platform != "win32":
        print("bench.py uses Windows Job Object accounting; run it on Windows.")
        return 2
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("--out", required=True)
    ap.add_argument("--profiles", default="gentle,balanced,maximum,8,1")
    ap.add_argument("--low-priority", default="on", choices=["on", "off", "both"])
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--mode", default="convert", choices=["convert", "check"])
    ap.add_argument("--workdir", default=str(Path(tempfile.gettempdir()) / "baleen-test" / "bench"))
    ns = ap.parse_args()
    workdir = Path(ns.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    machine = Machine.detect([ns.source])
    lps = {"on": [True], "off": [False], "both": [True, False]}[ns.low_priority]
    results = {"machine": machine.to_dict(), "cpu": platform.processor(), "platform": platform.platform(),
               "source": ns.source, "runs": []}
    for rep in range(ns.repeat):
        for prof in ns.profiles.split(","):
            for lp in lps:
                prefs = ({"processor_use": prof} if not prof.isdigit()
                         else {"processor_use": "custom", "processor_cores": int(prof)})
                b = resolve_budget(prefs, machine)
                print(f"[{rep + 1}/{ns.repeat}] {prof:9} low_priority={'on ' if lp else 'off'} B={b.B} ...",
                      end="", flush=True)
                r = run_once(ns.source, prof, lp, workdir, ns.mode)
                r["B"] = b.B
                r["ac15_ok"] = r["busiest_30s_cores"] <= b.B + 0.5
                results["runs"].append(r)
                print(f" {r['wall_s']:8.1f} s · {r['avg_cores']:5.2f} avg cores · busiest 30 s "
                      f"{r['busiest_30s_cores']:5.2f} (limit {b.B + 0.5}) · exit {r['exit_code']}")
                Path(ns.out).write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
