@echo off
setlocal enableextensions
cd /d "%~dp0"

set "PACK_DIR=%~dp0"
if "%PACK_DIR:~-1%"=="\" set "PACK_DIR=%PACK_DIR:~0,-1%"

for %%I in ("%PACK_DIR%\..") do set "GAME_ROOT=%%~fI"

set "UV_EXE=%GAME_ROOT%\.tools\uv\uv.exe"
set "TOOLS_DIR=%GAME_ROOT%\.tools"
set "UV_CACHE_DIR=%TOOLS_DIR%\uv-cache"
set "UV_PYTHON_INSTALL_DIR=%TOOLS_DIR%\python"
set "WHEELS_DIR=%PACK_DIR%\wheels"
set "VENV_DIR=%PACK_DIR%\.venv"
set "PYTHON_EXE=%VENV_DIR%\Scripts\python.exe"
set "REQUIREMENTS=%PACK_DIR%\requirements.txt"

REM Offline only when the wheel set looks complete. A lone modelscope-*.whl
REM (or wheels missing colorama) would otherwise fail on Windows with:
REM   no versions of colorama{sys_platform == 'win32'} / tqdm cannot be used
set "OFFLINE_MODE=0"
if exist "%WHEELS_DIR%\modelscope-*.whl" if exist "%WHEELS_DIR%\colorama-*.whl" if exist "%WHEELS_DIR%\tqdm-*.whl" if exist "%UV_EXE%" (
    set "OFFLINE_MODE=1"
)
if "%OFFLINE_MODE%"=="1" (
    set "UV_OFFLINE=1"
    set "UV_PYTHON_DOWNLOADS=never"
) else (
    set "UV_OFFLINE="
    set "UV_PYTHON_DOWNLOADS="
    if exist "%WHEELS_DIR%\modelscope-*.whl" (
        if not defined MODELSCOPE_UPLOAD_QUIET (
            echo WARNING: modelscope-upload\wheels is incomplete ^(need colorama + tqdm^).
            echo Falling back to online install. Re-run build_bundle.bat to refresh wheels.
        )
    )
)

if not exist "%UV_EXE%" (
    echo ERROR: Missing "%UV_EXE%"
    echo Run install.bat in the game-recorder project root first.
    goto :fail
)

set "MANAGED_PYTHON_EXE="
for /d %%D in ("%UV_PYTHON_INSTALL_DIR%\cpython-3.11.*-windows-*") do (
    if exist "%%D\python.exe" if not defined MANAGED_PYTHON_EXE set "MANAGED_PYTHON_EXE=%%D\python.exe"
)
if not defined MANAGED_PYTHON_EXE (
    echo ERROR: Managed Python 3.11 not found under "%UV_PYTHON_INSTALL_DIR%"
    goto :fail
)

if exist "%PYTHON_EXE%" (
    "%PYTHON_EXE%" "%PACK_DIR%\check_requirements.py" 2>nul
    if not errorlevel 1 goto :done
)

if not defined MODELSCOPE_UPLOAD_QUIET echo Installing modelscope upload environment...

if not exist "%VENV_DIR%\Scripts\python.exe" (
    "%UV_EXE%" venv --python "%MANAGED_PYTHON_EXE%" "%VENV_DIR%"
    if errorlevel 1 goto :fail
)

if not exist "%REQUIREMENTS%" (
    echo ERROR: Missing "%REQUIREMENTS%"
    goto :fail
)

if "%OFFLINE_MODE%"=="1" (
    "%UV_EXE%" pip install --offline --no-index --find-links "%WHEELS_DIR%" --python "%PYTHON_EXE%" -r "%REQUIREMENTS%"
) else (
    "%UV_EXE%" pip install --python "%PYTHON_EXE%" -r "%REQUIREMENTS%"
)
if errorlevel 1 goto :fail

"%PYTHON_EXE%" "%PACK_DIR%\check_requirements.py" 2>nul
if errorlevel 1 goto :fail

:done
exit /b 0

:fail
if not defined MODELSCOPE_UPLOAD_SKIP_PAUSE (
    echo.
    echo Press any key to close...
    pause >nul
)
exit /b 1
