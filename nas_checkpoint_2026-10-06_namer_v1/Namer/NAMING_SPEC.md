# NAMING_SPEC — 檔案命名工具（Namer）規格 v1.0

日期：2026-10-06
狀態：草案，待 Emily 確認 §14 的待決事項
適用：Baleen 轉檔完成之後的「命名」階段。轉檔、格式驗證、PDF/A 驗證一律由 Baleen 負責，本工具不轉檔、不重新編碼、不刪除任何檔案。

規則 ID（`N-`命名、`S-`安全、`UI-`介面、`AC-`驗收）是穩定編號，程式碼與測試以這些 ID 對照。

---

## 0. 來源與取代關係

本規格把 `04 打羊秀(2000-2005)` 的實際作業一般化，來源：

| 來源 | 採用的內容 |
|---|---|
| `RENAME_SPEC.md` v0.2 §2–§4 | 檔名格式、既有 DP2 優先、日期解析、巢狀繼承、字母／序號「最小未占用」、決定性排序 |
| session3 §4.1 | 不用 `_ORIGINALS`、不複製、只原地改名；影音 `.mp4`；`.JPG → .jpg` |
| session4 §2–§3 | 同父資料夾的子資料夾可共用字母；混咬表演共用 `200401h`、序號跨子資料夾連續；`2002   -   2004` 指定 `20022004` 無字母；`Thumbs.db` 與附件不占號 |
| session4 §7 | 改名前重新掃描比對（清單會過期）；不要讓使用者貼 PowerShell；輸出第一行要有筆數 |
| Baleen spec §5.2、§6.5、§7 | 系統檔清單；`<信件 stem>_attachments/` 結構；Baleen 報告位置 `_baleen/` |

RENAME_SPEC v0.2 的轉檔、staging、`_ORIGINALS`、veraPDF、manifest digest、`--execute` 流程全部不適用（轉檔已移交 Baleen）。

## 1. 範圍

做：掃描一個根資料夾 → 依規則產生完整改名預覽 → 使用者明確確認 → 原地改名 → 改名後驗證 → 寫紀錄，可依紀錄還原。

不做：轉檔、驗證檔案內容、刪除檔案、搬移檔案到別的資料夾、合併附件到 PDF、改資料夾名稱（唯一例外是 N-ATT 的附件資料夾跟隨改名）。

## 2. 安全原則

| ID | 規則 |
|---|---|
| S-1 | 預覽（掃描＋規劃）完全唯讀，不寫入根資料夾任何東西。 |
| S-2 | 不覆蓋：目標名稱（Windows 不分大小寫）已被占用 → 該列 `REVIEW`，不改。 |
| S-3 | 不猜：無法判定的一律 `REVIEW` 並寫 reason，不占字母。 |
| S-4 | 執行前必須通過 Gate（UI-G）：使用者輸入「確認 rename」。 |
| S-5 | 執行前重新掃描並重新規劃，與使用者核准的預覽逐列比對（資料夾、舊名、新名、大小）。任何差異 → 中止，一個檔案都不改，要求重新預覽。 |
| S-6 | 逐檔改名前再確認：來源存在且大小一致、目標不存在。不符 → 停止整個執行（不跳過繼續），已改的保留並記錄。 |
| S-7 | 每改一個檔立即寫入 log（append + flush + fsync）。 |
| S-8 | 純大小寫改名（`.JPG → .jpg`）用兩段式：先改成暫存名，再改成目標名；第二步失敗則改回。 |
| S-9 | 紀錄（rules、preview、log、報告）寫在工具自己的 `data/runs/<run-id>/`，不寫進根資料夾。 |
| S-10 | 還原只依 log：目前名稱存在、原名稱空著才改回；否則跳過並列出。 |
| S-11 | 程式中沒有任何刪除檔案的程式碼路徑（暫存名在 S-8 失敗時改回，不刪）。 |

## 3. 掃描（N-SCAN）

- 遞迴掃描根資料夾，不跟隨捷徑／symlink。排序用 NFC 正規化 + casefold，與檔案系統回傳順序無關。
- **忽略**（`SKIP`，原因 `SYSTEM_FILE`，不改名、不占號）：`.DS_Store`、`._*`、`Thumbs.db`、`ehthumbs.db`、`desktop.ini`、`~$*`、`.~lock.*#`、`Icon\r`。
- **不進入**的資料夾：`$RECYCLE.BIN`、`.Trashes`、`.Spotlight-V100`、`.fseventsd`、`@eaDir`、`#recycle`、`.@__thumb`、`_baleen`、`.baleen-staging`。
- 直接放在根資料夾底下的檔案 → `REVIEW`，`FILE_DIRECTLY_UNDER_ROOT`。

## 4. 檔名格式（N-NAME）

