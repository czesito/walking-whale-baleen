"""Build Baleen's portable bundle, or just its runtime/ folder — spec §14.6.

    python scripts/build_bundle.py --platform win-x64|mac-arm64|mac-x64 [--version X.Y.Z] [--cache DIR]
                                   [--dist DIR] [--clean] [--skip-smoke] [--smoke-expected CSV]
    python scripts/build_bundle.py --platform win-x64 --runtime-only --dest DIR [--cache DIR] [--work DIR]
    python scripts/build_bundle.py --smoke-only BUNDLE_FOLDER [--smoke-expected CSV] [--work DIR]

Every download is pinned in scripts/runtimes.json. It is fetched into the cache folder and its
SHA-256 is checked before use; a mismatch aborts the build. Installation never touches the system:
LibreOffice uses an administrative MSI install (Windows) or a read-only DMG mount (macOS), veraPDF a
headless IzPack install into runtime/verapdf, and Python packages go into runtime/python only.

The runtime step ends by writing runtime/VERSIONS.json (exact versions and the SHA-256 of every
download); its presence marks a complete runtime, and the script refuses to build into a non-empty
folder. A full build then adds the launchers, README.txt, LICENSE, NOTICE and THIRD_PARTY_NOTICES/,
runs the smoke test from the bundle folder (doctor, then convert on the smoke fixtures), and writes
dist/baleen-<ver>-<platform>.zip (folder Baleen-<ver>-<platform>/) plus dist/SHA256SUMS.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import email.parser
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
FFMPEG_LIBRARIES_JSON = REPO / "scripts" / "ffmpeg-libraries.json"
LICENSE_TEXTS = REPO / "scripts" / "licenses"
REQUIREMENTS_LOCK = REPO / "requirements.lock"
BUILD_REQUIREMENTS_LOCK = REPO / "scripts" / "build-requirements.lock"
LAUNCHERS = REPO / "launchers"
SMOKE_EXPECTED = REPO / "tests" / "fixtures" / "smoke-expected.csv"
RELEASES_URL = "https://github.com/czesito/walking-whale-baleen/releases"

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
    # Likewise the Visual C++ runtime merge modules land in <TARGETDIR>\System64 (x64) and \System
    # (x86) instead of C:\Windows\System32. soffice.bin needs vcruntime140.dll, msvcp140.dll, ... and a
    # clean Windows without the VC++ redistributable has none, so deploy the x64 DLLs app-locally next
    # to soffice.bin (Microsoft's supported "app-local" deployment) and drop both folders.
    vc_copied = []
    sys64 = target / "System64"
    if sys64.is_dir():
        for dll in sorted(sys64.glob("*.dll")):
            dst = soffice.parent / dll.name
            if not dst.exists():
                shutil.copy2(dll, dst)
                vc_copied.append(dll.name)
    for folder in (target / "System64", target / "System"):
        if folder.is_dir():
            shutil.rmtree(folder)
    version = ""
    ini = soffice.parent / "version.ini"
    if ini.is_file():
        text = ini.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"^MsiProductVersion=([\d.]+)", text, re.M) or re.search(r"^ProductVersion=([\d.]+)", text, re.M)
        version = m.group(1) if m else ""
    return {"soffice": rel(ctx, soffice), "product_version": version, "admin_image_msi_removed": removed,
            "fonts_moved_to_share_fonts_truetype": fonts_moved, "vc_runtime_app_local": vc_copied}


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


def rmtree_force(path: Path) -> None:
    """shutil.rmtree that also removes read-only files (git pack files on Windows)."""
    import stat

    def onexc(func, p, _exc):  # noqa: ANN001
        os.chmod(p, stat.S_IWRITE)
        func(p)

    if path.exists():
        shutil.rmtree(path, onexc=onexc)


def prepare_work(work: Path) -> None:
    if work.exists():
        if not (work / WORK_MARKER).is_file() and any(work.iterdir()):
            raise BuildError(f"work folder {work} is not empty and was not created by this script")
        rmtree_force(work)
    work.mkdir(parents=True)
    (work / WORK_MARKER).write_text("scratch space for scripts/build_bundle.py; safe to delete\n", encoding="utf-8")


def project_version() -> str:
    with open(REPO / "pyproject.toml", "rb") as f:
        return str(tomllib.load(f)["project"]["version"])


def build_runtime(ctx: Ctx) -> dict[str, Any]:
    """Build runtime/ and return the VERSIONS.json record."""
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
    return record


# --------------------------------------------------------------------------- bundle files


def text_bytes(text: str, crlf: bool) -> bytes:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return (text.replace("\n", "\r\n") if crlf else text).encode("utf-8")


def assemble_bundle(ctx: Ctx, bundle: Path, record: dict[str, Any]) -> None:
    """Launcher, README.txt, LICENSE, NOTICE and THIRD_PARTY_NOTICES/ next to runtime/ (§14.2)."""
    log("== bundle files")
    crlf = ctx.is_windows
    if ctx.is_windows:
        # .bat files must have CRLF line endings, whatever the checkout did.
        src = LAUNCHERS / "Start Baleen.bat"
        (bundle / src.name).write_bytes(text_bytes(src.read_text(encoding="utf-8"), True))
    else:
        src = LAUNCHERS / "Start Baleen.command"
        (bundle / src.name).write_bytes(text_bytes(src.read_text(encoding="utf-8"), False))
        (bundle / src.name).chmod(0o755)
    (bundle / "README.txt").write_bytes(text_bytes((LAUNCHERS / "README.txt").read_text(encoding="utf-8"), crlf))
    for name in ("LICENSE", "NOTICE"):
        (bundle / name).write_bytes(text_bytes((REPO / name).read_text(encoding="utf-8"), crlf))
    write_notices(ctx, bundle / "THIRD_PARTY_NOTICES", record)


# --------------------------------------------------------------------------- THIRD_PARTY_NOTICES


RULE = "-" * 78


def _read(path: Path) -> str:
    return read_text_any(path).replace("\r\n", "\n").replace("\r", "\n")


def _section(title: str, body: str) -> str:
    return f"\n\n{RULE}\n{title}\n{RULE}\n\n{body.strip()}\n"


def _wrap(text: str, indent: str = "") -> str:
    import textwrap

    return textwrap.fill(text, 78, initial_indent=indent, subsequent_indent=indent, break_on_hyphens=False,
                         break_long_words=False)


def ffmpeg_libraries(target: str) -> dict[str, Any]:
    builds = json.loads(FFMPEG_LIBRARIES_JSON.read_text(encoding="utf-8"))["builds"]
    build = builds[target]
    return builds[build["same_as"]] if "same_as" in build else build


def resolve_x264(text: str, x264: dict[str, Any]) -> str:
    """Fill the '@x264' placeholder of the macOS entries with the revision found at build time."""
    rev = x264.get("commit") or x264.get("commit_short") or "unknown"
    return text.replace("@x264", rev[:8])


class NoticeWriter:
    def __init__(self, out: Path, crlf: bool) -> None:
        self.out = out
        self.crlf = crlf
        self.written: list[str] = []

    def emit(self, rel_path: str, text: str) -> None:
        path = self.out / rel_path
        if path.exists():
            raise BuildError(f"{path} already exists (copied from the repository); generated notices never overwrite")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text_bytes(text.rstrip() + "\n", self.crlf))
        self.written.append(rel_path)


def _component_header(title: str, comp: dict[str, Any], pin: dict[str, Any]) -> str:
    version = comp["version"] + (f" ({comp['build']})" if comp.get("build") else "")
    lines = [title, "=" * len(title), "",
             f"Version:       {version}",
             f"Licence:       {comp['license']}",
             f"Homepage:      {pin.get('homepage', '')}",
             f"Installed in:  runtime/{comp['dest']}"]
    if pin.get("license_note"):
        lines += ["", _wrap(pin["license_note"])]
    lines += ["", "Downloaded from (SHA-256 checked when the bundle was built):"]
    for dl in comp["downloads"]:
        lines += [f"  {dl['url']}", f"    sha256 {dl['sha256']}"]
    lines += ["", "Source code:"]
    for s in comp["sources"]:
        lines.append(f"  {s['what']}:")
        lines.append(f"    {s['url']}")
    return "\n".join(lines) + "\n"


def write_notices(ctx: Ctx, out: Path, record: dict[str, Any]) -> list[str]:
    """One notice file per bundled component (licence text + source links), generated from
    runtimes.json, VERSIONS.json and the installed files. Files that the repository already keeps
    under THIRD_PARTY_NOTICES/ (e.g. assets/) are copied first and never overwritten."""
    repo_notices = REPO / "THIRD_PARTY_NOTICES"
    if repo_notices.is_dir():
        shutil.copytree(repo_notices, out, dirs_exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    w = NoticeWriter(out, ctx.is_windows)
    comps = record["components"]
    pins = ctx.pins["components"]

    # CPython
    c = comps["python"]
    text = _component_header("CPython (python-build-standalone)", c, pins["python"])
    lic = [ctx.runtime / "python" / "LICENSE.txt", *sorted((ctx.runtime / "python" / "lib").glob("python3*/LICENSE.txt"))]
    lic_file = next((p for p in lic if p.is_file()), None)
    text += _section(f"License (runtime/{rel(ctx, lic_file)})" if lic_file else "License",
                     _read(lic_file) if lic_file else "See https://docs.python.org/3/license.html")
    w.emit("python.txt", text)

    # Python packages in runtime/python
    for dist in sorted((ctx.runtime / "python").glob("**/site-packages/*.dist-info")):
        notice = _dist_notice(ctx, dist)
        if notice:
            w.emit(f"python-packages/{notice[0]}.txt", notice[1])

    # FFmpeg
    w.emit("ffmpeg.txt", _ffmpeg_notice(ctx, comps["ffmpeg"], pins["ffmpeg"]))

    # LibreOffice
    c = comps["libreoffice"]
    lo_root = (ctx.runtime / c["installed"]["soffice"]).parent.parent
    text = _component_header("LibreOffice", c, pins["libreoffice"])
    text += "\n" + _wrap("LibreOffice is shipped unmodified. Its own licence, credits and notice files are "
                         f"inside runtime/{rel(ctx, lo_root)} (license.txt or LICENSE, LICENSE.html, NOTICE, "
                         "CREDITS.fodt). On Windows the build moves the bundled fonts from the installer's "
                         "Fonts folder to share/fonts/truetype and places the Visual C++ runtime DLLs from the "
                         "installer next to soffice.bin, so no system folder is needed.") + "\n"
    candidates = [lo_root / "license.txt", lo_root / "LICENSE", lo_root / "Resources" / "LICENSE",
                  lo_root / "Resources" / "license.txt", *sorted((lo_root / "Resources").glob("LICENSE*"))]
    lic_file = next((p for p in candidates if p.is_file()), None)
    if lic_file:
        text += _section(f"License (runtime/{rel(ctx, lic_file)})", _read(lic_file))
    w.emit("libreoffice.txt", text)

    # Temurin JRE
    c = comps["jre"]
    jre = ctx.runtime / "jre"
    text = _component_header("Eclipse Temurin JRE", c, pins["jre"])
    text += "\n" + _wrap("The per-module notices of the third-party code inside the JRE are in runtime/jre/legal/.") + "\n"
    for p in (jre / "NOTICE", jre / "legal" / "java.base" / "LICENSE", jre / "legal" / "java.base" / "ASSEMBLY_EXCEPTION",
              jre / "legal" / "java.base" / "ADDITIONAL_LICENSE_INFO"):
        if p.is_file():
            text += _section(f"runtime/{rel(ctx, p)}", _read(p))
    w.emit("jre.txt", text)

    # veraPDF
    c = comps["verapdf"]
    text = _component_header("veraPDF (Greenfield CLI)", c, pins["verapdf"])
    text += "\n" + _wrap("veraPDF is dual licensed: you may use it under the GNU GPL version 3 or later, or under "
                         "the Mozilla Public License 2.0. Both texts follow, then the notices of the third-party "
                         "libraries packed inside the veraPDF CLI jar.") + "\n"
    text += _section("GNU General Public License, version 3", _read(LICENSE_TEXTS / "GPL-3.0.txt"))
    text += _section("Mozilla Public License, version 2.0", _read(LICENSE_TEXTS / "MPL-2.0.txt"))
    jar = ctx.runtime / c["installed"]["cli_jar"]
    with zipfile.ZipFile(jar) as zf:
        for name in sorted(zf.namelist()):
            if re.fullmatch(r"META-INF/[^/]*(LICEN[CS]E|NOTICE)[^/]*", name, re.I):
                body = zf.read(name).decode("utf-8", errors="replace")
                text += _section(f"{name} (inside runtime/{rel(ctx, jar)})", body)
    w.emit("verapdf.txt", text)

    w.emit("README.txt", _notices_index(ctx, record, w.written))
    log(f"   THIRD_PARTY_NOTICES: {len(w.written)} files")
    return w.written


def _dist_notice(ctx: Ctx, dist: Path) -> tuple[str, str] | None:
    meta_path = dist / "METADATA"
    if not meta_path.is_file():
        return None
    meta = email.parser.Parser().parsestr(_read(meta_path), headersonly=True)
    name, version = meta.get("Name", dist.name), meta.get("Version", "")
    canonical = re.sub(r"[-_.]+", "-", name).lower()
    if canonical == "walking-whale-baleen":
        return None
    licence = meta.get("License-Expression") or ""
    if not licence:
        raw = (meta.get("License") or "").strip()
        licence = raw if raw and len(raw) < 120 and "\n" not in raw else ""
    classifiers = [x.split("::", 1)[1].strip() for x in meta.get_all("Classifier") or [] if x.startswith("License ::")]
    urls = [u for u in [meta.get("Home-page")] if u] + [u.split(",", 1)[-1].strip() for u in meta.get_all("Project-URL") or []]
    lic_dir = dist / "licenses"
    files = (sorted(p for p in lic_dir.rglob("*") if p.is_file()) if lic_dir.is_dir() else
             sorted(p for p in dist.iterdir() if re.match(r"(LICEN[CS]E|COPYING|NOTICE|AUTHORS)", p.name, re.I)))
    title = f"{name} {version}"
    lines = [title, "=" * len(title), "",
             f"Licence:       {licence or ', '.join(classifiers) or 'see below'}",
             f"Installed in:  runtime/{rel(ctx, dist.parent)}"]
    for u in dict.fromkeys(urls):
        lines.append(f"Project URL:   {u}")
    text = "\n".join(lines) + "\n"
    for f in files:
        text += _section(f.relative_to(dist).as_posix(), _read(f))
    if not files:
        text += "\n(The package ships no licence file.)\n"
    return canonical, text


def _ffmpeg_notice(ctx: Ctx, comp: dict[str, Any], pin: dict[str, Any]) -> str:
    inst = comp["installed"]
    libs = ffmpeg_libraries(ctx.platform)
    x264 = inst["x264"]
    text = _component_header("FFmpeg and FFprobe", comp, pin)
    build = [
        f"Build:           {libs['builder']}",
        f"Version string:  {inst['version_line']}",
        f"Licence of this build (from ffmpeg -L): {inst['license_effective']}",
        f"x264:            {x264['sei']}" + (f" (commit {x264['commit']})" if x264.get("commit") else ""),
        "",
        "Configuration:",
        _wrap(inst["configuration"], "  "),
    ]
    text += _section("Build", "\n".join(build))
    lines = [_wrap("The ffmpeg and ffprobe programs in runtime/ffmpeg are statically linked with the libraries "
                   f"below. Versions are as recorded by the builder ({libs['versions_from']}). For each "
                   "copyleft library (GPL, LGPL, MPL) the exact source archive is attached to the GitHub "
                   f"Release of this bundle ({RELEASES_URL}) next to the FFmpeg source; where that is not "
                   "possible the entry says why. Permissive libraries are listed with their upstream "
                   "project, where their licence texts can be found."), ""]
    for lib in libs["libraries"]:
        lines.append(f"{lib['name']} {resolve_x264(lib['version'], x264)}  [{lib['license']}]")
        lines.append(f"    upstream: {lib['upstream']}")
        if "fetch" in lib:
            lines.append(f"    source:   attached to the release as {resolve_x264(lib['fetch']['file'], x264)}")
        elif "not_covered" in lib:
            lines.append("    source:   NOT COVERED. " + lib["not_covered"])
        else:
            lines.append("    source:   permissive licence; see upstream")
    if libs.get("unversioned"):
        lines += ["", _wrap(libs["unversioned_note"]), ""]
        for lib in libs["unversioned"]:
            lines.append(f"{lib['name']}  [{lib['license']}]  (version not published by the builder)")
    text += _section("Statically linked libraries", "\n".join(lines))
    spdx = "GPL-3.0" if "GPL-3.0" in inst["license_effective"] else "GPL-2.0"
    text += _section(f"GNU General Public License ({spdx})", _read(LICENSE_TEXTS / f"{spdx}.txt"))
    return text


def _notices_index(ctx: Ctx, record: dict[str, Any], written: list[str]) -> str:
    lines = ["Third-party notices", "===================", "",
             _wrap(f"Baleen {record['baleen_version']} ({record['platform']}) bundles the programs below in "
                   "runtime/. Each is shipped unmodified, runs as a separate program, and keeps its own licence. "
                   "Each file in this folder holds a component's licence text and where to get its source code."),
             ""]
    for key, comp in record["components"].items():
        lines.append(f"{comp['name']} {comp['version']}")
        lines.append(f"    licence: {comp['license']}")
        lines.append(f"    notice:  {key}.txt")
    pkgs = sorted(p for p in written if p.startswith("python-packages/"))
    if pkgs:
        lines += ["", "Python packages inside runtime/python (one file each in python-packages/):"]
        lines += [f"    {Path(p).stem}" for p in pkgs]
    if (ctx.runtime.parent / "THIRD_PARTY_NOTICES" / "assets").is_dir():
        lines += ["", "Files under assets/ cover the web assets of Baleen's own pages (for example htmx)."]
    lines += ["", _wrap("GPL source code: the exact source archives for the bundled FFmpeg build (FFmpeg, x264 and "
                        f"the other copyleft libraries listed in ffmpeg.txt) are attached to the GitHub Release: {RELEASES_URL}."),
              "", _wrap("runtime/VERSIONS.json records the exact version and SHA-256 of every download used to "
                        "build this bundle.")]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- smoke test


def bundle_python(bundle: Path) -> Path:
    win = bundle / "runtime" / "python" / "python.exe"
    return win if win.is_file() else bundle / "runtime" / "python" / "bin" / "python3"


def smoke_env(bundle: Path) -> dict[str, str]:
    """The launcher's environment (§14.3), without inheriting developer settings."""
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(("PYTHON", "BALEEN_", "PIP_", "VIRTUAL_ENV", "CONDA", "UV_", "JAVA",
                                        "_JAVA", "JDK_JAVA", "CLASSPATH"))}
    data = bundle / "data"
    for sub in ("tmp", "java"):
        (data / sub).mkdir(parents=True, exist_ok=True)
    tmp = str(data / "tmp")
    env.update({"BALEEN_HOME": str(bundle), "TEMP": tmp, "TMP": tmp, "TMPDIR": tmp,
                "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
                "JAVA_TOOL_OPTIONS": f'-Djava.util.prefs.userRoot="{data / "java"}" '
                                     f'-Djava.util.prefs.systemRoot="{data / "java"}" -Djava.io.tmpdir="{tmp}" '
                                     "-XX:-UsePerfData"})
    return env


