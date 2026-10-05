"""Locate the external tools, read their versions, and build their environment (§11, SEC-8).

Lookup order (§11): environment override -> bundled runtime/ -> PATH -> standard install
locations. Results are cached; `Toolset.detect(force=True)` re-runs detection (Tools ›
Check again).
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import sys
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import proc
from .home import Home

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
EXE = ".exe" if IS_WINDOWS else ""

TOOL_KEYS: tuple[str, ...] = ("libreoffice", "ffmpeg", "ffprobe", "java", "verapdf")


@dataclass(frozen=True)
class ToolSpec:
    key: str
    name: str
    role: str
    affects: str  # UI-T1 "Affects" text when missing
    fix: str  # UI-T1 "Fix" text when missing
    env: str  # override variable


SPECS: dict[str, ToolSpec] = {
    s.key: s
    for s in (
        ToolSpec("libreoffice", "LibreOffice", "Documents, text, web pages and e-mail → PDF/A",
                 "Documents, text files, web pages and e-mails are not converted (Converter missing).",
                 "Use the complete Baleen folder from the release zip, or install LibreOffice, then "
                 "choose Check again.", "BALEEN_SOFFICE"),
        ToolSpec("ffmpeg", "FFmpeg", "Audio and video conversion",
                 "Audio and video files are not converted (Converter missing).",
                 "Use the complete Baleen folder from the release zip, then choose Check again.",
                 "BALEEN_FFMPEG"),
        ToolSpec("ffprobe", "FFprobe", "Audio and video inspection and checks",
                 "Audio and video files can't be inspected or checked.",
                 "Use the complete Baleen folder from the release zip, then choose Check again.",
                 "BALEEN_FFPROBE"),
        ToolSpec("java", "Java runtime", "Runs veraPDF",
                 "PDF/A can't be verified. New PDFs are still written but marked Needs review.",
                 "Use the complete Baleen folder from the release zip, then choose Check again.",
                 "BALEEN_JAVA_HOME"),
        ToolSpec("verapdf", "veraPDF", "PDF/A validation",
                 "PDF/A can't be verified. New PDFs are still written but marked Needs review.",
                 "Use the complete Baleen folder from the release zip, then choose Check again.",
                 "BALEEN_VERAPDF"),
    )
}


@dataclass
class ToolInfo:
    key: str
    name: str
    role: str
    path: str | None
    version: str
    source: str  # "bundled" | "system" | "override" | "missing"
    error: str = ""
    home: str | None = None  # JAVA_HOME for java; install dir for others

    @property
    def found(self) -> bool:
        return self.path is not None and self.source != "missing"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- candidates


def _runtime_candidates(key: str, rt: Path) -> list[Path]:
    if key == "libreoffice":
        if IS_WINDOWS:
            return [
                rt / "libreoffice" / "program" / "soffice.exe",
                rt / "libreoffice" / "LibreOffice" / "program" / "soffice.exe",
            ]
        if IS_MAC:
            return [rt / "LibreOffice.app" / "Contents" / "MacOS" / "soffice"]
        return [rt / "libreoffice" / "program" / "soffice"]
    if key in ("ffmpeg", "ffprobe"):
        return [rt / "ffmpeg" / f"{key}{EXE}", rt / "ffmpeg" / "bin" / f"{key}{EXE}"]
    if key == "java":
        return [rt / "jre" / "bin" / f"java{EXE}"]
    if key == "verapdf":
        name = "verapdf.bat" if IS_WINDOWS else "verapdf"
        return [rt / "verapdf" / name]
    return []


def _standard_candidates(key: str) -> list[Path]:
    out: list[Path] = []
    if key == "libreoffice":
        if IS_WINDOWS:
            for env in ("ProgramFiles", "ProgramFiles(x86)"):
                base = os.environ.get(env)
                if base:
                    out.append(Path(base) / "LibreOffice" / "program" / "soffice.exe")
        elif IS_MAC:
            out.append(Path("/Applications/LibreOffice.app/Contents/MacOS/soffice"))
        else:
            out += [Path("/usr/bin/soffice"), Path("/usr/lib/libreoffice/program/soffice"),
                    Path("/opt/libreoffice/program/soffice")]
    elif key == "verapdf":
        if not IS_WINDOWS:
            out += [Path.home() / "verapdf" / "verapdf", Path("/opt/verapdf/verapdf")]
    return out


def _path_names(key: str) -> list[str]:
    return {
        "libreoffice": ["soffice", "libreoffice"],
        "ffmpeg": ["ffmpeg"],
        "ffprobe": ["ffprobe"],
        "java": ["java"],
        "verapdf": ["verapdf"],
    }[key]


def _override(key: str, env: dict[str, str]) -> Path | None:
    var = SPECS[key].env
    val = env.get(var)
    if not val:
        return None
    p = Path(val)
    if key == "java":
        # BALEEN_JAVA_HOME points at the JRE root.
        return p / "bin" / f"java{EXE}"
    return p


# --------------------------------------------------------------------------- versions


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _win_file_version(path: Path) -> str:
    """Product version from a Windows executable's version resource (no process start)."""
    import ctypes
    from ctypes import wintypes

    ver = ctypes.WinDLL("version")
    size = ver.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return ""
    buf = ctypes.create_string_buffer(size)
    if not ver.GetFileVersionInfoW(str(path), 0, size, buf):
        return ""
    ptr = ctypes.c_void_p()
    length = wintypes.UINT()
    if not ver.VerQueryValueW(buf, "\\", ctypes.byref(ptr), ctypes.byref(length)):
        return ""

    class VS_FIXEDFILEINFO(ctypes.Structure):  # noqa: N801
        _fields_ = [(n, wintypes.DWORD) for n in (
            "dwSignature", "dwStrucVersion", "dwFileVersionMS", "dwFileVersionLS",
            "dwProductVersionMS", "dwProductVersionLS", "dwFileFlagsMask", "dwFileFlags",
            "dwFileOS", "dwFileType", "dwFileSubtype", "dwFileDateMS", "dwFileDateLS")]

    info = ctypes.cast(ptr, ctypes.POINTER(VS_FIXEDFILEINFO)).contents
    ms, ls = info.dwProductVersionMS, info.dwProductVersionLS
    return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"


