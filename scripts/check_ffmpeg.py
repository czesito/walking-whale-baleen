"""Check an FFmpeg build against what Baleen needs and is allowed to ship (spec §6.7, §15).

    python scripts/check_ffmpeg.py DIR [TMPDIR]   # DIR holds ffmpeg[.exe] and ffprobe[.exe]

Fails (exit 1) unless:
  - `ffmpeg -L` says GPL version 2 or later, and the configuration has --enable-gpl and --enable-libx264
    but neither --enable-version3 nor --enable-nonfree;
  - every decoder of the §6 matrix, the aac and libx264 encoders, the mp4/mov/ipod muxers and the
    usual legacy demuxers are present;
  - a short H.264/AAC MP4 encodes, probes as h264 + aac, and decodes again.
Prints a coverage table either way.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

DECODERS = {
    "MJPEG": ["mjpeg"],
    "MPEG-1/2 video": ["mpeg1video", "mpeg2video"],
    "MPEG-4 Part 2, DivX/MS-MPEG4": ["mpeg4", "msmpeg4v1", "msmpeg4v2", "msmpeg4"],
    "WMV1/2/3, VC-1": ["wmv1", "wmv2", "wmv3", "vc1"],
    "DV": ["dvvideo"],
    "H.264, HEVC": ["h264", "hevc"],
    "VP8, VP9": ["vp8", "vp9"],
    "ProRes": ["prores"],
    "FLV / Sorenson": ["flv"],
    "WMA": ["wmav1", "wmav2", "wmapro", "wmalossless", "wmavoice"],
    "MP2, MP3": ["mp2", "mp3"],
    "AAC": ["aac"],
    "AC-3, E-AC-3": ["ac3", "eac3"],
    "PCM": ["pcm_s16le", "pcm_s16be", "pcm_s24le", "pcm_s24be", "pcm_s32le", "pcm_u8", "pcm_f32le",
            "pcm_alaw", "pcm_mulaw"],
    "FLAC": ["flac"],
    "Vorbis": ["vorbis"],
    "Opus": ["opus"],
    "AMR-NB, AMR-WB": ["amrnb", "amrwb"],
    "ADPCM": ["adpcm_ima_wav", "adpcm_ms", "adpcm_ima_qt", "adpcm_swf", "g726"],
}
# Present in a default FFmpeg configuration; reported, not required.
EXTRA_DECODERS = ["cinepak", "svq1", "svq3", "indeo3", "indeo5", "msvideo1", "msrle", "qtrle", "rpza", "smc",
                  "h263", "h261", "theora", "rv10", "rv20", "rv30", "rv40", "cook", "png", "camtasia", "zlib", "zmbv",
                  "flashsv", "vp6", "vp6f", "alac", "ape", "wavpack", "tta", "truehd", "dca", "qdm2", "atrac3",
                  "gsm_ms", "nellymoser", "mace3", "mace6", "rawvideo"]
ENCODERS = ["libx264", "aac"]
MUXERS = ["mp4", "mov", "ipod"]
DEMUXERS = ["avi", "asf", "mov", "mpeg", "mpegts", "matroska", "flv", "dv", "wav", "aiff", "mp3", "ogg",
            "flac", "amr", "rm", "au", "mpegvideo"]


def run(cmd: list[str], **kw) -> str:  # noqa: ANN003
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)
    return r.stdout + r.stderr


def names(listing: str) -> set[str]:
    """Names from -decoders/-encoders/-muxers/-demuxers output (after the ' --' separator line)."""
    out: set[str] = set()
    body = listing.split("\n --", 1)[-1] if "\n --" in listing else listing
    for line in body.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            out.update(parts[1].split(","))
    return out


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) not in (1, 2):
        print(__doc__)
        return 2
    d = Path(argv[0])
    exe = ".exe" if (d / "ffmpeg.exe").exists() else ""
    ffmpeg, ffprobe = str(d / f"ffmpeg{exe}"), str(d / f"ffprobe{exe}")
    problems: list[str] = []

    version = run([ffmpeg, "-hide_banner", "-version"])
    print(version.splitlines()[0] if version else "ffmpeg did not run")
    config = next((ln.split(":", 1)[1].strip() for ln in version.splitlines() if ln.startswith("configuration:")), "")
    lic = run([ffmpeg, "-hide_banner", "-L"])
    lic_line = " ".join(lic.split())
    m = re.search(r"either version (\d) of the License", lic_line)
    print(f"licence: GPL version {m.group(1) if m else '?'} or later" if "GNU General Public License" in lic_line
          else "licence: not GPL")
    if not m or m.group(1) != "2" or "nonfree" in lic_line.lower():
        problems.append("ffmpeg -L does not say GPL version 2 or later")
    for flag in ("--enable-gpl", "--enable-libx264"):
        if flag not in config:
            problems.append(f"configuration lacks {flag}")
    for flag in ("--enable-version3", "--enable-nonfree"):
        if flag in config:
            problems.append(f"configuration has {flag}")

    dec = names(run([ffmpeg, "-hide_banner", "-decoders"]))
    enc = names(run([ffmpeg, "-hide_banner", "-encoders"]))
    mux = names(run([ffmpeg, "-hide_banner", "-muxers"]))
    dmx = names(run([ffmpeg, "-hide_banner", "-demuxers"]))
    print(f"\n{len(dec)} decoders, {len(enc)} encoders, {len(mux)} muxers, {len(dmx)} demuxers")
    print("\nRequired decoders (spec 6.7 matrix):")
    for label, wanted in DECODERS.items():
        missing = [w for w in wanted if w not in dec]
        print(f"  {'ok  ' if not missing else 'MISS'} {label:30} {' '.join(wanted)}"
              + (f"   missing: {' '.join(missing)}" if missing else ""))
        problems += [f"decoder {w} missing" for w in missing]
    for kind, wanted, have in (("encoders", ENCODERS, enc), ("muxers", MUXERS, mux), ("demuxers", DEMUXERS, dmx)):
        missing = [w for w in wanted if w not in have]
        print(f"  {'ok  ' if not missing else 'MISS'} {kind:30} {' '.join(wanted)}"
              + (f"   missing: {' '.join(missing)}" if missing else ""))
        problems += [f"{kind[:-1]} {w} missing" for w in missing]
    extra_missing = [x for x in EXTRA_DECODERS if x not in dec]
    print(f"  legacy extras present: {len(EXTRA_DECODERS) - len(extra_missing)}/{len(EXTRA_DECODERS)}"
          + (f" (absent: {' '.join(extra_missing)})" if extra_missing else ""))

    if "lavfi" not in dmx:
        problems.append("the lavfi input is missing (test patterns for fixtures and this check)")
    with tempfile.TemporaryDirectory(dir=argv[1] if len(argv) > 1 else None) as tmp:
        mp4 = str(Path(tmp) / "check.mp4")
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=2",
               "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "128k", "-shortest", "-movflags", "+faststart", "-y", mp4]
        print("\ntest encode: " + " ".join(cmd[1:]))
        enc_run = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if enc_run.returncode != 0 or enc_run.stderr.strip():
            print(f"  encoder exit {enc_run.returncode}\n  " + "\n  ".join(enc_run.stderr.strip().splitlines()[-20:]))
        probe = run([ffprobe, "-v", "error", "-show_entries", "stream=codec_name", "-of", "csv=p=0", mp4])
        codecs = sorted(set(probe.split()))
        decode = subprocess.run([ffmpeg, "-hide_banner", "-v", "error", "-xerror", "-i", mp4, "-f", "null", "-"],
                                capture_output=True, text=True)
        sei = re.search(rb"x264 - core \d+(?: r\d+ [0-9a-f]+)?", Path(mp4).read_bytes()) if Path(mp4).exists() else None
    print(f"  result: streams {codecs}, decode exit {decode.returncode}, "
          f"{sei.group(0).decode() if sei else 'no x264 SEI'}")
    if codecs != ["aac", "h264"]:
        problems.append(f"test encode produced {codecs}, expected aac + h264")
    if decode.returncode != 0:
        problems.append(f"test decode failed: {decode.stderr.strip()[:300]}")

    if problems:
        print("\nFAILED:\n  " + "\n  ".join(problems))
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
