"""由 SourceFile 清單產生 manifest rows（純函式，不碰檔案系統）。

v0.2 規則優先級（RENAME_SPEC §2–§4）：
  1. 既有 DP2 命名結果（不重新分配字母與序號）
  2. 可解析日期
  3. 無日期
  4. 完整資料夾名稱
  5. 完整 path
"""
import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

from . import config as C
from .folderparse import FolderDate, folder_sort_key, nfc_fold, parse_folder_name
from .scanner import SourceFile

LETTERS = "abcdefghijklmnopqrstuvwxyz"
_DP2_RE = re.compile(r"^DP2_99_04_(\d{6})([a-z]+)_(\d+)$", re.I)     # 既有：DP2_99_04_YYYYMM?_##
_DP2_00_RE = re.compile(r"^DP2_99_04_00_(\d+)$", re.I)                # 既有：DP2_99_04_00_##（不屬於任何 YYYYMM）

MANIFEST_COLUMNS = [
    "source_path", "source_folder", "source_filename", "source_extension",
    "parsed_yyyymm", "folder_letter", "sequence_number",
    "target_filename", "target_extension", "target_path",
    "conversion_required", "conversion_method", "status", "reason",
    # 以下為額外欄位
    "source_size", "group_id", "proposed_target_filename", "note",
    # v0.2 新增
    "date_source_folder", "name_origin", "planned_action",
    "source_relative_path", "archive_path",
    "attachment_detected", "attachment_count",
]
# 計算 manifest digest 時使用的欄位（排除 note 等說明性欄位）
DIGEST_COLUMNS = MANIFEST_COLUMNS[:14] + ["source_size"] + MANIFEST_COLUMNS[18:]

# name_origin
NO_EXISTING, NO_ASSIGNED, NO_EXISTING_00 = "EXISTING_DP2", "ASSIGNED", "EXISTING_DP2_00"
# planned_action
A_VERIFY, A_EXT_NORM, A_COPY, A_CONVERT, A_NONE = (
    "VERIFY_ONLY", "NORMALIZE_EXT_IN_PLACE", "COPY_THEN_ARCHIVE", "CONVERT_THEN_ARCHIVE", "NONE")


@dataclass
class Row:
    source_path: str
    source_folder: str
    source_filename: str
    source_extension: str
    parsed_yyyymm: str = ""
    folder_letter: str = ""
    sequence_number: str = ""
    target_filename: str = ""
    target_extension: str = ""
    target_path: str = ""
    conversion_required: str = ""
    conversion_method: str = ""
    status: str = ""
    reason: str = ""
    source_size: int = -1
    group_id: str = ""
    proposed_target_filename: str = ""
    note: str = ""
    date_source_folder: str = ""
    name_origin: str = ""
    planned_action: str = A_NONE
    source_relative_path: str = ""
    archive_path: str = ""
    attachment_detected: str = ""
    attachment_count: str = ""

    def as_dict(self):
        return {c: getattr(self, c) for c in MANIFEST_COLUMNS}


def classify(ext: str) -> Tuple[str, str, str, str]:
    """回傳 (conversion_required, target_ext, method, kind)。kind 用於 executor。"""
    e = ext.lower()
    if e in (".jpg", ".jpeg"):
        return "no", ".jpg", "JPEG verify+copy (decode check)", "img_copy"
    if e in (".tif", ".tiff"):
        return "no", ".tif", "TIFF verify+copy (decode check)", "img_copy"
    if e in C.IMAGE_EXTS:
        return "yes", ".jpg", f"{e[1:].upper()} → JPEG (decode/encode; TIFF if alpha/16-bit/multipage, decided at execute)", "img_conv"
    if e == ".pdf":
        return "verify", ".pdf", "PDF → verify PDF/A (veraPDF); never overwritten; non-compliant → PDF_A_CONVERSION_REQUIRED", "pdf"
    if e in (".doc", ".docx"):
        return "yes", ".pdf", f"{e[1:].upper()} → PDF/A-2b (LibreOffice)", "doc"
    if e == ".rtf":
        return "yes", ".pdf", "RTF → PDF/A-2b (LibreOffice)", "rtf"
    if e == ".txt":
        return "yes", ".pdf", "TXT → PDF/A-2b (encoding detect + LibreOffice + CJK font check)", "txt"
    if e in (".html", ".htm"):
        return "yes", ".pdf", "HTML → render (Chromium) → PDF/A-2b (Ghostscript)", "html"
    if e == ".eml":
        return "yes", ".pdf", "EML → From/To/Cc/Date/Subject/Body → PDF/A-2b; attachments stay inside the archived EML (not converted separately)", "eml"
    if e == ".mp4":
        return "verify", ".mp4", "MP4 → ffprobe check; stream copy if codecs OK else re-encode H.264/AAC", "av"
    if e in C.AV_EXTS:
        return "yes", ".mp4", f"{e[1:].upper()} → MP4 (ffmpeg; stream copy if codecs OK else H.264/AAC)", "av"
    return "", "", "", "unknown"


