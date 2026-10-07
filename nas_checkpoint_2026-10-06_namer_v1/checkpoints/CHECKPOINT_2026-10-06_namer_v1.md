# Checkpoint 2026-10-06：Namer v1.0（命名 GUI）

分工：轉檔全部交給 Baleen；Namer 只負責命名（原地改名、驗證、紀錄、還原）。
規格：`claude/namer/NAMING_SPEC.md`（N-* 命名規則、S-* 安全、UI-*、AC-* 驗收）。

## 交付
- `Namer-1.0.0.zip`（已傳給 Emily）：`namer/` 套件（純標準庫 + tkinter）、`Start Namer.bat`、README、NAMING_SPEC、tests。
- 啟動：放在 Baleen 資料夾旁，雙擊 `Start Namer.bat`，用 Baleen 內附 Python 3.12（含 tkinter）；找不到時設 `NAMER_PYTHON`。

## 一般化了哪些 04 打羊秀的規則
- 前綴與年份範圍從根資料夾名稱推測（`04 打羊秀(2000-2005)` → `DP2_99_04_`、2000–2005），可改。
- 既有 DP2 命名優先（區分大小寫比對），字母／序號最小未占用，巢狀日期繼承。
- 同父資料夾（非根）的子資料夾既有 code 相同 → 自動合池（古樂器之夜 三弦＋琵琶）。
- 資料夾規則（存在 data/profiles）：指定群組代碼（20022004）、子資料夾共用（混咬表演）、排除。
- 非歸檔格式（doc/avi/eml…）→ 需檢查、不占號（先跑 Baleen）。
- `<stem>_attachments` 資料夾預設跟著信件 PDF 改名，內容不改、不占號。
- Gate：輸入「確認 rename」；執行前重新掃描比對，有變動就中止；改後自動驗證（含冪等）；data/runs 下有 preview / rename_log / name_map（含 Baleen 原始來源路徑）/ summary；可還原。

## 驗證
- 33 個單元／整合測試通過；AC-1：10/05 清單無覆寫時 147 個資料夾的 group 與 v0.2 manifest 完全相同。
- AC-2：重現 session4 決策（混咬表演共用、20022004、200401g 合池、200303 a/b）。
- AC-4/5：暫存目錄實際改名 → 驗證 → 重新規劃 0 筆 → 還原；預覽後資料夾變動會中止。
- GUI 在 Linux Xvfb + Python 3.12/Tk 8.6 跑完整流程 smoke test。
- **尚未在 Windows 上實測**：`Start Namer.bat` 的 Python 搜尋、Z: 網路磁碟上的改名速度、中文路徑下的 tkinter 資料夾對話框。

## 待決（NAMING_SPEC §14）
Q1 附件資料夾預設跟隨改名；Q2 非歸檔格式不占號；Q3 `00` 等非日期 code 出現新檔時接續編號；Q4 前綴基底 `DP2_99_`；Q5 Final Report 範本。

## 04 打羊秀本身
仍依 session4 §6：Phase 6 `-Execute`／`-RemoveAttachments` 輸出待貼、重新匯出清單驗證、Final Report。可用 Namer 的「掃描預覽」當作改名後的獨立驗證：選 04 資料夾即可（既有 code 會自動延續，不必設規則），預期改名 0 筆；殘留的 `_attachments` 資料夾會顯示為需檢查（ATTACHMENT_PARENT_NOT_FOUND），屬預期。

## Project 內的檔案位置（16:10 存檔）
完整原始碼已逐檔存進 Project `claude/namer/`，可直接還原成可執行的工具：

| Project 路徑 | 內容 |
|---|---|
| `claude/namer/NAMING_SPEC.md` | 規格 v1.0 |
| `claude/namer/README.txt` | 使用說明（CRLF） |
| `claude/namer/Start Namer.bat` | Windows 啟動檔（CRLF，內容只用 ASCII） |
| `claude/namer/namer/*.py` | `__init__` `__main__` `rules` `folderdate` `scan` `plan` `execute` `records` `gui` |
| `claude/namer/tests/test_namer.py` | 33 個測試（AC-1～AC-7） |
| `claude/namer/tests/gui_smoke.py` | GUI smoke test（`xvfb-run python3.12 tests/gui_smoke.py <截圖目錄>`） |
| `claude/namer/tests/data/expected_groups_v02.csv` | AC-1 golden：147 個資料夾 → v0.2 group |

未存：`tests/data/listing_20261005.csv`（343 KB），內容同 Project 檔 `nas_checkpoint_2026-10-06_v0.2.zip` 裡的 `input/nas_listing.csv`，還原時複製過去改名即可。

### 新對話還原步驟
1. `project_read` 上表每個檔，依相同相對路徑寫回 `Namer/`（`.bat`、`README.txt` 要 CRLF）。
2. 從 v0.2 zip 取出 `input/nas_listing.csv` → `Namer/tests/data/listing_20261005.csv`。
3. 沙箱要用 `python3.12`（有 tkinter；`apt-get install python3-tk xvfb`），跑 `python3.12 -m unittest discover -s tests`，應為 33 OK。
4. 打包：`zip -r Namer-1.0.0.zip Namer`（排除 `__pycache__`、`data/`）。

## 開發中修正過的問題（給下次參考）
- tkinter：Treeview 先建立、再 grid 到之後建立的 Frame 裡會被 Frame 蓋住 → `tree.lift(f)`。
- 日期 regex 原本擋掉 `20031101` 這種緊接寫法 → 月份後允許緊接兩位數日期。
- N-SIBLING 原本會讓根資料夾底下兩個不相干的資料夾共用既有字母 → 限定父資料夾不是根。
- `project_write` 的 `local_path` 以 `/home/claude` 為基準。

## 下一步
1. Emily 在 Windows 實測：雙擊 `Start Namer.bat` → 選 04 資料夾掃描預覽（預期改名 0 筆）。
2. 回覆 §14 Q1–Q5，必要時改規格再改程式。
3. 04 打羊秀的 Phase 6 收尾與 Final Report（見 session4 §6）。
