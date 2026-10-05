"""Audio and video through FFmpeg: the `media` route (spec §6.7, DR-11, DR-12, DR-13).

Plan time (parallel probes, read-only): FFprobe looks at the content and decides
- the route: a real video stream -> video (.mp4); otherwise audio streams -> audio (.m4a, or
  .mp4 per `audio_container`); neither -> UNSUPPORTED NO_MEDIA_STREAMS. Cover art
  (attached_pic) never counts as video;
- the action, per stream (DR-12: avoid generation loss): H.264/HEVC video and AAC/MP3 audio are
  copied, everything else is transcoded (libx264 + AAC). Byte copy when the source is already
  MP4 in the target extension (video: copyable codecs only; audio: any, §6.7); remux when
  every stream is copied; convert (transcode) otherwise.

Run time (Media lane): FFmpeg reads only the staged copy and writes only into the work folder
(P1, `-n` never overwrites). Every call has `-threads <ctx.tokens>` (§5.5) and the protocol
whitelist (SEC-9). Streams are mapped by index, so cover art, subtitles and data streams never
reach an output; dropped subtitle/data/attachment streams are noted STREAMS_DROPPED. The checks
of §8 (verify/media.py) run on the local result; copies and check-only sources get the same
V-AV-PROBE / V-AV-DECODE, so a damaged MP4/M4A source becomes SOURCE_INVALID.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import ClassVar

from .. import proc
from ..model import Action, Category, CheckResult, CheckState, Mode, Probe, clip_message
from ..paths import long_path
from ..scheduler import Lane, TaskContext
from ..verify import media as vm
from . import formats
from .base import ProbeContext, Route, RunContext, SourceRef, WorkItem, register

VIDEO_COPY = frozenset({"h264", "hevc"})
AUDIO_COPY = frozenset({"aac", "mp3"})  # DR-13: MP3 is carried into MP4 as-is
PAD = "pad=ceil(iw/2)*2:ceil(ih/2)*2"  # odd dimensions: pad, never resample
INTERLACED_SCALE = "scale=interl=1"  # interlace-aware chroma conversion; no deinterlacing (NG3)
ARCHIVAL_EXTS = (".mp4", ".m4a")


@dataclass(frozen=True)
class Quality:
    crf: int
    audio_bitrate: str


QUALITY: dict[str, Quality] = {"high": Quality(18, "192k"), "standard": Quality(23, "128k")}


def quality_for(settings: dict) -> Quality:
    return QUALITY.get(str(settings.get("video_quality", "high")), QUALITY["high"])


# --------------------------------------------------------------------------- decision table


@dataclass(frozen=True)
class StreamPlan:
    index: int  # input stream index (from FFprobe)
    kind: str  # "video" | "audio"
    codec: str
    copy: bool
    pad: bool = False
    interlaced: bool = False
    size: tuple[int, int] = (0, 0)


@dataclass
class Decision:
    kind: str | None  # "video" | "audio" | None (no audio or video)
    target_ext: str | None
    action: Action
    streams: list[StreamPlan] = field(default_factory=list)
    dropped: list[int] = field(default_factory=list)  # subtitle / data / attachment stream indexes

    @property
    def video(self) -> list[StreamPlan]:
        return [p for p in self.streams if p.kind == "video"]

    @property
    def audio(self) -> list[StreamPlan]:
        return [p for p in self.streams if p.kind == "audio"]


def classify(info: vm.MediaInfo) -> str | None:
    """§6.7: a real video stream -> video; otherwise audio streams -> audio; neither -> None."""
    if info.video:
        return "video"
    if info.audio:
        return "audio"
    return None


def decide(info: vm.MediaInfo, ext: str, audio_container: str = "m4a") -> Decision:
    """The §6.7 decision table for one probed file. `ext` is the lower-case source extension."""
    dropped = [s.index for s in info.dropped]
    kind = classify(info)
    if kind is None:
        return Decision(None, None, Action.NONE, [], dropped)
    plans: list[StreamPlan] = []
    if kind == "video":
        target = ".mp4"
        for s in info.video:
            copy = s.codec_name in VIDEO_COPY
            plans.append(StreamPlan(s.index, "video", s.codec_name, copy, pad=not copy and s.odd_size,
                                    interlaced=not copy and s.interlaced, size=(s.width, s.height)))
    else:
        target = "." + (audio_container if audio_container in ("m4a", "mp4") else "m4a")
    plans += [StreamPlan(s.index, "audio", s.codec_name, s.codec_name in AUDIO_COPY) for s in info.audio]
    all_copy = all(p.copy for p in plans)
    in_target = ext == target and info.is_mp4_family
    if in_target and (all_copy or kind == "audio"):
        action = Action.COPY  # already MP4/M4A in the target extension: byte copy (or check)
    elif all_copy:
        action = Action.REMUX
    else:
        action = Action.CONVERT
    return Decision(kind, target, action, plans, dropped)


# --------------------------------------------------------------------------- FFmpeg arguments


def build_args(ffmpeg: str, inp: str, out: str, d: Decision, *, threads: int, quality: Quality) -> list[str]:
    """One FFmpeg call for a remux or transcode (§6.7 'Always' options, §5.5 threads)."""
    t = str(max(1, threads))
    a = [ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-v", "error", "-n", "-filter_threads", t,
         "-threads", t, "-protocol_whitelist", vm.PROTOCOLS, "-fflags", "+genpts", "-i", vm.input_url(inp)]
    v, au = d.video, d.audio
    for p in v + au:
        a += ["-map", f"0:{p.index}"]
    for k, p in enumerate(v):
        if p.copy:
            a += [f"-c:v:{k}", "copy"]
            continue
        # yuv420p in TV range, as players expect: FFmpeg >= 7.1 would otherwise keep a full-range
        # source (MJPEG) full-range, which many players show with crushed blacks.
        a += [f"-c:v:{k}", "libx264", f"-preset:v:{k}", "slow", f"-crf:v:{k}", str(quality.crf),
              f"-pix_fmt:v:{k}", "yuv420p", f"-color_range:v:{k}", "tv"]
        chain = []
        if p.interlaced:
            a += [f"-flags:v:{k}", "+ildct+ilme"]  # keep the source's interlacing (no deinterlace)
            chain.append(INTERLACED_SCALE)
        if p.pad:
            chain.append(PAD)
        if chain:
            a += [f"-filter:v:{k}", ",".join(chain)]
    for k, p in enumerate(au):
        if p.copy:
            a += [f"-c:a:{k}", "copy"]
        else:
            a += [f"-c:a:{k}", "aac", f"-b:a:{k}", quality.audio_bitrate]  # rate and channels kept
    if d.kind == "audio":
        a.append("-vn")  # cover art is never carried over
    a += ["-sn", "-dn", "-map_metadata", "0", "-movflags", "+faststart", "-threads", t, "-f", "mp4",
          "file:" + out]
    return a


def short_version(version: str) -> str:
    """'9.0-full_build-www.gyan.dev' -> '9.0'; 'n7.1.1-12-g...' -> '7.1.1'; others as they are."""
    m = re.match(r"n?(\d+(?:\.\d+)+)", version or "")
    return m.group(1) if m else (version or "").strip()


def method_text(version: str, d: Decision, quality: Quality) -> str:
    """Report `method`, e.g. 'FFmpeg 7.1 · libx264 crf18 · aac 192k'."""
    parts: list[str] = []
    for p in d.streams:
        if p.copy:
            text = f"{p.codec} copy"
        elif p.kind == "video":
            text = f"libx264 crf{quality.crf}"
            if p.interlaced:
                text += " interlaced"
            if p.pad:
                w, h = p.size
                text += f" pad {w + w % 2}×{h + h % 2}"
        else:
            text = f"aac {quality.audio_bitrate}"
        if text not in parts:
            parts.append(text)
    head = f"FFmpeg {short_version(version)}".strip()
    return " · ".join([head, *parts])


# --------------------------------------------------------------------------- source_format


_CONTAINERS = {
    "avi": "AVI", "mpeg": "MPEG-PS", "mpegts": "MPEG-TS", "mpegvideo": "MPEG video", "wav": "WAV",
    "w64": "Wave64", "mp3": "MP3", "flac": "FLAC", "ogg": "Ogg", "aiff": "AIFF", "flv": "FLV", "dv": "DV",
    "amr": "AMR", "aac": "AAC", "au": "AU", "vob": "VOB", "m4v": "MPEG-4 video", "h264": "H.264",
}


def container_label(info: vm.MediaInfo, ext: str) -> str:
    names = info.format_names
    if "mp4" in names:
        b = info.brand.strip().lower()
        if not b or b.startswith("qt"):
            return "QuickTime"
        for prefix, label in (("m4a", "M4A"), ("m4v", "M4V"), ("3g2", "3G2"), ("3gp", "3GP")):
            if b.startswith(prefix):
                return label
        return "MP4"
    if "matroska" in names:
        return "WebM" if ext == ".webm" else "Matroska"
    if "asf" in names:
        return {".wma": "WMA", ".wmv": "WMV"}.get(ext, "ASF")
    for n in names:
        if n in _CONTAINERS:
            return _CONTAINERS[n]
    return (names[0].upper() if names else "") or ext.lstrip(".").upper()


def source_format(info: vm.MediaInfo, ext: str) -> str:
    """Report `source_format`, e.g. 'AVI (mjpeg / pcm_s16le)' - cover art and subtitles left out."""

    def uniq(xs: list[str]) -> list[str]:
        return list(dict.fromkeys(xs))

    groups = [", ".join(g) for g in (uniq([s.codec_name for s in info.video]),
                                     uniq([s.codec_name for s in info.audio])) if g]
    label = container_label(info, ext)
    return f"{label} ({' / '.join(groups)})" if groups else label


# --------------------------------------------------------------------------- checks


def _kind(work: WorkItem) -> str:
    return "video" if work.plan.category == Category.VIDEO else "audio"


def _cwd(work: WorkItem) -> str:
    """Where FFmpeg runs: the item's work folder, or data/tmp for checks in place (never the source)."""
    if os.path.isdir(long_path(work.work_dir)):
        return work.work_dir
    return str(work.run.home.tmp_dir)