def letter_at(n: int) -> Optional[str]:
    return LETTERS[n] if n < C.MAX_LETTERS else None


def file_sort_key(f: SourceFile):
    return (nfc_fold(f.stem), nfc_fold(f.ext), f.stem, f.ext)


def seq_str(n: int) -> str:
    return f"{n:0{C.MIN_SEQ_WIDTH}d}"


def _sep(path: str) -> str:
    return "\\" if "\\" in path else "/"


@dataclass
class Unit:
    """一個『資料來源單位』＝ 一個含檔案的資料夾（巢狀子資料夾各自獨立）。"""
    parts: Tuple[str, ...]
    fd: FolderDate                      # 用來決定 YYYYMM 的日期（可能繼承自祖先）
    date_source_parts: Tuple[str, ...] = ()
    inherited: bool = False
    existing_letters: Optional[Set[Tuple[str, str]]] = None     # {(yyyymm, letter)}
    existing_ym_mismatch: str = ""
    conflict: str = ""                  # 既有命名衝突 → 整個單位 REVIEW
    letter: Optional[str] = None


def _resolve_unit_date(fp: Tuple[str, ...]):
    """直接 parent 可解析 → 用自己；否則沿祖先往上找最近的可解析日期（日期繼承，RENAME_SPEC §3）。"""
    if not fp:
        return FolderDate(False, reason="FILE_DIRECTLY_UNDER_ROOT"), (), False
    own = parse_folder_name(fp[-1])
    if own.ok:
        return own, fp, False
    for i in range(len(fp) - 1, 0, -1):
        cand = parse_folder_name(fp[i - 1])
        if cand.ok:
            return cand, fp[:i], True
    return own, (), False


