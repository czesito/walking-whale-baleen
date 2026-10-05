"""Build Baleen's bundled runtime/ (and, from phase 2, the whole portable bundle) — spec §14.6.

    python scripts/build_bundle.py --platform win-x64|mac-arm64|mac-x64 [--version X.Y.Z] [--cache DIR]
    python scripts/build_bundle.py --platform win-x64 --runtime-only --dest DIR [--cache DIR] [--work DIR]

Every download is pinned in scripts/runtimes.json. It is fetched into the cache folder and its
SHA-256 is checked before use; a mismatch aborts the build. Installation never touches the system:
LibreOffice uses an administrative MSI install (Windows) or a read-only DMG mount (macOS), veraPDF a
headless IzPack install into runtime/verapdf, and Python packages go into runtime/python only.

The last step writes runtime/VERSIONS.json (exact versions and the SHA-256 of every download). Its
presence marks a complete runtime; the script refuses to build into a non-empty folder.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import tarfile
import time
import tomllib
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as xml_escape

REPO = Path(__file__).resolve().parents[1]
RUNTIMES_JSON = REPO / "scripts" / "runtimes.json"
REQUIREMENTS_LOCK = REPO / "requirements.lock"
BUILD_REQUIREMENTS_LOCK = REPO / "scripts" / "build-requirements.lock"

PLATFORMS = ("win-x64", "mac-arm64", "mac-x64")
# jre before verapdf: the veraPDF installer runs on the bundled Java.
COMPONENTS = ("python", "ffmpeg", "libreoffice", "jre", "verapdf")
WORK_MARKER = ".baleen-build-work"
USER_AGENT = "walking-whale-baleen-build/1 (+https://github.com/czesito/walking-whale-baleen)"


class BuildError(Exception):
    """A build step failed; the message says what to do."""


def log(msg: str = "") -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------- context


@dataclass
class Ctx:
    platform: str
    version: str
    cache: Path
    runtime: Path
    work: Path
    pins: dict[str, Any]
    records: dict[str, Any] = field(default_factory=dict)

    @property
    def is_windows(self) -> bool:
        return self.platform.startswith("win")

    @property
    def exe(self) -> str:
        return ".exe" if self.is_windows else ""

    @property
    def tmp(self) -> Path:
        p = self.work / "tmp"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def python_exe(self) -> Path:
        rt = self.runtime / "python"
        return rt / "python.exe" if self.is_windows else rt / "bin" / "python3"

    def java_exe(self) -> Path:
        return self.runtime / "jre" / "bin" / f"java{self.exe}"

    def child_env(self, **extra: str) -> dict[str, str]:
        """Environment for build-time child processes: no user/site config, temp files in work/."""
        env = {
            k: v for k, v in os.environ.items()
            if not k.upper().startswith(("PIP_", "PYTHON", "VIRTUAL_ENV", "CONDA", "UV_", "JAVA_TOOL_OPTIONS",
                                         "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS", "JAVA_HOME", "JAVA_OPTS", "JAVACMD",
                                         "CLASSPATH"))
        }
        tmp = str(self.tmp)
        env.update({
            "TEMP": tmp, "TMP": tmp, "TMPDIR": tmp,
            "PYTHONNOUSERSITE": "1",
            # pip's HTTP cache lives in the download cache; hashes are still checked on every install.
            "PIP_CACHE_DIR": str(self.cache / "pip-cache"),
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INPUT": "1",
            "PIP_CONFIG_FILE": os.devnull,
        })
        env.update(extra)
        return env

    def java_props(self) -> list[str]:
        """System properties that keep build-time Java off the real user profile."""
        home = self.work / "java-home"
        prefs = self.work / "java-prefs"
        home.mkdir(parents=True, exist_ok=True)
        prefs.mkdir(parents=True, exist_ok=True)
        return [
            f"-Djava.io.tmpdir={self.tmp}",
            f"-Duser.home={home}",
            f"-Djava.util.prefs.userRoot={prefs}",
            f"-Djava.util.prefs.systemRoot={prefs}",
            "-Djava.awt.headless=true",
            "-XX:-UsePerfData",
        ]


def run(cmd: list[str | Path] | str, *, env: dict[str, str] | None = None, cwd: Path | None = None,
        capture: bool = False, timeout: float | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    shown = cmd if isinstance(cmd, str) else " ".join(f'"{c}"' if " " in str(c) else str(c) for c in cmd)
    log(f"    $ {shown}")
    args = cmd if isinstance(cmd, str) else [str(c) for c in cmd]
    r = subprocess.run(
        args, env=env, cwd=cwd, timeout=timeout, text=True, encoding="utf-8", errors="replace",
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )
    if check and r.returncode != 0:
        tail = (r.stdout or "")[-3000:] if capture else ""
        raise BuildError(f"command failed with exit code {r.returncode}: {shown}\n{tail}")
    return r


# --------------------------------------------------------------------------- downloads


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _download(url: str, part: Path) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    h = hashlib.sha256()
    with urllib.request.urlopen(req, timeout=60) as resp, open(part, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        done, next_mark = 0, 0.1
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
            h.update(chunk)
            done += len(chunk)
            if total and done / total >= next_mark:
                log(f"            {done * 100 // total:3d}%  {done >> 20} / {total >> 20} MB")
                next_mark += 0.1
    if total and done != total:
        raise OSError(f"short read: {done} of {total} bytes")
    return h.hexdigest()


def fetch(dl: dict[str, Any], cache: Path) -> Path:
    """Return the verified cached file for one pinned download, downloading it if needed."""
    target = cache / dl["file"]
    want = str(dl["sha256"]).lower()
    if target.exists():
        got = sha256_file(target)
        if got != want:
            raise BuildError(
                f"SHA-256 mismatch for cached {target}\n  expected {want}\n  got      {got}\n"
                "Delete the file if runtimes.json is right; otherwise fix the pin in a reviewed change.")
        log(f"  cached   {dl['file']}  sha256 ok")
        return target
    part = target.with_name(target.name + ".part")
    last: Exception | None = None
    for url in [dl["url"], *dl.get("mirrors", [])]:
        for attempt in range(1, 4):
            log(f"  fetch    {url}" + (f"  (attempt {attempt})" if attempt > 1 else ""))
            try:
                got = _download(url, part)
            except (OSError, urllib.error.URLError) as e:
                last = e
                log(f"           failed: {e}")
                part.unlink(missing_ok=True)
                time.sleep(3 * attempt)
                continue
            if got != want:
                part.unlink(missing_ok=True)
                raise BuildError(f"SHA-256 mismatch for {url}\n  expected {want}\n  got      {got}\nBuild aborted.")
            os.replace(part, target)
            log(f"           sha256 ok  {want}")
            return target
    raise BuildError(f"could not download {dl['file']}: {last}")


# --------------------------------------------------------------------------- archives


def extract(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    name = archive.name.lower()
    if name.endswith((".tar.gz", ".tgz", ".tar.xz", ".tar.bz2", ".tar")):
        with tarfile.open(archive) as tf:
            tf.extractall(dest, filter="data")
    elif name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest)  # noqa: S202 - zipfile drops absolute and '..' components; inputs are hash-pinned
    else:
        raise BuildError(f"don't know how to extract {archive.name}")


def find_one(root: Path, pattern: str, what: str) -> Path:
    hits = sorted(root.rglob(pattern))
    if not hits:
        raise BuildError(f"{what}: nothing matches {pattern} under {root}")
    return hits[0]


def read_text_any(path: Path) -> str:
    """Read a log that may be UTF-16 (msiexec /l*v) or UTF-8/ANSI."""
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    return raw.decode("utf-8", errors="replace")


def rel(ctx: Ctx, p: Path) -> str:
    return p.relative_to(ctx.runtime).as_posix()


def git_info() -> dict[str, Any]:
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, check=True)
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=REPO,
                               capture_output=True, text=True, check=True)
        return {"commit": head.stdout.strip(), "dirty": bool(dirty.stdout.strip())}
    except (OSError, subprocess.CalledProcessError):
        return {"commit": None, "dirty": None}


# --------------------------------------------------------------------------- components


def install_python(ctx: Ctx, entry: dict[str, Any], files: list[Path]) -> dict[str, Any]:
    staging = ctx.work / "python-x"
    extract(files[0], staging)
    src = staging / entry["install"]["strip_prefix"].strip("/")
    if not src.is_dir():
        raise BuildError(f"python archive has no {src.name}/ folder")
    dest = ctx.runtime / "python"
    shutil.move(str(src), str(dest))
    py = ctx.python_exe()
    env = ctx.child_env()

    log("  pip      runtime dependencies (requirements.lock, hashes required)")
    run([py, "-m", "pip", "install", "--no-deps", "--require-hashes", "--only-binary", ":all:",
         "--no-warn-script-location", "-r", REQUIREMENTS_LOCK], env=env)

    wheel = build_baleen_wheel(ctx, py, env)
    log(f"  pip      {wheel.name}")
    run([py, "-m", "pip", "install", "--no-deps", "--no-index", "--no-warn-script-location", wheel], env=env)

    check = run([py, "-I", "-c",
                 "import sys, baleen, starlette, uvicorn, jinja2, multipart, PIL, pypdf; "
                 "print(sys.version.split()[0], baleen.__version__)"], env=env, capture=True)
    pyver, baleen_ver = check.stdout.split()[-2:]
    pkgs = json.loads(run([py, "-I", "-m", "pip", "list", "--format", "json"], env=env, capture=True).stdout
                      .strip().splitlines()[-1])
    pip_check = run([py, "-I", "-m", "pip", "check"], env=env, capture=True, check=False)
    if pip_check.returncode != 0:
        log("  warning  pip check reports:\n" + pip_check.stdout.strip())
    return {
        "python_version": pyver,
        "executable": rel(ctx, py),
        "baleen": {"version": baleen_ver, "wheel": wheel.name, "wheel_sha256": sha256_file(wheel), **git_info()},
        "packages": {p["name"]: p["version"] for p in pkgs},
        "requirements_lock_sha256": sha256_file(REQUIREMENTS_LOCK),
        "pip_check": pip_check.stdout.strip(),
    }


def build_baleen_wheel(ctx: Ctx, py: Path, env: dict[str, str]) -> Path:
    """Build the Baleen wheel with a hash-pinned setuptools (no unhashed build isolation)."""
    deps = ctx.work / "build-deps"
    log("  pip      build backend (scripts/build-requirements.lock, hashes required) -> work folder")
    run([py, "-m", "pip", "install", "--require-hashes", "--only-binary", ":all:", "--no-warn-script-location",
         "--target", deps, "-r", BUILD_REQUIREMENTS_LOCK], env=env)
    # Build from a copy so setuptools' build/ and *.egg-info never land in the checkout.
    src = ctx.work / "baleen-src"
    shutil.copytree(REPO / "src", src / "src", ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
    for name in ("pyproject.toml", "README.md", "LICENSE", "NOTICE"):
        shutil.copy2(REPO / name, src / name)
    out = ctx.work / "wheel"
    run([py, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "--no-index", "-w", out, src],
        env={**env, "PYTHONPATH": str(deps)})
    wheels = sorted(out.glob("walking_whale_baleen-*.whl"))
    if len(wheels) != 1:
        raise BuildError(f"expected one Baleen wheel in {out}, found {[w.name for w in wheels]}")
    return wheels[0]


def install_ffmpeg(ctx: Ctx, entry: dict[str, Any], files: list[Path]) -> dict[str, Any]:
    dest = ctx.runtime / "ffmpeg"
    dest.mkdir(parents=True)
    wanted = list(entry["install"]["binaries"])
    for i, archive in enumerate(files):
        staging = ctx.work / f"ffmpeg-x{i}"
        extract(archive, staging)
        for name in list(wanted):
            hits = [p for p in staging.rglob(name) if p.is_file()]
            if hits:
                shutil.copy2(hits[0], dest / name)
                (dest / name).chmod(0o755)
                wanted.remove(name)
    if wanted:
        raise BuildError(f"ffmpeg archives lack {wanted}")

    ffmpeg = dest / f"ffmpeg{ctx.exe}"
    ffprobe = dest / f"ffprobe{ctx.exe}"
    env = ctx.child_env()
    ver = run([ffmpeg, "-hide_banner", "-version"], env=env, capture=True).stdout
    probe_ver = run([ffprobe, "-hide_banner", "-version"], env=env, capture=True).stdout
    lic_text = run([ffmpeg, "-hide_banner", "-L"], env=env, capture=True).stdout
    if "nonfree" in lic_text.lower():
        raise BuildError("this FFmpeg build contains nonfree parts and may not be redistributed")
    if "version 3 of the License" in lic_text:
        license_effective = "GPL-3.0-or-later"
    elif "version 2 of the License" in lic_text:
        license_effective = "GPL-2.0-or-later"
    else:
        license_effective = "LGPL (no --enable-gpl): libx264 cannot be present"
    config = next((ln.split(":", 1)[1].strip() for ln in ver.splitlines() if ln.startswith("configuration:")), "")
    if "--enable-libx264" not in config:
        raise BuildError("FFmpeg build lacks --enable-libx264")

    # x264 revision from the encoder's SEI message (§15 needs the exact x264 sources).
    sample = ctx.tmp / "x264-probe.h264"
    run([ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=64x64:rate=1",
         "-frames:v", "1", "-c:v", "libx264", "-f", "h264", "-y", sample], env=env)
    m = re.search(rb"x264 - core (\d+)(?: r(\d+) ([0-9a-f]+))?", sample.read_bytes())
    if not m:
        raise BuildError("could not read the x264 version string from a test encode")
    x264 = {"core": int(m.group(1)), "revision": int(m.group(2)) if m.group(2) else None,
            "commit_short": m.group(3).decode() if m.group(3) else None,
            "sei": m.group(0).decode()}
    pin = entry.get("x264") or {}
    if pin.get("revision") and x264["revision"] != pin["revision"]:
        raise BuildError(f"x264 revision {x264['revision']} differs from the pinned r{pin['revision']}")
    if pin.get("commit") and x264["commit_short"] and not pin["commit"].startswith(x264["commit_short"]):
        raise BuildError(f"x264 commit {x264['commit_short']} differs from the pinned {pin['commit']}")
    if pin.get("commit"):
        x264["commit"] = pin["commit"]
    return {
        "ffmpeg": rel(ctx, ffmpeg),
        "ffprobe": rel(ctx, ffprobe),
        "version_line": ver.splitlines()[0],
        "ffprobe_version_line": probe_ver.splitlines()[0],
        "configuration": config,
        "license_effective": license_effective,
        "x264": x264,
    }


def install_libreoffice(ctx: Ctx, entry: dict[str, Any], files: list[Path]) -> dict[str, Any]:
    rule = entry["install"]["rule"]
    if rule == "msi-admin":
        return _libreoffice_msi(ctx, files[0])
    if rule == "dmg-ditto":
        return _libreoffice_dmg(ctx, files[0], entry["install"]["app"])
    raise BuildError(f"unknown LibreOffice install rule {rule}")


def _libreoffice_msi(ctx: Ctx, msi: Path) -> dict[str, Any]:
    target = ctx.runtime / "libreoffice"
    target.mkdir(parents=True)
    msi_log = ctx.work / "libreoffice-msiexec.log"
    # msiexec parses its own command line: PROPERTY="value" must be quoted this way, so pass a string.
    cmd = f'msiexec.exe /a "{msi}" /qn TARGETDIR="{target}" /l*v "{msi_log}"'
    started = time.monotonic()
    r = run(cmd, env=ctx.child_env(), check=False, timeout=3600)
    log(f"           msiexec exit {r.returncode} after {time.monotonic() - started:.0f} s")
    if r.returncode not in (0, 3010):
        tail = "\n".join(read_text_any(msi_log).splitlines()[-40:]) if msi_log.exists() else ""
        hint = ""
        if r.returncode in (1625, 1925, 1730) or "elevat" in tail.lower():
            hint = "\nThe administrative install appears to need elevation. Do not work around it; report it."
        raise BuildError(f"msiexec /a failed with exit code {r.returncode} (log: {msi_log}){hint}\n{tail}")
    candidates = [target / "program" / "soffice.exe", target / "LibreOffice" / "program" / "soffice.exe"]
    soffice = next((c for c in candidates if c.is_file()), None)
    if soffice is None:
        soffice = find_one(target, "soffice.exe", "LibreOffice")
        raise BuildError(f"soffice.exe landed at {soffice}, which tools.py does not look for")
    # The administrative image keeps a stripped copy of the MSI at its root; Baleen never uses it.
    removed = []
    for leftover in target.glob("*.msi"):
        leftover.unlink()
        removed.append(leftover.name)
    # A normal install puts LibreOffice's bundled fonts (Liberation, Carlito, Caladea, DejaVu, Noto, ...)
    # into C:\Windows\Fonts; the administrative image leaves them in <TARGETDIR>\Fonts, where soffice
    # never looks. soffice loads private fonts from share\fonts\truetype, so move them there to get the
    # same metric-compatible substitutes as an installed LibreOffice without touching the system.
    fonts_moved = 0
    fonts_src = target / "Fonts"
    if fonts_src.is_dir():
        fonts_dst = soffice.parent.parent / "share" / "fonts" / "truetype"
        fonts_dst.mkdir(parents=True, exist_ok=True)
        for font in sorted(fonts_src.iterdir()):
            if font.is_file():
                os.replace(font, fonts_dst / font.name)
                fonts_moved += 1
        fonts_src.rmdir()
    version = ""
    ini = soffice.parent / "version.ini"
    if ini.is_file():
        text = ini.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"^MsiProductVersion=([\d.]+)", text, re.M) or re.search(r"^ProductVersion=([\d.]+)", text, re.M)
        version = m.group(1) if m else ""
    return {"soffice": rel(ctx, soffice), "product_version": version, "admin_image_msi_removed": removed,
            "fonts_moved_to_share_fonts_truetype": fonts_moved}


def _libreoffice_dmg(ctx: Ctx, dmg: Path, app: str) -> dict[str, Any]:
    mnt = ctx.work / "lo-mount"
    mnt.mkdir(parents=True, exist_ok=True)
    run(["hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen", "-mountpoint", mnt, dmg])
    try:
        src = mnt / app
        if not src.is_dir():
            raise BuildError(f"{dmg.name} has no {app}")
        run(["ditto", src, ctx.runtime / app])
    finally:
        r = run(["hdiutil", "detach", mnt], check=False)
        if r.returncode != 0:
            run(["hdiutil", "detach", "-force", mnt], check=False)
    soffice = ctx.runtime / app / "Contents" / "MacOS" / "soffice"
    if not soffice.is_file():
        raise BuildError(f"{soffice} missing after ditto")
    with open(ctx.runtime / app / "Contents" / "Info.plist", "rb") as f:
        info = plistlib.load(f)
    return {"soffice": rel(ctx, soffice), "product_version": str(info.get("CFBundleShortVersionString", "")),
            "bundle_version": str(info.get("CFBundleVersion", ""))}


def install_jre(ctx: Ctx, entry: dict[str, Any], files: list[Path]) -> dict[str, Any]:
    staging = ctx.work / "jre-x"
    extract(files[0], staging)
    java_name = f"java{ctx.exe}"
    homes = [p.parent.parent for p in staging.rglob(java_name) if p.parent.name == "bin"]
    homes = [h for h in homes if (h / "release").is_file()]
    if len(homes) != 1:
        raise BuildError(f"expected one JRE home (bin/{java_name} + release) in {files[0].name}, found {homes}")
    # Windows/Linux archives: jdk-*-jre/ ; macOS: jdk-*-jre/Contents/Home/ -> runtime/jre (bin/java at top).
    shutil.move(str(homes[0]), str(ctx.runtime / "jre"))
    java = ctx.java_exe()
    release = (ctx.runtime / "jre" / "release").read_text(encoding="utf-8", errors="replace")
    props = dict(re.findall(r'^([A-Z_]+)="([^"]*)"', release, re.M))
    out = run([java, *ctx.java_props(), "-version"], env=ctx.child_env(), capture=True).stdout
    return {
        "java": rel(ctx, java),
        "java_version": props.get("JAVA_VERSION", ""),
        "java_runtime_version": props.get("JAVA_RUNTIME_VERSION", ""),
        "implementor": props.get("IMPLEMENTOR", ""),
        "version_output": out.strip().splitlines(),
    }


def install_verapdf(ctx: Ctx, entry: dict[str, Any], files: list[Path]) -> dict[str, Any]:
    staging = ctx.work / "verapdf-x"
    extract(files[0], staging)
    jar = find_one(staging, "verapdf-izpack-installer-*.jar", "veraPDF installer")
    target = ctx.runtime / "verapdf"
    template = (REPO / entry["install"]["template"]).read_text(encoding="utf-8")
    if "@INSTALL_PATH@" not in template:
        raise BuildError("verapdf-auto-install.xml lacks the @INSTALL_PATH@ placeholder")
    auto = ctx.work / "verapdf-auto-install.xml"
    auto.write_text(template.replace("@INSTALL_PATH@", xml_escape(str(target))), encoding="utf-8")
    java = ctx.java_exe()
    if not java.is_file():
        raise BuildError("the veraPDF installer needs runtime/jre first")
    run([java, *ctx.java_props(), "-jar", jar, auto], env=ctx.child_env(), cwd=ctx.tmp, timeout=1800)

    launcher = target / ("verapdf.bat" if ctx.is_windows else "verapdf")
    if not launcher.is_file():
        raise BuildError(f"veraPDF installer did not create {launcher}")
    cli_jars = sorted((target / "bin").glob("cli-*.jar"))
    if not cli_jars:
        raise BuildError(f"no bin/cli-*.jar under {target}")
    gui = sorted((target / "bin").glob("gui-*.jar"))
    if gui:
        raise BuildError(f"GUI pack was installed ({gui[0].name}); check verapdf-auto-install.xml")
    if not ctx.is_windows:
        launcher.chmod(0o755)
    # IzPack bookkeeping: an uninstaller would delete runtime/verapdf, and the bundle is removed by deletion.
    removed = []
    for leftover in (target / "Uninstaller", target / ".installationinformation"):
        if leftover.is_dir():
            shutil.rmtree(leftover)
            removed.append(leftover.name)
        elif leftover.is_file():
            leftover.unlink()
            removed.append(leftover.name)
    # verapdf.bat (appassembler) ignores JAVA_HOME and runs %JAVACMD%, else `java` from PATH; the Unix
    # script honours JAVACMD first too. Point both at the bundled JRE.
    java_env = ctx.child_env(JAVA_HOME=str(ctx.runtime / "jre"), JAVACMD=str(java))
    out = run([launcher, "--version"], env=java_env, capture=True, timeout=300).stdout
    m = re.search(r"veraPDF\s+([\d.]+)", out)
    jar_ver = re.search(r"cli-(\d+\.\d+\.\d+)", cli_jars[0].name)
    return {
        "launcher": rel(ctx, launcher),
        "cli_jar": rel(ctx, cli_jars[0]),
        "version": (m.group(1) if m else "") or (jar_ver.group(1) if jar_ver else ""),
        "version_output": out.strip().splitlines()[-3:],
        "izpack_leftovers_removed": removed,
    }


INSTALLERS = {
    "python": install_python,
    "ffmpeg": install_ffmpeg,
    "libreoffice": install_libreoffice,
    "jre": install_jre,
    "verapdf": install_verapdf,
}


# --------------------------------------------------------------------------- orchestration


def check_host(target: str) -> None:
    if target.startswith("win") and sys.platform != "win32":
        raise BuildError(f"{target} must be built on Windows (msiexec /a)")
    if target.startswith("mac") and sys.platform != "darwin":
        raise BuildError(f"{target} must be built on macOS (hdiutil, ditto)")
    if target.startswith("mac"):
        machine = platform.machine().lower()
        want = "arm64" if target == "mac-arm64" else "x86_64"
        if machine != want:
            log(f"warning: building {target} on a {machine} host; pip installs wheels for the bundled interpreter")


def check_ansi_path(label: str, path: Path) -> None:
    """Windows java.exe finds its own JRE through the ANSI code page: a JRE under a folder whose name
    has characters outside that code page fails with "could not find java.dll" (R-02). The build runs
    the bundled Java, so refuse such folders up front."""
    if sys.platform != "win32":
        return
    import ctypes

    codepage = f"cp{ctypes.windll.kernel32.GetACP()}"
    try:
        str(path).encode(codepage)
    except UnicodeEncodeError as e:
        raise BuildError(
            f"{label} {path} contains characters outside the Windows ANSI code page ({codepage}); the bundled "
            "Java cannot start from there (R-02). Build in a folder whose path uses only those characters.") from e
    except LookupError:
        pass


def prepare_work(work: Path) -> None:
    if work.exists():
        if not (work / WORK_MARKER).is_file() and any(work.iterdir()):
            raise BuildError(f"work folder {work} is not empty and was not created by this script")
        shutil.rmtree(work)
    work.mkdir(parents=True)
    (work / WORK_MARKER).write_text("scratch space for scripts/build_bundle.py; safe to delete\n", encoding="utf-8")


def project_version() -> str:
    with open(REPO / "pyproject.toml", "rb") as f:
        return str(tomllib.load(f)["project"]["version"])


def build_runtime(ctx: Ctx) -> Path:
    if ctx.runtime.exists() and any(ctx.runtime.iterdir()):
        raise BuildError(f"{ctx.runtime} is not empty. Build into a new folder (or delete it first).")
    ctx.runtime.mkdir(parents=True, exist_ok=True)
    ctx.cache.mkdir(parents=True, exist_ok=True)

    plan: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for key in COMPONENTS:
        comp = ctx.pins["components"][key]
        entry = comp["platforms"].get(ctx.platform)
        if entry is None:
            raise BuildError(f"runtimes.json has no {ctx.platform} entry for {key}")
        plan.append((key, comp, entry))

    log(f"== downloads -> {ctx.cache}")
    fetched: dict[str, list[Path]] = {}
    for key, _comp, entry in plan:
        fetched[key] = [fetch(dl, ctx.cache) for dl in entry["downloads"]]

    components: dict[str, Any] = {}
    for key, comp, entry in plan:
        log(f"== {key}: {comp['name']} {entry['version']}")
        started = time.monotonic()
        details = INSTALLERS[key](ctx, entry, fetched[key])
        log(f"   done in {time.monotonic() - started:.0f} s")
        components[key] = {
            "name": comp["name"],
            "version": entry["version"],
            **({"build": entry["build"]} if "build" in entry else {}),
            "license": comp["license"],
            "dest": comp["dest"],
            "downloads": [
                {"file": dl["file"], "url": dl["url"], "sha256": dl["sha256"], "checksum_source": dl["checksum_source"]}
                for dl in entry["downloads"]
            ],
            "sources": entry.get("gpl_sources", []) + comp.get("sources", []),
            "installed": details,
        }
        if key == "ffmpeg":
            components[key]["license"] = details["license_effective"]

    record = {
        "schema_version": 1,
        "baleen_version": ctx.version,
        "platform": ctx.platform,
        "built_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "build_host": {"os": platform.platform(), "python": platform.python_version()},
        "runtimes_json_sha256": sha256_file(RUNTIMES_JSON),
        "components": components,
    }
    out = ctx.runtime / "VERSIONS.json"
    out.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    log(f"== wrote {out}")
    return out


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    p = argparse.ArgumentParser(description="Build Baleen's bundled runtime (spec §14.6).")
    p.add_argument("--platform", required=True, choices=PLATFORMS)
    p.add_argument("--version", help="bundle version (default: pyproject.toml)")
    p.add_argument("--cache", type=Path, default=REPO / ".scratch" / "cache", help="download cache folder")
    p.add_argument("--work", type=Path, help="scratch folder (default: build/bundle-work-<platform>)")
    p.add_argument("--keep-work", action="store_true", help="keep the scratch folder afterwards")
    p.add_argument("--runtime-only", action="store_true", help="build only runtime/ into --dest")
    p.add_argument("--dest", type=Path, help="with --runtime-only: the runtime folder to create (default: ./runtime)")
    ns = p.parse_args(argv)

    started = time.monotonic()
    try:
        check_host(ns.platform)
        version = ns.version or project_version()
        if not ns.runtime_only:
            raise BuildError("Full bundle assembly (launchers, README.txt, THIRD_PARTY_NOTICES, smoke test, zip) "
                             "is not implemented yet; use --runtime-only --dest DIR.")
        runtime = (ns.dest or REPO / "runtime").resolve()
        work = (ns.work or REPO / "build" / f"bundle-work-{ns.platform}").resolve()
        if work == runtime or runtime in work.parents or work in runtime.parents:
            raise BuildError("--work and --dest must not contain each other")
        check_ansi_path("--dest", runtime)
        check_ansi_path("--work", work)
        prepare_work(work)
        pins = json.loads(RUNTIMES_JSON.read_text(encoding="utf-8"))
        ctx = Ctx(platform=ns.platform, version=version, cache=ns.cache.resolve(), runtime=runtime,
                  work=work, pins=pins)
        log(f"Baleen {version} runtime for {ns.platform}\n  dest  {runtime}\n  cache {ctx.cache}\n  work  {work}")
        build_runtime(ctx)
        if not ns.keep_work:
            shutil.rmtree(work, ignore_errors=True)
    except BuildError as e:
        log(f"\nBUILD FAILED: {e}")
        return 1
    log(f"\nRuntime complete in {time.monotonic() - started:.0f} s: {runtime}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
