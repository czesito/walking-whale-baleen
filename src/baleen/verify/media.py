"""Audio/video checks (spec §8): V-AV-PROBE, V-AV-DUR, V-AV-DECODE (FFprobe / FFmpeg).

Also the FFprobe reader and the stream classification of §6.7 that the media route shares:

- video streams with disposition.attached_pic = 1 are cover art: never a "real" video stream;
- subtitle, data and attachment streams are never carried into an output (STREAMS_DROPPED).

Every FFmpeg/FFprobe call reads through `-protocol_whitelist file,pipe` (SEC-9) with an explicit
`file:` URL, so a source name can never be read as a protocol, and carries an explicit
`-threads` (§5.5). Nothing here writes a file: probes and decode checks only read (P1).
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any

from .. import proc
from ..model import CheckResult, CheckState, clip_message

PROTOCOLS = "file,pipe"  # SEC-9: no network protocol can ever be opened
PROBE_TIMEOUT_S = 120.0  # one ffprobe call (headers + a few seconds of packets)
INTERLACED = frozenset({"tt", "bb", "tb", "bt"})
DROPPED_TYPES = frozenset({"subtitle", "data", "attachment"})
# Demuxers whose content is a reference to other files, or a still image rather than A/V:
# never converted (a playlist could pull other local files into an output).
INDIRECT_FORMATS = frozenset({"hls", "dash", "concat", "imf", "image2"})
ESTIMATE_WARNING = "Estimating duration from bitrate"


def input_url(path: str) -> str:
    """Explicit file: URL - a name like 'http:x.mp4' is never taken for a protocol."""
    return "file:" + path


def timeout_for(duration: float | None, min_s: float) -> float:
    """§6.7: max(av_timeout_min_s, 10 x duration)."""
    return max(float(min_s), 10.0 * duration) if duration and duration > 0 else float(min_s)


def duration_tolerance(source_s: float) -> float:
    """V-AV-DUR: |output - source| <= max(1 s, 1% of source)."""
    return max(1.0, 0.01 * source_s)


_POINTER = re.compile(r" @ (?:0x)?[0-9A-Fa-f]{6,16}\]")


def tidy(stderr: str, path: str | None = None) -> str:
    """One line of tool output for the report: no full paths, no memory addresses."""
    if path:
        stderr = stderr.replace(input_url(path), os.path.basename(path))
    return " ".join(_POINTER.sub("]", stderr).split())


def crashed(returncode: int | None) -> bool:
    """A tool that died (signal / Windows exception) rather than reporting an error."""
    if returncode is None:
        return True
    if sys.platform == "win32":
        return (returncode & 0xF0000000) == 0xC0000000  # NTSTATUS error, e.g. access violation
    return returncode < 0


def _float(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f >= 0 else None  # not NaN, not negative


# --------------------------------------------------------------------------- probe model


@dataclass
class Stream:
    index: int
    codec_type: str
    codec_name: str
    attached_pic: bool = False
    width: int = 0
    height: int = 0
    field_order: str = ""
    sample_rate: int = 0
    channels: int = 0

    @property
    def is_cover(self) -> bool:
        return self.codec_type == "video" and self.attached_pic

    @property
    def is_video(self) -> bool:
        """A real video stream (cover art does not count, §6.7)."""
        return self.codec_type == "video" and not self.attached_pic

    @property
    def is_audio(self) -> bool:
        return self.codec_type == "audio"

    @property
    def is_dropped(self) -> bool:
        """Never carried into an output; noted as STREAMS_DROPPED when a new file is written."""
        return not (self.is_video or self.is_audio or self.is_cover)

    @property
    def interlaced(self) -> bool:
        return self.field_order in INTERLACED

    @property
    def odd_size(self) -> bool:
        return bool(self.width % 2 or self.height % 2)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Stream:
        disp = d.get("disposition") or {}
        return cls(
            index=int(d.get("index", 0)),
            codec_type=str(d.get("codec_type") or "unknown"),
            codec_name=str(d.get("codec_name") or "unknown"),
            attached_pic=bool(disp.get("attached_pic")),
            width=int(d.get("width") or 0),
            height=int(d.get("height") or 0),
            field_order=str(d.get("field_order") or ""),
            sample_rate=int(_float(d.get("sample_rate")) or 0),
            channels=int(d.get("channels") or 0),
        )


@dataclass
class MediaInfo:
    """What FFprobe says about one file (`-show_format -show_streams -of json`)."""

    format_name: str
    format_long_name: str = ""
    brand: str = ""  # MP4/MOV major_brand ("isom", "M4A ", "qt  " ...)
    duration: float | None = None
    streams: list[Stream] = field(default_factory=list)
    duration_estimated: bool = False  # FFprobe guessed the duration from the bit rate

    @property
    def format_names(self) -> list[str]:
        return [n for n in self.format_name.split(",") if n]

    @property
    def video(self) -> list[Stream]:
        return [s for s in self.streams if s.is_video]

    @property
    def audio(self) -> list[Stream]:
        return [s for s in self.streams if s.is_audio]

    @property
    def covers(self) -> list[Stream]:
        return [s for s in self.streams if s.is_cover]

    @property
    def dropped(self) -> list[Stream]:
        return [s for s in self.streams if s.is_dropped]

    @property
    def is_mp4_family(self) -> bool:
        """ISO base media (MP4/M4A/3GP), not QuickTime: the mov demuxer and an MP4-family brand."""
        b = self.brand.strip().lower()
        return "mp4" in self.format_names and bool(b) and not b.startswith("qt")

    @property
    def indirect(self) -> bool:
        names = set(self.format_names)
        return bool(names & INDIRECT_FORMATS) or any(n.endswith("_pipe") for n in names)

    @classmethod
    def from_json(cls, data: dict[str, Any], stderr: str = "") -> MediaInfo | None:
        fmt = data.get("format")
        if not isinstance(fmt, dict):
            return None
        tags = {str(k).lower(): v for k, v in (fmt.get("tags") or {}).items()}
        streams = [Stream.from_json(s) for s in data.get("streams") or [] if isinstance(s, dict)]
        dur = _float(fmt.get("duration"))
        if dur is None:
            ends = [x for x in (_float(s.get("duration")) for s in data.get("streams") or []) if x]
            dur = max(ends) if ends else None
        return cls(
            format_name=str(fmt.get("format_name") or ""),
            format_long_name=str(fmt.get("format_long_name") or ""),
            brand=str(tags.get("major_brand") or ""),
            duration=dur,
            streams=streams,
            duration_estimated=ESTIMATE_WARNING in stderr,
        )


@dataclass
class ProbeRun:
    """One FFprobe call. `ok`: it parsed the file."""

    info: MediaInfo | None
    returncode: int | None = None
    stderr: str = ""
    timed_out: bool = False
    error: str = ""  # could not start FFprobe (missing / not executable)

    @property
    def ok(self) -> bool:
        return self.info is not None and self.returncode == 0 and not self.timed_out and not self.error

    @property
    def unavailable(self) -> bool:
        """The tool could not give an answer: missing, timed out or crashed (not a file verdict)."""
        return bool(self.error) or self.timed_out or crashed(self.returncode)

    @property
    def message(self) -> str:
        if self.error:
            return f"FFprobe could not start: {self.error}"
        if self.timed_out:
            return "FFprobe timed out."
        return clip_message(tidy(self.stderr) or "FFprobe could not read the file.", 400)


def ffprobe_args(exe: str, target: str, loglevel: str = "error") -> list[str]:
    """§6.7 probe: ffprobe -v error -show_format -show_streams -of json (one thread, read-only)."""
    return [exe, "-hide_banner", "-v", loglevel, "-threads", "1", "-protocol_whitelist", PROTOCOLS,
            "-show_format", "-show_streams", "-of", "json", target]


def ffprobe(exe: str | None, path: str | None = None, *, data: bytes | None = None,
            env: dict[str, str] | None = None, low_priority: bool = True,
            timeout: float = PROBE_TIMEOUT_S, loglevel: str = "error", cwd: str | None = None) -> ProbeRun:
    """Probe a file on disk (read-only) or bytes held in memory (piped on stdin, nothing written)."""
    if exe is None:
        return ProbeRun(None, error="FFprobe is missing")
    target = "pipe:0" if data is not None else input_url(path or "")
    r = proc.run(ffprobe_args(exe, target, loglevel), timeout=timeout, env=env, low_priority=low_priority,
                 input_bytes=data, cwd=cwd)
    err = r.err().replace(target, os.path.basename(path or "") or "input")  # name, not the full path
    info = None
    if r.returncode == 0 and not r.timed_out:
        try:
            info = MediaInfo.from_json(json.loads(r.stdout.decode("utf-8", "replace") or "{}"), err)
        except (ValueError, TypeError, AttributeError):
            info = None
    return ProbeRun(info, r.returncode, err, r.timed_out, r.error)


def exact_duration(exe: str, path: str, streams: list[int], *, env: dict[str, str] | None,
                   low_priority: bool, timeout: float) -> float | None:
    """Duration from the packets themselves (no decoding), for files whose header lies about it
    (e.g. VBR MP3 without a Xing header: FFprobe estimates it from the bit rate)."""
    args = [exe, "-hide_banner", "-v", "error", "-threads", "1", "-protocol_whitelist", PROTOCOLS,
            "-show_entries", "packet=stream_index,pts_time,duration_time", "-of", "csv=p=0", input_url(path)]
    r = proc.run(args, timeout=timeout, env=env, low_priority=low_priority)
    if r.returncode != 0 or r.timed_out:
        return None
    first: dict[int, float] = {}
    last: dict[int, float] = {}
    for line in r.out().splitlines():
        parts = line.strip().split(",")
        if len(parts) < 2:
            continue
        try:
            idx = int(parts[0])
        except ValueError:
            continue
        pts = _float(parts[1])
        if idx not in streams or pts is None:
            continue
        dur = _float(parts[2]) if len(parts) > 2 else None
        end = pts + (dur or 0.0)
        first[idx] = min(first.get(idx, pts), pts)
        last[idx] = max(last.get(idx, end), end)
    spans = [last[i] - first[i] for i in last]
    return max(spans) if spans else None


# --------------------------------------------------------------------------- checks


def v_av_probe(pr: ProbeRun, kind: str) -> CheckResult:
    """V-AV-PROBE: parses; MP4 family. Video target: >= 1 video stream. Audio target: no video
    stream (cover art doesn't count), >= 1 audio stream."""
    name = "V-AV-PROBE"
    if pr.error:
        return CheckResult(name, CheckState.UNAVAILABLE, message=pr.message, tool_missing=True)
    if pr.unavailable:
        return CheckResult(name, CheckState.UNAVAILABLE, message=pr.message)
    if not pr.ok or pr.info is None:
        return CheckResult(name, CheckState.FAIL, message=pr.message)
    info = pr.info
    if not info.is_mp4_family:
        what = info.format_name + (f" ({info.brand.strip()})" if info.brand.strip() else "")
        return CheckResult(name, CheckState.FAIL, message=f"The container is {what}, not MP4.")
    if kind == "video" and not info.video:
        return CheckResult(name, CheckState.FAIL, message="There is no video stream.")
    if kind == "audio":
        if info.video:
            return CheckResult(name, CheckState.FAIL, message="An audio file must not contain a video stream.")
        if not info.audio:
            return CheckResult(name, CheckState.FAIL, message="There is no audio stream.")
    return CheckResult(name, CheckState.PASS)


def v_av_dur(source_s: float | None, output_s: float | None) -> CheckResult:
    """V-AV-DUR (conversions only): |output - source| <= max(1 s, 1% of source)."""
    name = "V-AV-DUR"
    if source_s is None:
        return CheckResult(name, CheckState.UNAVAILABLE, message="The source's duration is unknown.")
    if output_s is None:
        return CheckResult(name, CheckState.FAIL, message="The output has no duration.")
    tol = duration_tolerance(source_s)
    if abs(output_s - source_s) <= tol:
        return CheckResult(name, CheckState.PASS)
    return CheckResult(name, CheckState.FAIL, message=(
        f"The output lasts {output_s:.2f} s but the source {source_s:.2f} s (allowed difference {tol:.2f} s)."))


def decode_args(exe: str, path: str, threads: int) -> list[str]:
    """V-AV-DECODE: ffmpeg -v error -i <file> -f null -, decoding every audio and video stream."""
    t = str(max(1, threads))
    return [exe, "-hide_banner", "-nostdin", "-nostats", "-v", "error", "-filter_threads", t, "-threads", t,
            "-protocol_whitelist", PROTOCOLS, "-i", input_url(path), "-map", "0:v?", "-map", "0:a?",
            "-threads", t, "-f", "null", "-"]


def v_av_decode(exe: str | None, path: str, *, threads: int, env: dict[str, str] | None, low_priority: bool,
                timeout: float, cwd: str | None = None) -> CheckResult:
    """V-AV-DECODE: exits 0 with empty stderr. Missing / crashed / timed out -> unavailable."""
    name = "V-AV-DECODE"
    if exe is None:
        return CheckResult(name, CheckState.UNAVAILABLE, message="FFmpeg is missing.", tool_missing=True)
    r = proc.run(decode_args(exe, path, threads), timeout=timeout, env=env, low_priority=low_priority, cwd=cwd)
    if r.error:
        return CheckResult(name, CheckState.UNAVAILABLE, message=f"FFmpeg could not start: {r.error}",
                           tool_missing=True)
    if r.timed_out:
        return CheckResult(name, CheckState.UNAVAILABLE, message=f"The decode check timed out after {timeout:.0f} s.")
    if crashed(r.returncode):
        return CheckResult(name, CheckState.UNAVAILABLE, message=f"FFmpeg stopped unexpectedly ({r.returncode}).")
    err = tidy(r.err(), path)
    if r.returncode == 0 and not err:
        return CheckResult(name, CheckState.PASS)
    return CheckResult(name, CheckState.FAIL, message=clip_message(
        "Decoding reported errors: " + (err or f"exit code {r.returncode}"), 400))