def tree_state(root: Path, skip: str = "data") -> dict[str, tuple[int, int]]:
    state: dict[str, tuple[int, int]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        if Path(dirpath) == root and skip in dirnames:
            dirnames.remove(skip)
        for name in filenames:
            p = Path(dirpath) / name
            st = p.lstat()
            state[p.relative_to(root).as_posix()] = (st.st_size, st.st_mtime_ns)
    return state


def _norm_source(path: str, src: Path) -> str:
    p = path.replace("\\", "/")
    s = src.as_posix()
    if p.lower().startswith(s.lower() + "/"):
        p = p[len(s) + 1:]
    return p[2:] if p.startswith("./") else p


def smoke_test(bundle: Path, work: Path, expected: Path | None = SMOKE_EXPECTED) -> dict[str, Any]:
    """Doctor (all five tools bundled), then convert the smoke fixtures from the bundle folder.
    With an expected-statuses CSV (when the file exists) every row must match it exactly."""
    log(f"== smoke test in {bundle}")
    py = bundle_python(bundle)
    if not py.is_file():
        raise BuildError(f"no bundled Python at {py}")
    before = tree_state(bundle)
    env = smoke_env(bundle)
    smoke = work / "smoke"
    if smoke.exists():
        shutil.rmtree(smoke)
    smoke.mkdir(parents=True)

    doc = subprocess.run([str(py), "-m", "baleen", "doctor", "--json"], env=env, cwd=bundle, capture_output=True,
                         text=True, encoding="utf-8", errors="replace", timeout=600, stdin=subprocess.DEVNULL)
    try:
        info = json.loads(doc.stdout[doc.stdout.index("{"):])
    except ValueError as e:
        raise BuildError(f"baleen doctor gave no JSON (exit {doc.returncode}):\n{doc.stdout}\n{doc.stderr}") from e
    tools = info["tools"]
    for key, t in tools.items():
        log(f"   doctor  {key:12} {t['source']:8} {t['version'] or '-':40} {t['path'] or t['error']}")
    bad = [k for k, t in tools.items() if not t["path"] or t["source"] != "bundled"]
    if bad:
        raise BuildError(f"doctor: not found in the bundle: {', '.join(bad)}")

    src, out = smoke / "source", smoke / "output"
    fx_env = dict(env, BALEEN_RUNTIME=str(bundle / "runtime"), BALEEN_HOME=str(smoke / "fixture-home"))
    run([py, REPO / "tests" / "fixtures" / "make_fixtures.py", "--smoke", src], env=fx_env, cwd=REPO, timeout=900)
    conv = subprocess.run([str(py), "-m", "baleen", "convert", str(src), str(out), "--quiet"], env=env, cwd=bundle,
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800,
                          stdin=subprocess.DEVNULL)
    log(f"   convert exit {conv.returncode}")
    if conv.returncode not in (0, 1, 2):
        raise BuildError(f"baleen convert failed (exit {conv.returncode}):\n{conv.stdout[-3000:]}\n{conv.stderr[-3000:]}")
    reports = sorted((out / "_baleen").glob("report-*.csv"), key=lambda p: p.stat().st_mtime)
    if not reports:
        raise BuildError(f"no report in {out / '_baleen'}:\n{conv.stderr[-3000:]}")
    with open(reports[-1], encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    by_source = {_norm_source(r["source_path"], src): r for r in rows}
    for path, r in sorted(by_source.items()):
        log(f"   {r['status']:13} {r['reason'] or '-':28} {path}")

    problems = []
    expected_files = sorted(p.relative_to(src).as_posix() for p in src.rglob("*") if p.is_file())
    problems += [f"no report row for {p}" for p in expected_files if p not in by_source]
    for path, r in by_source.items():
        if re.search(r"TOOL_MISSING|VALIDATOR_MISSING", r["reason"] or ""):
            problems.append(f"{path}: {r['status']} {r['reason']} (a bundled tool was not used)")
    if expected is not None and expected.is_file():
        log(f"   comparing with {expected}")
        problems += compare_expected(by_source, expected)
    elif expected is not None:
        log(f"   {expected} not found: checking only that every file has a row and no tool was missing")

    after = tree_state(bundle)
    changed = sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))
    problems += [f"the run changed {p} inside the bundle (runtime/ must stay read-only)" for p in changed[:20]]
    shutil.rmtree(bundle / "data", ignore_errors=True)
    if problems:
        raise BuildError("smoke test failed:\n  " + "\n  ".join(problems))
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    log(f"   smoke test passed: {len(rows)} rows {counts}")
    return {"rows": len(rows), "counts": counts, "convert_exit": conv.returncode,
            "expected_csv": bool(expected and expected.is_file())}


