@echo off
setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0"
set "PROJECT_DIR=%~dp0"
if "%PROJECT_DIR:~-1%"=="\" set "PROJECT_DIR=%PROJECT_DIR:~0,-1%"

set "RECORDINGS=%PROJECT_DIR%\recordings"
set "PYTHON_EXE=%PROJECT_DIR%\.venv\Scripts\python.exe"
set "UV_EXE=%PROJECT_DIR%\.tools\uv\uv.exe"
set "UPLOAD_SCRIPT=%PROJECT_DIR%\scripts\upload_recordings.py"

if not exist "%RECORDINGS%\" (
    echo ERROR: recordings folder not found: "%RECORDINGS%"
    goto :fail
)

if not exist "%PYTHON_EXE%" (
    echo ERROR: .venv not found. Run the project installer first.
    goto :fail
)

if not exist "%UPLOAD_SCRIPT%" (
    echo ERROR: upload script not found: "%UPLOAD_SCRIPT%"
    goto :fail
)

set "MODELSCOPE_LOG_LEVEL=40"

"%PYTHON_EXE%" -c "import modelscope" 2>nul
if errorlevel 1 (
    echo Installing modelscope...
    if exist "%UV_EXE%" (
        set "UV_CACHE_DIR=%PROJECT_DIR%\.tools\uv-cache"
        set "UV_PYTHON_INSTALL_DIR=%PROJECT_DIR%\.tools\python"
        "%UV_EXE%" pip install modelscope --python "%PYTHON_EXE%"
    ) else (
        "%PYTHON_EXE%" -m pip install -q modelscope
    )
    if errorlevel 1 (
        echo ERROR: failed to install modelscope.
        goto :fail
    )
)

"%PYTHON_EXE%" "%UPLOAD_SCRIPT%" "%RECORDINGS%"
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" goto :fail
goto :end

:fail
echo.
echo Press any key to close...
pause >nul
exit /b 1

:end
echo.
echo Press any key to close...
pause >nul
exit /b 0
