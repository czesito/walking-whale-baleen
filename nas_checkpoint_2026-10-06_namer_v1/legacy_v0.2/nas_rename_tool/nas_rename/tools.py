"""偵測外部工具。缺工具時絕不假裝成功（見規格 §10）。"""
import glob
import os
import shutil
import subprocess
from typing import Dict, Optional


def _first(*names) -> Optional[str]:
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return None


def detect() -> Dict[str, Optional[str]]:
    t = {
        "ffmpeg": _first("ffmpeg", "ffmpeg.exe"),
        "ffprobe": _first("ffprobe", "ffprobe.exe"),
        "soffice": _first("soffice", "soffice.exe", "libreoffice"),
        "verapdf": _first("verapdf", "verapdf.bat", "verapdf.cmd"),
        "gs": _first("gs", "gswin64c", "gswin64c.exe", "gswin32c.exe"),
        "pdftotext": _first("pdftotext", "pdftotext.exe"),
        "chromium": _first("chromium", "chromium-browser", "google-chrome", "chrome", "msedge"),
    }
    if not t["soffice"]:
        for pat in (r"C:\Program Files\LibreOffice\program\soffice.exe",
                    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"):
            if os.path.exists(pat):
                t["soffice"] = pat
    if not t["chromium"]:
        for pat in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                    "/opt/pw-browsers/chromium-*/chrome-linux/chrome"):
            hits = glob.glob(pat)
            if hits:
                t["chromium"] = hits[0]
                break
    if not t["verapdf"]:
        for pat in (r"C:\Program Files\veraPDF\verapdf.bat", r"C:\Program Files (x86)\veraPDF\verapdf.bat",
                    os.path.expanduser(r"~\veraPDF\verapdf.bat")):
            if os.path.exists(pat):
                t["verapdf"] = pat
                break
    if not t["gs"]:
        for pat in (r"C:\Program Files\gs\gs*\bin\gswin64c.exe", r"C:\Program Files (x86)\gs\gs*\bin\gswin32c.exe"):
            hits = sorted(glob.glob(pat))
            if hits:
                t["gs"] = hits[-1]
                break
    try:
        import PIL  # noqa
        t["pillow"] = "PIL"
    except ImportError:
        t["pillow"] = None
    return t


def run(cmd, timeout=3600, **kw):
    """永遠不用 shell=True。"""
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, shell=False, **kw)


# 每種 kind 需要的工具；缺任何一個 → CONVERSION_REQUIRED
# 例外：只缺 veraPDF 時（見 pdfa_only_missing）可以完成 conversion，但狀態必須是 PDF_A_NOT_VALIDATED
NEEDS = {
    "img_copy": ["pillow"],
    "img_conv": ["pillow"],
    "pdf": ["verapdf"],
    "doc": ["soffice", "verapdf"],
    "rtf": ["soffice", "verapdf"],
    "txt": ["soffice", "verapdf", "pdftotext"],
    "html": ["chromium", "gs", "verapdf"],
    "eml": ["chromium", "gs", "verapdf"],
    "av": ["ffmpeg", "ffprobe"],
}


def missing_for(kind: str, tools: Dict[str, Optional[str]]):
    return [n for n in NEEDS.get(kind, []) if not tools.get(n)]


PDF_KINDS = {"pdf", "doc", "rtf", "txt", "html", "eml"}   # 輸出為 PDF/A 的 kind


def pdfa_only_missing(kind: str, tools) -> bool:
    """True = 只缺 veraPDF（轉檔鏈完整，但無法驗證 PDF/A）。"""
    return kind in PDF_KINDS and missing_for(kind, tools) == ["verapdf"]
