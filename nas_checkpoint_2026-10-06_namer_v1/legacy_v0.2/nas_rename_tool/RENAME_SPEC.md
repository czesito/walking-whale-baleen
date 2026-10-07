# RENAME_SPEC.md — NAS 數位檔案批次重新命名＋格式轉換

版本：0.2-draft（已納入你 2026-10-05 的 12 項決策；尚未對 NAS 實際執行）
範圍：`\\Aaa_nas\iast\18_豆皮藝文咖啡廳第二期\數位檔案_整理\04 打羊秀(2000-2005)`
依據：`nas_listing.csv`（1,670 個檔案、149 個資料夾，2026-10-05 匯出）

文件結構：v0.2 變更對照（§0）、規則（§1–§10）、這次掃描的事實（§11）、尚待確認（§12）、使用方式（§13）。

---

## 0. v0.2 變更對照

| # | 你的決策 | 規格位置 | 結果 |
|---|---|---|---|
| 1 | 既有 DP2 命名優先 | §2、§4 | 328 個既有檔名的字母與序號一個都不變；200303 的 23 個檔案維持 `a`，蘇瑋婷得 `b` |
| 2 | 巢狀子資料夾 | §3.2、§4 | 日期繼承寫入規格；manifest 新增 `date_source_folder`；200401 的 6 個一般資料夾維持 a–f，13 個子資料夾接 g–s |
| 3 | `2002   -   2004` | §3.3 | 40 個檔案全部 `REVIEW_REQUIRED`，不猜日期、不動檔 |
| 4 | `00` 資料夾 | §2.3、§10 | 8 個 PDF 視為既有編目結果，不改名，只驗證 PDF/A |
| 5 | `.JPG` → `.jpg` | §2.2、§5 | 已命名的只做副檔名正規化；未命名的直接輸出正式檔名 |
| 6 | 原始檔處理 | §8 | 驗證成功後搬到 `_ORIGINALS`；失敗的不移動；相對路徑完整記錄 |
| 7 | EML 附件 | §5.2 | 新增 `attachment_detected`、`attachment_count`；附件不獨立轉檔 |
| 8 | 無日資料夾 | §4 | 排序優先級改為「既有 → 可解析日期 → 無日期 → 完整名稱 → 完整 path」 |
| 9 | RTF | §5 | `.rtf → PDF/A` 納入文件轉檔 |
| 10 | PDF/A 驗證 | §10 | 只認 veraPDF 的判定；沒有 veraPDF → `PDF_A_NOT_VALIDATED` |
| 11 | 工具偵測 | §13 | 新增 `environment_check.ps1` |
| 12 | 重新產生輸出 | §7 | `RENAME_SPEC.md`、manifest、`dry_run_report.txt` 已重產；`environment_report.txt` 需在你的 Windows 上執行腳本才會有內容 |

---

## 1. 安全原則

1. 第一階段只讀。程式預設 `--dry-run`，只寫出 manifest、report，不改任何來源檔。
2. 程式裡沒有任何刪除來源檔的程式碼路徑。唯一會清除的是本工具自己建立的暫存檔（`*.part`、staging）。
3. 不覆蓋、不自動加 `_copy` 或亂數。目標名稱已被佔用 → `COLLISION`，該項停止。
4. 不猜。無法判定的一律 `REVIEW_REQUIRED` 或具體狀態，並寫明 reason。
5. 每個檔案獨立處理；任一步驟（轉檔、驗證、複製）失敗 → 該檔停止，**原始檔不移動**。
6. `--execute` 需要你核准的 manifest digest；來源資料夾在 dry-run 之後有任何變動 → 中止。
7. 沒有 veraPDF 就不說「PDF/A verified」（§10）。

## 2. 最終檔名

```
DP2_99_04_<YYYYMM><letter>_<seq>.<ext>
例：DP2_99_04_200311a_01.jpg
```

| 部分 | 規則 |
|---|---|
| `DP2_99_04_` | 固定 |
| `YYYYMM` | 只從資料夾名稱解析（含日期繼承，§3），不看 metadata |
| `letter` | 同一 YYYYMM 內，每個資料來源單位一個字母；既有的保留，新的依 §4 分配 |
| `seq` | 單位內每個檔案一個號碼；既有的保留，新的依 §4 分配；至少兩位數，99 之後是 100、101…（不循環） |
| `ext` | 小寫：`.jpg` `.tif` `.pdf` `.mp4` |

### 2.1 既有 DP2 命名優先（最高優先級）