def _min_timeout(run: RunContext) -> float:
    return float(run.advanced.get("av_timeout_min_s", 600))


def verify_file(ctx: TaskContext, run: RunContext, path: str, kind: str, source_s: float | None, *,
                conversion: bool, cwd: str | None) -> list[CheckResult]:
    """V-AV-PROBE, V-AV-DUR (conversions only; n/a for copies), V-AV-DECODE on one local file."""
    env = run.tools_env()
    pr = vm.ffprobe(run.tools.path("ffprobe"), path, env=env, low_priority=ctx.low_priority, cwd=cwd)
    checks = [vm.v_av_probe(pr, kind)]
    if not conversion:
        checks.append(CheckResult("V-AV-DUR", CheckState.NA))
    elif pr.unavailable:
        checks.append(CheckResult("V-AV-DUR", CheckState.UNAVAILABLE, message=pr.message, tool_missing=bool(pr.error)))
    else:
        checks.append(vm.v_av_dur(source_s, pr.info.duration if pr.ok and pr.info else None))
    out_s = pr.info.duration if pr.ok and pr.info else None
    timeout = vm.timeout_for(out_s or source_s, _min_timeout(run))
    checks.append(vm.v_av_decode(run.tools.path("ffmpeg"), path, threads=ctx.tokens, env=env,
                                 low_priority=ctx.low_priority, timeout=timeout, cwd=cwd))
    return checks


