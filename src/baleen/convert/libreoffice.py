"""LibreOffice batches for the Documents lane (spec §5.5, §6.2, DR-35).

- Each Documents-lane instance uses its own profile, data/lo-profile-<k>, initialised on first
  use with Baleen's hardening settings (below). A profile is held by one instance at a time,
  also across Baleen processes that share a BALEEN_HOME (an OS file lock per profile).
- One `soffice --convert-to` call converts up to 8 files that share an import filter, export
  filter and PDF/A level (the batch key). Inputs in one call have unique stems (compared
  case-insensitively), so every output maps back to its input; jobs whose stems collide go
  into separate calls.
- Batch timeout 60 s + 30 s per file. A file the batch did not convert (the batch failed,
  crashed or timed out) is retried alone with the per-file limit (lo_timeout_s, 300 s), so one
  bad file cannot sink its neighbours (R-12). A PDF counts as produced only when it is complete
  (`%PDF-` header and a final `%%EOF`), so a file cut off by a kill is never used.
- Arguments are a list (no shell); on timeout the whole process tree is killed (proc.py, SEC-8)
  -> TIMEOUT. No output or an error -> CONVERSION_ERROR.

Profile hardening (P8, SEC-9, R-05). Measured on LibreOffice 26.8 (Windows): with a fresh
default profile, headless conversion fetches remote images linked from DOCX (external image
relationships) and from HTML <img>. `BlockUntrustedRefererLinks` stops that: links that come
from a document outside the trusted locations (there are none) are not loaded. The profile
also disables macros, active content (OLE/DDE), link updates, lock files, backups, the crash
reporter and the update check, so a conversion never prompts, blocks or phones home.
"""

from __future__ import annotations

import contextlib
import itertools
import os
import shutil
import sys
import threading
import xml.etree.ElementTree as ET  # noqa: S405 - only our own profile file is parsed
from pathlib import Path
from typing import IO

from .. import proc
from ..paths import long_path
from ..scheduler import TaskContext
from .base import LoJob, LoResult, RunContext

PDFA_VERSION = {"1b": "1", "2b": "2", "3b": "3"}
BATCH_BASE_S = 60
BATCH_PER_FILE_S = 30
MAX_BATCH = 8  # DR-35
INIT_TIMEOUT_S = 180

_OOR = "http://openoffice.org/2001/registry"
_XS = "http://www.w3.org/2001/XMLSchema"
_XSI = "http://www.w3.org/2001/XMLSchema-instance"

# (item path, property, value). Written to <profile>/user/registrymodifications.xcu.
PROFILE_SETTINGS: tuple[tuple[str, str, str], ...] = (
    # R-05 / P8: never load images or other links referenced by a document.
    ("/org.openoffice.Office.Common/Security/Scripting", "BlockUntrustedRefererLinks", "true"),
    # Documents are untrusted input (SEC-10): no macros, no OLE/DDE activation.
    ("/org.openoffice.Office.Common/Security/Scripting", "DisableMacrosExecution", "true"),
    ("/org.openoffice.Office.Common/Security/Scripting", "MacroSecurityLevel", "3"),
    ("/org.openoffice.Office.Common/Security/Scripting", "DisableActiveContent", "true"),
    # Never update links on load (Writer: 0 = never; Calc: 1 = never).
    ("/org.openoffice.Office.Writer/Content/Update", "Link", "0"),
    ("/org.openoffice.Office.Calc/Content/Update", "Link", "1"),
    # No lock files or backups (P1 keeps inputs in the work folder anyway).
    ("/org.openoffice.Office.Common/Misc", "UseDocumentOOoLockFile", "false"),
    ("/org.openoffice.Office.Common/Misc", "UseDocumentSystemFileLocking", "false"),
    ("/org.openoffice.Office.Common/Save/Document", "CreateBackup", "false"),
    ("/org.openoffice.Office.Common/Save/Document", "AutoSave", "false"),
    # Nothing that could show a dialog or use the network.
    ("/org.openoffice.Office.Common/Misc", "CrashReport", "false"),
    ("/org.openoffice.Office.Common/Misc", "ShowTipOfTheDay", "false"),
    ("/org.openoffice.Office.Common/Misc", "FirstRun", "false"),
    ("/org.openoffice.Office.Common/Misc", "ShowDonation", "false"),
    ("/org.openoffice.Office.Jobs/Jobs/org.openoffice.Office.Jobs:Job['UpdateCheck']/Arguments",
     "AutoCheckEnabled", "false"),
)

