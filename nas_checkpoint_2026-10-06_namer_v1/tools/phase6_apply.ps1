<#
  Phase 6：把信件 PDF 與附件合併後的 PDF/A 檔換進 Z:
  放在 Downloads\phase6\，旁邊要有 phase6_map.csv 與 merged\ 資料夾。
    .\phase6_apply.ps1                       # dry-run：只檢查（來源、大小、雜湊）
    .\phase6_apply.ps1 -Execute              # 備份到本機 backup\，再用合併檔覆蓋 Z: 上的信件 PDF
    .\phase6_apply.ps1 -RemoveAttachments    # 另一步：確認合併檔沒問題後，才刪除已併入的附件檔
  備份在 Downloads\phase6\backup\（保留相對路徑）；Z: 沒有資源回收桶，刪除後無法復原。
#>
param(
  [string]$Root = "Z:\18_豆皮藝文咖啡廳第二期\數位檔案_整理\04 打羊秀(2000-2005)",
  [switch]$Execute,
  [switch]$RemoveAttachments
)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$map = Import-Csv -LiteralPath (Join-Path $here 'phase6_map.csv') -Encoding UTF8
$backup = Join-Path $here 'backup'
Write-Host "map 筆數：$($map.Count)（預期 5）"
if ($map.Count -ne 5) { throw "map 筆數不對" }

# ---------- 刪除附件（獨立步驟）----------
if ($RemoveAttachments) {
  foreach ($r in $map) {
    $cur = Join-Path (Join-Path $Root $r.Folder) $r.TargetName
    $h = (Get-FileHash -LiteralPath $cur -Algorithm SHA256).Hash
    if ($h -ne $r.MergedSha256.ToUpper()) { Write-Host "略過（Z: 上的 PDF 不是合併檔）：$cur" -ForegroundColor Yellow; continue }
    $adir = Join-Path (Join-Path $Root $r.Folder) $r.AttachFolder
    foreach ($a in ($r.Attachments -split '\|')) {
      $p = Join-Path $adir $a
      if (-not (Test-Path -LiteralPath $p)) { Write-Host "附件不存在（略過）：$p"; continue }
      $b = Join-Path (Join-Path (Join-Path $backup $r.Folder) $r.AttachFolder) $a
      if (-not (Test-Path -LiteralPath $b)) { Write-Host "備份中沒有，不刪：$p" -ForegroundColor Yellow; continue }
      Remove-Item -LiteralPath $p
    }
    if ((Test-Path -LiteralPath $adir) -and -not (Get-ChildItem -LiteralPath $adir -Force)) { Remove-Item -LiteralPath $adir }
    elseif (Test-Path -LiteralPath $adir) { Write-Host "資料夾內還有檔案，保留：$adir" -ForegroundColor Yellow }
  }
  Write-Host "完成。"
  return
}

# ---------- Pre-flight ----------
$fail = @()
foreach ($r in $map) {
  $dir = Join-Path $Root $r.Folder
  $cur = Join-Path $dir $r.TargetName
  $mrg = Join-Path (Join-Path $here 'merged') $r.TargetName
  if (-not (Test-Path -LiteralPath $cur)) { $fail += "Z: 上找不到：$cur"; continue }
  $l = (Get-Item -LiteralPath $cur).Length
  $curHash = (Get-FileHash -LiteralPath $cur -Algorithm SHA256).Hash
  if ($l -ne [int64]$r.OrigLength -and $curHash -ne $r.MergedSha256.ToUpper()) { $fail += "Z: 上的 PDF 大小與清單不符：$cur  清單=$($r.OrigLength) 實際=$l" }
  if (-not (Test-Path -LiteralPath $mrg)) { $fail += "找不到合併檔：$mrg"; continue }
  if ((Get-FileHash -LiteralPath $mrg -Algorithm SHA256).Hash -ne $r.MergedSha256.ToUpper()) { $fail += "合併檔雜湊不符：$mrg" }
  $names = $r.Attachments -split '\|'; $lens = $r.AttachLengths -split '\|'
  for ($i = 0; $i -lt $names.Count; $i++) {
    $p = Join-Path (Join-Path $dir $r.AttachFolder) $names[$i]
    if (-not (Test-Path -LiteralPath $p)) { if ($curHash -ne $r.MergedSha256.ToUpper()) { $fail += "附件不存在：$p" } }
    elseif ((Get-Item -LiteralPath $p).Length -ne [int64]$lens[$i]) { $fail += "附件大小與清單不符：$p" }
  }
}
if ($fail.Count) { Write-Host "Pre-flight 失敗 $($fail.Count) 項，未改任何檔案：" -ForegroundColor Red; $fail | ForEach-Object { Write-Host "  $_" }; return }
Write-Host "Pre-flight 通過。" -ForegroundColor Green
if (-not $Execute) { Write-Host "dry-run 結束，未改任何檔案。要實際換檔請加 -Execute。"; return }

# ---------- Execute：備份 → 覆蓋 → 驗證 ----------
foreach ($r in $map) {
  $dir = Join-Path $Root $r.Folder
  $cur = Join-Path $dir $r.TargetName
  $mrg = Join-Path (Join-Path $here 'merged') $r.TargetName
  if ((Get-FileHash -LiteralPath $cur -Algorithm SHA256).Hash -eq $r.MergedSha256.ToUpper()) { Write-Host "已是合併檔，略過：$($r.TargetName)"; continue }
  $bdir = Join-Path $backup $r.Folder
  New-Item -ItemType Directory -Force -Path $bdir | Out-Null
  $bpdf = Join-Path $bdir $r.TargetName
  Copy-Item -LiteralPath $cur -Destination $bpdf
  if ((Get-FileHash -LiteralPath $bpdf -Algorithm SHA256).Hash -ne (Get-FileHash -LiteralPath $cur -Algorithm SHA256).Hash) { throw "備份驗證失敗：$cur" }
  $badir = Join-Path $bdir $r.AttachFolder
  New-Item -ItemType Directory -Force -Path $badir | Out-Null
  foreach ($a in ($r.Attachments -split '\|')) {
    $src = Join-Path (Join-Path $dir $r.AttachFolder) $a
    Copy-Item -LiteralPath $src -Destination (Join-Path $badir $a)
  }
  Copy-Item -LiteralPath $mrg -Destination $cur -Force
  if ((Get-FileHash -LiteralPath $cur -Algorithm SHA256).Hash -ne $r.MergedSha256.ToUpper()) { throw "覆蓋後雜湊不符：$cur" }
  Write-Host "已換檔：$($r.Folder)\$($r.TargetName)" -ForegroundColor Green
}
Write-Host "完成。備份在：$backup（附件尚未刪除；確認合併檔後再加 -RemoveAttachments）"
