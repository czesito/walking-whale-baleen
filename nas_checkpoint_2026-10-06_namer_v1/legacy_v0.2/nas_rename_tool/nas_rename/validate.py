"""輸出驗證。任何驗證工具缺失 → 回傳 UNAVAILABLE，呼叫端不得放行。"""
import json
import re
from typing import Dict, Optional, Tuple

from .tools import run

OK, FAIL, UNAVAILABLE = "OK", "FAIL", "UNAVAILABLE"


def validate_image(path: str, expect: str) -> Tuple[str, str]:
    try:
        from PIL import Image
    except ImportError:
        return UNAVAILABLE, "Pillow missing"
    try:
        with Image.open(path) as im:
            fmt = im.format
            im.load()                       # 真的 decode
        if expect == ".jpg" and fmt != "JPEG":
            return FAIL, f"expected JPEG got {fmt}"
        if expect == ".tif" and fmt != "TIFF":
            return FAIL, f"expected TIFF got {fmt}"
        return OK, fmt
    except Exception as e:  # noqa
        return FAIL, f"decode error: {e}"


def validate_pdfa(path: str, tools: Dict[str, Optional[str]]) -> Tuple[str, str]:
    """只有 veraPDF 判定 isCompliant="true" 才算 PDF/A。不看 metadata／XMP 宣告。
    沒有 veraPDF → UNAVAILABLE（呼叫端必須標 PDF_A_NOT_VALIDATED，不得放行）。"""
    v = tools.get("verapdf")
    if not v:
        return UNAVAILABLE, "veraPDF not installed"
    p = run([v, "--format", "xml", path], timeout=900)
    out = p.stdout or ""
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(out)
    except Exception as e:  # noqa
        return FAIL, f"veraPDF output not parseable ({type(e).__name__}); rc={p.returncode}; {(p.stderr or '')[:120]}"
    reports = [el for el in root.iter() if el.tag.split("}")[-1] == "validationReport"]
    if not reports:
        return FAIL, f"veraPDF produced no validationReport (failed to parse PDF?); rc={p.returncode}"
    rep = reports[0]
    prof = rep.attrib.get("profileName", "unknown profile")
    if rep.attrib.get("isCompliant") == "true":
        return OK, f"PDF/A compliant per veraPDF ({prof})"
    return FAIL, f"not PDF/A compliant per veraPDF ({prof}); rc={p.returncode}"


def probe_av(path: str, tools: Dict[str, Optional[str]]):
    fp = tools.get("ffprobe")
    if not fp:
        return None, "ffprobe missing"
    p = run([fp, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path], timeout=600)
    if p.returncode != 0:
        return None, (p.stderr or "ffprobe failed")[:300]
    try:
        return json.loads(p.stdout), ""
    except json.JSONDecodeError as e:
        return None, str(e)


def validate_mp4(path: str, tools, src_duration: Optional[float] = None) -> Tuple[str, str]:
    info, err = probe_av(path, tools)
    if info is None:
        return (UNAVAILABLE if "missing" in err else FAIL), err
    fmt = info.get("format", {}).get("format_name", "")
    if "mp4" not in fmt:
        return FAIL, f"container={fmt}"
    streams = info.get("streams", [])
    if not any(s.get("codec_type") in ("video", "audio") for s in streams):
        return FAIL, "no audio/video stream"
    dur = float(info["format"].get("duration") or 0)
    if src_duration and abs(dur - src_duration) > max(1.0, 0.01 * src_duration):
        return FAIL, f"duration {dur:.1f}s vs source {src_duration:.1f}s"
    # 實際解碼一遍，確認串流沒壞
    ff = tools.get("ffmpeg")
    if ff:
        p = run([ff, "-v", "error", "-i", path, "-f", "null", "-"], timeout=7200)
        if p.returncode != 0 or p.stderr.strip():
            return FAIL, f"decode errors: {p.stderr.strip()[:200]}"
    return OK, f"mp4 ok, {dur:.1f}s"
