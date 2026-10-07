# Checkpoint — NAS 打羊秀 重新命名＋轉檔（2026-10-06，v0.2）

## 狀態
- 只有 plan，尚未對 NAS rename／convert 任何檔案。
- RENAME_SPEC.md v0.2 已納入 2026-10-05 的 12 項決策；27 個單元測試通過；沙箱假目錄跑過完整 execute + rollback。
- 尚未在真實檔案上驗證；尚未用 veraPDF 判定過任何 PDF；HTML/EML（Chromium+Ghostscript）路徑未實測。

## 實機環境檢查（2026-10-05，DESKTOP-0PVPOJ9）
- FOUND：Chrome 154、PowerShell 5.1；NAS 路徑存在、可讀（頂層 138 個資料夾，可開檔）。
- NOT_FOUND：Python（只有 Microsoft Store 假 python.exe）、Pillow、FFmpeg、FFprobe、LibreOffice、Ghostscript、veraPDF、Java、pdftotext。
- environment_check.ps1 已修正：Store 假 python.exe 現在報 NOT_FOUND。

## 下一步
1. 安裝 Python 3.10+（再 pip install pillow）、FFmpeg、LibreOffice、Ghostscript、Java、veraPDF（verapdf.org）。
2. 重跑 environment_check.ps1 → 實機 dry-run（取得 EML 附件數、工具狀態、可核准的 digest）。
3. 確認 RENAME_SPEC §12 E1–E5（EML 有附件的處理、無 veraPDF 時的輸出、字母缺號、145 個 .JPG in-place 改名、核准 digest）。

## 內容
- nas_rename_tool/   程式、測試、RENAME_SPEC.md、environment_check.ps1、out/（manifest、sha256、dry_run_report、environment_report 占位）
- input/nas_listing.csv   1,670 筆檔案清單，可重現同一份 plan

## 重現（離線 plan）
  cd nas_rename_tool
  python rename_convert.py --root "\\Aaa_nas\iast\18_豆皮藝文咖啡廳第二期\數位檔案_整理\04 打羊秀(2000-2005)" --listing ../input/nas_listing.csv --out-dir out
  python -m unittest discover -s tests
