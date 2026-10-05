"""Media route (spec §6.7, §8, §5.5): FFmpeg/FFprobe argument building (threads, protocol
whitelist, stream maps, pad filter), V-AV-DUR tolerance, timeouts, check verdicts, and the
route's probe/process paths driven by a fake proc.run (no tools needed)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from baleen import proc
from baleen import settings as S
from baleen.convert import media as m
from baleen.convert.base import ProbeContext, RunContext, SourceRef, WorkItem
from baleen.home import Home
from baleen.model import Action, Category, CheckState, Mode, PlanItem
from baleen.proc import ProcResult
from baleen.verify import media as vm

MOV = "mov,mp4,m4a,3gp,3g2,mj2"


def mi(format_name: str, streams: list[dict], brand: str = "", duration: str = "3.0") -> vm.MediaInfo:
    fmt: dict = {"format_name": format_name, "duration": duration}
    if brand:
        fmt["tags"] = {"major_brand": brand}
    got = vm.MediaInfo.from_json({"streams": streams, "format": fmt})
    assert got is not None
    return got


def vs(i: int, codec: str, w: int = 320, h: int = 240, fo: str = "progressive", cover: bool = False) -> dict:
    return {"index": i, "codec_type": "video", "codec_name": codec, "width": w, "height": h, "field_order": fo,
            "disposition": {"attached_pic": int(cover)}}


def aus(i: int, codec: str) -> dict:
    return {"index": i, "codec_type": "audio", "codec_name": codec, "disposition": {}}


def sub(i: int) -> dict:
    return {"index": i, "codec_type": "subtitle", "codec_name": "subrip", "disposition": {}}


def value_after(args: list[str], flag: str) -> list[str]:
    return [args[i + 1] for i, x in enumerate(args[:-1]) if x == flag]


# --------------------------------------------------------------------------- build_args


def test_transcode_args_follow_spec() -> None:
    d = m.decide(mi("avi", [vs(0, "mjpeg"), aus(1, "pcm_s16le")]), ".avi")
    args = m.build_args("ffmpeg", "/w/1/input.avi", "/w/1/output.mp4", d, threads=6, quality=m.QUALITY["high"])
    i = args.index("-i")
    head, tail = args[:i], args[i + 2:]
    # Input side (§6.7 'Always'): -nostdin -n, the protocol whitelist (SEC-9), +genpts; -threads (§5.5)
    for flag in ("-nostdin", "-n"):
        assert flag in head
    assert value_after(head, "-protocol_whitelist") == ["file,pipe"]
    assert value_after(head, "-fflags") == ["+genpts"]
    assert value_after(head, "-threads") == ["6"] and value_after(head, "-filter_threads") == ["6"]
    assert args[i + 1] == "file:/w/1/input.avi"
    # Output side: explicit maps, libx264 slow CRF 18 yuv420p, AAC 192k, metadata, faststart, -f mp4
    assert value_after(tail, "-map") == ["0:0", "0:1"]
    assert value_after(tail, "-c:v:0") == ["libx264"]
    assert value_after(tail, "-preset:v:0") == ["slow"]
    assert value_after(tail, "-crf:v:0") == ["18"]
    assert value_after(tail, "-pix_fmt:v:0") == ["yuv420p"]
    assert value_after(tail, "-c:a:0") == ["aac"] and value_after(tail, "-b:a:0") == ["192k"]
    assert value_after(tail, "-map_metadata") == ["0"]
    assert value_after(tail, "-movflags") == ["+faststart"]
    assert value_after(tail, "-threads") == ["6"]
    assert tail[-3:] == ["-f", "mp4", "file:/w/1/output.mp4"]
    assert "-sn" in tail and "-dn" in tail
    # Frame rate, size and sample rate untouched; no deinterlacing; no scaling filter without need.
    for flag in ("-r", "-s", "-ar", "-ac", "-vf", "-filter:v:0", "-flags:v:0"):
        assert flag not in args
    assert "-y" not in args


def test_standard_quality() -> None:
    d = m.decide(mi("avi", [vs(0, "mjpeg"), aus(1, "pcm_s16le")]), ".avi")
    args = m.build_args("ffmpeg", "i", "o", d, threads=1, quality=m.QUALITY["standard"])
    assert value_after(args, "-crf:v:0") == ["23"] and value_after(args, "-b:a:0") == ["128k"]


def test_odd_size_gets_pad_filter() -> None:
    d = m.decide(mi(MOV, [vs(0, "mjpeg", 321, 241), aus(1, "aac")], brand="qt  "), ".mov")
    args = m.build_args("ffmpeg", "i", "o", d, threads=2, quality=m.QUALITY["high"])
    assert value_after(args, "-filter:v:0") == ["pad=ceil(iw/2)*2:ceil(ih/2)*2"]
    assert value_after(args, "-c:a:0") == ["copy"]  # AAC is copied even when the video is transcoded


def test_interlaced_source_keeps_interlacing() -> None:
    d = m.decide(mi("mpeg", [vs(0, "mpeg2video", 720, 576, "tt"), aus(1, "mp2")]), ".vob")
    args = m.build_args("ffmpeg", "i", "o", d, threads=2, quality=m.QUALITY["high"])
    assert value_after(args, "-flags:v:0") == ["+ildct+ilme"]
    assert value_after(args, "-filter:v:0") == ["scale=interl=1"]
    assert not any("yadif" in x or "bwdif" in x for x in args)


def test_remux_maps_by_index_and_copies() -> None:
    d = m.decide(mi("matroska,webm", [vs(0, "h264"), sub(1), aus(2, "aac"), vs(3, "mjpeg", cover=True)]), ".mkv")
    args = m.build_args("ffmpeg", "i", "o", d, threads=3, quality=m.QUALITY["high"])
    assert value_after(args, "-map") == ["0:0", "0:2"]  # never the subtitle or the cover
    assert value_after(args, "-c:v:0") == ["copy"] and value_after(args, "-c:a:0") == ["copy"]
    assert "libx264" not in args and "-vn" not in args


def test_audio_route_maps_only_audio() -> None:
    info = vm.MediaInfo.from_json({"format": {"format_name": "mp3", "duration": "3"}, "streams": [
        aus(0, "mp3"), vs(1, "mjpeg", 64, 64, cover=True)]})
    assert info is not None
    d = m.decide(info, ".mp3")
    args = m.build_args("ffmpeg", "i", "o.m4a", d, threads=2, quality=m.QUALITY["high"])
    assert value_after(args, "-map") == ["0:0"]
    assert "-vn" in args and value_after(args, "-c:a:0") == ["copy"]
    assert args[-3:] == ["-f", "mp4", "file:o.m4a"]  # .m4a is the MPEG-4 container too


def test_mixed_streams_use_per_stream_specifiers() -> None:
    d = m.decide(mi("matroska,webm", [vs(0, "h264"), vs(1, "mpeg4"), aus(2, "aac"), aus(3, "ac3")]), ".mkv")
    args = m.build_args("ffmpeg", "i", "o", d, threads=2, quality=m.QUALITY["high"])
    assert value_after(args, "-c:v:0") == ["copy"] and value_after(args, "-c:v:1") == ["libx264"]
    assert value_after(args, "-c:a:0") == ["copy"] and value_after(args, "-c:a:1") == ["aac"]
    assert value_after(args, "-b:a:1") == ["192k"] and not value_after(args, "-b:a:0")


def test_threads_never_below_one() -> None:
    d = m.decide(mi("avi", [vs(0, "mjpeg")]), ".avi")
    args = m.build_args("ffmpeg", "i", "o", d, threads=0, quality=m.QUALITY["high"])
    assert set(value_after(args, "-threads")) == {"1"}


def test_probe_and_decode_args() -> None:
    p = vm.ffprobe_args("ffprobe", "file:x.avi")
    assert p[:1] == ["ffprobe"] and value_after(p, "-v") == ["error"] and value_after(p, "-threads") == ["1"]
    assert value_after(p, "-protocol_whitelist") == ["file,pipe"]
    assert {"-show_format", "-show_streams"} <= set(p) and value_after(p, "-of") == ["json"]
    d = vm.decode_args("ffmpeg", "/o/out.mp4", 4)
    assert value_after(d, "-v") == ["error"] and set(value_after(d, "-threads")) == {"4"}
    assert value_after(d, "-protocol_whitelist") == ["file,pipe"] and "-nostdin" in d
    assert value_after(d, "-i") == ["file:/o/out.mp4"]
    assert d[-3:] == ["-f", "null", "-"]
    assert value_after(d, "-map") == ["0:v?", "0:a?"]  # every audio and video stream is decoded


def test_input_url_never_a_protocol() -> None:
    assert vm.input_url("http:evil.mp4") == "file:http:evil.mp4"
    assert vm.input_url(r"C:\a b\訪談.avi") == r"file:C:\a b\訪談.avi"


# --------------------------------------------------------------------------- thresholds


@pytest.mark.parametrize(("src", "tol"), [(0.5, 1.0), (30, 1.0), (100, 1.0), (300, 3.0), (3600, 36.0)])
def test_duration_tolerance(src: float, tol: float) -> None:
    assert vm.duration_tolerance(src) == pytest.approx(tol)


def test_v_av_dur() -> None:
    assert vm.v_av_dur(30.0, 31.0).state == CheckState.PASS  # exactly 1 s
    assert vm.v_av_dur(30.0, 31.01).state == CheckState.FAIL
    assert vm.v_av_dur(600.0, 594.0).state == CheckState.PASS  # 1% of 600 s
    assert vm.v_av_dur(600.0, 593.9).state == CheckState.FAIL
    r = vm.v_av_dur(None, 3.0)
    assert r.state == CheckState.UNAVAILABLE and not r.tool_missing
    assert vm.v_av_dur(3.0, None).state == CheckState.FAIL


def test_timeout_for() -> None:
    assert vm.timeout_for(30.0, 600) == 600  # max(600 s, 10 x 30 s)
    assert vm.timeout_for(3600.0, 600) == 36000
    assert vm.timeout_for(None, 600) == 600
    assert vm.timeout_for(0.0, 45) == 45


def test_crashed() -> None:
    assert vm.crashed(None)
    assert not vm.crashed(0) and not vm.crashed(1)
    if os.name == "nt":
        assert vm.crashed(0xC0000005)  # access violation
        assert not vm.crashed(4294967274)  # AVERROR(EINVAL) as an unsigned exit code
    else:
        assert vm.crashed(-11)


def test_tidy_strips_paths_and_pointers() -> None:
    text = "[h264 @ 0000022a0bb9e500] bad NAL\n[in#0/mov @ 0x55d0c0ffee00] file:/w/3/input.mp4: partial file\n"
    assert vm.tidy(text, "/w/3/input.mp4") == "[h264] bad NAL [in#0/mov] input.mp4: partial file"


# --------------------------------------------------------------------------- V-AV-PROBE verdicts


def run_of(info: vm.MediaInfo | None, rc: int | None = 0, **kw) -> vm.ProbeRun:  # noqa: ANN003
    return vm.ProbeRun(info, rc, **kw)


def test_v_av_probe_verdicts() -> None:
    mp4 = mi(MOV, [vs(0, "h264"), aus(1, "aac")], brand="isom")
    assert vm.v_av_probe(run_of(mp4), "video").state == CheckState.PASS
    assert vm.v_av_probe(run_of(mp4), "audio").state == CheckState.FAIL  # video in an audio target
    m4a_cover = mi(MOV, [aus(0, "aac"), vs(1, "png", cover=True)], brand="M4A ")
    assert vm.v_av_probe(run_of(m4a_cover), "audio").state == CheckState.PASS  # cover art doesn't count
    assert vm.v_av_probe(run_of(m4a_cover), "video").state == CheckState.FAIL
    assert vm.v_av_probe(run_of(mi(MOV, [vs(0, "png", cover=True)], brand="M4A ")), "audio").state == CheckState.FAIL
    assert vm.v_av_probe(run_of(mi(MOV, [vs(0, "h264")], brand="qt  ")), "video").state == CheckState.FAIL
    assert vm.v_av_probe(run_of(mi("matroska,webm", [vs(0, "h264")])), "video").state == CheckState.FAIL
    assert vm.v_av_probe(run_of(None, 1, stderr="moov atom not found"), "video").state == CheckState.FAIL
    missing = vm.v_av_probe(run_of(None, None, error="FileNotFoundError"), "video")
    assert missing.state == CheckState.UNAVAILABLE and missing.tool_missing
    hung = vm.v_av_probe(run_of(None, None, timed_out=True), "video")
    assert hung.state == CheckState.UNAVAILABLE and not hung.tool_missing


# --------------------------------------------------------------------------- fake tools


@dataclass
class FakeInfo:
    version: str = "9.0-test"
    path: str = ""
    found: bool = True


class FakeTools:
    def __init__(self, missing: tuple[str, ...] = ()) -> None:
        self.missing_tools = missing

    def path(self, key: str) -> str | None:
        return None if key in self.missing_tools else key

    def get(self, key: str) -> FakeInfo:
        return FakeInfo()

    def env(self) -> dict[str, str]:
        return {"FAKE": "1"}


class FakeProc:
    """Stands in for proc.run: canned FFprobe JSON per file, FFmpeg writes its output file."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.probe: dict[str, dict | None] = {}  # basename -> ffprobe JSON (None = unreadable)
        self.decode_stderr = ""
        self.convert_rc = 0
        self.convert_timeout = False

    def __call__(self, args, *, timeout, env=None, cwd=None, low_priority=False, input_bytes=None):  # noqa: ANN001, ANN204
        self.calls.append({"args": list(args), "timeout": timeout, "env": env, "cwd": cwd,
                           "low_priority": low_priority})
        exe = os.path.basename(args[0])
        target = args[-1]
        if exe == "ffprobe":
            name = os.path.basename(target.removeprefix("file:"))
            data = self.probe.get(name)
            if data is None:
                return ProcResult(list(args), 1, b"{}", f"{target}: Invalid data found".encode())
            return ProcResult(list(args), 0, json.dumps(data).encode(), b"")
        if target == "-":  # V-AV-DECODE
            return ProcResult(list(args), 0, b"", self.decode_stderr.encode())
        if self.convert_timeout:
            return ProcResult(list(args), None, timed_out=True)
        out = target.removeprefix("file:")
        if self.convert_rc == 0:
            Path(out).write_bytes(b"fake mp4")
            self.probe[os.path.basename(out)] = self.output_json
        return ProcResult(list(args), self.convert_rc, b"", b"" if self.convert_rc == 0 else b"encoder exploded")

    output_json: dict = {"format": {"format_name": MOV, "duration": "3.0", "tags": {"major_brand": "isom"}},
                         "streams": [vs(0, "h264"), aus(1, "aac")]}


