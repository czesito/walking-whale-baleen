@echo off
rem Start Baleen (Windows). Double-click this file: your browser opens Baleen, and this window
rem shows the log. Closing the window stops Baleen. Spec section 14.3.
rem Keep this file ASCII-only: cmd.exe reads it in the console code page.
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
title Baleen

rem cmd.exe cannot use a network path (\\server\share) as its working folder.
set "HERE=%~dp0"
if "%HERE:~0,2%"=="\\" goto :unc
cd /d "%HERE%"
if errorlevel 1 goto :nofolder
set "BALEEN_HOME=%CD%"
set "PYTHONHOME="
set "PYTHONPATH="

rem Source checkout (launchers\ inside the repository): use the repository root and its src\.
set "CHECKOUT="
if exist "%BALEEN_HOME%\..\pyproject.toml" if exist "%BALEEN_HOME%\..\src\baleen\__main__.py" set "CHECKOUT=1"
if not defined CHECKOUT goto :home_ready
for %%I in ("%BALEEN_HOME%\..") do set "BALEEN_HOME=%%~fI"
set "PYTHONPATH=%BALEEN_HOME%\src"
cd /d "%BALEEN_HOME%"
:home_ready

rem All state stays inside the folder: data\ holds settings, logs, temporary files and Java prefs.
set "DATA=%BALEEN_HOME%\data"
if not exist "%DATA%\tmp\" mkdir "%DATA%\tmp"
if not exist "%DATA%\java\" mkdir "%DATA%\java"
set "TEMP=%DATA%\tmp"
set "TMP=%DATA%\tmp"
set "PYTHONNOUSERSITE=1"
set "PYTHONDONTWRITEBYTECODE=1"
rem No outer quotes on this line: each path is its own quoted region, so "&" or ")" in a folder
rem name stays literal. The JVM accepts quoted values in JAVA_TOOL_OPTIONS.
set JAVA_TOOL_OPTIONS=-Djava.util.prefs.userRoot="%DATA%\java" -Djava.util.prefs.systemRoot="%DATA%\java" -Djava.io.tmpdir="%DATA%\tmp" -XX:-UsePerfData

rem Bundled Python first; a source checkout without runtime\ falls back to Python 3.12 on PATH.
set "PYEXE=%BALEEN_HOME%\runtime\python\python.exe"
set "PYARGS="
if exist "%PYEXE%" goto :python_ready
echo runtime\ not found: development mode, using Python from PATH (3.12 required).
set "PYEXE="
where py >nul 2>nul
if not errorlevel 1 (set "PYEXE=py" & set "PYARGS=-3.12")
if defined PYEXE goto :python_ready
where python >nul 2>nul
if not errorlevel 1 set "PYEXE=python"
if defined PYEXE goto :python_ready
where python3 >nul 2>nul
if not errorlevel 1 set "PYEXE=python3"
if not defined PYEXE goto :nopython
:python_ready

rem R-02: Java (and so veraPDF) cannot start from a folder whose path has characters outside the
rem Windows code page. Warn, but start anyway: everything except PDF/A validation still works.
"%PYEXE%" %PYARGS% -I -S -c "import os; os.environ['BALEEN_HOME'].encode('mbcs', 'strict')" >nul 2>nul
if errorlevel 1 call :warn_ansi

echo Starting Baleen from "%BALEEN_HOME%"
echo Close this window to stop Baleen.
echo.
"%PYEXE%" %PYARGS% -m baleen serve
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" goto :end
echo.
echo Baleen stopped with exit code %RC%. The messages above say why.
pause
goto :end

:warn_ansi
echo WARNING: this folder's path contains characters that this computer's Windows code page
echo cannot represent, so the bundled Java cannot start and PDF/A validation (veraPDF) will
echo not run: new PDFs will be marked Needs review. Move the Baleen folder to a path with only
echo Latin letters and digits, for example C:\Baleen, to fix this.
echo.
exit /b 0

:unc
echo Baleen cannot start from a network path:
echo   "%HERE%"
echo Copy the Baleen folder to a local disk, then start it from there.
echo.
pause
exit /b 3

:nofolder
echo Cannot open the folder "%HERE%".
pause
exit /b 3

:nopython
echo runtime\ is missing and no Python 3.12 was found on PATH.
echo Use the complete Baleen folder from the release zip.
pause
exit /b 3

:end
endlocal & exit /b %RC%
