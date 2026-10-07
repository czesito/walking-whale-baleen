<#
  environment_check.ps1  (RENAME_SPEC v0.2 §10)
  只檢查目前 Windows 環境；不修改 NAS、不寫入任何 NAS 路徑。
  唯一寫入的檔案是本機的 environment_report.txt（預設放在本腳本所在資料夾）。

  用法：
    powershell -ExecutionPolicy Bypass -File .\environment_check.ps1
    powershell -ExecutionPolicy Bypass -File .\environment_check.ps1 -NasRoot "\\Aaa_nas\iast\...\04 打羊秀(2000-2005)" -OutFile C:\work\environment_report.txt

  每個工具輸出 FOUND / NOT_FOUND 與版本號。
#>
param(
    [string]$NasRoot = '\\Aaa_nas\iast\18_豆皮藝文咖啡廳第二期\數位檔案_整理\04 打羊秀(2000-2005)',
    [string]$OutFile = ''
)

$ErrorActionPreference = 'Continue'
if ([string]::IsNullOrEmpty($OutFile)) {
    $here = $PSScriptRoot
    if ([string]::IsNullOrEmpty($here)) { $here = (Get-Location).Path }
    $OutFile = Join-Path $here 'environment_report.txt'
}

$script:lines = New-Object System.Collections.Generic.List[string]
function Add-Line([string]$s) { $script:lines.Add($s) | Out-Null }

function Get-FirstLine($exe, $argList) {
    # 執行外部程式，取第一行輸出（stdout+stderr）；任何錯誤回傳空字串
    if (-not $exe) { return '' }
    try {
        $out = & $exe @argList 2>&1 | ForEach-Object { "$_" } | Where-Object { $_.Trim().Length -gt 0 } | Select-Object -First 1
        if ($null -eq $out) { return '' }
        return $out.Trim()
    } catch { return '' }
}

function Get-FileVersionText($path) {
    try {
        $v = (Get-Item -LiteralPath $path).VersionInfo
        if ($v.ProductVersion) { return $v.ProductVersion.Trim() }
        if ($v.FileVersion) { return $v.FileVersion.Trim() }
    } catch { }
    return ''
}

function Find-Exe([string[]]$names, [string[]]$globs) {
    foreach ($n in $names) {
        $c = Get-Command $n -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($c -and $c.Source) { return $c.Source }
    }
    foreach ($g in $globs) {
        $hit = Get-ChildItem -Path $g -ErrorAction SilentlyContinue | Sort-Object FullName | Select-Object -Last 1
        if ($hit) { return $hit.FullName }
    }
    return $null
}

function Report-Tool([string]$label, $path, [string]$version, [string]$note) {
    if ($path) {
        $v = $version; if ([string]::IsNullOrEmpty($v)) { $v = '(version not reported)' }
        Add-Line ('{0,-14} FOUND      {1}' -f $label, $v)
        Add-Line ('{0,-14}            path: {1}' -f '', $path)
    } else {
        Add-Line ('{0,-14} NOT_FOUND' -f $label)
    }
    if ($note) { Add-Line ('{0,-14}            note: {1}' -f '', $note) }
}