@pytest.fixture()
def fake(monkeypatch) -> FakeProc:  # noqa: ANN001
    f = FakeProc()
    monkeypatch.setattr(proc, "run", f)
    return f


def settings(**workflow) -> dict:  # noqa: ANN003
    st = S.defaults()
    st["workflow"].update(workflow)
    st["advanced"]["av_timeout_min_s"] = 600
    return st


def probe_ctx(tmp_path: Path, mode: Mode = Mode.CONVERT, missing: tuple[str, ...] = (), **wf) -> ProbeContext:  # noqa: ANN003
    home = Home(tmp_path / "home")
    home.ensure()
    return ProbeContext(mode, settings(**wf), FakeTools(missing), home, low_priority=True)  # type: ignore[arg-type]


def src_ref(tmp_path: Path, name: str) -> SourceRef:
    p = tmp_path / "src" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    return SourceRef(name, os.path.splitext(name)[1].lower(), path=str(p), size=1)


ROUTE = m.MediaRoute()


# --------------------------------------------------------------------------- probe (plan time)


def test_probe_plans_by_content(tmp_path: Path, fake: FakeProc) -> None:
    fake.probe["clip.avi"] = {"format": {"format_name": "avi", "duration": "3.0"},
                              "streams": [vs(0, "mjpeg"), aus(1, "pcm_s16le")]}
    pr = ROUTE.probe(probe_ctx(tmp_path), src_ref(tmp_path, "clip.avi"))
    assert (pr.category, pr.target_ext, pr.action, pr.route) == (Category.VIDEO, ".mp4", Action.CONVERT, "media")
    assert pr.source_format == "AVI (mjpeg / pcm_s16le)"
    assert pr.method == "FFmpeg 9.0 · libx264 crf18 · aac 192k"
    assert pr.data["media"] == {"kind": "video", "duration": 3.0}
    call = fake.calls[0]
    assert value_after(call["args"], "-threads") == ["1"] and call["low_priority"] is True
    assert call["args"][-1].startswith("file:") and call["env"] == {"FAKE": "1"}