def _lo_version(soffice: Path, env: dict[str, str]) -> str:
    program = soffice.parent
    if IS_WINDOWS:
        for exe in (program / "soffice.bin", soffice):
            try:
                v = _win_file_version(exe)
            except OSError:
                v = ""
            if v:
                return v
    # Windows / Linux tarballs: program/version.ini ; macOS: Contents/Resources/version.plist / Info.plist
    for ini in (program / "version.ini", program / "versionrc"):
        try:
            text = ini.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = re.search(r"^MsiProductVersion=([\d.]+)", text, re.M) or re.search(
            r"^ProductMajor=(\d+)", text, re.M)
        if m:
            return m.group(1)
    if IS_MAC:
        plist = soffice.parent.parent / "Info.plist"
        try:
            text = plist.read_text(encoding="utf-8", errors="replace")
            m = re.search(r"<key>CFBundleShortVersionString</key>\s*<string>([^<]+)</string>", text)
            if m:
                return m.group(1)
        except OSError:
            pass
    r = proc.run([str(soffice), "--version"], timeout=60, env=env)
    m = re.search(r"LibreOffice\s+([\d.]+)", r.out())
    return m.group(1) if m else _first_line(r.out())


def _ff_version(exe: Path, env: dict[str, str]) -> str:
    r = proc.run([str(exe), "-hide_banner", "-version"], timeout=30, env=env)
    m = re.search(r"version\s+(\S+)", r.out())
    return m.group(1) if m else _first_line(r.out() or r.err())


def _java_version(java: Path, env: dict[str, str]) -> str:
    release = java.parent.parent / "release"
    try:
        text = release.read_text(encoding="utf-8", errors="replace")
        m = re.search(r'^JAVA_VERSION="([^"]+)"', text, re.M)
        impl = re.search(r'^IMPLEMENTOR="([^"]+)"', text, re.M)
        if m:
            vendor = "Temurin " if impl and "Adoptium" in impl.group(1) else ""
            return f"{vendor}{m.group(1)}"
    except OSError:
        pass
    r = proc.run([str(java), "-XX:-UsePerfData", "-version"], timeout=60, env=env)
    m = re.search(r'version "([^"]+)"', r.err() or r.out())
    return m.group(1) if m else _first_line(r.err())