def _reasons(text: str | None) -> set[str]:
    return {r.strip() for r in (text or "").split(";") if r.strip()}


def compare_expected(by_source: dict[str, dict[str, str]], expected: Path) -> list[str]:
    """Compare report rows with an expected-statuses CSV.

    The file has a header row with at least source_path and status, optionally reason ('#' lines are
    comments). source_path is relative to the smoke source folder with '/' separators; reason holds
    ';'-separated codes and is compared order-insensitively. Every report row must be listed, and every
    listed row must exist."""
    with open(expected, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(line for line in f if line.strip() and not line.lstrip().startswith("#")))
    problems: list[str] = []
    listed = set()
    for e in rows:
        path = (e.get("source_path") or "").strip().replace("\\", "/")
        listed.add(path)
        r = by_source.get(path)
        if r is None:
            problems.append(f"expected row missing from the report: {path}")
            continue
        if r["status"] != (e.get("status") or "").strip():
            problems.append(f"{path}: status {r['status']}, expected {e.get('status')}")
        if "reason" in e and _reasons(r.get("reason")) != _reasons(e.get("reason")):
            problems.append(f"{path}: reason {r.get('reason')!r}, expected {e.get('reason')!r}")
    problems += [f"{p}: in the report but not in {expected.name}" for p in sorted(set(by_source) - listed)]
    return problems


