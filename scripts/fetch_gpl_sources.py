"""Collect the corresponding source of a bundle's FFmpeg build (spec §15, R-08).

    python scripts/fetch_gpl_sources.py --platform win-x64|mac-arm64|mac-x64
                                        [--ffmpeg-from ARTIFACT] [--out dist/gpl-sources] [--cache DIR]

Baleen ships only the FFmpeg it builds itself (scripts/build_ffmpeg.sh), so the corresponding source
is exactly the pinned source archives in runtimes.json (components.ffmpeg.build.sources: FFmpeg,
x264 and, for Windows, zlib) plus the build script and its pins. With --ffmpeg-from the archives are
copied from the build artifact; otherwise they are fetched again. Either way each one must match its
pinned SHA-256. Writes the archives, build_ffmpeg.sh, runtimes.json, ffmpeg-config-<platform>.txt (when
the artifact has one) and GPL-SOURCES-<platform>.txt into --out, ready to attach to the GitHub Release.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_bundle as bb  # noqa: E402


def git_archive(src: dict, dest: Path, work: Path) -> None:
    """The x264 snapshot: archive of the pinned commit, made the same way as build_ffmpeg.sh."""
    clone = work / "x264.git"
    last: Exception | None = None
    for url in src["git"]:
        bb.rmtree_force(clone)
        try:
            subprocess.run(["git", "-c", "core.autocrlf=false", "clone", "--quiet", "--bare", "--filter=blob:none",
                            "-c", "core.autocrlf=false", "-c", "core.eol=lf", url, str(clone)], check=True, timeout=1800)
            with open(dest, "wb") as out:
                subprocess.run(["git", "-C", str(clone), "archive", "--format=tar",
                                f"--prefix=x264-{src['commit']}/", src["commit"]], check=True, stdout=out, timeout=1800)
            return
        except (OSError, subprocess.SubprocessError) as e:
            last = e
            print(f"  {url} failed: {e}", flush=True)
    raise bb.BuildError(f"could not archive x264 {src['commit']}: {last}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--platform", required=True, choices=bb.PLATFORMS)
    ap.add_argument("--ffmpeg-from", type=Path, help="ffmpeg.yml artifact (folder or zip) to copy the archives from")
    ap.add_argument("--out", type=Path, default=bb.REPO / "dist" / "gpl-sources")
    ap.add_argument("--cache", type=Path, default=bb.REPO / ".scratch" / "cache")
    ap.add_argument("--work", type=Path, default=bb.REPO / "build" / "gpl-sources-work")
    ns = ap.parse_args(argv)

    out, work, cache = ns.out.resolve(), ns.work.resolve(), ns.cache.resolve()
    pins = json.loads(bb.RUNTIMES_JSON.read_text(encoding="utf-8"))
    ctx = bb.Ctx(platform=ns.platform, version="", cache=cache, runtime=out, work=work, pins=pins)
    sources = bb.build_sources(ctx)
    try:
        bb.prepare_work(work)
        out.mkdir(parents=True, exist_ok=True)
        cache.mkdir(parents=True, exist_ok=True)
        artifact = bb.find_ffmpeg_artifact(ns.ffmpeg_from.resolve(), work) if ns.ffmpeg_from else None
        for name, src in sources.items():
            dest = out / src["file"]
            if artifact is not None:
                shutil.copy2(artifact / "sources" / src["file"], dest)
            elif "git" in src and "url" not in src:
                git_archive(src, dest, work)
            else:
                shutil.copy2(bb.fetch({"file": src["file"], "url": src["url"], "mirrors": src.get("mirrors", []),
                                       "sha256": src["sha256"]}, cache), dest)
            got = bb.sha256_file(dest)
            if got != src["sha256"]:
                raise bb.BuildError(f"{name}: {dest.name} has SHA-256 {got}, pinned {src['sha256']}")
            print(f"ok  {dest.name}  {got}", flush=True)
        shutil.copy2(bb.REPO / "scripts" / "build_ffmpeg.sh", out / "build_ffmpeg.sh")
        shutil.copy2(bb.RUNTIMES_JSON, out / "runtimes.json")
        config = None
        if artifact is not None and (artifact / "config.txt").is_file():
            config = out / f"ffmpeg-config-{ns.platform}.txt"
            shutil.copy2(artifact / "config.txt", config)
    except bb.BuildError as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 1
    finally:
        try:
            bb.rmtree_force(work)
        except OSError as e:
            print(f"warning: could not remove {work}: {e}", file=sys.stderr)

    lines = [f"Corresponding source of the FFmpeg in Baleen's {ns.platform} bundle", "",
             bb._wrap("runtime/ffmpeg/ffmpeg and ffprobe were built by scripts/build_ffmpeg.sh from these archives "
                      "and nothing else. build_ffmpeg.sh and runtimes.json (which pins the archives) are included; "
                      + (f"{config.name} has the exact configure lines and build configuration."
                         if config else "the configure lines are in build_ffmpeg.sh.")), "", "Archives:"]
    for src in sources.values():
        lines += [f"  {src['file']}", f"      {src['what']}  [{src['license']}]",
                  f"      from   {src.get('url') or ' or '.join(src['git'])}", f"      sha256 {src['sha256']}"]
    manifest = out / f"GPL-SOURCES-{ns.platform}.txt"
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(manifest.read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