凡檔名已符合 `DP2_99_04_YYYYMM?_##`（不分副檔名）的檔案，視為既有編目結果：

- 不重新分配 `?` 與 `##`，檔名主幹（stem）永遠不變。
- 只允許改副檔名：`.JPG → .jpg`（正規化），或需要轉檔時換成目標副檔名（`DP2_99_04_200202b_02.bmp → DP2_99_04_200202b_02.jpg`）。
- 它所在資料夾的字母被視為「已占用」，其他資料夾不得使用。
- 若同一資料夾含兩種以上既有字母、既有字母的 YYYYMM 與資料夾不符、或同一 `YYYYMM?` 被兩個資料夾共用 → 該資料夾整個 `REVIEW_REQUIRED`（目前資料沒有這種情況）。

### 2.2 `.JPG → .jpg`

| 情況 | 處理 |
|---|---|
| 已是 DP2 檔名（`DP2_99_04_200311a_01.JPG`） | 只做副檔名正規化 → `.jpg`。Windows 不分大小寫，不能複製，用兩段式 in-place 改名；`planned_action = NORMALIZE_EXT_IN_PLACE`。先驗證檔案真的是有效 JPEG，失敗則不改名。這是整個流程唯一不經 `_ORIGINALS` 就讓原檔名消失的操作，會寫入 log，rollback 可還原 |
| 尚未命名的 JPG | 直接輸出正式檔名 `DP2_99_04_YYYYMM?_##.jpg`；不重新編碼，位元組原樣複製，只確認是有效 JPEG（Pillow 完整 decode）；之後原檔搬到 `_ORIGINALS` |

### 2.3 `DP2_99_04_00_##`（`00` 資料夾）

視為既有編目結果：不屬於任何 YYYYMM，不改名、不分配字母；`name_origin = EXISTING_DP2_00`，`planned_action = VERIFY_ONLY`，只驗證 PDF/A（§10）。不是 PDF/A → `PDF_A_CONVERSION_REQUIRED`，**不覆蓋原 PDF**。

## 3. 資料夾名稱解析

### 3.1 格式

`YYYY MM [DD[-DD]]` 開頭，其後可接任何文字（含緊接的中文）。

```
2003 11 01 DJ阿義(豆皮員工簡正義)   → 200311, 日=01
2001 09賀香滋 大提琴演奏            → 200109, 無日
2004 06 12電音22                    → 200406, 日=12
2002 03 29-31 Wake Up(威爾剛)       → 200203, 日=29（範圍 29–31）
2003 11                             → 200311, 無日
```

解析失敗代碼：`NO_YYYY_MM_PREFIX`、`YEAR_OUT_OF_RANGE`（限 2000–2005）、`BAD_MONTH`、`BAD_DAY`（含不存在的日期）、`BAD_DAY_RANGE`。

### 3.2 巢狀子資料夾：日期繼承

直接 parent 無法解析日期時，**沿用最近的上一個可解析日期的祖先資料夾**。

```
2004 01 混咬表演\dj        → YYYYMM = 200401，日期來源 = 2004 01 混咬表演
2004 01 31 古樂器之夜\三弦  → YYYYMM = 200401（日=31），日期來源 = 2004 01 31 古樂器之夜
```

- 每個子資料夾各自是獨立的資料來源單位，各占一個字母。
- 繼承只決定 YYYYMM（以及排序用的日期），不改變任何既有資料夾的字母。
- manifest 的 `date_source_folder` 記錄 YYYYMM 是從哪個資料夾解析出來的：自己解析的填自己、繼承的填祖先、都找不到的留空。
- 往上找不到任何可解析日期 → `REVIEW_REQUIRED: DATE_NOT_DETERMINABLE_NO_GUESS`。

### 3.3 無法解析日期

- `2002   -   2004`（40 個 `image####.JPG`）：不猜 YYYYMM，不塞進任何月份。全部 `REVIEW_REQUIRED`，原檔不動，也不占用任何字母或序號。
- 直接放在 root 底下的檔案：`REVIEW_REQUIRED: FILE_DIRECTLY_UNDER_ROOT`。

## 4. 排序與分配（決定性，不依賴檔案系統回傳順序）

### 4.1 字母

優先級依序：

1. 既有 DP2 命名結果
2. 可解析日期（日小的在前；繼承而來的用祖先的日期）
3. 無日期（排在同月有日期的資料夾之後）
4. 完整資料夾名稱（NFC 正規化 + casefold，再依 Unicode code point；巢狀資料夾用相對路徑）
5. 完整 path