Add-Line 'ENVIRONMENT REPORT (read-only check; no NAS files modified)'
Add-Line ('Generated : {0}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
Add-Line ('Computer  : {0}   User: {1}' -f $env:COMPUTERNAME, $env:USERNAME)
Add-Line ('OS        : {0}' -f [System.Environment]::OSVersion.VersionString)
Add-Line ''
Add-Line '== Required tools =='

# Python（需 3.10+）
$py = Find-Exe @('python', 'python3') @('C:\Python3*\python.exe', "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe")
$pyNote = ''
$pyVer = ''
if ($py) {
    $pyVer = Get-FirstLine $py @('--version')
    if ($pyVer -match 'Python\s+(\d+)\.(\d+)') {
        if ([int]$Matches[1] -lt 3 -or ([int]$Matches[1] -eq 3 -and [int]$Matches[2] -lt 10)) { $pyNote = 'Python 3.10+ required by this tool' }
    }
    # Microsoft Store 的假 python.exe 會回傳空或錯誤訊息
    if ($pyVer -notmatch 'Python\s+\d') { $pyNote = 'only the Microsoft Store alias exists (' + $py + '); real Python is not installed'; $py = $null; $pyVer = '' }
}
Report-Tool 'Python' $py $pyVer $pyNote

# Pillow（Python 套件）
if ($py -and $pyVer -match 'Python\s+\d') {
    $pil = Get-FirstLine $py @('-c', 'import PIL; print(PIL.__version__)')
    if ($pil -match '^\d') { Report-Tool 'Pillow' $py $pil '' } else { Report-Tool 'Pillow' $null '' '' }
} else { Report-Tool 'Pillow' $null '' 'cannot check without a working Python' }

# FFmpeg / FFprobe
$ff = Find-Exe @('ffmpeg', 'ffmpeg.exe') @('C:\ffmpeg*\bin\ffmpeg.exe', 'C:\Program Files\ffmpeg*\bin\ffmpeg.exe')
Report-Tool 'FFmpeg' $ff (Get-FirstLine $ff @('-version')) ''
$fp = Find-Exe @('ffprobe', 'ffprobe.exe') @('C:\ffmpeg*\bin\ffprobe.exe', 'C:\Program Files\ffmpeg*\bin\ffprobe.exe')
Report-Tool 'FFprobe' $fp (Get-FirstLine $fp @('-version')) ''

# LibreOffice
$so = Find-Exe @('soffice', 'soffice.exe') @('C:\Program Files\LibreOffice\program\soffice.exe', 'C:\Program Files (x86)\LibreOffice\program\soffice.exe')
Report-Tool 'LibreOffice' $so (Get-FileVersionText $so) ''

# Ghostscript
$gs = Find-Exe @('gswin64c', 'gswin32c', 'gs') @('C:\Program Files\gs\gs*\bin\gswin64c.exe', 'C:\Program Files (x86)\gs\gs*\bin\gswin32c.exe')
Report-Tool 'Ghostscript' $gs (Get-FirstLine $gs @('-v')) ''

# veraPDF（PDF/A 驗證；沒有的話不得宣稱 PDF/A verified）
$vp = Find-Exe @('verapdf', 'verapdf.bat', 'verapdf.cmd') @('C:\Program Files\veraPDF\verapdf.bat', 'C:\Program Files (x86)\veraPDF\verapdf.bat', "$env:USERPROFILE\veraPDF\verapdf.bat")
$vpNote = ''
$vpVer = ''
if ($vp) {
    $vpVer = Get-FirstLine $vp @('--version')
    if ([string]::IsNullOrEmpty($vpVer)) { $vpNote = 'found but --version gave no output (Java missing?)' }
} else { $vpNote = 'without veraPDF the status stays PDF_A_NOT_VALIDATED' }
Report-Tool 'veraPDF' $vp $vpVer $vpNote

# Chrome / Chromium / Edge（HTML、EML 轉 PDF）
$ch = Find-Exe @('chrome', 'chromium', 'google-chrome') @('C:\Program Files\Google\Chrome\Application\chrome.exe', 'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe', "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe")
$chLabel = 'Chrome/Chromium'
if (-not $ch) {
    $ed = Find-Exe @('msedge') @('C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe', 'C:\Program Files\Microsoft\Edge\Application\msedge.exe')
    if ($ed) { Report-Tool $chLabel $null '' 'Chrome/Chromium not found; Edge present (works as fallback, see below)'; Report-Tool 'Edge (fallback)' $ed (Get-FileVersionText $ed) '' }
    else { Report-Tool $chLabel $null '' '' }
} else { Report-Tool $chLabel $ch (Get-FileVersionText $ch) '' }

# PowerShell
Add-Line ('{0,-14} FOUND      {1} {2}' -f 'PowerShell', $PSVersionTable.PSEdition, $PSVersionTable.PSVersion.ToString())
Add-Line ('{0,-14}            path: {1}' -f '', (Get-Process -Id $PID).Path)

Add-Line ''
Add-Line '== Optional / dependency tools =='
$jv = Find-Exe @('java', 'java.exe') @()
Report-Tool 'Java' $jv (Get-FirstLine $jv @('-version')) 'required by veraPDF'
$pt = Find-Exe @('pdftotext', 'pdftotext.exe') @('C:\Program Files\poppler*\Library\bin\pdftotext.exe', 'C:\poppler*\Library\bin\pdftotext.exe', 'C:\poppler*\bin\pdftotext.exe')
Report-Tool 'pdftotext' $pt (Get-FirstLine $pt @('-v')) 'used for the TXT → PDF text-loss check'

Add-Line ''
Add-Line '== NAS path (read-only) =='
Add-Line ('Path        : {0}' -f $NasRoot)
$nasOk = $false
try { $nasOk = Test-Path -LiteralPath $NasRoot -ErrorAction Stop } catch { $nasOk = $false }
if ($nasOk) {
    Add-Line 'Exists      : YES (FOUND)'
    try {
        $dirs = @(Get-ChildItem -LiteralPath $NasRoot -Directory -ErrorAction Stop)
        Add-Line ('Readable    : YES   top-level folders: {0}' -f $dirs.Count)
        $firstFile = Get-ChildItem -LiteralPath $NasRoot -File -Recurse -ErrorAction Stop | Select-Object -First 1
        if ($firstFile) {
            $fs = [System.IO.File]::OpenRead($firstFile.FullName)
            $buf = New-Object byte[] 1
            [void]$fs.Read($buf, 0, 1)
            $fs.Dispose()
            Add-Line ('File read   : OK (opened and read 1 byte of {0})' -f $firstFile.Name)
        } else { Add-Line 'File read   : no files found to test' }
        $oroot = Join-Path $NasRoot '_ORIGINALS'
        Add-Line ('_ORIGINALS  : {0}' -f $(if (Test-Path -LiteralPath $oroot) { 'already exists (check before execute)' } else { 'does not exist yet (expected before execute)' }))
    } catch {
        Add-Line ('Readable    : NO    {0}' -f $_.Exception.Message)
    }
} else {
    Add-Line 'Exists      : NO (NOT_FOUND) - check VPN / network drive / credentials'
}

Add-Line ''
Add-Line '== Summary =='
$need = @(
    @('Python', $py), @('FFmpeg', $ff), @('FFprobe', $fp), @('LibreOffice', $so),
    @('Ghostscript', $gs), @('veraPDF', $vp), @('Chrome/Chromium', $ch)
)
$missing = @($need | Where-Object { -not $_[1] } | ForEach-Object { $_[0] })
if ($missing.Count -eq 0) { Add-Line 'All required tools FOUND.' } else { Add-Line ('NOT_FOUND: ' + ($missing -join ', ')) }
if (-not $vp) { Add-Line 'veraPDF NOT_FOUND -> any PDF/A output stays PDF_A_NOT_VALIDATED (no "PDF/A verified" claims).' }

$text = $script:lines -join "`r`n"
[System.IO.File]::WriteAllText($OutFile, $text + "`r`n", (New-Object System.Text.UTF8Encoding($true)))
Write-Host $text
Write-Host ''
Write-Host ('Report written to: {0}' -f $OutFile)
