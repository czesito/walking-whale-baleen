#!/usr/bin/env python3
"""NAS 數位檔案批次重新命名＋格式轉換。預設 --dry-run（只讀）。

  dry-run（預設）：  python rename_convert.py --root "\\\\Aaa_nas\\...\\04 打羊秀(2000-2005)" --out-dir C:\\work\\out
  離線 plan：        python rename_convert.py --root "<同上>" --listing nas_listing.csv --out-dir out
  正式執行：         python rename_convert.py --root ... --execute --approved-digest out\\rename_conversion_manifest.sha256 --out-dir out
  最終驗證：         python rename_convert.py --root ... --verify --out-dir out
"""
import argparse
import os
import sys

from nas_rename import SPEC_VERSION
from nas_rename.config import STAGING_DIRNAME, ARCHIVE_DIRNAME
from nas_rename.manifest import write_manifest
from nas_rename.eml_info import enrich_eml
from nas_rename.planner import classify, manifest_digest, plan
from nas_rename.report import apply_tool_availability, build_report
from nas_rename.scanner import scan_fs, scan_listing
from nas_rename.tools import detect


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True)
    ap.add_argument("--out-dir", default="out", help="manifest/report/log 的輸出位置（不可在 root 底下）")
    ap.add_argument("--listing", help="使用 PowerShell 匯出的清單 CSV，完全不碰檔案系統")
    ap.add_argument("--dry-run", action="store_true", help="預設行為，列出僅為明確")
    ap.add_argument("--execute", action="store_true", help="正式處理；需 --approved-digest")
    ap.add_argument("--verify", action="store_true", help="完成後的最終驗證（只讀）")
    ap.add_argument("--approved-digest", help="dry-run 產生並經你確認的 rename_conversion_manifest.sha256")
    ap.add_argument("--staging", default=os.path.join(os.getcwd(), STAGING_DIRNAME), help="暫存轉檔目錄（建議本機磁碟）")
    ap.add_argument("--originals", choices=["archive", "keep"], default="archive",
                    help="archive=輸出驗證成功後把原檔『搬移』到 <root>\\_ORIGINALS（預設，不刪除）；keep=原檔留在原處（僅供測試）")
    ap.add_argument("--no-readable-check", action="store_true")
    a = ap.parse_args(argv)

    if a.execute and a.dry_run:
        ap.error("--execute 與 --dry-run 不可同時使用")
    root = a.root
    out = os.path.abspath(a.out_dir)
    if os.path.abspath(out).casefold().startswith(os.path.abspath(root).casefold()) and not a.listing:
        ap.error("--out-dir 不可位於 --root 底下")
    os.makedirs(out, exist_ok=True)

    if a.verify:
        from nas_rename.executor import verify_tree
        return verify_tree(root, out)

    if a.execute:
        if not a.approved_digest:
            ap.error("--execute 需要 --approved-digest")
        if a.listing:
            ap.error("--execute 不可搭配 --listing")
        from nas_rename.executor import run_execute
        return run_execute(root, a.approved_digest, out, a.staging, a.originals, None)

    # ---- dry-run（預設）：只讀 ----
    if a.listing:
        files = scan_listing(a.listing, root)
        note = f"listing CSV ({os.path.basename(a.listing)}); 檔案系統未被存取，工具可用性/內容未檢查"
        tools = None
    else:
        files = scan_fs(root, check_readable=not a.no_readable_check)
        tools = detect()
        note = "live filesystem (read-only)"
    rows = plan(files, root)
    if not a.listing:
        enrich_eml(rows)                    # 唯讀開啟 EML 偵測附件；listing 模式不碰檔案，attachment_detected=UNKNOWN
    digest = manifest_digest(rows)          # digest 與機器上有哪些工具無關（工具缺失只影響報告/狀態標註）
    if tools is not None:
        apply_tool_availability(rows, tools, classify)
    write_manifest(rows, os.path.join(out, "rename_conversion_manifest.csv"))
    with open(os.path.join(out, "rename_conversion_manifest.sha256"), "w", encoding="utf-8") as f:
        f.write(digest + "  rename_conversion_manifest.csv\n")
    with open(os.path.join(out, "dry_run_report.txt"), "w", encoding="utf-8") as f:
        f.write(build_report(rows, root, note, tools))
    print(f"[dry-run] spec {SPEC_VERSION}: {len(rows)} files planned → {out}  (未修改任何檔案)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
