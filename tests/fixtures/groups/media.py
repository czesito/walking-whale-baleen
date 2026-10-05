"""Fixture group 'media' (spec §16.1, §6.7). Owned by workstream (c): audio and video.

Synthetic only: every file is generated from FFmpeg's lavfi test sources (testsrc, sine,
aevalsrc), a few seconds long. Expected results: tests/integration/expected.csv, section media.

    video/mjpeg-pcm.avi        AVI MJPEG + PCM             -> transcode
    video/mpeg1-mp2.mpg        MPEG-1 + MP2                -> transcode
    video/copy-h264-aac.mp4    MP4 H.264 + AAC             -> byte copy (check)
    video/remux-h264-aac.mkv   MKV H.264 + AAC             -> remux
    video/odd-321x241.mov      MOV MJPEG 321x241 + AAC     -> transcode, padded; AAC copied
    video/subtitled.mkv        MKV H.264 + AAC + SubRip    -> remux, STREAMS_DROPPED
    video/audio-only.avi       AVI with PCM audio only     -> audio route (.m4a)
    video/subtitles-only.mkv   MKV with one SubRip stream  -> NO_MEDIA_STREAMS
    video/truncated.mp4        MP4 cut short (moov first)  -> SOURCE_INVALID
    video/not-a-video.avi      plain text                  -> SOURCE_UNREADABLE
    audio/voice.wma            WMA v2                      -> AAC .m4a
    audio/song-cover.mp3       MP3 + attached cover JPEG   -> remux .m4a, cover dropped
    audio/tone.wav             WAV PCM                     -> AAC .m4a
    audio/aac-copy.m4a         M4A AAC                     -> byte copy (remux with audio_mp4)
    audio/vbr-no-header.mp3    VBR MP3 without Xing header -> remux; FFprobe misjudges its
                               duration from the bit rate, V-AV-DUR must use the exact one
"""

from __future__ import annotations

import os
from pathlib import Path

from .common import jpeg, write_bytes, write_text

SECONDS = 3
SRT = (
    "1\n00:00:00,000 --> 00:00:01,500\nSynthetic subtitle one.\n\n"
    "2\n00:00:01,500 --> 00:00:02,900\nSynthetic subtitle two.\n"
)


def _video(size: str = "320x240", rate: int = 15, seconds: int = SECONDS) -> list[str]:
    return ["-f", "lavfi", "-i", f"testsrc=size={size}:rate={rate}:duration={seconds}"]


def _tone(freq: int = 440, rate: int = 44100, seconds: int = SECONDS) -> list[str]:
    return ["-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate={rate}:duration={seconds}"]


H264 = ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p"]


def build(dest: Path, tools) -> None:  # noqa: ANN001
    tools.need("ffmpeg")
    ff = tools.ffmpeg_run
    video, audio = dest / "video", dest / "audio"
    tmp = Path(tools.profile_dir).parent / "media-inputs"  # removed with the build's .work folder
    for d in (video, audio, tmp):
        os.makedirs(d, exist_ok=True)
    srt = write_text(tmp / "subs.srt", SRT)
    cover = jpeg(tmp / "cover.jpg", size=(64, 64), color=(150, 40, 40))

    # ---- video route
    ff([*_video(), *_tone(rate=22050), "-c:v", "mjpeg", "-q:v", "5", "-c:a", "pcm_s16le",
        str(video / "mjpeg-pcm.avi")])
    ff([*_video("352x288", 25), *_tone(), "-c:v", "mpeg1video", "-b:v", "1500k", "-c:a", "mp2", "-b:a", "128k",
        "-f", "mpeg", str(video / "mpeg1-mp2.mpg")])
    ff([*_video(), *_tone(), *H264, "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart",
        str(video / "copy-h264-aac.mp4")])
    ff([*_video(), *_tone(), *H264, "-c:a", "aac", "-b:a", "96k", str(video / "remux-h264-aac.mkv")])
    ff([*_video("321x241", 25), *_tone(), "-c:v", "mjpeg", "-pix_fmt", "yuvj422p", "-q:v", "4",
        "-c:a", "aac", "-b:a", "96k", str(video / "odd-321x241.mov")])
    ff([*_video(), *_tone(), "-i", str(srt), "-map", "0:v", "-map", "1:a", "-map", "2:s", *H264,
        "-c:a", "aac", "-b:a", "96k", "-c:s", "srt", str(video / "subtitled.mkv")])
    ff([*_tone(rate=22050), "-c:a", "pcm_s16le", "-f", "avi", str(video / "audio-only.avi")])
    ff(["-i", str(srt), "-c:s", "srt", str(video / "subtitles-only.mkv")])

    whole = tmp / "whole.mp4"
    ff([*_video(), *_tone(), *H264, "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(whole)])
    data = whole.read_bytes()
    write_bytes(video / "truncated.mp4", data[: int(len(data) * 0.6)])  # moov intact, media cut short
    write_text(video / "not-a-video.avi", "".join(
        f"This is a plain text file with a video extension, line {i}.\n" for i in range(40)))

    # ---- audio route
    ff([*_tone(330), "-c:a", "wmav2", "-b:a", "64k", str(audio / "voice.wma")])
    ff([*_tone(550), "-i", str(cover), "-map", "0:a", "-map", "1:v", "-c:a", "libmp3lame", "-b:a", "128k",
        "-c:v", "copy", "-disposition:v", "attached_pic", "-id3v2_version", "3", str(audio / "song-cover.mp3")])
    ff([*_tone(660, 22050), "-c:a", "pcm_s16le", str(audio / "tone.wav")])
    ff([*_tone(770), "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(audio / "aac-copy.m4a")])
    # Quiet first (low bit rate), loud noise after: FFprobe's bit-rate estimate is ~3x too long.
    expr = "if(lt(t,2),0.002*sin(2*PI*440*t),0.8*(random(0)*2-1))"
    ff(["-f", "lavfi", "-i", f"aevalsrc='{expr}':s=44100:d=6", "-ac", "2", "-c:a", "libmp3lame", "-q:a", "0",
        "-write_xing", "0", str(audio / "vbr-no-header.mp3")])
