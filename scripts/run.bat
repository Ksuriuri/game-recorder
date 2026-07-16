@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul

cd /d "%~dp0"
REM Template lives in scripts\; install.bat copies it to project root.
if not exist "%CD%\.venv\Scripts\game-recorder.exe" (
    if exist "%CD%\..\.venv\Scripts\game-recorder.exe" cd /d "%CD%\.."
)
set "PROJECT_DIR=%CD%"
set "RECORDER_EXE=%PROJECT_DIR%\.venv\Scripts\game-recorder.exe"
set "PATH=%PROJECT_DIR%\ffmpeg\bin;%PROJECT_DIR%\.venv\Scripts;%PATH%"
set "PYTHONPATH=%PROJECT_DIR%\src"
set "SHOW_CONSOLE=0"
set "SKIP_ID_PROMPT=0"
set "FORWARD_ARGS="

:parse_args
if "%~1"=="" goto :launch
if /I "%~1"=="--console" (
    set "SHOW_CONSOLE=1"
) else (
    if /I "%~1"=="--list-audio-devices" (
        set "SHOW_CONSOLE=1"
        set "SKIP_ID_PROMPT=1"
    )
    if /I "%~1"=="--no-overlay" set "SHOW_CONSOLE=1"
    if /I "%~1"=="--help" (
        set "SHOW_CONSOLE=1"
        set "SKIP_ID_PROMPT=1"
    )
    if /I "%~1"=="-h" (
        set "SHOW_CONSOLE=1"
        set "SKIP_ID_PROMPT=1"
    )
    set "ARG=%~1"
    if /I "!ARG:~0,14!"=="--recording-id" set "SKIP_ID_PROMPT=1"
    set FORWARD_ARGS=!FORWARD_ARGS! "%~1"
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

if "%SKIP_ID_PROMPT%"=="0" (
    call :prompt_recording_id
    if errorlevel 1 (
        pause
        exit /b 1
    )
    set FORWARD_ARGS=!FORWARD_ARGS! --recording-id=!RECORDING_ID!
)

if "%SHOW_CONSOLE%"=="1" (
    "%RECORDER_EXE%" !FORWARD_ARGS!
    exit /b %ERRORLEVEL%
)

cscript //nologo "%PROJECT_DIR%\scripts\launch_background.vbs" !FORWARD_ARGS!
exit /b %ERRORLEVEL%

:prompt_recording_id
set "ID_EMPTY_TRIES=0"
:prompt_recording_id_retry
set "RECORDING_ID="
set /p "RECORDING_ID=请输入录制 ID（字母、数字和连字符 -，不能为空）: "
if not defined RECORDING_ID (
    set /a ID_EMPTY_TRIES+=1
    if !ID_EMPTY_TRIES! GEQ 5 (
        echo [错误] 未输入录制 ID，已取消。
        exit /b 1
    )
    echo [错误] 录制 ID 不能为空，请重新输入。
    goto :prompt_recording_id_retry
)
"%PROJECT_DIR%\.venv\Scripts\python.exe" -c "import re,sys; sys.exit(0 if re.fullmatch(r'[A-Za-z0-9-]+', sys.argv[1]) else 1)" "!RECORDING_ID!"
if errorlevel 1 (
    echo [错误] 录制 ID 只能包含字母、数字和连字符 -，请重新输入。
    goto :prompt_recording_id_retry
)
exit /b 0
