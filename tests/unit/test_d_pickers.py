"""Native folder dialogs (spec §12.9): command construction and output parsing. The dialogs
themselves cannot be automated; they are on the manual checklist (§16.5, risk R-03)."""

from __future__ import annotations

import base64

import pytest

from baleen.proc import ProcResult
from baleen.server import pickers


def test_detect_per_platform() -> None:
    assert pickers.detect("win32", {"SystemRoot": r"C:\Windows"}, exists=lambda p: True) == "windows"
    assert pickers.detect("win32", {"SystemRoot": r"C:\Windows"}, exists=lambda p: False) is None
    assert pickers.detect("darwin", {}, exists=lambda p: p == "/usr/bin/osascript") == "macos"
    assert pickers.detect("linux", {}, which=lambda n: "/usr/bin/" + n) is None  # no display
    assert pickers.detect("linux", {"DISPLAY": ":0"}, which=lambda n: "/usr/bin/zenity" if n == "zenity" else None) \
        == "zenity"
    assert pickers.detect("linux", {"WAYLAND_DISPLAY": "w"}, which=lambda n: "/x/kdialog" if n == "kdialog" else None) \
        == "kdialog"
    assert pickers.detect("linux", {"DISPLAY": ":0"}, which=lambda n: None) is None


def test_windows_command_is_sta_encoded_and_takes_the_start_folder_from_the_environment() -> None:
    start = r"C:\Archive\04 Events 訪談'; Remove-Item C:\ -Recurse #"
    cmd = pickers.build_command("windows", start, env={"SystemRoot": r"C:\Windows", "PATH": "x"},
                                tmp_dir=r"C:\Baleen\data\tmp")
    assert cmd.argv[0] == r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    for flag in ("-NoProfile", "-NonInteractive", "-STA", "-EncodedCommand"):
        assert flag in cmd.argv
    script = base64.b64decode(cmd.argv[cmd.argv.index("-EncodedCommand") + 1]).decode("utf-16-le")
    assert "FolderBrowserDialog" in script and "TopMost = $true" in script
    assert "UTF8Encoding" in script and "-STA" not in script
    assert "Remove-Item" not in script and start not in " ".join(cmd.argv)  # never inside the command
    assert cmd.env is not None
    assert cmd.env["BALEEN_PICK_INITIAL"] == start
    assert cmd.env["TEMP"] == cmd.env["TMP"] == cmd.env["LOCALAPPDATA"] == r"C:\Baleen\data\tmp"
    assert pickers.build_command("windows", "relative", env={}).env["BALEEN_PICK_INITIAL"] == ""


def test_macos_command_activates_and_passes_folders_as_arguments() -> None:
    cmd = pickers.build_command("macos", "/Volumes/Archive/訪談 \"x\"")
    assert cmd.argv[0] == "/usr/bin/osascript"
    lines = [cmd.argv[i + 1] for i, a in enumerate(cmd.argv) if a == "-e"]
    assert "activate" in lines and any("choose folder" in x for x in lines)
    assert cmd.argv[-2:] == [pickers.TITLE, "/Volumes/Archive/訪談 \"x\""]
    assert not any("Archive" in x for x in lines)
    assert pickers.build_command("macos", None).argv[-1] == pickers.TITLE


def test_linux_commands() -> None:
    z = pickers.build_command("zenity", "/home/mia/Archive/", which=lambda n: "/usr/bin/zenity")
    assert z.argv[:3] == ["/usr/bin/zenity", "--file-selection", "--directory"]
    assert "--filename=/home/mia/Archive/" in z.argv
    k = pickers.build_command("kdialog", "/home/mia", which=lambda n: "/usr/bin/kdialog")
    assert k.argv[:3] == ["/usr/bin/kdialog", "--getexistingdirectory", "/home/mia"]
    with pytest.raises(ValueError):
        pickers.build_command("beos", None)


@pytest.mark.parametrize(("kind", "rc", "out", "err", "path", "cancelled"), [
    ("windows", 0, "OK:C:\\Archive\\訪談".encode(), b"", "C:\\Archive\\訪談", False),
    ("windows", 0, "\ufeffOK:D:\\x\r\n".encode(), b"", "D:\\x", False),
    ("windows", 0, b"OK:\\\\nas\\share\\a", b"", "\\\\nas\\share\\a", False),
    ("windows", 0, b"CANCEL", b"", None, True),
    ("windows", 1, b"", b"Add-Type : boom", None, False),
    ("macos", 0, "/Volumes/Archive/訪談/\n".encode(), b"", "/Volumes/Archive/訪談", False),
    ("macos", 0, b"/\n", b"", "/", False),
    ("macos", 1, b"", b"execution error: User canceled. (-128)", None, True),
    ("macos", 1, b"", b"", None, True),
    ("macos", 1, b"", b"execution error: no display", None, False),
    ("zenity", 0, b"/home/mia/Archive\n", b"", "/home/mia/Archive", False),
    ("zenity", 1, b"", b"", None, True),
    ("kdialog", 0, b"/home/mia/x/\n", b"", "/home/mia/x", False),
    ("kdialog", 255, b"", b"oops", None, False),
])
def test_parse_output(kind: str, rc: int, out: bytes, err: bytes, path: str | None, cancelled: bool) -> None:
    r = pickers.parse_output(kind, rc, out, err)
    assert r.path == path
    assert r.cancelled is cancelled
    if path is None and not cancelled:
        assert r.error


def test_relative_answers_and_timeouts_are_not_used() -> None:
    assert pickers.parse_output("zenity", 0, b"relative/dir\n").path is None
    t = pickers.parse_output("windows", None, b"", b"", timed_out=True)
    assert t.cancelled and t.path is None


def test_pick_folder_runs_the_command_with_the_10_minute_timeout() -> None:
    seen: dict = {}

    def runner(argv, timeout, env):  # noqa: ANN001, ANN202
        seen.update(argv=argv, timeout=timeout, env=env)
        return ProcResult(argv, 0, b"/home/mia/out\n", b"")

    r = pickers.pick_folder("zenity", "/home/mia", runner=runner)
    assert r.path == "/home/mia/out"
    assert seen["timeout"] == 600 == pickers.TIMEOUT_S
    failed = pickers.pick_folder("zenity", None, runner=lambda argv, timeout, env: ProcResult(argv, None,
                                                                                              error="not found"))
    assert failed.path is None and failed.error == "not found"


def test_only_one_dialog_at_a_time() -> None:
    assert pickers._busy.acquire(blocking=False)
    try:
        r = pickers.pick_folder("zenity", None, runner=lambda *a, **k: pytest.fail("must not run"))
        assert r.path is None and r.cancelled
    finally:
        pickers._busy.release()
