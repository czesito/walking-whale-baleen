# Checkpoint 2026-10-06（session 5：打包存檔）

專案：批次轉檔、命名。本 session 只做打包，沒有新增 NAS 作業，也沒有動 Z: 上任何檔案。

## 1. 目前狀態

| 項目 | 狀態 |
|---|---|
| Namer v1.0（命名 GUI／CLI） | 完成，33 個測試通過（含 AC-1：147 個資料夾與 v0.2 golden 一致） |
| Namer 在 Windows 實測 | **尚未做**（`Start Namer.bat` 的 Python 搜尋、Z: 上的改名速度、中文路徑的資料夾對話框） |
| 04 打羊秀 Phase 5 改名 | 完成，678/678，已用重新匯出的清單獨立驗證 |
| 04 打羊秀 Phase 6 合併 PDF/A | 5 個合併檔製作完成，veraPDF 1.30.2 全數 PASS；`-Execute` Emily 回報完成但**沒有貼輸出，未驗證**；`-RemoveAttachments` **未確認是否執行** |
| Final Report | 尚未產生 |
| NAMING_SPEC §14 Q1–Q5 | 待 Emily 回覆 |

## 2. 本 session 做的事

- 把 Project 內的 Namer 原始碼、規格、測試、四份既有 checkpoint、`tools/` 腳本還原成可執行的目錄結構，連同 v0.2 舊版工具一起打包。
- 還原時發現測試資料 `expected_groups_v02.csv` 有兩個資料夾名稱結尾帶私用字元 U+F028（`2002 01 05 DJ Davis (巫國華)` 與 `2003 11`）。原檔在 Project 內顯示時這個字元不可見，重建時遺失，導致 AC-1 失敗。已補回，33 個測試全過。實際 NAS 上這兩個資料夾名稱結尾確有此字元，PowerShell 必須用 `-LiteralPath`。

## 3. Zip 內容

```
CHECKPOINT.md                      本檔
Namer/                             Namer v1.0 完整可執行套件（namer/、tests/、Start Namer.bat、README.txt、NAMING_SPEC.md）
  tests/data/listing_20261005.csv  10/05 NAS 清單（AC-1 用）
tools/                             plan_rename.py、merge_pdfa.py、phase5_rename.ps1、phase6_apply.ps1、phase6_map.csv
checkpoints/                       session2、session3、session4、namer_v1 四份 checkpoint
legacy_v0.2/                       v0.2 zip 解開後的內容（含 nas_rename_tool，plan_rename.py 依賴其 folderparse）
```

不在 zip 內（仍在 Project）：Baleen 執行環境與第三方授權檔、`docs/baleen-spec.html`、`claude/SOURCE_BUNDLE.md`、`claude/RENAME_SPEC.md`、`claude/CHECKPOINT.md`、兩個原始上傳 zip（`nas_checkpoint_2026-10-05.zip`、`nas_checkpoint_2026-10-06_v0.2.zip`）。

## 4. 還原與測試

1. 解壓縮 zip。
2. `cd Namer` 後執行 `python -m unittest discover -s tests`，應為 33 OK（GUI smoke test 需 tkinter 與 Xvfb，本 session 未跑）。
3. Windows：Namer 資料夾放在 Baleen 旁，雙擊 `Start Namer.bat`；找不到 Python 時設環境變數 `NAMER_PYTHON`。
4. 注意：`.ps1`、`.csv`、`.bat`、`README.txt` 已還原為 CRLF（`.ps1`、`.csv` 另帶 BOM），編輯時請保留。

## 5. 下一步

1. 請 Emily 貼 Phase 6 `-Execute` 的輸出，並確認是否已跑 `-RemoveAttachments`。
2. 重新匯出清單（`nas_listing_final.csv`）：5 個合併 PDF 的 sha256 對照 `phase6_map.csv`；檔案總數 1680（未刪附件）或 1672（已刪，4 個 `_attachments` 資料夾消失，謎樣那個只剩 Thumbs.db）。
3. 在 Windows 上用 Namer「掃描預覽」選 04 資料夾，預期改名 0 筆；殘留 `_attachments` 會標為 ATTACHMENT_PARENT_NOT_FOUND。
4. 抽驗 04 內其餘 PDF 的 PDF/A，產生 Final Report（session4 §6 第 4 點列出要寫入的項目）。
5. 回覆 NAMING_SPEC §14 Q1–Q5。
6. 清理 `Downloads\phase6\backup\`，確認保留期限。