演算法：先把所有既有字母標為已占用；再把「還沒有字母」的資料夾依 2–5 排序，每個取**最小的尚未被占用的字母**。因此新資料夾不會改動任何既有字母，不論它的日期早於或晚於既有資料夾。目前資料沒有字母缺號，「最小未占用」與「接在最後」的結果相同。

超過 26 個資料夾（z 之後）規格沒有定義 → `REVIEW_REQUIRED: LETTER_OVERFLOW`。目前最多的月份是 200401（19 個）。

### 4.2 序號

單位內：既有檔案保留自己的號碼；其餘檔案依下列順序，各取最小的尚未使用的號碼。**單位內每一個檔案都占一個號碼，不論它的狀態**（包含 `REVIEW_REQUIRED`），這樣之後解決某個例外時，其他檔案的編號不會跟著變。

檔案順序：檔名（不含副檔名）NFC + casefold → 副檔名 casefold → 原始大小寫字串。比較是字串順序，不是自然數字順序（`file10` 會排在 `file2` 之前）。

例（三個混合資料夾）：`2002 05 18 陳衍宏…` 已有 `…200205b_01`–`07`，未命名的 `五月的黑色浪漫.doc` 得 `…200205b_08.pdf`。

## 5. 格式矩陣

| 來源 | 目標 | 方法 | 驗證 |
|---|---|---|---|
| jpg / jpeg | .jpg | 解碼確認內容真的是 JPEG 後，位元組原樣複製（不重新編碼） | Pillow 完整 decode |
| tif / tiff | .tif | 同上 | decode |
| png gif bmp webp | .jpg | decode → encode，quality 95、4:4:4。有 alpha、16-bit、CMYK 則輸出 TIFF（LZW 無損）。多影格 GIF → 停止，需人工 | decode |
| doc docx **rtf** | .pdf | LibreOffice 直接匯出 PDF/A-2b | veraPDF |
| txt | .pdf | 偵測編碼 → UTF-8 → LibreOffice PDF/A-2b；抽出 PDF 文字，字數低於原文 98% 視為缺字型 → 失敗 | veraPDF + 字數比對 |
| html | .pdf | Chromium render → Ghostscript PDF/A-2b；有缺失資源 → `CONVERSION_FAILED` | veraPDF |
| eml | .pdf | From / To / Cc / Date / Subject / Body → HTML → Chromium → Ghostscript PDF/A-2b（§5.2） | veraPDF |
| pdf | .pdf | 只驗證是否 PDF/A，**不自動轉換、不覆蓋** | veraPDF |
| avi mov mkv mpeg mpg wmv wma mp3 wav m4a aac 等 | .mp4 | ffmpeg。影像 codec 為 h264/hevc 且音訊為 aac/mp3 才 stream copy，否則重新編碼 H.264 (CRF 18) + AAC 192k | ffprobe + 完整解碼 + 長度誤差 ≤ max(1 秒, 1%) |
| mp4 | .mp4 | 同上（不因副檔名就假設合規） | 同上 |

TXT 編碼：只有 BOM 或嚴格 UTF-8 視為確定。Big5/CP950/GB18030 只是「猜測」→ 停止並等人工確認。

### 5.2 EML 與附件

- PDF 內容：From、To、Cc（若有）、Date、Subject、Body。HTML 本文裡以 `cid:` 引用的內嵌圖片會嵌進畫面，不算附件。
- 附件**不獨立轉檔、不另產生檔案**，視為 EML 原始內容；EML 轉成 PDF 後，原 EML 搬到 `_ORIGINALS`（含附件，不刪除）。
- 附件定義：不是本文、也不是 `cid:` 內嵌圖片的 MIME 部件（`Content-Disposition: attachment`、有檔名的非內嵌部件、`message/rfc822`）。
- manifest：`attachment_detected`（`YES` / `NO` / `UNKNOWN`）、`attachment_count`。`UNKNOWN` 表示離線 listing 模式沒有開檔；實機 dry-run 才會填入。
- 實機 dry-run 發現 `attachment_count > 0`，或 EML 無法解析 → `REVIEW_REQUIRED`（`EML_ATTACHMENT_NOT_PRESERVED_IN_PDF`）。理由：PDF/A-2b 不能內嵌附件，附件內容不會出現在正式輸出裡。程式絕不自行刪除附件。見 §12 E1。

## 6. 狀態字彙

