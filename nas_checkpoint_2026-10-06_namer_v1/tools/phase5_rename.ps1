<#
  Phase 5 改名腳本（只改名，不複製、不轉檔、不刪除）
  預設只做 pre-flight（dry-run），不會改任何檔案。
  用法（PowerShell 5.1 以上，把 phase5_rename.ps1 與 rename_map.csv 放在同一個資料夾）：
    .\phase5_rename.ps1                 # dry-run：只檢查
    .\phase5_rename.ps1 -Execute        # 實際改名
    .\phase5_rename.ps1 -Rollback       # 依 log 還原（只還原 log 裡狀態 OK 的項目）
#>
param(
  [string]$Root = "Z:\18_豆皮藝文咖啡廳第二期\數位檔案_整理\04 打羊秀(2000-2005)",
  [switch]$Execute,
  [switch]$Rollback
)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$mapPath = Join-Path $here 'rename_map.csv'
$logPath = Join-Path $env:TEMP 'phase5_rename_log.csv'

if (-not (Test-Path -LiteralPath $Root)) { throw "找不到 Root：$Root" }

# ---------- Rollback ----------
if ($Rollback) {
  if (-not (Test-Path -LiteralPath $logPath)) { throw "找不到 log：$logPath" }
  $log = Import-Csv -LiteralPath $logPath -Encoding UTF8 | Where-Object { $_.Status -eq 'OK' }
  $n = 0; $err = 0
  foreach ($r in $log) {
    $dir = if ($r.Folder) { Join-Path $Root $r.Folder } else { $Root }
    $cur = Join-Path $dir $r.NewName
    $old = Join-Path $dir $r.OldName
    if ((Test-Path -LiteralPath $cur) -and -not (Test-Path -LiteralPath $old)) {
      Rename-Item -LiteralPath $cur -NewName $r.OldName; $n++
    } else { Write-Host "略過（狀態不符）：$cur" -ForegroundColor Yellow; $err++ }
  }
  Write-Host "還原 $n 個，略過 $err 個"
  return
}

# ---------- 讀 map ----------
$map = Import-Csv -LiteralPath $mapPath -Encoding UTF8
Write-Host "map 筆數：$($map.Count)（預期 678）"

# ---------- Pre-flight ----------
$fail = New-Object System.Collections.Generic.List[string]
$seenTarget = @{}
foreach ($r in $map) {
  $dir = if ($r.Folder) { Join-Path $Root $r.Folder } else { $Root }
  $old = Join-Path $dir $r.OldName
  $new = Join-Path $dir $r.NewName
  $item = Get-Item -LiteralPath $old -Force -ErrorAction SilentlyContinue
  if (-not $item) { $fail.Add("來源不存在：$old"); continue }
  if ($item.Length -ne [int64]$r.Length) { $fail.Add("大小與清單不符（檔案被動過？）：$old  清單=$($r.Length) 實際=$($item.Length)") }
  if (Test-Path -LiteralPath $new) { $fail.Add("目標已存在：$new") }
  $key = $new.ToLowerInvariant()
  if ($seenTarget.ContainsKey($key)) { $fail.Add("目標重複：$new") } else { $seenTarget[$key] = $true }
  if ($new.Length -ge 255) { $fail.Add("路徑過長：$new") }
}
if ($fail.Count -gt 0) {
  Write-Host "Pre-flight 失敗 $($fail.Count) 項，未改任何檔案：" -ForegroundColor Red
  $fail | Select-Object -First 30 | ForEach-Object { Write-Host "  $_" }
  $fail | Set-Content -LiteralPath (Join-Path $env:TEMP 'phase5_preflight_failures.txt') -Encoding UTF8
  Write-Host "完整清單：$env:TEMP\phase5_preflight_failures.txt"
  return
}
Write-Host "Pre-flight 通過：$($map.Count) 個來源都存在、大小一致、目標都沒被占用。" -ForegroundColor Green
if (-not $Execute) { Write-Host "dry-run 結束，未改任何檔案。要實際改名請加 -Execute。"; return }

# ---------- Execute（遇到第一個錯誤就停）----------
$logRows = New-Object System.Collections.Generic.List[object]
$ok = 0
try {
  foreach ($r in $map) {
    $dir = if ($r.Folder) { Join-Path $Root $r.Folder } else { $Root }
    $old = Join-Path $dir $r.OldName
    Rename-Item -LiteralPath $old -NewName $r.NewName
    $logRows.Add([pscustomobject]@{ Folder=$r.Folder; OldName=$r.OldName; NewName=$r.NewName; Status='OK'; Time=(Get-Date -Format s) })
    $ok++
  }
} catch {
  $logRows.Add([pscustomobject]@{ Folder=$r.Folder; OldName=$r.OldName; NewName=$r.NewName; Status="ERROR: $($_.Exception.Message)"; Time=(Get-Date -Format s) })
  Write-Host "錯誤，已停止：$($_.Exception.Message)" -ForegroundColor Red
}
$logRows | Export-Csv -LiteralPath $logPath -NoTypeInformation -Encoding UTF8
Write-Host "已改名 $ok / $($map.Count)；log：$logPath"

# ---------- Post-rename 驗證 ----------
$missing = 0; $stale = 0
foreach ($r in $map) {
  $dir = if ($r.Folder) { Join-Path $Root $r.Folder } else { $Root }
  if (-not (Test-Path -LiteralPath (Join-Path $dir $r.NewName))) { $missing++ }
  if (Test-Path -LiteralPath (Join-Path $dir $r.OldName)) { $stale++ }
}
Write-Host "驗證：新名缺少 $missing 個、舊名仍存在 $stale 個（兩者都應為 0）"
$total = (Get-ChildItem -LiteralPath $Root -Recurse -File -Force | Measure-Object).Count
Write-Host "Root 底下檔案總數：$total（改名前 1680，改名不會增減檔案數，應維持 1680）"
