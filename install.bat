@echo off
setlocal enableextensions
chcp 65001 >nul

cd /d "%~dp0"
set "PROJECT_DIR=%~dp0"
if "%PROJECT_DIR:~-1%"=="\" set "PROJECT_DIR=%PROJECT_DIR:~0,-1%"

REM ---- All paths are project-local, nothing goes to system drive ----
set "TOOLS_DIR=%PROJECT_DIR%\.tools"
set "UV_DIR=%TOOLS_DIR%\uv"
set "UV_EXE=%UV_DIR%\uv.exe"
set "FFMPEG_DIR=%PROJECT_DIR%\ffmpeg"
set "FFMPEG_EXE=%FFMPEG_DIR%\bin\ffmpeg.exe"
set "VENV_DIR=%PROJECT_DIR%\.venv"
set "WHEELS_DIR=%PROJECT_DIR%\wheels"

REM ---- Redirect uv state into project dir (avoid %LOCALAPPDATA%, %APPDATA%) ----
set "UV_CACHE_DIR=%TOOLS_DIR%\uv-cache"
set "UV_PYTHON_INSTALL_DIR=%TOOLS_DIR%\python"
set "UV_TOOL_DIR=%TOOLS_DIR%\uv-tools"
set "UV_TOOL_BIN_DIR=%TOOLS_DIR%\uv-tools\bin"

REM ---- Detect offline portable bundle: presence of wheels/ + pre-baked uv/ffmpeg/python ----
REM  When this script is unzipped from a build_offline_bundle.bat artifact, every dependency
REM  is already on disk; we must NOT touch the network (the target box is typically a 网吧
REM  PC behind a firewall / no proxy).
set "OFFLINE_MODE=0"
if exist "%WHEELS_DIR%" if exist "%UV_EXE%" if exist "%FFMPEG_EXE%" if exist "%UV_PYTHON_INSTALL_DIR%" set "OFFLINE_MODE=1"
if "%OFFLINE_MODE%"=="1" (
    REM Tell uv: never reach out to PyPI or python-build-standalone
    set "UV_OFFLINE=1"
    set "UV_PYTHON_DOWNLOADS=never"
)