| 狀態 | 意義 |
|---|---|
| `PLANNED` | 只需改名／複製，格式依副檔名已符合 |
| `CONVERSION_PLANNED` | 需轉檔，方法已指定 |
| `ALREADY_VALID` | 既有 DP2 檔名且副檔名已是目標（內容仍會在 execute 時驗證） |
| `REVIEW_REQUIRED` | 需要你決定，reason 說明原因 |
| `PDF_A_NOT_VALIDATED` | 沒有 veraPDF，無法驗證 PDF/A（§10） |
| `PDF_A_CONVERSION_REQUIRED` | 既有 PDF 經 veraPDF 判定不是 PDF/A；原檔不動 |
| `CONVERSION_UNSUPPORTED` | 未知格式 |
| `CONVERSION_REQUIRED` | 缺少可靠工具（實機 dry-run 才會判定；離線清單模式不判定） |
| `CONVERSION_FAILED` / `COLLISION` / `UNREADABLE` | 如字面 |

Collision 偵測不分大小寫（Windows）。兩種情況都算：兩個來源算出相同目標；或目標名稱剛好是同資料夾另一個來源檔的現有名稱。

## 7. Manifest 與 Log

`rename_conversion_manifest.csv`（UTF-8 with BOM）：你要求的 14 欄，加上

| 欄位 | 內容 |
|---|---|
| `source_size` | 來源大小（execute 時用來偵測來源是否變動） |
| `group_id` | `YYYYMM` + 字母 |
| `proposed_target_filename` | `REVIEW_REQUIRED` 項目若已算得出名稱，放在這裡 |
| `note` | 說明性文字（不進 digest） |
| `date_source_folder` | YYYYMM 是從哪個資料夾解析（或繼承）而來，完整路徑 |
| `name_origin` | `EXISTING_DP2` / `EXISTING_DP2_00` / `ASSIGNED` |
| `planned_action` | `VERIFY_ONLY` / `NORMALIZE_EXT_IN_PLACE` / `COPY_THEN_ARCHIVE` / `CONVERT_THEN_ARCHIVE` / `NONE` |
| `source_relative_path` | 原檔相對於 root 的路徑（含原檔名） |
| `archive_path` | 驗證成功後原檔會被搬到的位置：`<root>\_ORIGINALS\<source_relative_path>`；不會搬移的留空 |
| `attachment_detected` / `attachment_count` | 僅 EML（§5.2） |

`rename_conversion_manifest.sha256`：核心欄位（含新增欄位，不含 `note`、`group_id`、`proposed_target_filename`）的 digest，`--execute` 用它確認計畫沒有變動。**核准用的 digest 必須來自實機 dry-run**：離線 listing 模式下 EML 附件欄位是 `UNKNOWN`，與實機結果不同，digest 也會不同。

`rename_conversion_log.csv`：你要求的 9 欄，加上 `action`、`target_sha256`、`target_size`、`archived_path`、`original_relative_path`。每筆寫完立即 flush + fsync。

## 8. 執行流程（`--execute`，預設 `--originals archive`）

1. 重新掃描（略過 `_TEMP_CONVERSION`、`_ORIGINALS`、`_ROLLBACK_QUARANTINE`）→ 2. 重新規劃（含 EML 附件偵測）並核對 digest，不符即中止 → 3. 逐檔：確認來源存在且大小未變 → 4. 轉檔／複製到 staging（預設本機 `_TEMP_CONVERSION`，不放 NAS）→ 5. 驗證輸出（圖片 decode、veraPDF、ffprobe＋完整解碼）→ 6. 確認目標名稱不存在 → 7. 先寫 `<目標>.part`，hash 與 staging 一致後才改成正式名稱 → 8. **原始檔搬移到 `_ORIGINALS`**。

```
原始檔 → 轉檔 → 驗證成功 → 新檔案正式命名 → 原始檔移到 _ORIGINALS
```