_INITIALISED = "baleen-profile-ok"  # marker file inside the profile folder

# stdout/stderr lines that are noise, not errors
_NOISE = ("Could not find platform independent libraries", "Could not find platform dependent libraries",
          "Consider setting $PYTHONHOME", "Overwriting:")


class ProfileError(Exception):
    """The private profile could not be initialised; LibreOffice cannot run."""


# --------------------------------------------------------------------------- profiles


class _FileLock:
    """Non-blocking exclusive OS lock on a file; released on close or process death."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.fh: IO[bytes] | None = None

    def try_acquire(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(long_path(self.path), "a+b")  # noqa: SIM115
        except OSError:
            return False
        try:
            if sys.platform == "win32":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self.fh = fh
        return True

    def release(self) -> None:
        if self.fh is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                self.fh.seek(0)
                with contextlib.suppress(OSError):
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self.fh.close()
            self.fh = None


class ProfilePool:
    """Hands out profile indexes 0..n so concurrent instances never share a profile.

    Within this process a set of busy indexes is kept; across processes (two Baleen
    instances with one BALEEN_HOME) each profile is guarded by an OS file lock.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._busy: dict[int, _FileLock | None] = {}

    def acquire(self, lock_dir: Path | None = None) -> int:
        with self._lock:
            k = 0
            while True:
                if k not in self._busy:
                    fl = None
                    if lock_dir is not None:
                        fl = _FileLock(lock_dir / f"lo-profile-{k}.lock")
                        if not fl.try_acquire():
                            k += 1
                            continue
                    self._busy[k] = fl
                    return k
                k += 1

    def release(self, k: int) -> None:
        with self._lock:
            fl = self._busy.pop(k, None)
        if fl is not None:
            fl.release()


POOL = ProfilePool()
_batch_ids = itertools.count(1)


def _xcu_path(profile: Path) -> Path:
    return profile / "user" / "registrymodifications.xcu"


def _settings_ok(xcu: Path) -> bool:
    try:
        root = ET.parse(long_path(xcu)).getroot()  # noqa: S314
    except (OSError, ET.ParseError):
        return False
    have: dict[tuple[str, str], str | None] = {}
    for item in root:
        path = item.get(f"{{{_OOR}}}path")
        for prop in item:
            v = prop.find("value")
            have[(path or "", prop.get(f"{{{_OOR}}}name") or "")] = v.text if v is not None else None
    return all(have.get((p, n)) == v for p, n, v in PROFILE_SETTINGS)


def write_settings(profile: Path) -> None:
    """Merge PROFILE_SETTINGS into the profile's registrymodifications.xcu (atomic write)."""
    xcu = _xcu_path(profile)
    if _settings_ok(xcu):
        return
    ET.register_namespace("oor", _OOR)
    ET.register_namespace("xs", _XS)
    ET.register_namespace("xsi", _XSI)
    ours = {(p, n) for p, n, _ in PROFILE_SETTINGS}
    root: ET.Element | None = None
    try:
        root = ET.parse(long_path(xcu)).getroot()  # noqa: S314
    except (OSError, ET.ParseError):
        root = None
    if root is None or root.tag != f"{{{_OOR}}}items":
        root = ET.Element(f"{{{_OOR}}}items")
    for item in list(root):
        path = item.get(f"{{{_OOR}}}path") or ""
        for prop in list(item):
            if (path, prop.get(f"{{{_OOR}}}name") or "") in ours:
                item.remove(prop)
        if len(item) == 0:
            root.remove(item)
    for p, n, v in PROFILE_SETTINGS:
        item = ET.SubElement(root, "item", {f"{{{_OOR}}}path": p})
        prop = ET.SubElement(item, "prop", {f"{{{_OOR}}}name": n, f"{{{_OOR}}}op": "fuse"})
        ET.SubElement(prop, "value").text = v
    xcu.parent.mkdir(parents=True, exist_ok=True)
    tmp = xcu.with_name(xcu.name + ".baleen-tmp")
    data = ET.tostring(root, encoding="UTF-8", xml_declaration=True)
    with open(long_path(tmp), "wb") as f:
        f.write(data)
    os.replace(long_path(tmp), long_path(xcu))