def test_probe_audio_only_avi_goes_audio_route(tmp_path: Path, fake: FakeProc) -> None:
    fake.probe["memo.avi"] = {"format": {"format_name": "avi", "duration": "3.0"}, "streams": [aus(0, "pcm_s16le")]}
    pr = ROUTE.probe(probe_ctx(tmp_path), src_ref(tmp_path, "memo.avi"))
    assert (pr.category, pr.target_ext, pr.action) == (Category.AUDIO, ".m4a", Action.CONVERT)
    pr = ROUTE.probe(probe_ctx(tmp_path, audio_container="mp4"), src_ref(tmp_path, "memo.avi"))
    assert pr.target_ext == ".mp4"


def test_probe_unreadable_source(tmp_path: Path, fake: FakeProc) -> None:
    pr = ROUTE.probe(probe_ctx(tmp_path), src_ref(tmp_path, "bad.avi"))
    assert pr.reasons == ["SOURCE_UNREADABLE"] and pr.target_ext == ".mp4"
    assert "bad.avi: Invalid data found" in pr.message and "file:" not in pr.message


def test_probe_unreadable_mp4_is_left_to_the_checks(tmp_path: Path, fake: FakeProc) -> None:
    # §8: SOURCE_INVALID takes precedence over SOURCE_UNREADABLE for MP4/M4A sources.
    for name, target in (("bad.mp4", ".mp4"), ("bad.m4a", ".m4a")):
        pr = ROUTE.probe(probe_ctx(tmp_path), src_ref(tmp_path, name))
        assert (pr.action, pr.reasons, pr.target_ext) == (Action.COPY, [], target)


