"""Fixture group 'text' (spec §16.1): UTF-8 with CJK; UTF-8 BOM; UTF-16 BOM; Big5
(ENCODING_UNCERTAIN under auto, OK with big5). Plus a Windows-1252 file (ENCODING_UNCERTAIN
under auto with a windows-1252 hint; ENCODING_MISMATCH with big5). Synthetic text only."""

from __future__ import annotations

from pathlib import Path

from .common import write_bytes

CJK = (
    "訪談紀錄（合成測試文件）\n"
    "日期：二〇〇四年五月六日\n"
    "這是一份為 Baleen 自動測試產生的合成文字，不含任何真實檔案內容。\n"
    "日本語のテキストも少し含みます。한국어 문장도 있습니다.\n"
    "English line: the quick brown fox jumps over the lazy dog.\n"
)
BIG5 = "舊檔案：繁體中文的 Big5 編碼文字。\n第二行，測試用的合成內容。\n"


def build(dest: Path, tools) -> None:  # noqa: ANN001
    write_bytes(dest / "utf8_cjk.txt", CJK.encode("utf-8"))
    write_bytes(dest / "utf8_bom.txt", b"\xef\xbb\xbf" + CJK.replace("\n", "\r\n").encode("utf-8"))
    write_bytes(dest / "utf16_bom.txt", CJK.encode("utf-16"))  # BOM + native (LE) order
    big5 = BIG5.encode("big5")
    try:
        big5.decode("utf-8")
        raise AssertionError("the Big5 fixture must not be valid UTF-8")
    except UnicodeDecodeError:
        pass
    write_bytes(dest / "big5.txt", big5)
    write_bytes(dest / "latin1.txt", "Café crème brûlée, naïve façade.\r\n".encode("cp1252"))
