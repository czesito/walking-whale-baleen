"""Media route (spec §6.7): stream classification and the decision table, on stored synthetic
FFprobe JSON (no tools needed). Also source_format / method texts (§10.1)."""

from __future__ import annotations

import json

import pytest

from baleen.convert import media as m
from baleen.model import Action
from baleen.verify import media as vm

# A verbatim (trimmed) `ffprobe -show_format -show_streams -of json` of an MP3 with cover art.
MP3_WITH_COVER = json.loads("""
{
  "streams": [
    {"index": 0, "codec_name": "mp3", "codec_type": "audio", "sample_rate": "44100", "channels": 1,
     "duration": "3.000000", "disposition": {"default": 0, "attached_pic": 0}},
    {"index": 1, "codec_name": "mjpeg", "codec_type": "video", "width": 64, "height": 64,
     "pix_fmt": "yuvj420p", "disposition": {"default": 0, "attached_pic": 1},
     "tags": {"comment": "Cover (front)"}}
  ],
  "format": {"filename": "file:song.mp3", "nb_streams": 2, "format_name": "mp3",
             "format_long_name": "MP2/3 (MPEG audio layer 2/3)", "duration": "3.000000",
             "probe_score": 52, "tags": {"encoder": "Lavf63.1.100"}}
}
""")


def v(index: int, codec: str, w: int = 320, h: int = 240, field_order: str = "progressive",
      cover: bool = False) -> dict:
    return {"index": index, "codec_type": "video", "codec_name": codec, "width": w, "height": h,
            "field_order": field_order, "disposition": {"attached_pic": int(cover)}}


def a(index: int, codec: str, rate: int = 44100, ch: int = 2) -> dict:
    return {"index": index, "codec_type": "audio", "codec_name": codec, "sample_rate": str(rate),
            "channels": ch, "disposition": {"attached_pic": 0}}


def other(index: int, codec_type: str, codec: str) -> dict:
    return {"index": index, "codec_type": codec_type, "codec_name": codec, "disposition": {}}


MOV_FAMILY = "mov,mp4,m4a,3gp,3g2,mj2"


def info(format_name: str, streams: list[dict], brand: str = "", duration: str | None = "3.0",
         stderr: str = "") -> vm.MediaInfo:
    fmt: dict = {"format_name": format_name, "format_long_name": format_name}
    if duration is not None:
        fmt["duration"] = duration
    if brand:
        fmt["tags"] = {"major_brand": brand}
    got = vm.MediaInfo.from_json({"streams": streams, "format": fmt}, stderr)
    assert got is not None
    return got


# --------------------------------------------------------------------------- parsing


def test_parse_real_ffprobe_json_cover_art_is_not_video() -> None:
    i = vm.MediaInfo.from_json(MP3_WITH_COVER)
    assert i is not None
    assert i.format_name == "mp3" and i.duration == 3.0
    assert [s.index for s in i.audio] == [0]
    assert i.video == [] and [s.index for s in i.covers] == [1]
    assert i.dropped == []
    assert m.classify(i) == "audio"


def test_parse_missing_format_is_unparsed() -> None:
    assert vm.MediaInfo.from_json({}) is None
    assert vm.MediaInfo.from_json({"streams": []}) is None


def test_duration_falls_back_to_longest_stream() -> None:
    i = info("matroska,webm", [dict(v(0, "h264"), duration="2.5"), dict(a(1, "aac"), duration="2.9")],
             duration=None)
    assert i.duration == pytest.approx(2.9)


def test_bitrate_estimate_is_flagged() -> None:
    i = info("mp3", [a(0, "mp3")], stderr="[mp3 @ 0x1] Estimating duration from bitrate, this may be inaccurate\n")
    assert i.duration_estimated
    assert not info("mp3", [a(0, "mp3")]).duration_estimated