def plan(files: List[SourceFile], root: str) -> List[Row]:
    sep = _sep(root)
    root_n = root.rstrip("\\/")
    archive_root = root_n + sep + C.ARCHIVE_DIRNAME
    groups: Dict[Tuple[str, ...], List[SourceFile]] = defaultdict(list)
    for f in files:
        groups[f.folder_parts].append(f)

    # ---- 1. 每個單位的日期（含繼承）與既有 DP2 命名 ----
    units: Dict[Tuple[str, ...], Unit] = {}
    for fp, flist in groups.items():
        fd, src_parts, inh = _resolve_unit_date(fp)
        u = Unit(fp, fd, src_parts, inh, set())
        mism = set()
        for f in flist:
            m = _DP2_RE.match(f.stem)
            if m:
                u.existing_letters.add((m.group(1), m.group(2).lower()))
                if fd.ok and m.group(1) != fd.yyyymm:
                    mism.add(m.group(1))
        if len(u.existing_letters) > 1:
            u.conflict = "EXISTING_MULTIPLE_LETTERS:" + ",".join(sorted(f"{a}{b}" for a, b in u.existing_letters))
        elif mism:
            u.conflict = f"EXISTING_NAME_YYYYMM_MISMATCH(folder={fd.yyyymm}, name={','.join(sorted(mism))})"
        units[fp] = u

    # 既有字母佔用表：(yyyymm, letter) -> [unit parts]
    claims: Dict[Tuple[str, str], List[Tuple[str, ...]]] = defaultdict(list)
    for fp, u in units.items():
        for key in u.existing_letters:
            claims[key].append(fp)
    for key, fps in claims.items():
        if len(fps) > 1:
            for fp in fps:
                if not units[fp].conflict:
                    units[fp].conflict = f"EXISTING_LETTER_SHARED_BY_{len(fps)}_FOLDERS:{key[0]}{key[1]}"

    # ---- 2. 字母：既有優先；新單位依 日期 → 無日 → 完整名稱 → 完整 path，取尚未被占用的最小字母 ----
    occupied: Dict[str, Set[str]] = defaultdict(set)
    for (ym, le) in claims:
        occupied[ym].add(le)
    for fp, u in units.items():
        if len(u.existing_letters) == 1 and not u.conflict:
            u.letter = next(iter(u.existing_letters))[1]
    new_by_ym: Dict[str, List[Unit]] = defaultdict(list)
    for fp, u in units.items():
        if u.fd.ok and not u.existing_letters and not u.conflict:
            new_by_ym[u.fd.yyyymm].append(u)
    for ym, us in new_by_ym.items():
        us.sort(key=lambda u: folder_sort_key(
            u.fd, sep.join(u.parts) if u.inherited else u.parts[-1], sep.join(u.parts)))
        for u in us:
            for le in LETTERS[:C.MAX_LETTERS]:
                if le not in occupied[ym]:
                    occupied[ym].add(le)
                    u.letter = le
                    break
            else:
                u.letter = None          # LETTER_OVERFLOW

    # ---- 3. 逐檔建立 row ----
    rows: Dict[str, Row] = {}
    for fp, flist in groups.items():
        u = units[fp]
        fd = u.fd
        folder_path = root_n + (sep + sep.join(fp) if fp else "")
        dsf = (root_n + sep + sep.join(u.date_source_parts)) if u.date_source_parts else ""
        # 序號：既有的保留；其餘依 file_sort_key 取尚未使用的最小號碼（所有檔案都佔號，不論狀態）
        used: Set[int] = set()
        existing_seq: Dict[str, int] = {}
        for f in flist:
            m = _DP2_RE.match(f.stem)
            if m and not u.conflict and u.letter:
                existing_seq[f.path] = int(m.group(3))
                used.add(int(m.group(3)))
        seq_of: Dict[str, int] = dict(existing_seq)
        nxt = 1
        for f in sorted(flist, key=file_sort_key):
            if f.path in seq_of:
                continue
            while nxt in used:
                nxt += 1
            seq_of[f.path] = nxt
            used.add(nxt)

        for f in sorted(flist, key=file_sort_key):
            conv, t_ext, method, kind = classify(f.ext)
            rel = sep.join(f.rel_parts)
            r = Row(f.path, folder_path, f.filename, f.ext, source_size=f.size,
                    conversion_required=conv, conversion_method=method,
                    source_relative_path=rel, date_source_folder=dsf)
            if kind == "eml":
                r.attachment_detected, r.attachment_count = "UNKNOWN", ""
            rows[f.path] = r
            if f.size == 0:
                r.status, r.reason = C.ST_UNREADABLE, "EMPTY_FILE"
            if f.readable is False:
                r.status, r.reason = C.ST_UNREADABLE, "CANNOT_READ"

            # ---- DP2_99_04_00_## 既有系列：視為既有編目結果，不改名，只驗證 PDF/A ----
            m0 = _DP2_00_RE.match(f.stem)
            if m0:
                r.name_origin = NO_EXISTING_00
                r.sequence_number = str(int(m0.group(1))).zfill(max(C.MIN_SEQ_WIDTH, len(m0.group(1))))
                if kind == "pdf" and f.ext == ".pdf":
                    r.target_filename, r.target_extension = f.filename, ".pdf"
                    r.target_path = folder_path + sep + f.filename
                    if not r.status:
                        r.status, r.planned_action = C.ST_ALREADY, A_VERIFY
                        r.reason = "EXISTING_DP2_00_SERIES (keep name; verify PDF/A with veraPDF at execute)"
                elif not r.status:
                    r.status, r.reason = C.ST_REVIEW, f"EXISTING_DP2_00_SERIES_NOT_PDF:{f.ext}"
                continue

            if kind == "unknown":
                if not r.status:
                    r.status, r.reason = C.ST_UNSUPPORTED, f"UNKNOWN_FORMAT:{f.ext or '(none)'}"
                continue

            if not fd.ok:
                if not r.status:
                    r.status = C.ST_REVIEW
                    r.reason = (f"DATE_NOT_DETERMINABLE_NO_GUESS:{fd.reason}" if fp
                                else "FILE_DIRECTLY_UNDER_ROOT")
                continue
            r.parsed_yyyymm = fd.yyyymm
            if u.conflict:
                if not r.status:
                    r.status, r.reason = C.ST_REVIEW, u.conflict
                continue
            if u.letter is None:
                if not r.status:
                    r.status, r.reason = C.ST_REVIEW, "LETTER_OVERFLOW_MORE_THAN_26_FOLDERS"
                continue
            r.folder_letter = u.letter
            r.sequence_number = seq_str(seq_of[f.path])
            r.group_id = f"{fd.yyyymm}{u.letter}"
            existing = f.path in existing_seq
            r.name_origin = NO_EXISTING if existing else NO_ASSIGNED
            r.target_extension = t_ext
            r.target_filename = f"{C.PREFIX}{r.group_id}_{r.sequence_number}{t_ext}"
            r.target_path = folder_path + sep + r.target_filename
            if not r.status:
                _decide(r, f, kind, existing)
            if u.inherited:
                r.note = _join(r.note, f"DATE_INHERITED_FROM_PARENT_FOLDER({sep.join(u.date_source_parts)})")
            if fd.day is None and not u.inherited and r.status != C.ST_REVIEW:
                r.note = _join(r.note, "FOLDER_HAS_NO_DAY(sorted after dated folders)")
            if r.status == C.ST_REVIEW and r.target_filename:
                r.proposed_target_filename = r.target_filename
    out = _finalize(list(rows.values()))
    for r in out:
        if r.planned_action in (A_COPY, A_CONVERT) and r.status in (C.ST_PLANNED, C.ST_CONV_PLANNED):
            r.archive_path = archive_root + sep + r.source_relative_path
    return out