def _verapdf_version(exe: Path, env: dict[str, str]) -> str:
    # Installer leaves bin/greenfield-apps-<ver>.jar (or similar); avoid a JVM start if possible.
    bindir = exe.parent / "bin"
    try:
        for f in sorted(bindir.glob("*.jar")):
            m = re.search(r"(?:greenfield-apps|verapdf-apps|cli)-(\d+\.\d+\.\d+)", f.name)
            if m:
                return m.group(1)
    except OSError:
        pass
    r = proc.run([str(exe), "--version"], timeout=120, env=env)
    m = re.search(r"veraPDF\s+([\d.]+)", r.out() + r.err())
    return m.group(1) if m else _first_line(r.out())


# --------------------------------------------------------------------------- environment


_KEEP_ENV = (
    "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "SYSTEMDRIVE", "PROGRAMFILES",
    "PROGRAMFILES(X86)", "PROGRAMW6432", "PROGRAMDATA", "COMMONPROGRAMFILES",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS", "USERNAME", "USERPROFILE",
    "HOMEDRIVE", "HOMEPATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "PATH",
    "DISPLAY", "XDG_RUNTIME_DIR", "__CF_USER_TEXT_ENCODING",
)


def tool_env(home: Home, java_home: str | None = None) -> dict[str, str]:
    """Sanitised environment for every child process (SEC-8, §14.3).

    TMP/TEMP/TMPDIR -> data/tmp; Java user and system prefs -> data/java.
    """
    tmp = str(home.tmp_dir)
    home.tmp_dir.mkdir(parents=True, exist_ok=True)
    env: dict[str, str] = {}
    upper = {k.upper(): k for k in os.environ}
    for k in _KEEP_ENV:
        src = upper.get(k)
        if src is not None:
            env[k if not IS_WINDOWS else src] = os.environ[src]
    env["TMP"] = tmp
    env["TEMP"] = tmp
    env["TMPDIR"] = tmp
    if not IS_WINDOWS and "LANG" not in env:
        env["LANG"] = "C.UTF-8"
    javadir = str(home.java_dir)
    home.java_dir.mkdir(parents=True, exist_ok=True)
    # The JVM splits JAVA_TOOL_OPTIONS on whitespace unless a value is quoted: quote the paths
    # so a Baleen folder with spaces still starts Java.
    env["JAVA_TOOL_OPTIONS"] = (
        f'-Djava.util.prefs.userRoot="{javadir}" -Djava.util.prefs.systemRoot="{javadir}" '
        f'-Djava.io.tmpdir="{tmp}" -XX:-UsePerfData '
        # AC-15: a veraPDF JVM holds one processor token; keep its GC and JIT threads to one CPU.
        "-XX:ActiveProcessorCount=1 -XX:+UseSerialGC -XX:TieredStopAtLevel=1"
    )
    # AC-15: each LibreOffice instance holds one token; cap its internal thread pool to match.
    env["MAX_CONCURRENCY"] = "1"
    if java_home:
        env["JAVA_HOME"] = java_home
        # The veraPDF launchers pick the JVM from JAVACMD (verapdf.bat ignores JAVA_HOME and
        # would otherwise fall back to "java" on PATH).
        env["JAVACMD"] = os.path.join(java_home, "bin", f"java{EXE}")
    return env


# --------------------------------------------------------------------------- detection