def test_probe_no_media_streams(tmp_path: Path, fake: FakeProc) -> None:
    fake.probe["subs.mkv"] = {"format": {"format_name": "matroska,webm"}, "streams": [sub(0)]}
    pr = ROUTE.probe(probe_ctx(tmp_path), src_ref(tmp_path, "subs.mkv"))
    assert pr.reasons == ["NO_MEDIA_STREAMS"] and pr.action == Action.NONE
    fake.probe["pic.avi"] = {"format": {"format_name": "jpeg_pipe"}, "streams": [vs(0, "mjpeg")]}
    assert ROUTE.probe(probe_ctx(tmp_path), src_ref(tmp_path, "pic.avi")).reasons == ["NO_MEDIA_STREAMS"]


def test_probe_streams_dropped_note_only_for_new_files_in_convert(tmp_path: Path, fake: FakeProc) -> None:
    fake.probe["s.mkv"] = {"format": {"format_name": "matroska,webm", "duration": "3"},
                           "streams": [vs(0, "h264"), aus(1, "aac"), sub(2)]}
    pr = ROUTE.probe(probe_ctx(tmp_path), src_ref(tmp_path, "s.mkv"))
    assert pr.action == Action.REMUX and pr.notes == ["STREAMS_DROPPED"]
    assert ROUTE.probe(probe_ctx(tmp_path, Mode.CHECK), src_ref(tmp_path, "s.mkv")).notes == []
    fake.probe["s.mp4"] = {"format": {"format_name": MOV, "duration": "3", "tags": {"major_brand": "isom"}},
                           "streams": [vs(0, "h264"), aus(1, "aac"), sub(2)]}
    pr = ROUTE.probe(probe_ctx(tmp_path), src_ref(tmp_path, "s.mp4"))
    assert pr.action == Action.COPY and pr.notes == [] and pr.method == "byte copy"


