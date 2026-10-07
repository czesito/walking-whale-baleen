"""從「直接 parent folder 名稱」解析日期。不使用檔案 metadata。"""
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Optional

from .config import FIRST_YEAR, LAST_YEAR

# YYYY MM [DD[-DD]] ；日期 token 之後不可直接接數字（避免 "2001 09賀香滋" 之外的誤判）
_RE = re.compile(r"^\s*(\d{4})\s+(\d{2})(?!\d)(?:\s+(\d{2})(?!\d)(?:-(\d{2})(?!\d))?)?")


@dataclass(frozen=True)
class FolderDate:
    ok: bool
    year: int = 0
    month: int = 0
    day: Optional[int] = None      # None = 資料夾名稱沒有日
    day_end: Optional[int] = None  # 例如 "03 29-31"
    reason: str = ""

    @property
    def yyyymm(self) -> str:
        return f"{self.year:04d}{self.month:02d}"


def nfc_fold(s: str) -> str:
    return unicodedata.normalize("NFC", s).casefold()


def parse_folder_name(name: str) -> FolderDate:
    m = _RE.match(name)
    if not m:
        return FolderDate(False, reason="NO_YYYY_MM_PREFIX")
    y, mo = int(m.group(1)), int(m.group(2))
    d = int(m.group(3)) if m.group(3) else None
    de = int(m.group(4)) if m.group(4) else None
    if not (FIRST_YEAR <= y <= LAST_YEAR):
        return FolderDate(False, reason=f"YEAR_OUT_OF_RANGE:{y}")
    if not 1 <= mo <= 12:
        return FolderDate(False, reason=f"BAD_MONTH:{mo}")
    for dd in (d, de):
        if dd is not None:
            try:
                date(y, mo, dd)
            except ValueError:
                return FolderDate(False, reason=f"BAD_DAY:{y}-{mo:02d}-{dd:02d}")
    if d is not None and de is not None and de < d:
        return FolderDate(False, reason="BAD_DAY_RANGE")
    return FolderDate(True, y, mo, d, de)


def folder_sort_key(fd: FolderDate, folder_name: str, full_path: str):
    """a/b/c… 的排序：日期 → 完整資料夾名稱 → 完整 path。
    沒有日的資料夾排在同月份有日的資料夾之後（見 RENAME_SPEC §5，需使用者確認）。"""
    day_key = (0, fd.day) if fd.day is not None else (1, 0)
    return (fd.year, fd.month, day_key, nfc_fold(folder_name), folder_name,
            nfc_fold(full_path), full_path)