class Toolset:
    """Thread-safe, cached tool detection for one BALEEN_HOME."""

    def __init__(self, home: Home, env: dict[str, str] | None = None) -> None:
        self.home = home
        self._env = dict(os.environ if env is None else env)
        self._lock = threading.Lock()
        self._cache: dict[str, ToolInfo] | None = None

    def detect(self, force: bool = False) -> dict[str, ToolInfo]:
        with self._lock:
            if self._cache is not None and not force:
                return dict(self._cache)
            found: dict[str, ToolInfo] = {}
            java = self._find("java", None)
            found["java"] = java
            env = tool_env(self.home, java.home if java.found else None)
            for key in ("libreoffice", "ffmpeg", "ffprobe", "verapdf"):
                found[key] = self._find(key, env)
            self._cache = found
            return dict(found)

    def get(self, key: str) -> ToolInfo:
        return self.detect()[key]

    def path(self, key: str) -> str | None:
        t = self.get(key)
        return t.path if t.found else None

    def env(self) -> dict[str, str]:
        java = self.get("java")
        return tool_env(self.home, java.home if java.found else None)

    def missing(self) -> list[str]:
        return [k for k, t in self.detect().items() if not t.found]

    def _locate(self, key: str) -> tuple[Path | None, str]:
        ov = _override(key, self._env)
        if ov is not None:
            return (ov if ov.exists() else None), "override"
        for c in _runtime_candidates(key, self.home.runtime_dir):
            if c.exists():
                return c, "bundled"
        for name in _path_names(key):
            w = shutil.which(name, path=self._env.get("PATH"))
            if w:
                return Path(w), "system"
        for c in _standard_candidates(key):
            if c.exists():
                return c, "system"
        return None, "missing"

    def _find(self, key: str, env: dict[str, str] | None) -> ToolInfo:
        spec = SPECS[key]
        path, source = self._locate(key)
        if path is None:
            err = f"{spec.env} points to a missing file" if source == "override" else "Not found"
            return ToolInfo(key, spec.name, spec.role, None, "", "missing", err)
        if key in ("java", "verapdf"):
            bad = non_ansi_path(path)
            if bad:
                # R-02: the Java launcher uses the ANSI code page on Windows; it cannot start from
                # a folder whose name has characters outside it. Report it honestly as missing.
                return ToolInfo(key, spec.name, spec.role, None, "", "missing",
                                f"Java can't start from a folder whose path contains characters outside this "
                                f"computer's code page ({bad}). Move the Baleen folder to a path with only "
                                "Latin letters and digits.")
        if env is None:
            env = tool_env(self.home)
        try:
            if key == "libreoffice":
                ver = _lo_version(path, env)
                home = str(path.parent)
            elif key in ("ffmpeg", "ffprobe"):
                ver = _ff_version(path, env)
                home = str(path.parent)
            elif key == "java":
                ver = _java_version(path, env)
                home = str(path.parent.parent)
            else:
                ver = _verapdf_version(path, env)
                home = str(path.parent)
        except Exception as e:  # a broken tool is reported, never fatal
            return ToolInfo(key, spec.name, spec.role, str(path), "", source, f"{e.__class__.__name__}: {e}")
        return ToolInfo(key, spec.name, spec.role, str(path), ver, source, "", home)


def non_ansi_path(path: str | os.PathLike[str]) -> str:
    """Windows: the characters of `path` that the ANSI code page cannot represent ('' if none).

    Java (and therefore veraPDF) receives paths through the ANSI code page on Windows unless
    the system-wide UTF-8 option is on (R-02).
    """
    if not IS_WINDOWS:
        return ""
    s = os.fspath(path)
    try:
        s.encode("mbcs", "strict")
        return ""
    except UnicodeEncodeError:
        return "".join(dict.fromkeys(ch for ch in s if not _ansi_ok(ch)))


def _ansi_ok(ch: str) -> bool:
    try:
        ch.encode("mbcs", "strict")
        return True
    except UnicodeEncodeError:
        return False


def library_versions() -> dict[str, dict[str, str]]:
    """Python-side components for the run JSON `tools` block (§10.2)."""
    out: dict[str, dict[str, str]] = {
        "python": {"path": sys.executable, "version": platform.python_version()},
    }
    try:
        import PIL

        out["pillow"] = {"version": PIL.__version__}
    except Exception:
        out["pillow"] = {"version": ""}
    try:
        import pypdf

        out["pypdf"] = {"version": pypdf.__version__}
    except Exception:
        out["pypdf"] = {"version": ""}
    return out
