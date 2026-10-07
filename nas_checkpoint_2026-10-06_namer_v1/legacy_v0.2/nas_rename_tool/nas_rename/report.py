import os
from collections import Counter, defaultdict
from typing import Dict, List, Optional

from . import config as C
from .planner import Row


def apply_tool_availability(rows: List[Row], tools: Dict[str, Optional[str]], classify):
    """實機 dry-run 才呼叫：缺工具的項目標為 CONVERSION_REQUIRED，並記錄缺少的工具。"""
    from .tools import missing_for, pdfa_only_missing
    for r in rows:
        if r.status in (C.ST_PLANNED, C.ST_CONV_PLANNED, C.ST_ALREADY):
            kind = classify(r.source_extension)[3]
            miss = missing_for(kind, tools)
            if not miss:
                continue
            r.note = (r.note + "; " if r.note else "") + "missing tools: " + ",".join(miss)
            if pdfa_only_missing(kind, tools):
                # 轉檔鏈完整，只缺 veraPDF：可以轉，但不得宣稱 PDF/A 已驗證
                r.status = C.ST_PDFA_NOT_VALIDATED
                r.reason = "veraPDF not found: output PDF/A cannot be verified; stays in staging until tool confirmed"
            elif kind not in ("img_copy",):
                r.status = C.ST_CONV_REQUIRED
                r.reason = "missing tools: " + ",".join(miss)


def build_report(rows: List[Row], root: str, mode_note: str, tools: Optional[Dict] = None) -> str:
    L: List[str] = []
    add = L.append
    st = Counter(r.status for r in rows)
    folders = {r.source_folder for r in rows}
    ym = defaultdict(set)
    for r in rows:
        if r.parsed_yyyymm and r.folder_letter:
            ym[r.parsed_yyyymm].add(r.source_folder)
    add("DRY RUN REPORT（只讀，未修改任何檔案）")
    add(f"Root   : {root}")
    add(f"Source : {mode_note}")
    add("")
    add("== 摘要 ==")
    add(f"Total source files           : {len(rows)}")
    add(f"Folders scanned              : {len(folders)}")
    add(f"YYYYMM groups                : {len(ym)}")
    valid = st[C.ST_PLANNED] + st[C.ST_CONV_PLANNED] + st[C.ST_ALREADY]
    add(f"Valid (plan complete)        : {valid}   (PLANNED {st[C.ST_PLANNED]} + CONVERSION_PLANNED {st[C.ST_CONV_PLANNED]} + ALREADY_VALID {st[C.ST_ALREADY]})")
    add(f"Conversion required (format) : {sum(1 for r in rows if r.conversion_required == 'yes')}")
    add(f"Maybe (verify content)       : {sum(1 for r in rows if r.conversion_required == 'verify')}")
    add(f"Already valid                : {st[C.ST_ALREADY]}")
    add(f"Review required              : {st[C.ST_REVIEW]}")
    add(f"PDF/A not validated (no tool): {st[C.ST_PDFA_NOT_VALIDATED]}")
    add(f"Conversion unsupported       : {st[C.ST_UNSUPPORTED]}")
    add(f"Conversion required (tools)  : {st[C.ST_CONV_REQUIRED]}")
    add(f"Filename collision           : {st[C.ST_COLLISION]}")
    add(f"Unreadable                   : {st[C.ST_UNREADABLE]}")
    add("")
    add("== 副檔名 ==")
    for e, n in Counter(r.source_extension for r in rows).most_common():
        tgt = Counter(r.target_extension for r in rows if r.source_extension == e and r.target_extension)
        add(f"{e or '(none)':8} {n:5}  → {dict(tgt) if tgt else '-'}")
    add("")
    add("== 每個 YYYYMM 的資料夾數（a, b, c… 依序）==")
    for k in sorted(ym):
        add(f"{k}: {len(ym[k])} folders")
    add("")
    add("== 命名來源 ==")
    no = Counter(r.name_origin or "(none)" for r in rows)
    for k, n in no.most_common():
        add(f"{k:18} {n}")
    changed = [r for r in rows if r.name_origin == "EXISTING_DP2" and r.target_filename
               and os.path.splitext(r.source_filename)[0] != os.path.splitext(r.target_filename)[0]]
    add(f"既有 DP2 檔名的 letter/## 被改動的檔案數 : {len(changed)}   (必須為 0)")
    add("")
    add("== 計畫動作 ==")
    for k, n in Counter(r.planned_action for r in rows).most_common():
        add(f"{k:24} {n}")
    add("")
    inh = defaultdict(set)
    for r in rows:
        if r.date_source_folder and r.date_source_folder != r.source_folder:
            inh[r.source_folder].add((r.date_source_folder, r.group_id))
    add(f"== 巢狀子資料夾（日期繼承）: {len(inh)} 個資料夾 ==")
    for sf in sorted(inh, key=str.casefold):
        for dsf, gid in sorted(inh[sf]):
            add(f"{gid or '-':9} {sf[len(root):].lstrip(chr(92)+'/')}   ← date_source: {dsf[len(root):].lstrip(chr(92)+'/')}")
    add("")
    add("== EML ==")
    emls = [r for r in rows if r.source_extension.lower() == ".eml"]
    add(f"EML 檔數 {len(emls)}；attachment_detected: " + ", ".join(f"{k}={v}" for k, v in Counter(r.attachment_detected for r in emls).items()))
    add("")
    add("== 狀態與原因 ==")
    why = Counter((r.status, r.reason.split(";")[0].split(":")[0].split("(")[0].strip()) for r in rows)
    for (s, w), n in sorted(why.items()):
        add(f"{s:24} {w:42} {n}")
    if tools is not None:
        add("")
        add("== 工具偵測 ==")
        for k, v in tools.items():
            add(f"{k:10} {v or 'NOT FOUND'}")
    add("")
    add("== REVIEW_REQUIRED / COLLISION / UNSUPPORTED / UNREADABLE 清單 ==")
    bad = [r for r in rows if r.status in (C.ST_REVIEW, C.ST_COLLISION, C.ST_UNSUPPORTED, C.ST_UNREADABLE, C.ST_CONV_REQUIRED)]
    for r in sorted(bad, key=lambda x: (x.status, x.source_path.casefold())):
        rel = r.source_path[len(root):].lstrip("\\/")
        add(f"[{r.status}] {rel}  ← {r.reason}")
    return "\n".join(L) + "\n"
