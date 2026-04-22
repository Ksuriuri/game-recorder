@echo off
setlocal enableextensions enabledelayedexpansion
chcp 65001 >nul

REM ============================================================
REM   Game Recorder - Offline Portable Bundle Builder
REM
REM   Run this on a Windows box WITH internet (e.g. your dev laptop).
REM   Output: game-recorder-portable-YYYYMMDD.zip in the project root.
REM
REM   Workflow at the cafe:
REM     1) Copy the zip onto a USB stick.
REM     2) On the cafe PC, extract the zip into D:\game-recorder
REM        (or any folder NOT on the system drive — see install.bat warning).
REM     3) Double-click install.bat       (~10 s, no network).
REM     4) Double-click run.bat           (start recording).
REM
REM   What goes into the zip:
REM     .tools\          uv.exe + managed Python 3.11 + uv cache
REM     ffmpeg\          BtbN gpl FFmpeg (NVENC + libx264 + dshow)
REM     wheels\          pre-downloaded dependency wheels (numpy, opencv-headless,
REM                      dxcam, soundcard, cffi, pycparser …)
REM     src\, pyproject.toml, install.bat, README.md
REM
REM   What is NOT shipped:
REM     .venv\           path-bound; install.bat recreates it offline from wheels\
REM     recordings\      user data
REM     this script and any *.zip
REM ============================================================

cd /d "%~dp0\.."
set "PROJECT_DIR=%CD%"
set "WHEELS_DIR=%PROJECT_DIR%\wheels"
set "VENV_DIR=%PROJECT_DIR%\.venv"
set "TOOLS_DIR=%PROJECT_DIR%\.tools"
set "UV_EXE=%TOOLS_DIR%\uv\uv.exe"

echo ============================================================
echo   Building offline portable bundle
echo   Project: %PROJECT_DIR%
echo ============================================================
echo.

REM ----------------------------------------------------------------
REM  Step 1: Run install.bat in ONLINE mode to materialise:
REM    - .tools\uv\uv.exe
REM    - .tools\python\<managed cpython 3.11>\
REM    - ffmpeg\bin\ffmpeg.exe
REM    - .venv\ + populated .tools\uv-cache\
REM
REM  install.bat is idempotent: it skips any download whose target
REM  already exists, so re-running this script is cheap.
REM ----------------------------------------------------------------
echo [1/4] Running install.bat (online) to populate uv / Python / FFmpeg / cache ...
if exist "%WHEELS_DIR%" (
    echo       Removing stale wheels\ so we get a clean re-download.
    rmdir /s /q "%WHEELS_DIR%"
)
call "%PROJECT_DIR%\install.bat"
if errorlevel 1 (
    echo.
    echo [ERROR] install.bat failed. Aborting bundle build.
    exit /b 1
)

if not exist "%UV_EXE%"           goto :missing_uv
if not exist "%VENV_DIR%\Scripts\python.exe" goto :missing_venv

REM ----------------------------------------------------------------
REM  Step 2: Pre-download every runtime wheel into wheels\ so the
REM  target machine can install fully offline.  We freeze the venv
REM  first to capture exact resolved versions (incl. transitive deps
REM  like cffi/pycparser pulled in by soundcard).
REM ----------------------------------------------------------------
echo.
echo [2/4] Freezing resolved versions and downloading wheels ...
mkdir "%WHEELS_DIR%" >nul 2>&1

set "FREEZE_FILE=%PROJECT_DIR%\.tools\bundle-freeze.txt"
"%UV_EXE%" pip freeze --python "%VENV_DIR%\Scripts\python.exe" --exclude-editable > "%FREEZE_FILE%"
if errorlevel 1 (
    echo [ERROR] uv pip freeze failed.
    exit /b 1
)

REM uv has no `pip download` (see uv pip --help). Bootstrap pip into the venv, then use pip.
"%UV_EXE%" pip install --python "%VENV_DIR%\Scripts\python.exe" pip
if errorlevel 1 (
    echo [ERROR] Could not install pip into venv for wheel download.
    exit /b 1
)
"%VENV_DIR%\Scripts\python.exe" -m pip download -d "%WHEELS_DIR%" -r "%FREEZE_FILE%"
if errorlevel 1 (
    echo [ERROR] pip download failed; wheels\ may be incomplete.
    exit /b 1
)

