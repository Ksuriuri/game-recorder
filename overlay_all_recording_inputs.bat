@echo off
chcp 65001 >nul
set "PYTHONUTF8=1"
setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0"

set "OVERLAY_MAX_WIDTH=960"
set "OVERLAY_CRF=26"
set "OVERLAY_PRESET=veryfast"
set "OVERLAY_AUDIO_BITRATE=64k"

set "RECORDINGS=%CD%\recordings"
set "OUTPUT_DIR_NAME=overlay"
set "OUTPUT_DIR=%RECORDINGS%\%OUTPUT_DIR_NAME%"

if not exist "%RECORDINGS%\" (
    echo ERROR: recordings folder not found: "%RECORDINGS%"
    echo 按任意键继续...
    pause >nul
    exit /b 1
)

where uv >nul 2>nul
if not errorlevel 1 (
    set "USE_UV=1"
) else if exist "%CD%\.venv\Scripts\python.exe" (
    set "PYTHON_EXE=%CD%\.venv\Scripts\python.exe"
) else (
    echo ERROR: uv not in PATH and .venv\Scripts\python.exe not found
    echo 按任意键继续...
    pause >nul
    exit /b 1
)

if not exist "%OUTPUT_DIR%\" mkdir "%OUTPUT_DIR%"

echo Processing all videos under recordings
echo Output: "%OUTPUT_DIR%"
echo.

if defined USE_UV (
    uv run python scripts/batch_overlay_inputs.py "%RECORDINGS%" --output-dir "%OUTPUT_DIR%" --exclude-dir %OUTPUT_DIR_NAME% --max-width %OVERLAY_MAX_WIDTH% --crf %OVERLAY_CRF% --preset %OVERLAY_PRESET% --audio-bitrate %OVERLAY_AUDIO_BITRATE%
) else (
    "!PYTHON_EXE!" scripts\batch_overlay_inputs.py "%RECORDINGS%" --output-dir "%OUTPUT_DIR%" --exclude-dir %OUTPUT_DIR_NAME% --max-width %OVERLAY_MAX_WIDTH% --crf %OVERLAY_CRF% --preset %OVERLAY_PRESET% --audio-bitrate %OVERLAY_AUDIO_BITRATE%
)

set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo.
    echo Finished with errors.
    echo 按任意键继续...
    pause >nul
    exit /b %EXIT_CODE%
)

echo.
echo 按任意键继续...
pause >nul
exit /b 0