def test_probe_without_ffprobe_falls_back_to_extension(tmp_path: Path, fake: FakeProc) -> None:
    ctx = probe_ctx(tmp_path, missing=("ffprobe",))
    pr = ROUTE.probe(ctx, src_ref(tmp_path, "clip.avi"))
    assert (pr.target_ext, pr.action, pr.reasons) == (".mp4", Action.CONVERT, ["TOOL_MISSING"])
    pr = ROUTE.probe(ctx, src_ref(tmp_path, "voice.wma"))
    assert (pr.category, pr.target_ext, pr.reasons) == (Category.AUDIO, ".m4a", ["TOOL_MISSING"])
    pr = ROUTE.probe(ctx, src_ref(tmp_path, "clip.mp4"))
    assert (pr.action, pr.reasons) == (Action.COPY, [])  # copied; its checks say VALIDATOR_MISSING
    # Check mode: convertible files become NOT_ARCHIVAL_FORMAT in the planner, not TOOL_MISSING.
    pr = ROUTE.probe(probe_ctx(tmp_path, Mode.CHECK, missing=("ffprobe",)), src_ref(tmp_path, "clip.avi"))
    assert (pr.action, pr.reasons) == (Action.CONVERT, [])
    pr = ROUTE.probe(probe_ctx(tmp_path, Mode.CHECK, missing=("ffprobe",), audio_container="mp4"),
                     src_ref(tmp_path, "song.m4a"))
    assert (pr.action, pr.target_ext) == (Action.COPY, ".m4a")
    assert fake.calls == []


