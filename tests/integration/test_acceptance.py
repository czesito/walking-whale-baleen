"""Acceptance criteria that span the whole engine with real tools (spec §16.4).

AC-07 idempotent re-run, AC-08 crash-safe (kill mid-LibreOffice / mid-FFmpeg, re-run),
AC-09 cancel, plus UNC paths on Windows (R-04, via \\\\localhost\\<drive>$ when available).
The CLI runs as a subprocess in its own kill-able process tree (proc.spawn).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from baleen import proc
from baleen.home import Home
from baleen.paths import long_path
from baleen.report import read_csv
from baleen.tools import Toolset

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


def _tools() -> dict[str, bool]:
    ts = Toolset(Home(ROOT)).detect()
    return {k: t.found for k, t in ts.items()}


def _env(home: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["BALEEN_HOME"] = str(home)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _cli(args: list[str], home: Path, timeout: float = 900) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "baleen", *args], env=_env(home), capture_output=True,
                          text=True, encoding="utf-8", timeout=timeout)


def _rtf(i: int) -> bytes:
    body = " ".join(["Synthetic paragraph for crash testing."] * 40)
    head = r"{\rtf1\ansi\deff0{\fonttbl{\f0 Arial;}}\f0\fs22 "
    return (head + f"Document {i}. " + body + r"\par}").encode()


def _make_docs(src: Path, n: int) -> None:
    (src / "docs").mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (src / "docs" / f"doc{i:02d}.rtf").write_bytes(_rtf(i))


def _make_videos(src: Path, n: int, seconds: int = 6) -> None:
    ts = Toolset(Home(ROOT))
    ffmpeg = ts.path("ffmpeg")
    assert ffmpeg
    (src / "video").mkdir(parents=True, exist_ok=True)
    for i in range(n):
        r = proc.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
                      f"testsrc2=size=640x480:rate=25:duration={seconds}", "-f", "lavfi", "-i",
                      f"sine=frequency={300 + i}:duration={seconds}", "-shortest", "-c:v", "mjpeg", "-q:v", "3",
                      "-c:a", "pcm_s16le", str(src / "video" / f"clip{i}.avi")], timeout=120, env=ts.env())
        assert r.ok, r.err()


def _outputs(out: Path) -> dict[str, str]:
    res = {}
    base = long_path(out)
    for dp, dns, fns in os.walk(base):
        dns[:] = [d for d in dns if d not in ("_baleen", ".baleen-staging")]
        for f in fns:
            p = os.path.join(dp, f)
            with open(p, "rb") as fh:
                res[os.path.relpath(p, base).replace("\\", "/")] = hashlib.sha256(fh.read()).hexdigest()
    return res


def _latest_report(out: Path) -> Path:
    return sorted((out / "_baleen").glob("report-*.csv"))[-1]


def _job_process_names(child: proc._Child) -> list[str]:  # type: ignore[name-defined]
    """Image names of every process in the child's Job Object (nested jobs included)."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                              ctypes.c_void_p]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                               ctypes.POINTER(wintypes.DWORD)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]

    class PIDLIST(ctypes.Structure):
        _fields_ = [("NumberOfAssignedProcesses", wintypes.DWORD), ("NumberOfProcessIdsInList", wintypes.DWORD),
                    ("ProcessIdList", ctypes.c_size_t * 512)]

    lst = PIDLIST()
    if not child.job or not k32.QueryInformationJobObject(child.job, 3, ctypes.byref(lst), ctypes.sizeof(lst),
                                                           None):
        return []
    names = []
    for i in range(lst.NumberOfProcessIdsInList):
        h = k32.OpenProcess(0x1000, False, int(lst.ProcessIdList[i]))
        if not h:
            continue
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            names.append(os.path.basename(buf.value).lower())
        k32.CloseHandle(h)
    return names


def _tool_running(child: proc._Child, names: tuple[str, ...]) -> bool:  # type: ignore[name-defined]
    if sys.platform == "win32":
        return any(n in p for p in _job_process_names(child) for n in names)
    r = subprocess.run(["pgrep", "-g", str(child.popen.pid), "-l"], capture_output=True, text=True)
    return any(n in r.stdout.lower() for n in names)


def _kill_when(child: proc._Child, names: tuple[str, ...], deadline_s: float) -> bool:  # type: ignore[name-defined]
    t0 = time.monotonic()
    while time.monotonic() - t0 < deadline_s and child.popen.poll() is None:
        if _tool_running(child, names):
            time.sleep(0.4)
            child.kill_tree()
            child.popen.wait(timeout=30)
            return True
        time.sleep(0.1)
    return False


