"""資料夾名稱日期解析（N-DATE）。只看名稱，不看 metadata。"""
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Optional

_SEP = r"[ ._\-]*"
# 月份後面不可直接接數字，除非是緊接的兩位數日期（20031101）
_RE = re.compile(r"^\s*(\d{4})" + _SEP + r"(\d{2})(?:(?!\d)|(?=\d{2}(?!\d)))(?:" + _SEP + r"(\d{2})(?!\d)(?:-(\d{2})(?!\d))?)?")


@dataclass(frozen=True)
class FolderDate:
    ok: bool
    year: int = 0
    month: int = 0
    day: Optional[int] = None
    day_end: Optional[int] = None
    reason: str = ""

    @property
    def yyyymm(self) -> str:
        return f"{self.year:04d}{self.month:02d}"


def nfc_fold(s: str) -> str:
    return unicodedata.normalize("NFC", s).casefold()


def parse_folder_name(name: str, year_from: int = 1900, year_to: int = 2099) -> FolderDate:
    m = _RE.match(name)
    if not m:
        return FolderDate(False, reason="NO_DATE_PREFIX")
    y, mo = int(m.group(1)), int(m.group(2))
    d = int(m.group(3)) if m.group(3) else None
    de = int(m.group(4)) if m.group(4) else None
    if not (year_from <= y <= year_to):
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


def folder_sort_key(fd: FolderDate, name: str, full_path: str):
    """N-LETTER 排序：日期（無日排在同月有日之後）→ 名稱 → 完整路徑。"""
    day_key = (0, fd.day) if fd.day is not None else (1, 0)
    return (fd.year, fd.month, day_key, nfc_fold(name), name, nfc_fold(full_path), full_path)
