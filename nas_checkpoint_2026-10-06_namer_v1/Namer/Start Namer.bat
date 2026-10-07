@echo off
rem Namer launcher. Uses the Python bundled with Baleen (has tkinter), so nothing needs installing.
rem Search order: NAMER_PYTHON, Baleen folders next to this folder / on Desktop / Downloads / C:\, then py / python.
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONNOUSERSITE=1"
set "PYTHONDONTWRITEBYTECODE=1"
set "PY="

if defined NAMER_PYTHON if exist "%NAMER_PYTHON%" set "PY=%NAMER_PYTHON%"
if not defined PY call :find "%~dp0.."
if not defined PY call :find "%USERPROFILE%\Desktop"
if not defined PY call :find "%USERPROFILE%\Downloads"
if not defined PY call :find "C:\Baleen"
if not defined PY call :find "C:"

if defined PY goto run
where py >nul 2>&1 && (py -3 -c "import tkinter" >nul 2>&1) && (set "PY=py" & set "PYARG=-3")
if defined PY goto run
where python >nul 2>&1 && (python -c "import tkinter" >nul 2>&1) && set "PY=python"
if defined PY goto run

echo.
echo [Namer] Cannot find a Python with tkinter.
echo Put this folder next to the Baleen folder (Baleen-x.y.z-win-x64), or set NAMER_PYTHON
echo to the full path of Baleen's runtime\python\python.exe, then start again.
echo.
pause
exit /b 1

:run
echo [Namer] Python: %PY% %PYARG%
"%PY%" %PYARG% -c "import tkinter" >nul 2>&1
if errorlevel 1 (
  echo [Namer] This Python has no tkinter: %PY%
  pause
  exit /b 1
)
"%PY%" %PYARG% -m namer
if errorlevel 1 pause
exit /b 0

:find
rem %1 = a folder that may contain Baleen-* or baleen-*\Baleen-*
for /d %%D in ("%~1\Baleen-*") do if exist "%%~fD\runtime\python\python.exe" set "PY=%%~fD\runtime\python\python.exe"
if defined PY exit /b 0
for /d %%D in ("%~1\baleen-*") do for /d %%E in ("%%~fD\Baleen-*") do if exist "%%~fE\runtime\python\python.exe" set "PY=%%~fE\runtime\python\python.exe"
exit /b 0