```
<prefix><group>_<seq><ext>
例：DP2_99_04_200311a_01.jpg　DP2_99_04_20022004_07.jpg　DP2_99_04_00_03.pdf
```

| 部分 | 規則 |
|---|---|
| `prefix` | 每個根資料夾一個，例 `DP2_99_04_`。必須符合 `^[A-Za-z0-9]+(_[A-Za-z0-9]+)*_$`。預設值：`DP2_99_` + 根資料夾名稱開頭的數字（`04 打羊秀…` → `DP2_99_04_`），使用者可改。 |
| `group` | 一般為 `YYYYMM` + 字母（N-LETTER）；也可以是使用者指定或既有的群組代碼（`00`、`20022004`），規則 `[0-9A-Za-z]+`。 |
| `seq` | 至少兩位數，99 之後 100、101…（N-SEQ）。 |
| `ext` | 小寫；`.jpeg → .jpg`、`.tiff → .tif`。 |

### N-ARCH 可命名的格式

只有歸檔格式會分配序號：`.jpg .jpeg .tif .tiff .pdf .mp4 .m4a`（設定可改）。其他副檔名（`.doc .avi .eml .bmp…`）→ `REVIEW`，`NOT_ARCHIVAL_FORMAT`（請先用 Baleen 轉檔），**不占序號**；轉檔後的輸出再跑一次本工具時，會取得最小未占用的號碼。

### N-EXIST 既有命名優先

stem 符合 `^<prefix>(<code>)_(\d{2,})$`（區分大小寫）的檔案是既有編目結果：

- stem 永遠不變，只允許副檔名正規化（`DP2_…_02.JPG → DP2_…_02.jpg`）。
- 它的 `code` 與 `seq` 視為已占用。
- 只有一個字母結尾的 `YYYYMM` code 會占用該月份的字母。
- `code = 00` 與其他非日期 code 不占任何月份的字母。

## 5. 日期解析（N-DATE）

從**資料夾名稱**開頭解析，不看 metadata：

```
YYYY [sep] MM [sep DD[-DD]]，sep 為空白、點、底線、連字號（可 0 個或多個）
2003 11 01 DJ阿義      → 200311，日 01
2001 09賀香滋          → 200109，無日
2004 06 12電音22       → 200406，日 12
2002 03 29-31 Wake Up  → 200203，日 29（範圍 29–31）
20031101 xxx           → 200311，日 01
2002   -   2004        → 無法解析
```

- 年份必須在年份範圍內。預設從根資料夾名稱的 `(YYYY-YYYY)` 取得，沒有就是 1900–2099，可改。
- 失敗代碼：`NO_DATE_PREFIX`、`YEAR_OUT_OF_RANGE`、`BAD_MONTH`、`BAD_DAY`、`BAD_DAY_RANGE`。

### N-INHERIT 巢狀繼承

資料夾自己解析不出日期時，沿用最近一個可解析的祖先。`2004 01 混咬表演\dj` → 200401，日期來源 `2004 01 混咬表演`。都找不到 → `REVIEW`，`DATE_NOT_DETERMINABLE`。

## 6. 命名單位與分配池（N-UNIT、N-POOL）

- **單位**：每個直接含有檔案的資料夾（`_attachments` 資料夾除外，見 N-ATT）。
- **分配池**：共用一個 group 與一條序號序列的單位集合。預設一個單位一個池。以下情況會合併：
  - **N-SHARE（使用者指定）**：在資料夾 P 設定「子資料夾共用一個群組」→ P 本身與其底下所有單位合成一個池。
  - **N-SIBLING（既有命名推得）**：多個單位的既有 code 相同，且它們的父資料夾相同、而且不是根資料夾 → 合併成一個池（例：`古樂器之夜\三弦`、`琵琶` 都是 `200401g`）。父資料夾不同，或都直接在根資料夾底下 → 這些單位全部 `REVIEW`，`CODE_SHARED_BY_UNRELATED_FOLDERS`。
  - 使用者指定的代碼（N-OVERRIDE）與其他池相同時，依同一條規則處理。

### N-GROUP 每個池的 group

依序判斷：

1. 使用者設定「排除」→ 全部 `SKIP`，`EXCLUDED_BY_USER`。
2. 使用者指定代碼 → 用該代碼。
3. 池內有既有 code：
   - 兩種以上 → 全池 `REVIEW`，`EXISTING_MULTIPLE_CODES`。
   - 一種：若是 `YYYYMM` + 字母、且池的日期可解析但月份不同 → `REVIEW`，`EXISTING_CODE_DATE_MISMATCH`；否則沿用（新檔延續編號）。
4. 池的日期可解析 → 分配新字母（N-LETTER）。
5. 否則 → `REVIEW`，`DATE_NOT_DETERMINABLE`。