def test_check_mode_accepts_m4a_and_audio_mp4_whatever_audio_container(tmp_path: Path, fake: FakeProc) -> None:
    fake.probe["a.mp4"] = {"format": {"format_name": MOV, "duration": "3", "tags": {"major_brand": "isom"}},
                           "streams": [aus(0, "aac")]}
    fake.probe["b.m4a"] = {"format": {"format_name": MOV, "duration": "3", "tags": {"major_brand": "M4A "}},
                           "streams": [aus(0, "aac")]}
    for container in ("m4a", "mp4"):
        for name in ("a.mp4", "b.m4a"):
            pr = ROUTE.probe(probe_ctx(tmp_path, Mode.CHECK, audio_container=container), src_ref(tmp_path, name))
            got = (pr.category, pr.action, pr.target_ext)
            assert got == (Category.AUDIO, Action.COPY, name[-4:]), (container, name)
    # Convert mode follows the setting (DR-11): the audio-only .mp4 is remuxed into .m4a.
    pr = ROUTE.probe(probe_ctx(tmp_path), src_ref(tmp_path, "a.mp4"))
    assert (pr.action, pr.target_ext) == (Action.REMUX, ".m4a")


# --------------------------------------------------------------------------- process (run time)


@dataclass
class Ctx:
    tokens: int = 5
    low_priority: bool = True


def work_item(tmp_path: Path, name: str, *, action: Action, category: Category, target: str,
              check_only: bool = False, **wf) -> WorkItem:  # noqa: ANN003
    home = Home(tmp_path / "home")
    home.ensure()
    run = RunContext(run_id="r1", mode=Mode.CONVERT, home=home, tools=FakeTools(),  # type: ignore[arg-type]
                     settings=settings(**wf), source_root=str(tmp_path / "src"), output_root=str(tmp_path / "out"),
                     work_root=str(tmp_path / "work"))
    ext = os.path.splitext(name)[1].lower()
    plan = PlanItem(n=1, source_path=name, abs_path=str(tmp_path / "src" / name), size=1, mtime_ns=1, ext=ext,
                    category=category, action=action, route="media", target_ext=target, out_dir="",
                    output_path=os.path.splitext(name)[0] + target)
    wi = WorkItem(plan=plan, run=run, work_dir=str(tmp_path / "work" / "1"), source_abs=plan.abs_path,
                  check_only=check_only, category=category, action=action)
    if not check_only:
        os.makedirs(wi.work_dir, exist_ok=True)
        wi.staged = os.path.join(wi.work_dir, "input" + ext)
        Path(wi.staged).write_bytes(b"x")
    return wi