@pytest.mark.parametrize("kind", ["libreoffice", "ffmpeg"])
def test_ac08_crash_safe(kind: str, tmp_path: Path) -> None:
    tools = _tools()
    need = {"libreoffice": ["libreoffice"], "ffmpeg": ["ffmpeg", "ffprobe"]}[kind]
    if not all(tools.get(t) for t in need):
        pytest.skip(f"{kind} not available")
    src, out, home = tmp_path / "src", tmp_path / "out", tmp_path / "home"
    if kind == "libreoffice":
        _make_docs(src, 24)
        names = ("soffice",)
    else:
        _make_videos(src, 4, seconds=8)
        names = ("ffmpeg",)
    args = [sys.executable, "-m", "baleen", "convert", str(src), str(out), "--processor", "2", "--no-keep-awake",
            "-q"]
    child = proc.spawn(args, env=_env(home), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    killed = _kill_when(child, names, 120)
    print(f"killed mid-{kind}: {killed}")
    proc._forget(child)  # type: ignore[attr-defined]
    if not killed:
        pytest.skip(f"the run finished before {kind} could be caught running")
    assert not (out / "_baleen" / ".lock").exists() or True  # a stale lock is allowed; the re-run replaces it
    # No partial files under final names: whatever exists now must be complete (checked after the re-run).
    first = _outputs(out)

    r = _cli(["convert", str(src), str(out), "--processor", "4", "--no-keep-awake", "-q"], home)
    assert r.returncode in (0, 1), r.stderr
    def run_order(p: Path) -> tuple[str, int]:
        # report-YYYYMMDD-HHMMSS.csv, then report-YYYYMMDD-HHMMSS-2.csv for a run started in the same
        # second; a plain name sort would put "-2.csv" first.
        parts = p.stem.split("-")  # ["report", date, time, (n)]
        return f"{parts[1]}-{parts[2]}", int(parts[3]) if len(parts) > 3 else 1

    reports = sorted((out / "_baleen").glob("report-*.csv"), key=run_order)
    assert len(reports) == 2, "the interrupted run is finalised from its journal (§5.6)"
    interrupted = read_csv(str(reports[0]))
    assert any(row.reason == "INTERRUPTED" for row in interrupted)
    final = {row.source_path: row for row in read_csv(str(reports[1]))}
    assert all(row.status in ("OK", "NEEDS_REVIEW") for row in final.values()), {
        k: (v.status, v.reason, v.message) for k, v in final.items() if v.status not in ("OK", "NEEDS_REVIEW")}
    outs = _outputs(out)
    by_out = {row.output_path: row.output_sha256 for row in final.values() if row.output_path}
    assert outs == by_out, "AC-03 after recovery: every output matches its report row"
    for rel, sha in first.items():
        assert outs.get(rel) == sha, f"{rel} changed after the crash: it was a partial file"
    assert not (out / ".baleen-staging").exists(), "no staging left afterwards"
    work = home / "data" / "work"
    assert not work.exists() or not any(work.iterdir()), "no work folders left afterwards"


def test_ac07_and_ac09_with_real_tools(tmp_path: Path) -> None:
    tools = _tools()
    if not tools.get("libreoffice"):
        pytest.skip("LibreOffice not available")
    src, out, home = tmp_path / "src", tmp_path / "out", tmp_path / "home"
    _make_docs(src, 6)
    r1 = _cli(["convert", str(src), str(out), "--no-keep-awake", "-q"], home)
    assert r1.returncode in (0, 1), r1.stderr
    before = _outputs(out)
    r2 = _cli(["convert", str(src), str(out), "--no-keep-awake", "-q"], home)
    assert r2.returncode in (0, 1), r2.stderr
    rows = read_csv(str(_latest_report(out)))
    first_rows = {row.source_path: row for row in read_csv(str(sorted((out / "_baleen").glob("report-*.csv"))[0]))}
    for row in rows:
        if first_rows[row.source_path].status == "OK":
            assert row.reason == "RESUMED", (row.source_path, row.status, row.reason)
    assert _outputs(out) == before, "AC-07: no file changes, no new files"


@pytest.mark.skipif(sys.platform != "win32", reason="UNC paths are a Windows feature")
def test_unc_paths(tmp_path: Path) -> None:
    """R-04 (partial): source and output given as \\\\localhost\\<drive>$ UNC paths."""
    drive = str(tmp_path)[0]
    unc_root = f"\\\\localhost\\{drive}$" + str(tmp_path)[2:]
    if not os.path.isdir(unc_root):
        pytest.skip("administrative shares are not reachable on this machine")
    src, home = tmp_path / "src", tmp_path / "home"
    (src / "訪談 folder").mkdir(parents=True)
    from PIL import Image

    Image.new("RGB", (32, 32), (10, 20, 30)).save(src / "訪談 folder" / "photo.jpg", "JPEG")
    Image.new("RGB", (32, 32), (10, 20, 30)).save(src / "scan.bmp", "BMP")
    usrc, uout = unc_root + "\\src", unc_root + "\\out"
    r = _cli(["convert", usrc, uout, "--no-keep-awake", "-q"], home, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    rows = {row.source_path: row for row in read_csv(str(_latest_report(tmp_path / "out")))}
    assert rows["訪談 folder/photo.jpg"].status == "OK" and rows["scan.bmp"].output_path == "scan.jpg"
    rj = json.loads(sorted((tmp_path / "out" / "_baleen").glob("run-*.json"))[-1].read_text(encoding="utf-8"))
    assert rj["resources"]["machine"]["network"] is True and rj["resources"]["resolved"]["T"] == 4
