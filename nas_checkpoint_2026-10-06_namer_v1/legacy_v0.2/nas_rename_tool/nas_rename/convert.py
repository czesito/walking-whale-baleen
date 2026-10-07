"""轉檔：全部輸出到 staging，不碰來源。回傳 (status, message)。
注意：此處的轉檔鏈尚未在真實資料上驗證；dry-run 不會呼叫本模組。"""
import os
import shutil
import tempfile
from typing import Dict, Optional, Tuple

from . import config as C
from .tools import run
from .validate import probe_av

OK, FAIL = "OK", "FAIL"


# ---------- 圖片 ----------
def convert_image(src: str, dst_base: str, kind: str) -> Tuple[str, str, str]:
    """回傳 (status, final_path, msg)。jpg/tif 通過 decode 後 byte-for-byte 複製（無損）。"""
    from PIL import Image
    ext = os.path.splitext(src)[1].lower()
    try:
        with Image.open(src) as im:
            frames = getattr(im, "n_frames", 1)
            im.load()
            fmt = im.format
            has_alpha = im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info
            deep = im.mode in ("I;16", "I;16B", "I;16L", "I", "F")
            if kind == "img_copy":
                want = "JPEG" if ext in (".jpg", ".jpeg") else "TIFF"
                if fmt != want:
                    return FAIL, "", f"extension {ext} but content is {fmt}"
                out = dst_base + (".jpg" if want == "JPEG" else ".tif")
                shutil.copyfile(src, out)
                return OK, out, f"verified {fmt}, copied bytes"
            if frames > 1:
                return FAIL, "", f"multi-frame image ({frames} frames) needs human decision"
            if has_alpha or deep or im.mode == "CMYK":
                out = dst_base + ".tif"
                im.save(out, "TIFF", compression="tiff_lzw", icc_profile=im.info.get("icc_profile"))
                return OK, out, f"TIFF (mode={im.mode})"
            out = dst_base + ".jpg"
            rgb = im.convert("RGB")
            rgb.save(out, "JPEG", quality=95, subsampling=0, optimize=True,
                     icc_profile=im.info.get("icc_profile"))
            return OK, out, f"JPEG q95 (from {fmt}, mode={im.mode})"
    except Exception as e:  # noqa
        return FAIL, "", f"image error: {e}"


# ---------- 文件 ----------
def _lo_pdfa(src: str, outdir: str, tools) -> Tuple[str, str]:
    profile = tempfile.mkdtemp(prefix="lo_profile_")
    filt = 'pdf:writer_pdf_Export:{"SelectPdfVersion":{"type":"long","value":"2"}}'  # PDF/A-2b
    p = run([tools["soffice"], f"-env:UserInstallation=file:///{profile.replace(os.sep, '/').lstrip('/')}",
             "--headless", "--convert-to", filt, "--outdir", outdir, src], timeout=900)
    produced = os.path.join(outdir, os.path.splitext(os.path.basename(src))[0] + ".pdf")
    shutil.rmtree(profile, ignore_errors=True)       # 自己建立的暫存 profile
    if p.returncode != 0 or not os.path.exists(produced):
        return FAIL, (p.stderr or p.stdout or "LibreOffice failed")[:300]
    return OK, produced


def detect_text_encoding(path: str) -> Tuple[Optional[str], bool]:
    """回傳 (encoding, confident)。只有 BOM 或嚴格 UTF-8 視為 confident。"""
    raw = open(path, "rb").read()
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig", True
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return "utf-16", True
    try:
        raw.decode("utf-8")
        return "utf-8", True
    except UnicodeDecodeError:
        pass
    for enc in ("big5hkscs", "cp950", "gb18030"):
        try:
            raw.decode(enc)
            return enc, False        # 不確定 → 需要人工確認
        except UnicodeDecodeError:
            continue
    return None, False


def convert_txt(src: str, stage_dir: str, tools) -> Tuple[str, str, str]:
    enc, confident = detect_text_encoding(src)
    if enc is None:
        return FAIL, "", "cannot decode text with utf-8/utf-16/big5/gb18030"
    if not confident:
        return FAIL, "", f"encoding guessed as {enc}; needs human confirmation (REVIEW_REQUIRED)"
    text = open(src, encoding=enc).read()
    tmp = os.path.join(stage_dir, "_in_" + os.path.basename(src))
    with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
        f.write(text)
    st, res = _lo_pdfa(tmp, stage_dir, tools)
    if st != OK:
        return FAIL, "", res
    # 字型缺失檢查：PDF 內可抽出的非空白字元數需接近原文
    p = run([tools["pdftotext"], "-enc", "UTF-8", res, "-"])
    want = len("".join(text.split()))
    got = len("".join(p.stdout.split()))
    if want and got < 0.98 * want:
        return FAIL, "", f"text loss: source {want} chars, pdf {got} chars (CJK font missing?)"
    return OK, res, f"txt({enc}) → PDF/A-2b, chars {got}/{want}"


def convert_doc(src: str, stage_dir: str, tools) -> Tuple[str, str, str]:
    st, res = _lo_pdfa(src, stage_dir, tools)
    return (OK, res, "LibreOffice PDF/A-2b") if st == OK else (FAIL, "", res)


