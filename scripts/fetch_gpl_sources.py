"""Fetch the corresponding source archives for a bundle's FFmpeg build (spec §15, R-08).

    python scripts/fetch_gpl_sources.py --platform win-x64|mac-arm64|mac-x64
                                        [--versions RUNTIME/VERSIONS.json] [--out dist/gpl-sources]
                                        [--cache DIR] [--work DIR] [--strict]

Always fetched (the build fails without them):
  - the FFmpeg release tarball pinned in runtimes.json (SHA-256 checked), and
  - x264 at the exact revision of the build (pinned for win-x64; read from VERSIONS.json for macOS,
    where the build script records it from the encoder's SEI message).
Then every copyleft library listed with a 'fetch' rule in scripts/ffmpeg-libraries.json. A library
that cannot be fetched is reported as NOT COVERED in the manifest (fatal with --strict).

Writes the archives and GPL-SOURCES-<platform>.txt (what each archive is, its SHA-256, and what is
not covered) into --out, ready to attach to the GitHub Release next to the zips.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_bundle as bb  # noqa: E402


def sha256(path: Path) -> str:
    return bb.sha256_file(path)


def download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": bb.USER_AGENT})
    part = dest.with_name(dest.name + ".part")
    with urllib.request.urlopen(req, timeout=120) as resp, open(part, "wb") as out:
        ctype = resp.headers.get("Content-Type", "")
        if ctype.startswith("text/html"):
            raise OSError(f"got an HTML page instead of an archive (Content-Type {ctype})")
        shutil.copyfileobj(resp, out, 1 << 20)
    part.replace(dest)


def git_archive(repo: str, rev: str, dest: Path, work: Path) -> str:
    """Archive one revision of a git repository; returns the full commit id."""
    clone = work / (dest.name + ".git")
    bb.rmtree_force(clone)
    subprocess.run(["git", "clone", "--quiet", "--bare", "--filter=blob:none", repo, str(clone)], check=True,
                   timeout=1800)
    full = subprocess.run(["git", "-C", str(clone), "rev-parse", "--verify", f"{rev}^{{commit}}"], check=True,
                          capture_output=True, text=True).stdout.strip()
    stem = dest.name.removesuffix(".tar.gz")
    subprocess.run(["git", "-C", str(clone), "archive", "--format=tar.gz", f"--prefix={stem}/", "-o", str(dest), full],
                   check=True, timeout=1800)
    bb.rmtree_force(clone)
    return full


def x264_revision(target: str, versions: Path | None) -> str:
    pinned = next(lib for lib in bb.ffmpeg_libraries(target)["libraries"] if lib["name"] == "x264")
    rev = pinned.get("fetch", {}).get("rev", "")
    if rev and rev != "@x264":
        return rev
    if versions is None or not versions.is_file():
        raise bb.BuildError(f"{target}: the x264 revision comes from runtime/VERSIONS.json; pass --versions")
    x = json.loads(versions.read_text(encoding="utf-8"))["components"]["ffmpeg"]["installed"]["x264"]
    rev = x.get("commit") or x.get("commit_short")
    if not rev:
        raise bb.BuildError("VERSIONS.json has no x264 revision")
    return rev


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--platform", required=True, choices=bb.PLATFORMS)
    ap.add_argument("--versions", type=Path, help="runtime/VERSIONS.json of the bundle (needed for macOS)")
    ap.add_argument("--out", type=Path, default=bb.REPO / "dist" / "gpl-sources")
    ap.add_argument("--cache", type=Path, default=bb.REPO / ".scratch" / "cache")
    ap.add_argument("--work", type=Path, default=bb.REPO / "build" / "gpl-sources-work")
    ap.add_argument("--strict", action="store_true", help="fail when any listed library cannot be fetched")
    ns = ap.parse_args(argv)

    target = ns.platform
    out, work, cache = ns.out.resolve(), ns.work.resolve(), ns.cache.resolve()
    out.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    pins = json.loads(bb.RUNTIMES_JSON.read_text(encoding="utf-8"))["components"]["ffmpeg"]["platforms"][target]
    libs = bb.ffmpeg_libraries(target)
    fetched: list[dict[str, Any]] = []
    missing: list[dict[str, str]] = []
    try:
        bb.prepare_work(work)
        # 1. FFmpeg release tarball (pinned hash).
        for src in pins["gpl_sources"]:
            if src.get("sha256") and src["what"].startswith("FFmpeg"):
                path = bb.fetch({"file": src["file"], "url": src["url"], "sha256": src["sha256"]}, cache)
                shutil.copy2(path, out / src["file"])
                fetched.append({"file": src["file"], "what": src["what"], "license": "LGPL-2.1-or-later / GPL",
                                "url": src["url"], "sha256": src["sha256"]})
        if not fetched:
            raise bb.BuildError(f"runtimes.json has no pinned FFmpeg source tarball for {target}")

        # 2. x264 and the copyleft libraries.
        x264_rev = x264_revision(target, ns.versions)
        for lib in libs["libraries"]:
            rule = lib.get("fetch")
            if rule is None:
                if "not_covered" in lib:
                    missing.append({"name": f"{lib['name']} {lib['version']}", "why": lib["not_covered"]})
                continue
            is_x264 = lib["name"] == "x264"
            rev = x264_rev if rule.get("rev") == "@x264" else rule.get("rev", "")
            file = rule["file"].replace("@x264", x264_rev[:8])
            dest = out / file
            print(f"fetch  {lib['name']} {lib['version']} -> {file}", flush=True)
            entry = {"file": file, "what": f"{lib['name']} {lib['version']}", "license": lib["license"]}
            try:
                if "git" in rule:
                    try:
                        full = git_archive(rule["git"], rev, dest, work)
                        entry.update(url=f"{rule['git']} @ {full}")
                    except (OSError, subprocess.SubprocessError) as e:
                        if "fallback_url" not in rule:
                            raise
                        print(f"       git failed ({e}); trying {rule['fallback_url']}", flush=True)
                        url = rule["fallback_url"].replace("@x264", x264_rev)
                        download(url, dest)
                        entry.update(url=url)
                else:
                    download(rule["url"], dest)
                    entry.update(url=rule["url"])
            except (OSError, subprocess.SubprocessError) as e:
                if is_x264:
                    raise bb.BuildError(f"could not fetch the x264 source: {e}") from e
                dest.unlink(missing_ok=True)
                missing.append({"name": entry["what"], "why": f"fetch failed: {e}"})
                print(f"       FAILED: {e}", flush=True)
                continue
            entry["sha256"] = sha256(dest)
            fetched.append(entry)
    except bb.BuildError as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 1
    finally:
        try:
            bb.rmtree_force(work)
        except OSError as e:
            print(f"warning: could not remove {work}: {e}", file=sys.stderr)

    manifest = out / f"GPL-SOURCES-{target}.txt"
    lines = [f"Corresponding sources for the FFmpeg build in Baleen's {target} bundle",
             "", f"Build: {libs['builder']} (FFmpeg {pins['version']}).",
             f"Library versions as recorded by the builder: {libs['versions_from']}.", "", "Archives:"]
    for f in fetched:
        lines += [f"  {f['file']}", f"      {f['what']}  [{f['license']}]", f"      from   {f['url']}",
                  f"      sha256 {f['sha256']}"]
    if missing:
        lines += ["", "NOT COVERED (no exact source archive could be provided):"]
        lines += [f"  {m['name']}: {m['why']}" for m in missing]
    if libs.get("unversioned"):
        lines += ["", libs["unversioned_note"]]
        lines += [f"  {u['name']}  [{u['license']}]" for u in libs["unversioned"]]
    lines += ["", "Permissively licensed libraries (BSD, MIT, ISC, Zlib, Apache) are listed with their upstream "
                  "projects in THIRD_PARTY_NOTICES/ffmpeg.txt inside the bundle."]
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n{len(fetched)} archives, {len(missing)} not covered -> {out}")
    print(manifest.read_text(encoding="utf-8"))
    failed = [m for m in missing if m["why"].startswith("fetch failed")]
    return 1 if (ns.strict and failed) else 0


if __name__ == "__main__":
    raise SystemExit(main())