# --------------------------------------------------------------------------- zip


def make_zip(bundle: Path, zip_path: Path) -> str:
    log(f"== zip {zip_path}")
    part = zip_path.with_name(zip_path.name + ".part")
    part.unlink(missing_ok=True)
    if sys.platform == "darwin":
        # ditto keeps symlinks, permissions and extended attributes inside LibreOffice.app.
        run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", bundle, part])
    else:
        with zipfile.ZipFile(part, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6,
                             strict_timestamps=False) as zf:
            for dirpath, dirnames, filenames in os.walk(bundle):
                dirnames.sort()
                d = Path(dirpath)
                arc_dir = (Path(bundle.name) / d.relative_to(bundle)).as_posix()
                if not filenames and not dirnames:
                    zf.writestr(arc_dir.rstrip("/") + "/", b"")
                for name in sorted(filenames):
                    zf.write(d / name, f"{arc_dir}/{name}")
    os.replace(part, zip_path)
    digest = sha256_file(zip_path)
    zip_path.with_name(zip_path.name + ".sha256").write_text(f"{digest}  {zip_path.name}\n", encoding="utf-8")
    sums = sorted(p.read_text(encoding="utf-8").strip() for p in zip_path.parent.glob("baleen-*.zip.sha256"))
    (zip_path.parent / "SHA256SUMS").write_text("\n".join(sums) + "\n", encoding="utf-8")
    log(f"   {zip_path.stat().st_size >> 20} MB  sha256 {digest}")
    return digest


