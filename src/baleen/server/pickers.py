"""Native folder dialogs (spec §12.9, UI-C6, risk R-03).

`POST /api/pick-folder` makes the server open the OS dialog and return an absolute path, or
"cancelled". Timeout 10 minutes. The dialogs cannot be automated, so tests cover command
construction and output parsing; the dialogs themselves are on the manual checklist (§16.5).

- Windows: PowerShell -STA, System.Windows.Forms.FolderBrowserDialog with a top-most owner,
  UTF-8 output. The script is fixed and passed as -EncodedCommand; the starting folder travels
  in an environment variable, never inside the script text.
- macOS: osascript with `activate` + `choose folder`; the starting folder is an argv item.
- Linux: zenity, else kdialog, when a display is available.
- None of these: Browse is hidden; pasting a path always works.
"""

from __future__ import annotations

import base64
import os
import shutil
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .. import proc

TIMEOUT_S = 600
TITLE = "Choose a folder for Baleen"

PS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
$owner.ShowInTaskbar = $false
$owner.FormBorderStyle = 'None'
$owner.Opacity = 0
$owner.StartPosition = 'CenterScreen'
$owner.Size = New-Object System.Drawing.Size(1, 1)
$owner.Show()
$owner.Activate()
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = $env:BALEEN_PICK_TITLE
$dialog.ShowNewFolderButton = $true
$start = $env:BALEEN_PICK_INITIAL
if ($start -and (Test-Path -LiteralPath $start -PathType Container)) { $dialog.SelectedPath = $start }
$result = $dialog.ShowDialog($owner)
$owner.Close()
if ($result -eq [System.Windows.Forms.DialogResult]::OK) {
  [Console]::Out.Write('OK:' + $dialog.SelectedPath)
} else {
  [Console]::Out.Write('CANCEL')
}
"""

OSA_LINES = (
    "on run argv",
    "activate",
    "if (count of argv) > 1 then",
    "set chosen to choose folder with prompt (item 1 of argv) default location (POSIX file (item 2 of argv))",
    "else",
    "set chosen to choose folder with prompt (item 1 of argv)",
    "end if",
    "return POSIX path of chosen",
    "end run",
)


@dataclass(frozen=True)
class PickerCommand:
    kind: str  # windows | macos | zenity | kdialog
    argv: list[str]
    env: dict[str, str] | None = None


@dataclass(frozen=True)
class PickResult:
    path: str | None  # absolute path, or None
    cancelled: bool
    error: str = ""


def powershell_path(env: Mapping[str, str] = os.environ) -> str:
    root = env.get("SystemRoot") or env.get("SYSTEMROOT") or r"C:\Windows"
    return os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")


def detect(platform: str = sys.platform, env: Mapping[str, str] = os.environ,
           which: Callable[[str], str | None] = shutil.which,
           exists: Callable[[str], bool] = os.path.exists) -> str | None:
    """Which dialog this machine can show, or None (then Browse is hidden)."""
    if platform == "win32":
        return "windows" if exists(powershell_path(env)) else None
    if platform == "darwin":
        return "macos" if exists("/usr/bin/osascript") else None
    if not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
        return None
    if which("zenity"):
        return "zenity"
    if which("kdialog"):
        return "kdialog"
    return None


def build_command(kind: str, initial: str | None, *, env: Mapping[str, str] = os.environ,
                  which: Callable[[str], str | None] = shutil.which,
                  tmp_dir: str | None = None) -> PickerCommand:
    start = initial if initial and os.path.isabs(initial) else None
    if kind == "windows":
        encoded = base64.b64encode(PS_SCRIPT.encode("utf-16-le")).decode("ascii")
        child_env = dict(env)
        child_env["BALEEN_PICK_TITLE"] = TITLE
        child_env["BALEEN_PICK_INITIAL"] = start or ""
        if tmp_dir:
            # Keep PowerShell's caches and temp files inside BALEEN_HOME/data (spec §14.5).
            for k in ("TEMP", "TMP", "LOCALAPPDATA"):
                child_env[k] = tmp_dir
        return PickerCommand("windows", [
            powershell_path(env), "-NoLogo", "-NoProfile", "-NonInteractive", "-STA",
            "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded,
        ], child_env)
    if kind == "macos":
        argv = ["/usr/bin/osascript"]
        for line in OSA_LINES:
            argv += ["-e", line]
        argv.append(TITLE)
        if start:
            argv.append(start)
        return PickerCommand("macos", argv, None)
    if kind == "zenity":
        exe = which("zenity") or "zenity"
        argv = [exe, "--file-selection", "--directory", f"--title={TITLE}"]
        if start:
            argv.append(f"--filename={start.rstrip('/')}/")
        return PickerCommand("zenity", argv, None)
    if kind == "kdialog":
        exe = which("kdialog") or "kdialog"
        return PickerCommand("kdialog", [exe, "--getexistingdirectory", start or os.path.expanduser("~"),
                                         "--title", TITLE], None)
    raise ValueError(f"unknown picker {kind!r}")


def parse_output(kind: str, returncode: int | None, stdout: bytes, stderr: bytes = b"",
                 timed_out: bool = False) -> PickResult:
    """Turn the dialog's output into an absolute path, "cancelled", or an error."""
    if timed_out:
        return PickResult(None, True, "The folder dialog was open for more than 10 minutes.")
    text = stdout.decode("utf-8", "replace").lstrip("\ufeff").strip("\r\n")
    if kind == "windows":
        if returncode == 0 and text.startswith("OK:"):
            return _absolute(text[3:])
        if returncode == 0 and text.startswith("CANCEL"):
            return PickResult(None, True)
        return PickResult(None, False, _err(stderr) or "The folder dialog could not be opened.")
    if kind == "macos":
        if returncode == 0 and text:
            p = text.strip()
            if len(p) > 1:
                p = p.rstrip("/")
            return _absolute(p)
        err = _err(stderr)
        if "-128" in err or (returncode == 1 and not err):
            return PickResult(None, True)
        return PickResult(None, False, err or "The folder dialog could not be opened.")
    if kind in ("zenity", "kdialog"):
        if returncode == 0 and text.strip():
            p = text.strip()
            return _absolute(p.rstrip("/") if len(p) > 1 else p)
        if returncode == 1:
            return PickResult(None, True)
        return PickResult(None, False, _err(stderr) or "The folder dialog could not be opened.")
    raise ValueError(f"unknown picker {kind!r}")


def _err(stderr: bytes) -> str:
    return stderr.decode("utf-8", "replace").strip()[:300]


def _absolute(p: str) -> PickResult:
    p = p.strip()
    if not p or not os.path.isabs(p):
        return PickResult(None, False, "The folder dialog returned a path Baleen can't use.")
    return PickResult(p, False)


_busy = threading.Lock()


def pick_folder(kind: str, initial: str | None = None, *, tmp_dir: str | None = None,
                runner: Callable[..., proc.ProcResult] = proc.run) -> PickResult:
    """Open the dialog and wait for it (at most 10 minutes). One dialog at a time."""
    if not _busy.acquire(blocking=False):
        return PickResult(None, True, "A folder dialog is already open.")
    try:
        cmd = build_command(kind, initial, tmp_dir=tmp_dir)
        r = runner(cmd.argv, timeout=TIMEOUT_S, env=cmd.env)
        if r.error:
            return PickResult(None, False, r.error)
        return parse_output(kind, r.returncode, r.stdout, r.stderr, timed_out=r.timed_out)
    finally:
        _busy.release()
