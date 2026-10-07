Namer 命名工具 v1.0
====================

用途：Baleen 轉檔完成之後，把資料夾裡的檔案改成 DP2 編目名稱
（例：DP2_99_04_200311a_01.jpg）。不轉檔、不刪檔，只原地改名。
完整規則見 NAMING_SPEC.md。

啟動
----
1. 把整個 Namer 資料夾放在 Baleen 資料夾旁邊（同一層），例如：
     C:\Baleen\Baleen-0.1.0-win-x64\
     C:\Baleen\Namer\
   也可以放在桌面或下載資料夾，只要 Baleen 也在那裡。
2. 雙擊「Start Namer.bat」。會用 Baleen 內附的 Python，不用另外安裝。
   找不到時，設定環境變數 NAMER_PYTHON 指向 Baleen 的
   runtime\python\python.exe 即可。

流程
----
1. 根資料夾：選要命名的資料夾（例：Z:\...\04 打羊秀(2000-2005)），按「掃描預覽」。
   前綴（DP2_99_04_）與年份範圍會從資料夾名稱自動帶入，可以改，改完按「套用設定」。
   預覽完全不會改動檔案。
2. 看預覽：左邊是資料夾（命名單位），右邊是每個檔案的新名稱。
   「需檢查」的檔案不會被改，原因寫在右邊。常見的有：
     - NOT_ARCHIVAL_FORMAT：還沒用 Baleen 轉檔的 doc / avi / eml 等
     - DATE_NOT_DETERMINABLE：資料夾名稱沒有日期
3. 資料夾規則（左邊選資料夾 →「資料夾規則…」，可選上層資料夾）：
     - 指定群組代碼：例如「2002   -   2004」指定 20022004
     - 子資料夾共用一個群組：例如「2004 01 混咬表演」全部共用一個字母，序號連續
     - 排除：整個資料夾不處理
   規則會記住，下次選同一個資料夾自動載入。
4. 「執行改名…」→ 輸入「確認 rename」→ 執行。
   執行前會重新掃描；預覽之後資料夾有變動就中止，一個檔案都不改。
   改完自動驗證（新名稱都在、大小不變、檔案數不變、再規劃一次沒有待改名）。
5. 紀錄在 Namer\data\runs\<時間>\：
     preview.csv     核准的預覽
     rename_log.csv  每一筆改名
     name_map.csv    所有檔案的最終名稱與舊名稱（有 Baleen 報告時附上原始來源路徑）
     summary.txt     摘要與「需檢查」清單
6. 「還原上次執行…」可依 rename_log.csv 全部改回。

命令列（選用）
--------------
  python -m namer plan  "<根資料夾>" --out preview.csv
  python -m namer apply "<根資料夾>" --confirm "確認 rename"
  python -m namer rollback "<根資料夾>" --log data\runs\<時間>\rename_log.csv --confirm "確認 rename"

測試
----
  python -m unittest discover -s tests