def longest_path(bundle: Path) -> tuple[int, str]:
    best = (0, "")
    for dirpath, _dirs, files in os.walk(bundle):
        for name in files:
            relp = (Path(bundle.name) / Path(dirpath, name).relative_to(bundle)).as_posix()
            best = max(best, (len(relp), relp))
    return best


# --------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    p = argparse.ArgumentParser(description="Build Baleen's portable bundle or its runtime/ (spec §14.6).")
    p.add_argument("--platform", choices=PLATFORMS, help="target platform (required unless --smoke-only)")
    p.add_argument("--version", help="bundle version (default: pyproject.toml)")
    p.add_argument("--cache", type=Path, default=REPO / ".scratch" / "cache", help="download cache folder")
    p.add_argument("--work", type=Path, help="scratch folder (default: build/bundle-work-<platform>)")
    p.add_argument("--keep-work", action="store_true", help="keep the scratch folder afterwards")
    p.add_argument("--runtime-only", action="store_true", help="build only runtime/ into --dest")
    p.add_argument("--dest", type=Path, help="with --runtime-only: the runtime folder to create (default: ./runtime)")
    p.add_argument("--dist", type=Path, default=REPO / "dist", help="output folder for the bundle and zip")
    p.add_argument("--clean", action="store_true", help="replace an existing bundle folder and zip in --dist")
    p.add_argument("--skip-smoke", action="store_true", help="do not run the smoke test (development only)")
    p.add_argument("--smoke-only", type=Path, metavar="BUNDLE", help="run the smoke test on an existing bundle folder")
    p.add_argument("--smoke-expected", type=Path, default=SMOKE_EXPECTED, metavar="CSV",
                   help="expected source_path/status/reason of the smoke run; skipped when the file does not exist "
                        "(default: tests/fixtures/smoke-expected.csv)")
    ns = p.parse_args(argv)

    started = time.monotonic()
    try:
        if ns.smoke_only:
            work = (ns.work or REPO / "build" / "smoke-work").resolve()
            prepare_work(work)
            smoke_test(ns.smoke_only.resolve(), work, ns.smoke_expected.resolve())
            if not ns.keep_work:
                shutil.rmtree(work, ignore_errors=True)
            log(f"\nSmoke test passed in {time.monotonic() - started:.0f} s")
            return 0
        if not ns.platform:
            p.error("--platform is required")
        check_host(ns.platform)
        version = ns.version or project_version()
        name = f"Baleen-{version}-{ns.platform}"
        dist = ns.dist.resolve()
        bundle = dist / name
        zip_path = dist / f"baleen-{version}-{ns.platform}.zip"
        runtime = (ns.dest or REPO / "runtime").resolve() if ns.runtime_only else bundle / "runtime"
        work = (ns.work or REPO / "build" / f"bundle-work-{ns.platform}").resolve()
        if work == runtime or runtime in work.parents or work in runtime.parents:
            raise BuildError("--work and the output folder must not contain each other")
        check_ansi_path("output folder", runtime)
        check_ansi_path("--work", work)
        if not ns.runtime_only and bundle.exists():
            if not ns.clean:
                raise BuildError(f"{bundle} exists; pass --clean to replace it")
            shutil.rmtree(bundle)
            zip_path.unlink(missing_ok=True)
        prepare_work(work)
        pins = json.loads(RUNTIMES_JSON.read_text(encoding="utf-8"))
        ctx = Ctx(platform=ns.platform, version=version, cache=ns.cache.resolve(), runtime=runtime,
                  work=work, pins=pins)
        log(f"Baleen {version} for {ns.platform}\n  runtime {runtime}\n  cache   {ctx.cache}\n  work    {work}")
        record = build_runtime(ctx)
        if not ns.runtime_only:
            assemble_bundle(ctx, bundle, record)
            length, longest = longest_path(bundle)
            log(f"   longest path inside the zip: {length} characters ({longest})")
            if not ns.skip_smoke:
                smoke_test(bundle, work, ns.smoke_expected.resolve())
            make_zip(bundle, zip_path)
        if not ns.keep_work:
            shutil.rmtree(work, ignore_errors=True)
    except BuildError as e:
        log(f"\nBUILD FAILED: {e}")
        return 1
    done = runtime if ns.runtime_only else zip_path
    log(f"\nDone in {time.monotonic() - started:.0f} s: {done}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