def base_args(soffice: str, profile: Path) -> list[str]:
    return [
        soffice,
        f"-env:UserInstallation={profile.resolve().as_uri()}",
        "--headless", "--invisible", "--nologo", "--norestore", "--nolockcheck", "--nodefault",
    ]


def ensure_profile(profile: Path, soffice: str, env: dict[str, str], *, low_priority: bool,
                   timeout: float = INIT_TIMEOUT_S) -> None:
    """Initialise a private profile on first use (§6.2), then keep its settings in place.

    The hardening settings are written before LibreOffice first starts, so they apply from
    the very first run. A failed initialisation removes the half-made profile and retries
    once; a second failure raises ProfileError.
    """
    marker = profile / _INITIALISED
    if marker.exists():
        write_settings(profile)  # no-op unless something removed our settings
        return
    last = ""
    for _attempt in range(2):
        shutil.rmtree(long_path(profile), ignore_errors=True)
        profile.mkdir(parents=True, exist_ok=True)
        write_settings(profile)
        res = proc.run([*base_args(soffice, profile), "--terminate_after_init"], timeout=timeout, env=env,
                       low_priority=low_priority, cwd=str(profile))
        if res.ok and (profile / "user").is_dir():
            write_settings(profile)  # LibreOffice rewrites the file on exit; ours must survive
            if _settings_ok(_xcu_path(profile)):
                marker.write_text("1\n", encoding="utf-8")
                return
            last = "the profile settings did not persist"
        else:
            last = ("timed out" if res.timed_out else
                    _excerpt(res) or f"exit code {res.returncode}")
    shutil.rmtree(long_path(profile), ignore_errors=True)
    raise ProfileError(f"LibreOffice could not set up its profile ({last}).")


# --------------------------------------------------------------------------- calls


def convert_args(soffice: str, profile_dir: Path, jobs: list[LoJob], outdir: str) -> list[str]:
    head = jobs[0]
    level = PDFA_VERSION[head.pdfa_level]
    args = base_args(soffice, profile_dir)
    if head.infilter:
        args.append(f"--infilter={head.infilter}")
    args += [
        "--convert-to",
        f'pdf:{head.export_filter}:{{"SelectPdfVersion":{{"type":"long","value":"{level}"}}}}',
        "--outdir", outdir,
    ]
    args += [j.input_path for j in jobs]
    return args


def short_version(version: str) -> str:
    """'26.8.0.3' -> '26.8.0' (the method column names the release, §10.1)."""
    parts = [p for p in (version or "").split(".") if p]
    return ".".join(parts[:3]) if parts else ""


def method_for(run: RunContext, job: LoJob) -> str:
    ver = short_version(run.tools.get("libreoffice").version)
    name = f"LibreOffice {ver}" if ver else "LibreOffice"
    return f"{name} · {job.export_filter} · PDF/A-{job.pdfa_level}"


_method = method_for  # backwards-compatible name


def _stem_key(job: LoJob) -> str:
    return Path(job.input_path).stem.casefold()


def plan_calls(jobs: list[LoJob], max_batch: int = MAX_BATCH) -> list[list[int]]:
    """Split job indexes into soffice calls: at most max_batch each, unique stems per call.

    Order is kept: each job goes into the first call (from the front) that has room and no
    job with the same stem.
    """
    calls: list[list[int]] = []
    stems: list[set[str]] = []
    for i, j in enumerate(jobs):
        key = _stem_key(j)
        for c, s in zip(calls, stems, strict=True):
            if len(c) < max_batch and key not in s:
                c.append(i)
                s.add(key)
                break
        else:
            calls.append([i])
            stems.append({key})
    return calls


def pdf_complete(path: str) -> bool:
    """True if the file starts with %PDF- and ends with %%EOF (a run killed mid-write fails this)."""
    try:
        with open(long_path(path), "rb") as f:
            head = f.read(5)
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 2048))
            tail = f.read()
    except OSError:
        return False
    return head == b"%PDF-" and b"%%EOF" in tail


def _collect(jobs: list[LoJob], outdir: str) -> dict[int, str]:
    """Map job index -> produced PDF (moved into the job's out_dir). Incomplete files are ignored."""
    got: dict[int, str] = {}
    for i, j in enumerate(jobs):
        produced = os.path.join(outdir, Path(j.input_path).stem + ".pdf")
        if os.path.exists(long_path(produced)) and pdf_complete(produced):
            os.makedirs(long_path(j.out_dir), exist_ok=True)
            dest = j.expected_output
            if os.path.abspath(produced) != os.path.abspath(dest):
                os.replace(long_path(produced), long_path(dest))
            got[i] = dest
    return got