def _join(a: str, b: str) -> str:
    return f"{a}; {b}" if a else b


def _decide(r: Row, f: SourceFile, kind: str, existing: bool):
    target_stem = r.target_filename[:-len(r.target_extension)]
    same_stem = (f.stem == target_stem)
    no_conv = r.conversion_required in ("no", "verify")
    if kind == "eml":
        r.note = _join(r.note, "attachments: see attachment_detected/attachment_count (live dry-run only)")
    if existing and same_stem and no_conv and f.ext == r.target_extension:
        r.status, r.planned_action = C.ST_ALREADY, A_VERIFY
        r.reason = "EXISTING_DP2_NAME_AND_EXT (content verified at execute)"
        return
    if existing and same_stem and no_conv and f.ext.lower() == r.target_extension:
        # 已經是 DP2 檔名，只差副檔名大小寫：允許的 filename normalization（in-place 兩段式改名）
        r.status, r.planned_action = C.ST_PLANNED, A_EXT_NORM
        r.reason = "EXISTING_DP2_NAME: extension normalization only (.JPG→.jpg), keep letter and number"
        return
    if r.conversion_required == "yes":
        r.status, r.planned_action = C.ST_CONV_PLANNED, A_CONVERT
        r.reason = ("EXISTING_DP2_NAME kept; needs conversion" if existing else "needs conversion")
    else:
        r.status, r.planned_action = C.ST_PLANNED, A_COPY
        r.reason = "format verified at execute; copy to target name" + ("" if not existing else " (existing name kept)")
        if r.conversion_required == "verify":
            r.reason = "format verified at execute; converted only if codecs not MP4-compatible"
    r.reason += "; original → _ORIGINALS after validated output"


def _finalize(rows: List[Row]) -> List[Row]:
    """collision 偵測（不分大小寫，Windows 檔案系統）。"""
    cur = {r.source_path.casefold(): r for r in rows}
    by_target: Dict[str, List[Row]] = defaultdict(list)
    for r in rows:
        if r.target_path and r.status in (C.ST_PLANNED, C.ST_CONV_PLANNED, C.ST_ALREADY, C.ST_REVIEW):
            by_target[r.target_path.casefold()].append(r)
    for tp, rs in by_target.items():
        occupied = cur.get(tp)
        if len(rs) > 1:
            for r in rs:
                r.status, r.reason, r.planned_action = C.ST_COLLISION, f"DUPLICATE_TARGET x{len(rs)}", A_NONE
        for r in rs:
            if occupied is not None and occupied is not r and r.status != C.ST_COLLISION:
                r.status = C.ST_COLLISION
                r.planned_action = A_NONE
                r.reason = f"TARGET_OCCUPIED_BY_OTHER_SOURCE: {occupied.source_filename}"
    rows.sort(key=lambda r: (r.source_folder.casefold(), r.sequence_number, r.source_filename.casefold()))
    return rows


def manifest_digest(rows: List[Row]) -> str:
    h = hashlib.sha256()
    for r in sorted(rows, key=lambda x: x.source_path):
        h.update(("\x1f".join(str(getattr(r, c)) for c in DIGEST_COLUMNS) + "\n").encode("utf-8"))
    return h.hexdigest()
