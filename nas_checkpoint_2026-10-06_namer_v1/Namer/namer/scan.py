"""唯讀掃描（N-SCAN）。來源：實際檔案系統，或 PowerShell 匯出的清單 CSV。"""
import csv
import fnmatch
import os
from dataclasses import dataclass
from typing import List

from .folderdate import nfc_fold

SYSTEM_FILES = (".ds_store", "thumbs.db", "ehthumbs.db", "desktop.ini", "icon\r")
SYSTEM_PATTERNS = ("._*", "~$*", ".~lock.*#")
SKIP_DIRS = ("$recycle.bin", ".trashes", ".spotlight-v100", ".fseventsd", "@eadir", "#recycle",
             ".@__thumb", "_baleen", ".baleen-staging")


@dataclass
class Entry:
    rel: tuple          # 相對 root 的各層名稱，最後一個是自己的名稱
    is_dir: bool
    size: int = -1

    @property
    def name(self) -> str:
        return self.rel[-1]

    @property
    def folder(self) -> tuple:
        return self.rel[:-1]


def is_system_file(name: str) -> bool:
    low = name.casefold()
    if low in SYSTEM_FILES:
        return True
    return any(fnmatch.fnmatchcase(low, p) for p in SYSTEM_PATTERNS)


def is_skip_dir(name: str) -> bool:
    return name.casefold() in SKIP_DIRS


def _sorted(names):
    return sorted(names, key=lambda n: (nfc_fold(n), n))


def scan_fs(root: str, progress=None) -> List[Entry]:
    out: List[Entry] = []
    count = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root)
        base = () if rel_dir in (".", "") else tuple(rel_dir.split(os.sep))
        keep = []
        for d in _sorted(dirnames):
            full = os.path.join(dirpath, d)
            if is_skip_dir(d) or os.path.islink(full):
                continue
            keep.append(d)
            out.append(Entry(base + (d,), True))
        dirnames[:] = keep
        for fn in _sorted(filenames):
            full = os.path.join(dirpath, fn)
            if os.path.islink(full):
                continue
            try:
                size = os.stat(full).st_size
            except OSError:
                size = -1
            out.append(Entry(base + (fn,), False, size))
            count += 1
            if progress and count % 200 == 0:
                progress(count)
    return out


def scan_listing(csv_path: str, root: str) -> List[Entry]:
    """讀 Get-ChildItem 匯出的 CSV（欄位 FullName, Length）。資料夾由路徑推得。"""
    root_n = root.rstrip("\\/")
    files, dirs = [], set()
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            full = row["FullName"]
            if not full.casefold().startswith(root_n.casefold()):
                raise ValueError(f"清單中的路徑不在根資料夾底下：{full}")
            rel = tuple(p for p in full[len(root_n):].replace("/", "\\").split("\\") if p)
            if any(is_skip_dir(p) for p in rel[:-1]):
                continue
            files.append(Entry(rel, False, int(row.get("Length") or -1)))
            for i in range(1, len(rel)):
                dirs.add(rel[:i])
    return [Entry(d, True) for d in sorted(dirs)] + files
