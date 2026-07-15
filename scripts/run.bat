@echo off
setlocal EnableExtensions
chcp 65001 >nul

cd /d "%~dp0"
set "PROJECT_DIR=%CD%"
set "RECORDER_EXE=%PROJECT_DIR%\.venv\Scripts\game-recorder.exe"
set "PATH=%PROJECT_DIR%\ffmpeg\bin;%PROJECT_DIR%\.venv\Scripts;%PATH%"
set "PYTHONPATH=%PROJECT_DIR%\src"
set "SHOW_CONSOLE=0"
set "FORWARD_ARGS="

:parse_args
if "%~1"=="" goto :launch
if /I "%~1"=="--console" (
    set "SHOW_CONSOLE=1"
) else (
    if /I "%~1"=="--list-audio-devices" set "SHOW_CONSOLE=1"
    if /I "%~1"=="--no-overlay" set "SHOW_CONSOLE=1"
    set FORWARD_ARGS=%FORWARD_ARGS% "%~1"
)
shift
goto :parse_args

:launch
if not exist "%RECORDER_EXE%" (
    echo [错误] 未找到 %RECORDER_EXE%
    echo        请先运行项目根目录 install.bat。
    pause
    exit /b 1
)

if "%SHOW_CONSOLE%"=="1" (
    "%RECORDER_EXE%" %FORWARD_ARGS%
    exit /b %ERRORLEVEL%
)

cscript //nologo "%PROJECT_DIR%\scripts\launch_background.vbs" %FORWARD_ARGS%
exit /b %ERRORLEVEL%