def test_convert_passes_tokens_priority_and_timeout(tmp_path: Path, fake: FakeProc) -> None:
    fake.probe["input.avi"] = {"format": {"format_name": "avi", "duration": "120.0"},
                               "streams": [vs(0, "mjpeg", 321, 241), aus(1, "pcm_s16le")]}
    fake.output_json = {"format": {"format_name": MOV, "duration": "120.2", "tags": {"major_brand": "isom"}},
                        "streams": [vs(0, "h264", 322, 242), aus(1, "aac")]}
    wi = work_item(tmp_path, "clip.avi", action=Action.CONVERT, category=Category.VIDEO, target=".mp4")
    ROUTE.process(Ctx(tokens=7), wi)  # type: ignore[arg-type]
    assert not wi.reasons, wi.messages
    assert wi.new_output and wi.result_path and wi.result_path.startswith(wi.work_dir)
    assert wi.action == Action.CONVERT
    assert wi.method == "FFmpeg 9.0 · libx264 crf18 pad 322×242 · aac 192k"
    assert [c.report_form() for c in wi.checks] == ["V-AV-PROBE=pass", "V-AV-DUR=pass", "V-AV-DECODE=pass"]
    ffmpeg_calls = [c for c in fake.calls if os.path.basename(c["args"][0]) == "ffmpeg"]
    assert len(ffmpeg_calls) == 2  # the conversion and V-AV-DECODE
    for c in ffmpeg_calls:  # §5.5: every FFmpeg call carries the task's tokens
        assert set(value_after(c["args"], "-threads")) == {"7"}
        assert c["low_priority"] is True and c["env"] == {"FAKE": "1"}
        assert value_after(c["args"], "-protocol_whitelist") == ["file,pipe"]
    conv = ffmpeg_calls[0]
    assert conv["timeout"] == 1200  # max(600 s, 10 x 120 s)
    assert conv["cwd"] == wi.work_dir
    assert value_after(conv["args"], "-i") == ["file:" + wi.staged]  # only ever the staged copy (P1)
    assert value_after(conv["args"], "-filter:v:0") == ["pad=ceil(iw/2)*2:ceil(ih/2)*2"]


def test_convert_duration_mismatch_is_verify_failed(tmp_path: Path, fake: FakeProc) -> None:
    fake.probe["input.wav"] = {"format": {"format_name": "wav", "duration": "30.0"}, "streams": [aus(0, "pcm_s16le")]}
    fake.output_json = {"format": {"format_name": MOV, "duration": "12.0", "tags": {"major_brand": "isom"}},
                        "streams": [aus(0, "aac")]}
    wi = work_item(tmp_path, "tone.wav", action=Action.CONVERT, category=Category.AUDIO, target=".m4a")
    ROUTE.process(Ctx(), wi)  # type: ignore[arg-type]
    assert [c.report_form() for c in wi.checks] == ["V-AV-PROBE=pass", "V-AV-DUR=fail", "V-AV-DECODE=pass"]
    assert ROUTE.decide(wi) == ["VERIFY_FAILED"]
    assert any(msg.startswith("V-AV-DUR:") for msg in wi.messages)


def test_convert_timeout_and_error(tmp_path: Path, fake: FakeProc) -> None:
    fake.probe["input.wma"] = {"format": {"format_name": "asf", "duration": "3.0"}, "streams": [aus(0, "wmav2")]}
    fake.convert_timeout = True
    wi = work_item(tmp_path, "v.wma", action=Action.CONVERT, category=Category.AUDIO, target=".m4a")
    ROUTE.process(Ctx(), wi)  # type: ignore[arg-type]
    assert wi.reasons == ["TIMEOUT"] and wi.done and not wi.result_path
    fake.convert_timeout = False
    fake.convert_rc = 1
    wi = work_item(tmp_path / "b", "v.wma", action=Action.CONVERT, category=Category.AUDIO, target=".m4a")
    ROUTE.process(Ctx(), wi)  # type: ignore[arg-type]
    assert wi.reasons == ["CONVERSION_ERROR"] and "encoder exploded" in wi.messages[-1]