REM Offline `uv pip install -e .` needs pyproject build-system deps plus uv's editable helper.
REM   hatchling -> packaging, pathspec, pluggy, trove-classifiers
REM   editables   -> required by uv when installing -e from a local path in isolation
echo       Also downloading hatchling + editables ^(+ deps^) for offline editable installs ...
"%VENV_DIR%\Scripts\python.exe" -m pip download -d "%WHEELS_DIR%" hatchling "editables>=0.3,<1"
if errorlevel 1 (
    echo [ERROR] pip download hatchling/editables failed.
    exit /b 1
)

REM Sanity check: must contain at least one wheel for each direct dep.
for %%P in (numpy opencv_python_headless dxcam soundcard hatchling editables) do (
    dir /b "%WHEELS_DIR%\%%P-*.whl" >nul 2>&1 || (
        echo [ERROR] No wheel found for %%P in wheels\.  Bundle would be unusable.
        exit /b 1
    )
)
echo       Wheels staged in: %WHEELS_DIR%

REM ----------------------------------------------------------------
REM  Step 3: Drop the path-bound venv.  install.bat on the target
REM  machine will recreate it from wheels\ in a few seconds.
REM ----------------------------------------------------------------
echo.
echo [3/4] Removing path-bound .venv\ (will be rebuilt offline on target) ...
if exist "%VENV_DIR%" rmdir /s /q "%VENV_DIR%"

REM ----------------------------------------------------------------
REM  Step 4: Pack everything we need into a single zip.
REM
REM  Compress-Archive is built into PowerShell 5.1+ on every Windows
REM  10/11 box, so this script needs no extra tooling.  It does NOT
REM  preserve permissions, but for our payload (binaries + scripts)
REM  Windows doesn't need exec bits anyway.
REM ----------------------------------------------------------------
echo.
echo [4/4] Compressing bundle ...

for /f %%D in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set "DATESTAMP=%%D"
set "BUNDLE=%PROJECT_DIR%\game-recorder-portable-%DATESTAMP%.zip"
if exist "%BUNDLE%" del /q "%BUNDLE%"

REM Per-item array because Compress-Archive otherwise drags in the project root
REM as a parent directory, which makes the unzipped layout one level too deep.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ErrorActionPreference='Stop';" ^
    "$items = @('.tools','ffmpeg','wheels','src','scripts','pyproject.toml','install.bat','README.md') | Where-Object { Test-Path $_ };" ^
    "Compress-Archive -Path $items -DestinationPath '%BUNDLE%' -CompressionLevel Optimal -Force"
if errorlevel 1 (
    echo [ERROR] Compress-Archive failed.
    exit /b 1
)

for %%S in ("%BUNDLE%") do set "BUNDLE_SIZE=%%~zS"
set /a BUNDLE_MB=%BUNDLE_SIZE% / 1048576

echo.
echo ============================================================
echo   Bundle built successfully
echo ============================================================
echo   File : %BUNDLE%
echo   Size : %BUNDLE_MB% MB
echo.
echo   Cafe deployment:
echo     1) Copy the zip to D:\ on the target PC ^(NOT C:\^)
echo     2) Right-click -^> Extract All
echo     3) Double-click install.bat   ^(~10 s, no internet needed^)
echo     4) Double-click run.bat        ^(Ctrl+F9 to toggle recording^)
echo.
echo   On THIS machine after a build:
echo     .venv\ was removed so the zip does not embed a path-bound venv.
echo     Run install.bat once to recreate .venv\ ^(offline from wheels\ if present^).
echo ============================================================
exit /b 0


:missing_uv
echo [ERROR] %UV_EXE% missing after install.bat. Aborting.
exit /b 1

:missing_venv
echo [ERROR] %VENV_DIR%\Scripts\python.exe missing after install.bat. Aborting.
exit /b 1