@pytest.mark.parametrize(("names", "brand", "expected"), [
    (MOV_FAMILY, "isom", True),
    (MOV_FAMILY, "mp42", True),
    (MOV_FAMILY, "M4A ", True),
    (MOV_FAMILY, "3gp4", True),
    (MOV_FAMILY, "qt  ", False),  # QuickTime is not MP4
    (MOV_FAMILY, "", False),  # old QuickTime without ftyp
    ("matroska,webm", "", False),
    ("avi", "", False),
])
def test_mp4_family(names: str, brand: str, expected: bool) -> None:
    assert info(names, [v(0, "h264")], brand=brand).is_mp4_family is expected


@pytest.mark.parametrize("names", ["hls", "concat", "image2", "jpeg_pipe", "png_pipe"])
def test_indirect_or_still_image_formats(names: str) -> None:
    assert info(names, [v(0, "mjpeg")]).indirect
    assert not info("avi", [v(0, "mjpeg")]).indirect


# --------------------------------------------------------------------------- classification


def test_classify() -> None:
    assert m.classify(info("avi", [v(0, "mjpeg"), a(1, "pcm_s16le")])) == "video"
    assert m.classify(info("avi", [a(0, "pcm_s16le")])) == "audio"  # audio-only AVI: audio route
    assert m.classify(info("matroska,webm", [other(0, "subtitle", "subrip")])) is None
    assert m.classify(info(MOV_FAMILY, [v(0, "mjpeg", cover=True)], brand="M4A ")) is None  # cover only
    assert m.classify(info("mp3", [a(0, "mp3"), v(1, "png", cover=True)])) == "audio"


# --------------------------------------------------------------------------- decision table


def test_mp4_h264_aac_is_byte_copy() -> None:
    d = m.decide(info(MOV_FAMILY, [v(0, "h264"), a(1, "aac")], brand="isom"), ".mp4")
    assert (d.kind, d.target_ext, d.action) == ("video", ".mp4", Action.COPY)


def test_mp4_hevc_mp3_without_audio_or_with_mp3_is_copy() -> None:
    assert m.decide(info(MOV_FAMILY, [v(0, "hevc")], brand="mp42"), ".mp4").action == Action.COPY
    assert m.decide(info(MOV_FAMILY, [v(0, "hevc"), a(1, "mp3")], brand="mp42"), ".mp4").action == Action.COPY


def test_same_codecs_other_container_is_remux() -> None:
    for names, ext, brand in (("matroska,webm", ".mkv", ""), ("flv", ".flv", ""), ("mpegts", ".ts", ""),
                              (MOV_FAMILY, ".mov", "qt  "), (MOV_FAMILY, ".m4v", "M4V "),
                              (MOV_FAMILY, ".mp4", "qt  ")):
        d = m.decide(info(names, [v(0, "h264"), a(1, "aac")], brand=brand), ext)
        assert (d.target_ext, d.action) == (".mp4", Action.REMUX), (names, ext, brand)
        assert all(p.copy for p in d.streams)


def test_other_codecs_are_transcoded() -> None:
    d = m.decide(info("avi", [v(0, "mjpeg"), a(1, "pcm_s16le")]), ".avi")
    assert d.action == Action.CONVERT
    assert [(p.kind, p.copy) for p in d.streams] == [("video", False), ("audio", False)]
    d = m.decide(info("mpeg", [v(0, "mpeg1video"), a(1, "mp2")]), ".mpg")
    assert d.action == Action.CONVERT and not any(p.copy for p in d.streams)


def test_per_stream_copy_keeps_h264_and_transcodes_ac3() -> None:
    # DR-12: avoid generation loss - an AVCHD clip keeps its H.264, only AC-3 becomes AAC.
    d = m.decide(info("mpegts", [v(0, "h264"), a(1, "ac3")]), ".mts")
    assert d.action == Action.CONVERT
    assert [(p.codec, p.copy) for p in d.streams] == [("h264", True), ("ac3", False)]
    # ... and an MP4 with MPEG-4 Part 2 video keeps its AAC.
    d = m.decide(info(MOV_FAMILY, [v(0, "mpeg4"), a(1, "aac")], brand="isom"), ".mp4")
    assert d.action == Action.CONVERT
    assert [(p.codec, p.copy) for p in d.streams] == [("mpeg4", False), ("aac", True)]


