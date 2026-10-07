#!/usr/bin/env python3
"""依 rename_conversion_log.csv 反向處理。預設只列出會做什麼（dry-run）；除非你明確要求，不要執行。
  python rollback.py --log out\\rename_conversion_log.csv                 # 預覽
  python rollback.py --log out\\rename_conversion_log.csv --execute-rollback
原則：不覆蓋、不刪除。
  COPIED            → 驗證 hash 一致後，將新檔『移到』每個檔案旁的 _ROLLBACK_QUARANTINE（不刪除）
  RENAMED_IN_PLACE  → 原路徑沒被佔用時，改回原檔名（例：.jpg → .JPG）
  ARCHIVED          → 先把 _ORIGINALS 內的原檔搬回原路徑（原路徑仍空時），再把新檔 hash 驗證後移到 quarantine
任一項目的來源/目的地狀態不符 → 跳過該項並記錄。"""
import argparse
import csv
import os
import sys

from nas_rename.executor import sha256


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", required=True)
    ap.add_argument("--execute-rollback", action="store_true")
    ap.add_argument("--quarantine", help="預設：每個檔案旁的 _ROLLBACK_QUARANTINE 子資料夾")
    a = ap.parse_args(argv)
    rows = list(csv.DictReader(open(a.log, encoding="utf-8-sig", newline="")))
    done = skipped = 0
    for r in reversed(rows):                 # 後做的先還原
        st, src, tgt = r["rename_status"], r["source_path"], r["target_path"]
        acts = []
        if st == "ARCHIVED":
            arch = r["archived_path"]
            if os.path.exists(arch) and not os.path.exists(src):
                acts.append(("move", arch, src))
            else:
                print(f"SKIP archived original (state mismatch): {src}")
        elif st == "RENAMED_IN_PLACE":
            if os.path.exists(tgt) and (not os.path.exists(src) or src.casefold() == tgt.casefold()):
                acts.append(("move", tgt, src))       # 注意：Windows 不分大小寫，需兩段式
            else:
                print(f"SKIP case-rename (state mismatch): {src}")
        if st in ("COPIED", "ARCHIVED", "COPIED_ARCHIVE_FAILED"):
            if os.path.exists(tgt) and r["target_sha256"] and sha256(tgt) == r["target_sha256"]:
                qdir = a.quarantine or os.path.join(os.path.dirname(tgt), "_ROLLBACK_QUARANTINE")
                dest = os.path.join(qdir, os.path.basename(tgt))
                if os.path.exists(dest):
                    print(f"SKIP quarantine target exists: {dest}")
                else:
                    acts.append(("quarantine", tgt, dest))
            else:
                print(f"SKIP new file missing or modified since conversion: {tgt}")
        if not acts:
            skipped += 1
            continue
        for kind, s, d in acts:
            print(f"{'DO ' if a.execute_rollback else 'PLAN'} {kind}: {s} -> {d}")
            if a.execute_rollback:
                os.makedirs(os.path.dirname(d), exist_ok=True)
                if kind == "move" and s.casefold() == d.casefold():
                    os.rename(s, s + ".rbtmp")
                    s = s + ".rbtmp"
                os.rename(s, d)
            done += 1
    print(f"{'executed' if a.execute_rollback else 'planned'}={done} skipped={skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