`_ORIGINALS` 位置：`04 打羊秀(2000-2005)\_ORIGINALS\`，底下保留原本的子資料夾結構與原檔名，例如 `_ORIGINALS\2003 11 01 DJ阿義(豆皮員工簡正義)\IMG_0001.JPG`。

- 任何轉檔或驗證失敗的檔案：原檔不移動，log 記錄原因。
- `_ORIGINALS` 內已有同名檔 → 原檔留在原處，狀態 `COPIED_ARCHIVE_FAILED`，execute 結束碼為 2。
- 不會搬移的：`VERIFY_ONLY`（本身就是輸出）、`NORMALIZE_EXT_IN_PLACE`（只是改名）、所有 `REVIEW_REQUIRED` 項目。
- 目前計畫會搬到 `_ORIGINALS` 的有 1,296 個檔案、約 0.81 GB；搬完之前新檔與原檔同時存在，磁碟會短暫多用同樣的容量，`_ORIGINALS` 由你決定何時處理，程式不會刪。

## 9. Rollback（`rollback.py`）

預設只預覽，需 `--execute-rollback` 才動作；不覆蓋、不刪除。

- `ARCHIVED`：先把 `_ORIGINALS` 內的原檔搬回原路徑（原路徑仍空時），再把新檔 hash 驗證後**移到**每個檔案旁的 `_ROLLBACK_QUARANTINE`（不刪）
- `RENAMED_IN_PLACE`（`.jpg` → `.JPG`）：原路徑沒被佔用時改回
- `COPIED` / `COPIED_ARCHIVE_FAILED`：新檔 hash 與 log 一致時移到 quarantine
- 任何狀態不符 → 跳過該項並印出

## 10. PDF/A 驗證標準

- 判定只靠 **veraPDF** 的報告（解析 XML 的 `validationReport` 是否 `isCompliant="true"`）。不看 PDF metadata、XMP 或檔案內的 PDF/A 標記。
- 沒有 veraPDF：
  - 不得宣稱「PDF/A verified」。
  - 文件類（doc/docx/rtf/txt/html/eml）轉檔鏈完整時，可以完成 conversion，但 status 是 `PDF_A_NOT_VALIDATED`；轉出的 PDF 只留在 staging，**不正式命名、不放進 NAS、不搬移原檔**，等你確認工具後再決定。
  - `00` 資料夾的既有 PDF 同樣標 `PDF_A_NOT_VALIDATED`，原檔不動。
- 有 veraPDF 但判定不合規：轉出的 PDF 不發布；既有 PDF（`00`）標 `PDF_A_CONVERSION_REQUIRED`，不覆蓋原檔，轉換方式另行討論。
- 正式執行前先在你的電腦跑 `environment_check.ps1`（§13），確認 Python、FFmpeg、FFprobe、LibreOffice、Ghostscript、veraPDF、Chrome/Chromium 是否可用。

---

## 11. 這次掃描的事實（來自 listing，檔案內容尚未檢查）

| 項目 | 數量 |
|---|---|
| 檔案總數 | 1,670 |
| 資料夾（含檔案者） | 149 |
| YYYYMM 群組 | 35（200011 起至 200506） |
| 每月資料夾數最多 | 200401：19（含 13 個巢狀）；200404：13；200312：12 |
| 零長度檔案 / Collision | 0 / 0 |

**命名來源**：既有 DP2 328 個（33 個資料夾）、`00` 系列 8 個、本次新分配 1,294 個、不分配 40 個。既有檔名被改動的 letter／序號：**0 個**。

**狀態**（離線 listing 模式）：

| 狀態 / 動作 | 檔案數 |
|---|---|
| `PLANNED` / `COPY_THEN_ARCHIVE`（未命名 jpg／tif，複製後原檔封存） | 1,233 |
| `PLANNED` / `NORMALIZE_EXT_IN_PLACE`（既有檔名 `.JPG → .jpg`） | 145 |
| `ALREADY_VALID` / `VERIFY_ONLY`（181 個既有 + 8 個 `00` PDF） | 189 |
| `CONVERSION_PLANNED` / `CONVERT_THEN_ARCHIVE` | 63 |
| `REVIEW_REQUIRED`（全部是 `2002   -   2004`） | 40 |

轉檔 63 個：doc 18、AVI 16、eml 14、MPG 6、rtf 5、wma 2、bmp 1、txt 1。其中 2 個已有 DP2 檔名（`DP2_99_04_200202b_02.bmp`、`DP2_99_04_200303a_09.AVI`），名稱主幹保留。

**與 v0.1 的差異**：只有 200303 的 25 個檔案的目標名稱改變（23 個既有檔回到 `a`，蘇瑋婷 2 個從 `a` 變 `b`）；其餘未命名資料的名稱與 v0.1 完全相同。v0.1 的 312 個 `REVIEW_REQUIRED` 現在剩 40 個。

**既有 `.JPG` 改副檔名的檔案是 145 個**，不是 v0.1 寫的 124 個：200303 的 23 個在 v0.1 因為字母衝突被擋下，現在恢復為單純的副檔名正規化。

**巢狀子資料夾 13 個**（236 個檔案）：`2004 01 31 古樂器之夜\三弦、琵琶`（→ 200401g、h）與 `2004 01 混咬表演\` 底下 11 個（→ 200401i–s）。完整對照見 `dry_run_report.txt`。

## 12. 尚待確認（只剩這幾項；不影響離線 plan）

**E1 EML 有附件的處理**
我把你的「附件內容無法被合理保留」解讀為：只要偵測到非內嵌附件，就標 `REVIEW_REQUIRED`，因為 PDF/A-2b 無法內嵌附件，PDF 裡不會有附件內容。這會讓有附件的 EML 全部停下來。如果你的意思是「原 EML 封存在 `_ORIGINALS` 就算保留」，改成只在 PDF 內列出附件檔名即可（一行改動）。附件有幾個要實機 dry-run 才知道。

**E2 沒有 veraPDF 時轉出的 PDF**
目前只留在 staging，不放進 NAS。如果你希望仍用正式檔名先放進去、之後再驗證，要告訴我，但那樣原檔也不該搬，需要另一套狀態管理。

**E3 新字母與新序號取「最小未占用」**
目前資料沒有缺號，結果與「接在最後」相同。如果之後有人刪過資料夾造成缺號，你想不想讓新資料夾重用缺號，需要你決定。

**E4 `.JPG → .jpg` 的 145 個 in-place 改名**
這是唯一不經 `_ORIGINALS` 的原檔名變動。log 會記錄、rollback 可還原；若你希望連這些也走 `_ORIGINALS`，請告訴我。

**E5 離線 plan 與核准 digest**
本次輸出是離線 listing 模式（沒有碰 NAS）。正式核准要在你的電腦上跑一次實機 dry-run，才能取得 EML 附件資訊與工具可用性，並產生可用於 `--execute` 的 digest。

## 13. 使用方式

```
# 0. 環境檢查（只讀；輸出 environment_report.txt）
powershell -ExecutionPolicy Bypass -File .\environment_check.ps1