def test_copy_checks_staged_copy_and_failure_is_source_invalid(tmp_path: Path, fake: FakeProc) -> None:
    fake.probe["input.mp4"] = fake.output_json
    wi = work_item(tmp_path, "c.mp4", action=Action.COPY, category=Category.VIDEO, target=".mp4")
    ROUTE.process(Ctx(tokens=3), wi)  # type: ignore[arg-type]
    assert wi.result_path == wi.staged and not wi.new_output and wi.method == "byte copy"
    assert [c.report_form() for c in wi.checks] == ["V-AV-PROBE=pass", "V-AV-DUR=n/a", "V-AV-DECODE=pass"]
    assert all(c["args"][-1] != "file:" + str(wi.source_abs) for c in fake.calls)  # reads the copy
    fake.decode_stderr = "[in#0/mov @ 0x1] stream 1: partial file"
    wi = work_item(tmp_path / "b", "c.mp4", action=Action.COPY, category=Category.VIDEO, target=".mp4")
    ROUTE.process(Ctx(), wi)  # type: ignore[arg-type]
    assert ROUTE.decide(wi) == ["SOURCE_INVALID"]


def test_check_only_reads_source_in_place(tmp_path: Path, fake: FakeProc) -> None:
    fake.probe["c.m4a"] = {"format": {"format_name": MOV, "duration": "3", "tags": {"major_brand": "M4A "}},
                           "streams": [aus(0, "aac"), vs(1, "png", cover=True)]}
    wi = work_item(tmp_path, "c.m4a", action=Action.CHECK, category=Category.AUDIO, target=".m4a", check_only=True)
    ROUTE.process(Ctx(), wi)  # type: ignore[arg-type]
    assert wi.result_path is None and wi.method == ""
    assert [c.report_form() for c in wi.checks] == ["V-AV-PROBE=pass", "V-AV-DUR=n/a", "V-AV-DECODE=pass"]
    assert {c["args"][-1] for c in fake.calls if "-show_format" in c["args"]} == {"file:" + str(wi.source_abs)}
    assert not os.path.exists(wi.work_dir)  # nothing written for a check-only item
    assert all(c["cwd"] == str(wi.run.home.tmp_dir) for c in fake.calls)


def test_resume_rechecks_existing_output(tmp_path: Path, fake: FakeProc) -> None:
    fake.probe["clip.avi"] = {"format": {"format_name": "avi", "duration": "3.0"},
                              "streams": [vs(0, "mjpeg"), aus(1, "pcm_s16le")]}
    fake.probe["clip.mp4"] = fake.output_json
    wi = work_item(tmp_path, "clip.avi", action=Action.CONVERT, category=Category.VIDEO, target=".mp4")
    existing = tmp_path / "out" / "clip.mp4"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"old")
    wi.resume, wi.existing_output, wi.action = True, str(existing), Action.CHECK
    Path(str(wi.source_abs)).parent.mkdir(parents=True, exist_ok=True)
    ROUTE.check_existing(Ctx(), wi)  # type: ignore[arg-type]
    assert [c.report_form() for c in wi.checks] == ["V-AV-PROBE=pass", "V-AV-DUR=pass", "V-AV-DECODE=pass"]
    assert existing.read_bytes() == b"old"


def test_tool_missing_at_check_time_is_validator_missing(tmp_path: Path, fake: FakeProc) -> None:
    wi = work_item(tmp_path, "c.mp4", action=Action.COPY, category=Category.VIDEO, target=".mp4")
    wi.run.tools = FakeTools(missing=("ffprobe", "ffmpeg"))  # type: ignore[assignment]
    ROUTE.process(Ctx(), wi)  # type: ignore[arg-type]
    assert [c.state for c in wi.checks] == [CheckState.UNAVAILABLE, CheckState.NA, CheckState.UNAVAILABLE]
    assert ROUTE.decide(wi) == ["VALIDATOR_MISSING"]


def test_exact_duration_from_packets(monkeypatch) -> None:  # noqa: ANN001
    csv = "0,0.000000,0.026122\n0,0.026122,0.026122\n1,0.000000,0.040000\n0,5.982041,0.026122\n0,N/A,0.02\n"

    def fake_run(args, **kw):  # noqa: ANN001, ANN003, ANN202
        assert value_after(args, "-protocol_whitelist") == ["file,pipe"]
        return ProcResult(list(args), 0, csv.encode(), b"")

    monkeypatch.setattr(proc, "run", fake_run)
    got = vm.exact_duration("ffprobe", "x.mp3", [0], env=None, low_priority=True, timeout=60)
    assert got == pytest.approx(6.008163)
