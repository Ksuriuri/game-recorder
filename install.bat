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

REM ---- Redirect uv state into project dir (avoid %LOCALAPPDATA%, %APPDATA%) ----
set "UV_CACHE_DIR=%TOOLS_DIR%\uv-cache"
set "UV_PYTHON_INSTALL_DIR=%TOOLS_DIR%\python"
set "UV_TOOL_DIR=%TOOLS_DIR%\uv-tools"
set "UV_TOOL_BIN_DIR=%TOOLS_DIR%\uv-tools\bin"

echo ============================================================
echo   Game Recorder - Windows One-Click Installer
echo ============================================================
echo   Install location : %PROJECT_DIR%
echo   uv cache / Python: %TOOLS_DIR%
echo   (Nothing will be written to your system drive's user dir.)
echo ============================================================
echo.

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
REM  Step 3/4: Download FFmpeg (essentials build)
REM ============================================================
echo.
if exist "%FFMPEG_EXE%" (
    echo [3/4] FFmpeg already present, skipping download.
) else (
    echo [3/4] Downloading FFmpeg ^(essentials build, ~80MB^) ...
    set "FFMPEG_ZIP=%TOOLS_DIR%\ffmpeg.zip"
    set "FFMPEG_TMP=%TOOLS_DIR%\ffmpeg-extract"

    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "$ProgressPreference='SilentlyContinue';" ^
        "Invoke-WebRequest -Uri 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' -OutFile '%TOOLS_DIR%\ffmpeg.zip'"
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
REM ============================================================
echo.
echo [4/4] Creating virtual environment and installing game-recorder ...
"%UV_EXE%" venv --python 3.11 "%VENV_DIR%"
if errorlevel 1 goto :fail_venv

"%UV_EXE%" pip install --python "%VENV_DIR%\Scripts\python.exe" -e .
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
echo [ERROR] Failed to download FFmpeg from gyan.dev. Check your internet / proxy and retry.
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