def _html_to_pdfa(html_path: str, stage_dir: str, tools) -> Tuple[str, str, str]:
    """Chromium 列印成 PDF，再用 Ghostscript 轉 PDF/A-2b。缺資源 → FAIL（不輸出內容遺失的 PDF）。
    尚未在真實資料驗證（語料中無 html）。"""
    if not (tools.get("chromium") and tools.get("gs")):
        return FAIL, "", "needs Chromium + Ghostscript"
    raw_pdf = os.path.join(stage_dir, "_raw.pdf")
    p = run([tools["chromium"], "--headless", "--disable-gpu", "--no-sandbox",
             f"--print-to-pdf={raw_pdf}", "--no-pdf-header-footer", "file:///" + html_path.replace(os.sep, "/").lstrip("/")],
            timeout=300)
    if not os.path.exists(raw_pdf):
        return FAIL, "", (p.stderr or "chromium failed")[:300]
    out = os.path.join(stage_dir, "_pdfa.pdf")
    p = run([tools["gs"], "-dPDFA=2", "-dBATCH", "-dNOPAUSE", "-dNOOUTERSAVE", "-sDEVICE=pdfwrite",
             "-sColorConversionStrategy=RGB", "-dPDFACompatibilityPolicy=1", f"-sOutputFile={out}", raw_pdf], timeout=600)
    if p.returncode != 0 or not os.path.exists(out):
        return FAIL, "", (p.stderr or "gs failed")[:300]
    return OK, out, "Chromium → Ghostscript PDF/A-2b (external resource completeness NOT yet checked)"


def convert_html(src: str, stage_dir: str, tools):
    return _html_to_pdfa(os.path.abspath(src), stage_dir, tools)


def convert_eml(src: str, stage_dir: str, tools):
    """輸出 From / To / Cc / Date / Subject / Body。附件不獨立轉檔（規格 §5.3）：
    有非內嵌附件 → FAIL（呼叫端標 REVIEW_REQUIRED，原 EML 不動、附件不刪）。
    內嵌圖片（body 以 cid: 引用）以 data URI 嵌入畫面。"""
    import base64
    import re
    from email import message_from_binary_file, policy
    from html import escape
    from .eml_info import inspect_eml
    n, names, err = inspect_eml(src)
    if n < 0:
        return FAIL, "", f"EML cannot be parsed: {err}"
    if n > 0:
        return FAIL, "", f"REVIEW_REQUIRED: {n} attachment(s) would not be preserved in PDF/A: {names}"
    with open(src, "rb") as f:
        msg = message_from_binary_file(f, policy=policy.default)
    body = msg.get_body(preferencelist=("html", "plain"))
    content = body.get_content() if body else ""
    is_html = body is not None and body.get_content_type() == "text/html"
    if is_html:
        for part in msg.walk():
            cid = (part.get("Content-ID") or "").strip("<> ")
            if cid and part.get_content_maintype() == "image":
                data = base64.b64encode(part.get_payload(decode=True) or b"").decode()
                content = re.sub(r"cid:" + re.escape(cid), f"data:{part.get_content_type()};base64,{data}", content, flags=re.I)
    hdr = "".join(f"<tr><th align='left'>{k}</th><td>{escape(str(msg.get(k, '')))}</td></tr>"
                  for k in ("From", "To", "Cc", "Date", "Subject") if msg.get(k))
    inner = content if is_html else f"<pre style='white-space:pre-wrap'>{escape(content)}</pre>"
    page = f"<!doctype html><meta charset='utf-8'><table>{hdr}</table><hr>{inner}"
    tmp = os.path.join(stage_dir, "_eml.html")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(page)
    return _html_to_pdfa(tmp, stage_dir, tools)


# ---------- 影音 ----------
def convert_av(src: str, dst: str, tools) -> Tuple[str, str, float]:
    info, err = probe_av(src, tools)
    if info is None:
        return FAIL, f"ffprobe: {err}", 0.0
    dur = float(info.get("format", {}).get("duration") or 0)
    vs = [s for s in info["streams"] if s["codec_type"] == "video"]
    au = [s for s in info["streams"] if s["codec_type"] == "audio"]
    if not vs and not au:
        return FAIL, "no audio/video streams", dur
    can_copy_v = all(s["codec_name"] in C.MP4_COPY_VIDEO for s in vs)
    can_copy_a = all(s["codec_name"] in C.MP4_COPY_AUDIO for s in au)
    cmd = [tools["ffmpeg"], "-nostdin", "-v", "error", "-n", "-i", src, "-map", "0:v?", "-map", "0:a?"]
    cmd += ["-c:v", "copy"] if can_copy_v else ["-c:v", "libx264", "-crf", "18", "-preset", "slow", "-pix_fmt", "yuv420p"]
    cmd += ["-c:a", "copy"] if can_copy_a else ["-c:a", "aac", "-b:a", "192k"]
    cmd += ["-movflags", "+faststart", dst]
    p = run(cmd, timeout=6 * 3600)
    if p.returncode != 0 or not os.path.exists(dst):
        return FAIL, (p.stderr or "ffmpeg failed")[:300], dur
    how = f"video={'copy' if can_copy_v else 'x264'}, audio={'copy' if can_copy_a else 'aac'}"
    return OK, how, dur