def test_odd_dimensions_padded_only_when_transcoding() -> None:
    d = m.decide(info(MOV_FAMILY, [v(0, "mjpeg", 321, 241), a(1, "aac")], brand="qt  "), ".mov")
    assert d.video[0].pad and d.video[0].size == (321, 241)
    assert not m.decide(info("avi", [v(0, "mjpeg", 320, 240)]), ".avi").video[0].pad
    assert not m.decide(info("matroska,webm", [v(0, "h264", 321, 241)]), ".mkv").video[0].pad  # copied


def test_interlaced_flag_only_when_transcoding() -> None:
    d = m.decide(info("mpeg", [v(0, "mpeg2video", 720, 576, "tt"), a(1, "mp2")]), ".vob")
    assert d.video[0].interlaced
    assert not m.decide(info("dv", [v(0, "dvvideo", field_order="progressive")]), ".dv").video[0].interlaced
    assert not m.decide(info("mpegts", [v(0, "h264", field_order="tt")]), ".ts").video[0].interlaced


def test_all_real_video_and_audio_streams_kept_cover_and_subtitles_dropped() -> None:
    d = m.decide(info("matroska,webm", [v(0, "h264"), a(1, "aac"), a(2, "ac3"), other(3, "subtitle", "subrip"),
                                         v(4, "mjpeg", cover=True), other(5, "attachment", "ttf")]), ".mkv")
    assert [p.index for p in d.streams] == [0, 1, 2]
    assert d.dropped == [3, 5]  # cover art is dropped silently (no note), §6.7


def test_audio_route_targets_and_copy_rules() -> None:
    cover_mp3 = vm.MediaInfo.from_json(MP3_WITH_COVER)
    assert cover_mp3 is not None
    d = m.decide(cover_mp3, ".mp3")
    assert (d.kind, d.target_ext, d.action) == ("audio", ".m4a", Action.REMUX)  # DR-13: MP3 copied
    assert [p.index for p in d.streams] == [0]  # the cover is never mapped
    assert m.decide(cover_mp3, ".mp3", "mp4").target_ext == ".mp4"  # DR-11 setting
    assert m.decide(info("asf", [a(0, "wmav2")]), ".wma").action == Action.CONVERT
    assert m.decide(info("wav", [a(0, "pcm_s16le")]), ".wav").action == Action.CONVERT
    assert m.decide(info("aac", [a(0, "aac")]), ".aac").action == Action.REMUX


def test_audio_already_in_target_container_and_extension_is_byte_copy() -> None:
    m4a = info(MOV_FAMILY, [a(0, "aac"), v(1, "png", cover=True)], brand="M4A ")
    assert m.decide(m4a, ".m4a").action == Action.COPY
    # Not the target extension any more (audio_container = mp4): remux into .mp4.
    d = m.decide(m4a, ".m4a", "mp4")
    assert (d.target_ext, d.action) == (".mp4", Action.REMUX)
    # An audio-only .mp4 with audio_container = mp4 is in place.
    assert m.decide(info(MOV_FAMILY, [a(0, "aac")], brand="isom"), ".mp4", "mp4").action == Action.COPY
    # §6.7 audio byte copy does not look at the codec (e.g. Apple Lossless stays lossless).
    assert m.decide(info(MOV_FAMILY, [a(0, "alac")], brand="M4A "), ".m4a").action == Action.COPY
    # An audio-only .mp4 with the default .m4a target: remux into .m4a.
    d = m.decide(info(MOV_FAMILY, [a(0, "aac")], brand="isom"), ".mp4")
    assert (d.kind, d.target_ext, d.action) == ("audio", ".m4a", Action.REMUX)