# 1. 第一階段（預設 dry-run；只讀）
python rename_convert.py --root "\\Aaa_nas\iast\...\04 打羊秀(2000-2005)" --out-dir C:\work\out

# 已經有 PowerShell 匯出的清單時（完全不碰 NAS）
python rename_convert.py --root "<同上>" --listing nas_listing.csv --out-dir out

# 2. 你核准之後（需要實機 dry-run 產生的 digest）
python rename_convert.py --root "<同上>" --execute --approved-digest out\rename_conversion_manifest.sha256 --out-dir out
python rename_convert.py --root "<同上>" --verify --out-dir out
python rollback.py --log out\rename_conversion_log.csv          # 預覽；加 --execute-rollback 才會動作
```

`--originals keep` 可讓原檔留在原處（僅供測試）；預設是 `archive`。

### 已驗證與尚未驗證

- 27 個單元測試通過（既有命名優先、200303、混合資料夾序號、巢狀繼承、`00`、`2002   -   2004`、RTF、EML 附件偵測）。
- 在沙箱的假目錄（不是 NAS）跑過完整 execute＋rollback：有效 JPEG 複製後原檔搬到 `_ORIGINALS`、損壞的 `.JPG` 轉檔失敗後原檔不動、既有 `.JPG` 只改副檔名、bmp 轉 jpg、AVI 轉 mp4、RTF 因沒有 veraPDF 只留在 staging 並標 `PDF_A_NOT_VALIDATED`、`00` 的 PDF 標 `PDF_A_NOT_VALIDATED`，rollback 全數還原。
- **尚未在你的真實檔案上驗證**，**尚未用 veraPDF 判定過任何 PDF**（沙箱沒有 veraPDF），HTML／EML 的 Chromium + Ghostscript 路徑未實測，`environment_check.ps1` 沒有在 Windows 上執行過（沙箱沒有 PowerShell，只做了靜態檢查）。
- 清單用 `Get-ChildItem -File` 匯出，不含隱藏／系統檔（如 `Thumbs.db`、`desktop.ini`）與空資料夾。實機掃描會看到它們並標為 `CONVERSION_UNSUPPORTED`。
- 資料夾 `2003 11` 與 `2002 01 05 DJ Davis (巫國華)` 的名稱末尾帶有一個私用區字元（U+F028）。不影響命名（目標名稱不使用資料夾名稱），但實機處理時要確認路徑能正常存取。