def _excerpt(res: proc.ProcResult) -> str:
    """The useful part of LibreOffice's output, for the report message."""
    lines = []
    for line in (res.err() + "\n" + res.out()).splitlines():
        s = line.strip()
        if not s or s.startswith("convert ") or any(s.startswith(n) for n in _NOISE):
            continue
        lines.append(s)
    text = " ".join(lines)
    if res.error:
        text = (res.error + " " + text).strip()
    if "source file could not be loaded" in text:
        return "LibreOffice couldn't open the file (it may be damaged or in an unsupported variant)."
    return text[:400]


def _call(run: RunContext, ctx: TaskContext, profile: Path, jobs: list[LoJob], timeout: float, outdir: str
          ) -> tuple[dict[int, str], proc.ProcResult]:
    soffice = run.tools.path("libreoffice")
    assert soffice
    os.makedirs(long_path(outdir), exist_ok=True)
    args = convert_args(soffice, profile, jobs, outdir)
    res = proc.run(args, timeout=timeout, env=run.tools_env(), low_priority=ctx.low_priority, cwd=outdir)
    return _collect(jobs, outdir), res


def batch_timeout(n: int) -> float:
    return float(BATCH_BASE_S + BATCH_PER_FILE_S * n)


def convert_batch(ctx: TaskContext, run: RunContext, jobs: list[LoJob]) -> list[LoResult]:
    """Convert jobs that share one batch key. Always returns one LoResult per job, in order."""
    if not jobs:
        return []
    soffice = run.tools.path("libreoffice")
    if soffice is None:
        return [LoResult(j, False, None, "TOOL_MISSING", "LibreOffice is not available.") for j in jobs]
    keys = {j.batch_key for j in jobs}
    if len(keys) != 1:
        raise ValueError(f"jobs in one batch must share a batch key, got {keys}")
    per_file = float(run.advanced.get("lo_timeout_s", 300))
    results: list[LoResult | None] = [None] * len(jobs)
    dirs: list[str] = []

    def new_outdir() -> str:
        d = os.path.join(run.work_root, f"lo-batch-{next(_batch_ids)}")
        dirs.append(d)
        return d

    k = POOL.acquire(run.home.data_dir)
    try:
        profile = run.home.lo_profile(k)
        try:
            ensure_profile(profile, soffice, run.tools_env(), low_priority=ctx.low_priority,
                           timeout=max(INIT_TIMEOUT_S, per_file))
        except ProfileError as e:
            return [LoResult(j, False, None, "CONVERSION_ERROR", str(e), method=method_for(run, j)) for j in jobs]
        for call in plan_calls(jobs):
            alone = call
            if len(call) > 1:
                group = [jobs[i] for i in call]
                got, _res = _call(run, ctx, profile, group, batch_timeout(len(group)), new_outdir())
                for gi, path in got.items():
                    i = call[gi]
                    results[i] = LoResult(jobs[i], True, path, method=method_for(run, jobs[i]))
                alone = [i for i in call if results[i] is None]
            # Not converted by the batch (or a call of one): one process per file, per-file limit.
            for i in alone:
                j = jobs[i]
                retried = len(call) > 1
                got, res = _call(run, ctx, profile, [j], per_file, new_outdir())
                if 0 in got:
                    results[i] = LoResult(j, True, got[0], method=method_for(run, j), retried_alone=retried)
                elif res.timed_out:
                    results[i] = LoResult(j, False, None, "TIMEOUT",
                                          f"LibreOffice took longer than {int(per_file)} s and was stopped.",
                                          method=method_for(run, j), retried_alone=retried)
                else:
                    msg = _excerpt(res) or f"LibreOffice produced no PDF (exit code {res.returncode})."
                    results[i] = LoResult(j, False, None, "CONVERSION_ERROR", msg, method=method_for(run, j),
                                          retried_alone=retried)
    finally:
        POOL.release(k)
        for d in dirs:
            shutil.rmtree(long_path(d), ignore_errors=True)
    out = [r for r in results if r is not None]
    assert len(out) == len(jobs)
    return out
