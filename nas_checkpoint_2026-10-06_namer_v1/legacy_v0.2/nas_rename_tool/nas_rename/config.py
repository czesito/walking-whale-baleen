"""集中管理規則常數。任何規則變更都應先改 RENAME_SPEC.md，再改這裡。"""

PREFIX = "DP2_99_04_"          # 固定前綴
MIN_SEQ_WIDTH = 2              # ## 至少兩位數，>99 自然變三位數
FIRST_YEAR, LAST_YEAR = 2000, 2005   # 根目錄名稱 (2000-2005)
MAX_LETTERS = 26               # a-z；超過 → REVIEW_REQUIRED（規格未定義 aa, ab…）
STAGING_DIRNAME = "_TEMP_CONVERSION"
ARCHIVE_DIRNAME = "_ORIGINALS"            # 位於 root 底下；驗證成功後原始檔「搬移」到這裡（不刪除）
QUARANTINE_DIRNAME = "_ROLLBACK_QUARANTINE"
SKIP_DIRNAMES = (STAGING_DIRNAME, ARCHIVE_DIRNAME, QUARANTINE_DIRNAME)   # 掃描時略過

# 最終允許副檔名（小寫）
FINAL_EXTS = {".jpg", ".jpeg", ".tif", ".tiff", ".pdf", ".mp4"}

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp"}
DOC_EXTS = {".doc", ".docx", ".rtf", ".txt", ".html", ".htm", ".eml", ".pdf"}   # .rtf 於 v0.2 納入（決策 9）
AV_EXTS = {".avi", ".mov", ".mkv", ".mpeg", ".mpg", ".wmv", ".mp4", ".mp3", ".wav",
           ".m4a", ".aac", ".wma", ".flv", ".3gp", ".m4v", ".ogg", ".flac"}

# MP4 可 stream copy 的 codec 白名單（見 RENAME_SPEC.md §8）
MP4_COPY_VIDEO = {"h264", "hevc"}
MP4_COPY_AUDIO = {"aac", "mp3"}

# 狀態字彙
ST_PLANNED = "PLANNED"                       # 只需改名/複製，格式依副檔名已符合
ST_CONV_PLANNED = "CONVERSION_PLANNED"       # 需轉檔，方法已指定
ST_ALREADY = "ALREADY_VALID"                 # 檔名與格式皆已是目標（仍需內容驗證）
ST_REVIEW = "REVIEW_REQUIRED"
ST_UNSUPPORTED = "CONVERSION_UNSUPPORTED"    # 未知／無法轉換的格式
ST_COLLISION = "COLLISION"
ST_UNREADABLE = "UNREADABLE"
ST_CONV_REQUIRED = "CONVERSION_REQUIRED"     # 缺少可靠工具（僅在實機掃描時判定）
ST_CONV_FAILED = "CONVERSION_FAILED"

# v0.2 新增狀態
ST_PDFA_NOT_VALIDATED = "PDF_A_NOT_VALIDATED"          # 沒有 veraPDF：不得宣稱 PDF/A 已驗證
ST_PDFA_CONV_REQUIRED = "PDF_A_CONVERSION_REQUIRED"    # 既有 PDF 經 veraPDF 判定不是 PDF/A；不覆蓋原檔