### N-LETTER 新字母

- 每個 YYYYMM：先把所有既有與指定代碼的字母標為已占用（含 `REVIEW` 池的，避免日後撞號）。
- 需要新字母的池依下列順序排序，各取**最小未占用**字母：日期（日小的在前；無日的排在同月有日之後）→ 名稱（NFC + casefold；繼承日期的單位用相對路徑）→ 完整相對路徑。
- 超過 `z` → `REVIEW`，`LETTER_OVERFLOW`。

### N-SEQ 序號

- 池內既有檔案保留自己的號碼。
- 其他可命名的檔案依「單位相對路徑（NFC + casefold）→ 檔名 stem（NFC + casefold）→ 副檔名 → 原字串」排序，各取最小未使用號碼。
- 同池兩個既有檔案號碼相同 → 這兩個 `REVIEW`，`DUPLICATE_EXISTING_SEQ`。
- 號碼在最終檢查（N-CHECK）之前分配：之後因路徑過長等原因變成 `REVIEW` 的檔案仍占號，處理完不影響其他檔案編號。

## 7. 附件資料夾（N-ATT）

Baleen 轉 `.eml` 時會產生 `<信件 stem>_attachments/` 與 `<信件 stem>.pdf` 並排。

- `_attachments` 資料夾及其所有內容不是命名單位：裡面的檔案 `SKIP`，`ATTACHMENT_CONTENT`，不改名、不占號。
- 設定 `attachments = follow`（預設）：同資料夾內找到 `<stem>.pdf`，它的新名稱是 `X.pdf` → 附件資料夾改名為 `X_attachments`。信件 PDF 不改名 → 附件資料夾也不動。找不到 `<stem>.pdf` → 附件資料夾 `REVIEW`，`ATTACHMENT_PARENT_NOT_FOUND`。
- 設定 `attachments = keep`：附件資料夾保持原名（`SKIP`）。
- 合併附件到信件 PDF（04 的 Phase 6）不在本工具範圍。

## 8. 使用者覆寫（N-OVERRIDE）

以資料夾相對路徑為鍵，存在規則檔：

| 類型 | 效果 | 04 的例子 |
|---|---|---|
| `auto` | 預設，依 N-GROUP | — |
| `code: <代碼>` | 這個單位（或池）使用指定 group，不分配字母 | `2002   -   2004` → `20022004` |
| `share` | 本資料夾與所有子資料夾合成一個池（N-SHARE） | `2004 01 混咬表演` → 全部 `200401h` |
| `exclude` | 不處理 | — |

`share` 可以和 `code` 同時用：整個池用指定代碼。

規則檔（JSON）內容：`prefix`、`year_from`、`year_to`、`attachments`、`archival_exts`、`overrides`。每個根資料夾一份，存在 `data/profiles/`，下次選同一個資料夾自動載入；每次執行另存一份到 run 紀錄。

## 9. 最終檢查（N-CHECK）

對每個 `RENAME` 列：

- 目標與同資料夾其他列的目標相同（不分大小寫）→ 雙方 `REVIEW`，`DUPLICATE_TARGET`。
- 目標與同資料夾中「不會被改名」的檔案或資料夾同名 → `REVIEW`，`TARGET_OCCUPIED`。
- 目標與「會被改名」的另一個來源同名（改名鏈）→ `REVIEW`，`TARGET_IS_ANOTHER_SOURCE`。
- 完整路徑 ≥ 250 字元 → `REVIEW`，`PATH_TOO_LONG`。
- 新名稱與舊名稱完全相同 → `KEEP`。

## 10. 動作與原因

| 動作 | 意義 | 執行時 |
|---|---|---|
| `RENAME` | 會改成新名稱 | 改名 |
| `KEEP` | 已經是正確名稱 | 不動 |
| `REVIEW` | 需要人決定，reason 說明 | 不動 |
| `SKIP` | 系統檔、附件內容、使用者排除 | 不動 |

每列另有 `group`、`seq`、`origin`（`EXISTING` / `ASSIGNED` / `OVERRIDE`）、`date_source`、`note`。

## 11. 執行、驗證、還原

**執行（N-EXEC）**：S-5 比對 → 先改檔案，再改附件資料夾 → 逐檔 S-6、S-7、S-8。

**驗證（N-VERIFY）**，執行後自動跑，也可單獨跑：
1. 每個已改名的新名稱存在且大小不變；舊名稱不存在。
2. 根資料夾的檔案總數（不含 N-SCAN 的系統檔，因為 Explorer 會自動產生 `Thumbs.db`）與執行前相同。
3. 用同一份規則重新規劃，`RENAME` 應為 0（冪等）。

**還原（N-ROLLBACK）**：依 log 反向逐列改回（S-10），先資料夾後檔案。