def test_byte_copy_keeps_extra_streams_without_note() -> None:
    d = m.decide(info(MOV_FAMILY, [v(0, "h264"), a(1, "aac"), other(2, "data", "none")], brand="isom"), ".mp4")
    assert d.action == Action.COPY and d.dropped == [2]  # the route notes STREAMS_DROPPED only for new files


def test_no_media_streams() -> None:
    d = m.decide(info("matroska,webm", [other(0, "subtitle", "subrip")]), ".mkv")
    assert (d.kind, d.target_ext, d.action, d.dropped) == (None, None, Action.NONE, [0])


def test_video_extension_with_only_audio_goes_audio_route() -> None:
    d = m.decide(info("avi", [a(0, "pcm_s16le", 22050, 1)]), ".avi")
    assert (d.kind, d.target_ext, d.action) == ("audio", ".m4a", Action.CONVERT)


# --------------------------------------------------------------------------- report texts


def test_source_format_texts() -> None:
    assert m.source_format(info("avi", [v(0, "mjpeg"), a(1, "pcm_s16le")]), ".avi") == "AVI (mjpeg / pcm_s16le)"
    assert m.source_format(info("mpeg", [v(0, "mpeg1video"), a(1, "mp2")]), ".mpg") == "MPEG-PS (mpeg1video / mp2)"
    assert m.source_format(info("asf", [a(0, "wmav2")]), ".wma") == "WMA (wmav2)"
    cover = vm.MediaInfo.from_json(MP3_WITH_COVER)
    assert cover is not None and m.source_format(cover, ".mp3") == "MP3 (mp3)"
    assert m.source_format(info(MOV_FAMILY, [v(0, "mjpeg")], brand="qt  "), ".mov") == "QuickTime (mjpeg)"
    assert m.source_format(info(MOV_FAMILY, [a(0, "aac")], brand="M4A "), ".m4a") == "M4A (aac)"
    assert m.source_format(info("matroska,webm", [v(0, "vp8"), a(1, "vorbis"), a(2, "opus")]), ".webm") == (
        "WebM (vp8 / vorbis, opus)")
    assert m.source_format(info("matroska,webm", [other(0, "subtitle", "subrip")]), ".mkv") == "Matroska"


def test_method_texts() -> None:
    q = m.QUALITY["high"]
    conv = m.decide(info("avi", [v(0, "mjpeg"), a(1, "pcm_s16le")]), ".avi")
    assert m.method_text("7.1", conv, q) == "FFmpeg 7.1 · libx264 crf18 · aac 192k"  # §10.1 example
    std = m.QUALITY["standard"]
    assert m.method_text("7.1", conv, std) == "FFmpeg 7.1 · libx264 crf23 · aac 128k"
    remux = m.decide(info("matroska,webm", [v(0, "h264"), a(1, "aac")]), ".mkv")
    assert m.method_text("9.0-full_build-www.gyan.dev", remux, q) == "FFmpeg 9.0 · h264 copy · aac copy"
    odd = m.decide(info(MOV_FAMILY, [v(0, "mjpeg", 321, 241), a(1, "aac")], brand="qt  "), ".mov")
    assert m.method_text("n8.0.1-3-gabc", odd, q) == "FFmpeg 8.0.1 · libx264 crf18 pad 322×242 · aac copy"
    inter = m.decide(info("mpeg", [v(0, "mpeg2video", 720, 576, "bb")]), ".vob")
    assert m.method_text("9.0", inter, q) == "FFmpeg 9.0 · libx264 crf18 interlaced"


def test_short_version() -> None:
    assert m.short_version("9.0-full_build-www.gyan.dev") == "9.0"
    assert m.short_version("n7.1.1-12-g0123abc-20260101") == "7.1.1"
    assert m.short_version("7.1") == "7.1"
    assert m.short_version("N-121234-gabcdef-20260101") == "N-121234-gabcdef-20260101"  # master build
    assert m.short_version("") == ""
