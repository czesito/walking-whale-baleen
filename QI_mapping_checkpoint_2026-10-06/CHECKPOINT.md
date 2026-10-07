# Checkpoint：打羊秀 → QI Inventory 自動填表

存檔時間：2026-10-06 17:xx（UTC+8）｜對應 SPEC v0.4

## 目前狀態

打羊秀 profile 已實作，對真資料夾跑過 dry-run：1,670 個媒體檔 → 1,670 列，欄 0–15 無空值，例外 1 筆（Thumbs.db）。Q5 確認系列碼 04，Q7 確認產草稿。

## 這個包裡有什麼

| 路徑 | 內容 |
|---|---|
| `docs/SPEC_打羊秀_QI_mapping.md` | 完整規格 v0.4（§12 為 dry-run 結果與待決事項） |
| `code/qi_mapper.py` | 共用引擎＋打羊秀 profile（v0.4 draft） |
| `data/folder_map_打羊秀.csv` | QI 節點對照表，44 個節點 |
| `data/豆皮_Event工作表1.csv` | 豆皮活動登記表（189 列） |

## 這個包裡沒有的東西

以下檔案存在於先前執行 dry-run 的環境，目前的工作環境讀不到，所以不在 zip 內：

- `打羊秀_QI_dryrun.xlsx`／`.csv`（dry-run 輸出）
- `yangxiu_entries.json`（資料夾列表）、`probe.json`（PDF 頁數、mp4 時長、PDF 文字）、`events_raw.csv`
- `QI_Inventory_豆皮_打羊秀Records.csv`（79 欄範本，`qi_mapper.py` 讀取表頭與常數欄用）

重跑 dry-run 需要重新連接打羊秀資料夾、重新掃描與 probe，並重新上傳範本 CSV。`qi_mapper.py` 的 `__main__` 路徑（`/home/claude/qi/`、`/mnt/user-data/uploads/`）要照新環境調整。

## 待你決定（SPEC §12.4）

1. 43 個 `cross_series` 資料夾（2003 年底至 2004 年 1 月為打羊秀＋豬頭劇合辦，2004 年 2 月以後多為混咬）：留在打羊秀、改歸豬頭劇（34193），或另建混咬節點。
2. `2004 01 混咬表演`（224 張）的資料夾名是活動名，標題草稿不適用，需手動處理。
3. 英文標題裡的中文表演者名未翻譯或拼音，需統一譯名。
4. 照片內容尚未判讀，Content Type 與描述只依檔案類型。
5. 轉檔註記需要 rename／轉檔工具的 log。

## Event 表已知問題

年份誤植（`27/1/2204`、`19/11/2204`、`13/6/2024`、`26/6/2026`、`4/3/207`）、日期格式異常（`11/082008`、`28/09/2008`、`見以下四列`）、2003 年多日期單列需拆開、部分列無名稱或參展者、2000 年完全沒有資料。

## 下一步

1. 處理上面五項待決事項。
2. 補照片內容判讀（挑選要做的資料夾）。
3. 修正 Event 表後重跑 dry-run，核對 `Review_ByFolder` 的 Reviewed 欄。
4. 通過驗收條件（SPEC §5）後，貼回 `QI Inventory_豆皮_打羊秀Records`。
