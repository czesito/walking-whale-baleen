"""唯讀掃描。兩種來源：實體檔案系統（Windows UNC 可用）或 PowerShell 匯出的清單 CSV。"""
import csv
import os
from dataclasses import dataclass
from pathlib import PurePath
from typing import Iterator, List, Optional

from .config import SKIP_DIRNAMES


@dataclass
class SourceFile:
    path: str            # 完整路徑（保持來源的分隔符號）
    rel_parts: tuple     # 相對於 root 的各層名稱（最後一個是檔名）
    size: int
    readable: Optional[bool] = None   # None = 未檢查（listing 模式）

    @property
    def filename(self) -> str:
        return self.rel_parts[-1]

    @property
    def stem(self) -> str:
        return os.path.splitext(self.filename)[0]

    @property
    def ext(self) -> str:
        return os.path.splitext(self.filename)[1]

    @property
    def parent_name(self) -> str:
        return self.rel_parts[-2] if len(self.rel_parts) >= 2 else ""

    @property
    def folder_parts(self) -> tuple:
        return self.rel_parts[:-1]


def scan_fs(root: str, check_readable: bool = True) -> List[SourceFile]:
    """os.walk 只讀；跳過 staging 目錄。"""
    out: List[SourceFile] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(SKIP_DIRNAMES))
        for fn in sorted(filenames):
            p = os.path.join(dirpath, fn)
            rel = os.path.relpath(p, root).split(os.sep)
            try:
                size = os.stat(p).st_size
            except OSError:
                size = -1
            readable = None
            if check_readable:
                try:
                    with open(p, "rb") as f:
                        f.read(1)
                    readable = True
                except OSError:
                    readable = False
            out.append(SourceFile(p, tuple(rel), size, readable))
    return out


def scan_listing(csv_path: str, root: str) -> List[SourceFile]:
    """讀取 Get-ChildItem 匯出的 CSV（欄位 FullName, Length）。不碰檔案系統。"""
    out: List[SourceFile] = []
    root_n = root.rstrip("\\/")
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            full = row["FullName"]
            if not full.lower().startswith(root_n.lower()):
                raise ValueError(f"清單中的路徑不在 root 底下: {full}")
            rel = [p for p in full[len(root_n):].replace("/", "\\").split("\\") if p]
            if any(p.startswith(SKIP_DIRNAMES) for p in rel):
                continue
            out.append(SourceFile(full, tuple(rel), int(row.get("Length") or -1), None))
    return out