def _add_checks(work: WorkItem, checks: list[CheckResult]) -> None:
    """Record the checks; failed or unavailable ones also explain themselves in the message."""
    work.checks += checks
    for c in checks:
        if c.state in (CheckState.FAIL, CheckState.UNAVAILABLE) and c.message:
            work.messages.append(f"{c.check}: {c.message}")


# --------------------------------------------------------------------------- the route


class MediaRoute(Route):
    key: ClassVar[str] = "media"
    lane: ClassVar[Lane] = Lane.MEDIA
    converter_tools: ClassVar[tuple[str, ...]] = ("ffmpeg",)

    # ------------------------------------------------------------------ plan time

    def probe(self, ctx: ProbeContext, src: SourceRef) -> Probe:
        fmt = formats.lookup(src.ext)
        ext_category = fmt.category if fmt else Category.VIDEO
        archival = src.ext in ARCHIVAL_EXTS
        container = str(ctx.workflow.get("audio_container", "m4a"))
        if ctx.mode == Mode.CHECK and archival:
            # Check has no options (UI-K1): M4A and MP4 are both archival, whatever audio_container says.
            container = src.ext.lstrip(".")
        fallback = formats.default_target(src.ext, {"workflow": {"audio_container": container}})
        base = {"category": ext_category, "target_ext": fallback, "route": self.key,
                "source_format": src.ext.lstrip(".").upper()}
        exe = ctx.tools.path("ffprobe")
        pr = None
        if exe is not None:
            pr = vm.ffprobe(exe, src.path, data=src.data, env=ctx.tools.env(), low_priority=ctx.low_priority,
                            cwd=str(ctx.home.tmp_dir))
        if pr is None or pr.error:
            # No FFprobe: fall back to the extension. Conversions can't be planned (TOOL_MISSING);
            # MP4/M4A sources are still copied and checked (their checks report VALIDATOR_MISSING).
            if archival and fallback == src.ext:
                return Probe(action=Action.COPY, message="FFprobe is missing.", **base)
            reasons = ["TOOL_MISSING"] if ctx.mode == Mode.CONVERT else []
            return Probe(action=Action.CONVERT, reasons=reasons, message="Missing: ffprobe", **base)
        if pr.timed_out:
            return Probe(action=Action.CONVERT, reasons=["TIMEOUT"], message="FFprobe took too long to read the file.",
                         **base)
        if not pr.ok or pr.info is None:
            if archival:
                # §8: for MP4/M4A sources SOURCE_INVALID takes precedence over SOURCE_UNREADABLE.
                # Plan the copy; its V-AV-PROBE / V-AV-DECODE fail and the runner reports SOURCE_INVALID.
                return Probe(action=Action.COPY, **base)
            return Probe(action=Action.CONVERT, reasons=["SOURCE_UNREADABLE"], message=pr.message, **base)

        info = pr.info
        base["source_format"] = source_format(info, src.ext)
        if info.indirect:
            return Probe(action=Action.NONE, reasons=["NO_MEDIA_STREAMS"], message=(
                f"FFprobe reads this as {info.format_name}, not as an audio or video recording."), **base)
        d = decide(info, src.ext, container)
        if d.kind is None:
            return Probe(action=Action.NONE, reasons=["NO_MEDIA_STREAMS"],
                         message="The file contains no audio or video stream.", **base)
        notes: list[str] = []
        message = ""
        if d.dropped and d.action in (Action.REMUX, Action.CONVERT) and ctx.mode == Mode.CONVERT:
            notes.append("STREAMS_DROPPED")
            n = len(d.dropped)
            message = f"Not carried over: {n} subtitle or data stream{'s' if n != 1 else ''}."
        if ctx.mode == Mode.CHECK:
            method = ""
        elif d.action == Action.COPY:
            method = "byte copy"
        else:
            method = method_text(ctx.tools.get("ffmpeg").version, d, quality_for(ctx.workflow))
        return Probe(
            category=Category.VIDEO if d.kind == "video" else Category.AUDIO,
            target_ext=d.target_ext, action=d.action, source_format=base["source_format"], method=method,
            notes=notes, message=message, route=self.key,
            data={"media": {"kind": d.kind, "duration": info.duration}},
        )

    # ------------------------------------------------------------------ run time

    def process(self, ctx: TaskContext, work: WorkItem) -> None:
        if work.check_only or work.action == Action.COPY:
            self._check_source(ctx, work)
        else:
            self._convert(ctx, work)

    def check_existing(self, ctx: TaskContext, work: WorkItem) -> None:
        """§7.5 resume: re-run the checks on the existing output, read-only."""
        conversion = work.plan.action in (Action.CONVERT, Action.REMUX)
        source_s = self._planned_duration(ctx, work) if conversion else None
        _add_checks(work, verify_file(ctx, work.run, work.input_path(), _kind(work), source_s,
                                      conversion=conversion, cwd=str(work.run.home.tmp_dir)))

    def _check_source(self, ctx: TaskContext, work: WorkItem) -> None:
        """Copy (staged copy, then publish) or check in place (read-only): same checks, and a
        failure means the archival source itself is damaged (SOURCE_INVALID, new_output=False)."""
        work.new_output = False
        work.method = "" if work.check_only else "byte copy"
        _add_checks(work, verify_file(ctx, work.run, work.input_path(), _kind(work), None, conversion=False,
                                      cwd=_cwd(work)))
        if not work.check_only:
            work.result_path = work.staged

    def _convert(self, ctx: TaskContext, work: WorkItem) -> None:
        run = work.run
        ffmpeg, ffprobe = run.tools.path("ffmpeg"), run.tools.path("ffprobe")
        missing = [n for n, p in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)) if p is None]
        if missing:
            work.fail("TOOL_MISSING", "Missing: " + ", ".join(missing))
            return
        assert ffmpeg is not None and ffprobe is not None
        inp = work.input_path()
        out = work.out("output" + (work.plan.target_ext or ".mp4"))  # creates the work folder
        env = run.tools_env()
        cwd = work.work_dir
        # Re-read the staged copy (same bytes as at plan time) for stream indexes and duration.
        src = vm.ffprobe(ffprobe, inp, env=env, low_priority=ctx.low_priority, loglevel="warning", cwd=cwd)
        if not src.ok or src.info is None:
            work.fail("TIMEOUT" if src.timed_out else "SOURCE_UNREADABLE", src.message)
            return
        d = decide(src.info, work.plan.ext, str(work.settings.get("audio_container", "m4a")))
        if d.kind is None or d.target_ext != work.plan.target_ext:
            work.fail("CONVERSION_ERROR", "The file's streams no longer match the plan.")
            return
        quality = quality_for(work.settings)
        source_s = src.info.duration
        if src.info.duration_estimated:
            source_s = vm.exact_duration(ffprobe, inp, [p.index for p in d.streams], env=env,
                                         low_priority=ctx.low_priority,
                                         timeout=vm.timeout_for(source_s, _min_timeout(run))) or source_s
        timeout = vm.timeout_for(source_s, _min_timeout(run))
        work.action = Action.REMUX if d.action in (Action.REMUX, Action.COPY) else Action.CONVERT
        work.method = method_text(run.tools.get("ffmpeg").version, d, quality)
        r = proc.run(build_args(ffmpeg, inp, out, d, threads=ctx.tokens, quality=quality), timeout=timeout,
                     env=env, cwd=cwd, low_priority=ctx.low_priority)
        if r.timed_out:
            work.fail("TIMEOUT", f"FFmpeg did not finish within {timeout:.0f} s.")
            return
        err = vm.tidy(vm.tidy(r.err(), inp), out)
        if not r.ok or not os.path.isfile(long_path(out)):
            work.fail("CONVERSION_ERROR", clip_message(f"FFmpeg failed: {r.error or err or r.returncode}", 400))
            return
        if err:
            work.messages.append(clip_message("FFmpeg reported: " + err, 300))
        work.result_path = out
        work.new_output = True
        _add_checks(work, verify_file(ctx, run, out, d.kind, source_s, conversion=True, cwd=cwd))

    def _planned_duration(self, ctx: TaskContext, work: WorkItem) -> float | None:
        """Source duration for V-AV-DUR on resume: read from the source (read-only), exact when
        FFprobe only estimates it; the plan's value if the source can't be probed."""
        dur = (work.plan.data.get("media") or {}).get("duration")
        planned = float(dur) if isinstance(dur, int | float) else None
        src = work.source_abs or work.staged
        ffprobe = work.run.tools.path("ffprobe")
        if not (src and ffprobe):
            return planned
        env = work.run.tools_env()
        cwd = str(work.run.home.tmp_dir)
        pr = vm.ffprobe(ffprobe, src, env=env, low_priority=ctx.low_priority, loglevel="warning", cwd=cwd)
        if not pr.ok or pr.info is None:
            return planned
        if not pr.info.duration_estimated:
            return pr.info.duration if pr.info.duration is not None else planned
        d = decide(pr.info, work.plan.ext, str(work.settings.get("audio_container", "m4a")))
        exact = vm.exact_duration(ffprobe, src, [p.index for p in d.streams], env=env, low_priority=ctx.low_priority,
                                  timeout=vm.timeout_for(pr.info.duration, _min_timeout(work.run)))
        return exact if exact is not None else pr.info.duration


register(MediaRoute())