## 12. 紀錄（N-LOG）

`data/runs/<YYYYMMDD-HHMMSS>/`：

| 檔案 | 內容 |
|---|---|
| `rules.json` | 本次使用的規則（含覆寫） |
| `preview.csv` | 核准的完整預覽（UTF-8 BOM，Excel 可直接開） |
| `rename_log.csv` | 每次改名一列：時間、資料夾、舊名、新名、大小、狀態 |
| `name_map.csv` | 執行後所有檔案的最終名稱與舊名稱；若根資料夾有 `_baleen/report-*.csv`，加上 Baleen 的原始來源路徑（原始檔名 → 轉檔輸出 → DP2 名稱的出處鏈） |
| `summary.txt` | 筆數、驗證結果、`REVIEW` 清單 |

## 13. 介面（UI）

Windows 用 Baleen 內附的 Python 3.12（含 tkinter）啟動，免安裝。

| ID | 需求 |
|---|---|
| UI-1 | 選擇根資料夾（貼路徑或瀏覽）。自動帶入前綴與年份範圍建議；同一資料夾有存過的規則就載入。 |
| UI-2 | 「掃描預覽」在背景執行，期間按鈕停用、顯示進度。 |
| UI-3 | 左側：命名單位清單（資料夾、group、檔數、改名數、REVIEW 數、覆寫）。右側：檔案清單（資料夾、目前名稱、新名稱、動作、原因）。可依動作篩選、文字搜尋。點左側單位 → 右側只顯示該單位。 |
| UI-4 | 選一個資料夾 →「資料夾規則…」設定 N-OVERRIDE；改完自動重新規劃（不用重新掃描）。 |
| UI-5 | 摘要列：各動作筆數。有 `REVIEW` 時明確顯示「REVIEW 的檔案不會被改」。 |
| UI-6 | 匯出預覽 CSV。 |
| UI-G | 「執行改名…」對話框：顯示根資料夾、改名筆數、REVIEW 筆數，必須輸入「確認 rename」才能按執行。 |
| UI-7 | 執行中顯示進度，完成後顯示驗證結果與紀錄位置。 |
| UI-8 | 「還原上次執行」：先列出會還原幾筆，確認後執行。 |
| UI-9 | 所有路徑、檔名、空白（含連續空白、全形字元、私用區字元）原樣處理，不壓縮空白。 |

另提供 CLI（`python -m namer plan | apply | verify | rollback`），供測試與批次使用；CLI 的 `apply` 同樣需要 `--confirm "確認 rename"`。

## 14. 待決事項

| # | 問題 | 目前預設 |
|---|---|---|
| Q1 | 附件資料夾要不要跟著信件 PDF 改名 | `follow`（跟著改）；04 是另外合併進 PDF，不適用此預設 |
| Q2 | 非歸檔格式要不要占序號 | 不占（等 Baleen 轉完再命名） |
| Q3 | `00` 這類非日期既有 code 的資料夾出現新檔時要不要接續編號 | 接續編號（列為 `RENAME`，origin 寫 `CONTINUE`，預覽中可看到） |
| Q4 | 前綴預設的 `DP2_99_` 是否所有專案都一樣 | 是，可在介面改 |
| Q5 | Final Report 的範本 | 先輸出 `summary.txt`；拿到 Emily 的範本後再對齊 |

## 15. 驗收條件

| ID | 條件 |
|---|---|
| AC-1 | 用 10/05 的 1,670 筆清單、不加任何覆寫，各資料夾的 group 與 RENAME_SPEC v0.2 manifest 完全相同。 |
| AC-2 | 重現 04 v2 決策：`2002   -   2004` 設 `code 20022004` → `_01`–`_40`；`混咬表演` 設 `share` → 全部 `200401h`，序號依子資料夾順序連續；`三弦`＋`琵琶` 既有 `200401g` 自動合池；200303 既有 `a` 保留、蘇瑋婷得 `b`。 |
| AC-3 | 輸入順序打亂，結果相同（決定性）。 |
| AC-4 | 在暫存目錄執行後，驗證通過；再規劃一次 `RENAME = 0`；還原後所有檔名回到原狀。 |
| AC-5 | 執行前改動資料夾（新增或刪除一個歸檔檔案、改大小）→ 執行中止，沒有任何檔案被改。系統檔（`Thumbs.db`）出現或消失不影響。 |
| AC-6 | 目標被占用、重複目標、純大小寫改名（`.JPG→.jpg`）、雙空格與全形符號檔名都正確處理。 |
| AC-7 | 附件資料夾跟隨信件 PDF 改名；附件內容不改名、不占號。 |
| AC-8 | GUI 可在無 Baleen 以外安裝的 Windows 啟動；未輸入「確認 rename」時執行按鈕不可按。 |