echo ============================================================
echo   Game Recorder - Windows One-Click Installer
echo ============================================================
echo   Install location : %PROJECT_DIR%
echo   uv cache / Python: %TOOLS_DIR%
if "%OFFLINE_MODE%"=="1" (
    echo   Mode             : OFFLINE ^(restoring from local wheels/^)
) else (
    echo   Mode             : ONLINE  ^(will download uv / Python / FFmpeg / wheels^)
)
echo   ^(Nothing will be written to your system drive's user dir.^)
echo ============================================================
echo.

REM ---- Warn if installed on the system drive (网吧 still-restore wipes it on reboot) ----
set "PROJECT_DRIVE=%PROJECT_DIR:~0,1%"
set "SYS_DRIVE=%SystemDrive:~0,1%"
if /I "%PROJECT_DRIVE%"=="%SYS_DRIVE%" (
    echo [WARN] Project is on the system drive ^(%SystemDrive%^).
    echo        On internet-cafe / shared PCs with system-restore software
    echo        ^(网吧还原系统 / 影子系统^), every reboot will wipe the install
    echo        AND all recordings under this directory.
    echo        Strongly recommend moving the project to a non-system drive
    echo        ^(e.g. D:\game-recorder^) before continuing.
    echo.
    choice /c YN /n /m "Continue anyway? [Y/N] "
    if errorlevel 2 exit /b 1
    echo.
)

if not exist "%TOOLS_DIR%"           mkdir "%TOOLS_DIR%"
if not exist "%UV_CACHE_DIR%"        mkdir "%UV_CACHE_DIR%"
if not exist "%UV_PYTHON_INSTALL_DIR%" mkdir "%UV_PYTHON_INSTALL_DIR%"

REM ============================================================
REM  Step 1/4: Download uv (standalone, ~15MB)
REM ============================================================
if exist "%UV_EXE%" (
    echo [1/4] uv already present, skipping download.
) else (
    echo [1/4] Downloading uv ...
    if not exist "%UV_DIR%" mkdir "%UV_DIR%"
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "$ProgressPreference='SilentlyContinue';" ^
        "Invoke-WebRequest -Uri 'https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip' -OutFile '%UV_DIR%\uv.zip'"
    if errorlevel 1 goto :fail_download_uv

    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "Expand-Archive -Path '%UV_DIR%\uv.zip' -DestinationPath '%UV_DIR%' -Force"
    del /q "%UV_DIR%\uv.zip" >nul 2>&1

    if not exist "%UV_EXE%" goto :fail_extract_uv
    echo       uv installed: "%UV_EXE%"
)

REM ============================================================
REM  Step 2/4: Install Python 3.11 (into project dir via uv)
REM ============================================================
echo.
echo [2/4] Installing managed Python 3.11 ...
"%UV_EXE%" python install 3.11
if errorlevel 1 goto :fail_install_python

REM ============================================================
REM  Step 3/4: Download FFmpeg (BtbN gpl — encoders: NVENC, libx264, dshow, …)
REM ============================================================
REM  Note: Upstream static win64 FFmpeg (incl. BtbN) often has NO wasapi
REM  *demuxer*; system audio is usually captured via DirectShow (Stereo Mix,
REM  VoiceMeeter route, etc.).  Gyan "essentials" is too stripped; gpl is full.
REM  URL: master-latest; replace with a release addin (e.g. n7.1) if you need
REM  a specific branch (folder name still ffmpeg-* after extract).
REM ============================================================
echo.
if exist "%FFMPEG_EXE%" (
    echo [3/4] FFmpeg already present, skipping download.
) else (
    echo [3/4] Downloading FFmpeg ^(BtbN gpl, ~140MB, NVENC + dshow + libx264^) ...
    set "FFMPEG_ZIP=%TOOLS_DIR%\ffmpeg.zip"
    set "FFMPEG_TMP=%TOOLS_DIR%\ffmpeg-extract"

    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "$ProgressPreference='SilentlyContinue';" ^
        "Invoke-WebRequest -Uri 'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip' -OutFile '%TOOLS_DIR%\ffmpeg.zip'"
    if errorlevel 1 goto :fail_download_ffmpeg

    if exist "%TOOLS_DIR%\ffmpeg-extract" rmdir /s /q "%TOOLS_DIR%\ffmpeg-extract"
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "Expand-Archive -Path '%TOOLS_DIR%\ffmpeg.zip' -DestinationPath '%TOOLS_DIR%\ffmpeg-extract' -Force"

    if exist "%FFMPEG_DIR%" rmdir /s /q "%FFMPEG_DIR%"
    for /d %%D in ("%TOOLS_DIR%\ffmpeg-extract\ffmpeg-*") do (
        move "%%D" "%FFMPEG_DIR%" >nul
    )
    rmdir /s /q "%TOOLS_DIR%\ffmpeg-extract" >nul 2>&1
    del /q "%TOOLS_DIR%\ffmpeg.zip" >nul 2>&1

    if not exist "%FFMPEG_EXE%" goto :fail_extract_ffmpeg
    echo       FFmpeg installed: "%FFMPEG_EXE%"
)

REM ============================================================
REM  Step 4/4: Create venv + install project (editable)
REM
REM  Online: uv resolves + downloads from PyPI, fills %UV_CACHE_DIR%.
REM  Offline (wheels/ present): uv resolves from --find-links wheels/ only,
REM  with --no-index --offline so a missing wheel fails loudly instead of
REM  silently hanging on a connect attempt.
REM ============================================================
echo.
echo [4/4] Creating virtual environment and installing game-recorder ...
"%UV_EXE%" venv --python 3.11 "%VENV_DIR%"
if errorlevel 1 goto :fail_venv

if "%OFFLINE_MODE%"=="1" (
    echo       Offline mode: installing from "%WHEELS_DIR%" ^(no PyPI access^).
    "%UV_EXE%" pip install --offline --no-index --find-links "%WHEELS_DIR%" --python "%VENV_DIR%\Scripts\python.exe" -e .
) else (
    "%UV_EXE%" pip install --python "%VENV_DIR%\Scripts\python.exe" -e .
)
if errorlevel 1 goto :fail_install

REM ============================================================
REM  Generate run.bat (sets PATH so ffmpeg + venv are found)
REM ============================================================
> "%PROJECT_DIR%\run.bat" (
    echo @echo off
    echo setlocal
    echo cd /d "%%~dp0"
    echo set "PATH=%%~dp0ffmpeg\bin;%%~dp0.venv\Scripts;%%PATH%%"
    echo game-recorder %%*
    echo endlocal
)

echo.
echo ============================================================
echo   Installation complete!
echo ============================================================
echo   Start recording  :  run.bat
echo   No-hotkey mode   :  run.bat --no-hotkey
echo   Custom params    :  run.bat --fps 60 --quality 18
echo ============================================================
echo.
pause
exit /b 0


:fail_download_uv
echo.
echo [ERROR] Failed to download uv. Check your internet / proxy and retry.
pause & exit /b 1

:fail_extract_uv
echo.
echo [ERROR] uv archive extracted but uv.exe not found.
pause & exit /b 1

:fail_install_python
echo.
echo [ERROR] uv python install failed.
pause & exit /b 1

:fail_download_ffmpeg
echo.
echo [ERROR] Failed to download FFmpeg from github.com/BtbN. Check your internet / proxy and retry.
pause & exit /b 1

:fail_extract_ffmpeg
echo.
echo [ERROR] FFmpeg extracted but ffmpeg.exe not found.
pause & exit /b 1

:fail_venv
echo.
echo [ERROR] Failed to create virtual environment.
pause & exit /b 1

:fail_install
echo.
echo [ERROR] uv pip install -e . failed.
pause & exit /b 1
