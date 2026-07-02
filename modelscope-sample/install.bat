@echo off
setlocal enableextensions
cd /d "%~dp0"

set "PACK_DIR=%~dp0"
if "%PACK_DIR:~-1%"=="\" set "PACK_DIR=%PACK_DIR:~0,-1%"

for %%I in ("%PACK_DIR%\..") do set "GAME_ROOT=%%~fI"

set "TOOLS_DIR=%PACK_DIR%\.tools"
if exist "%GAME_ROOT%\.tools\uv\uv.exe" if not exist "%TOOLS_DIR%\uv\uv.exe" (
    set "TOOLS_DIR=%GAME_ROOT%\.tools"
)

set "UV_EXE=%TOOLS_DIR%\uv\uv.exe"
if exist "%GAME_ROOT%\.tools\uv-cache" (
    set "UV_CACHE_DIR=%GAME_ROOT%\.tools\uv-cache"
) else (
    set "UV_CACHE_DIR=%TOOLS_DIR%\uv-cache"
)
set "UV_PYTHON_INSTALL_DIR=%TOOLS_DIR%\python"
set "WHEELS_DIR=%PACK_DIR%\wheels"
set "VENV_DIR=%PACK_DIR%\.venv"
set "PYTHON_EXE=%VENV_DIR%\Scripts\python.exe"

set "OFFLINE_MODE=0"
if exist "%WHEELS_DIR%\modelscope-*.whl" if exist "%UV_EXE%" set "OFFLINE_MODE=1"
if "%OFFLINE_MODE%"=="1" (
    set "UV_OFFLINE=1"
    set "UV_PYTHON_DOWNLOADS=never"
)

if not exist "%UV_EXE%" (
    echo ERROR: Missing uv.exe
    echo.
    echo Portable bundle: extract the full zip so .tools\ exists inside this folder.
    echo Dev machine: run install.bat in game-recorder root, then build_bundle.bat here.
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
    "%PYTHON_EXE%" -c "import modelscope" 2>nul
    if not errorlevel 1 goto :done
)

if not defined MODELSCOPE_SAMPLE_QUIET echo Installing modelscope sample environment...

if not exist "%VENV_DIR%\Scripts\python.exe" (
    "%UV_EXE%" venv --python "%MANAGED_PYTHON_EXE%" "%VENV_DIR%"
    if errorlevel 1 goto :fail
)

if "%OFFLINE_MODE%"=="1" (
    "%UV_EXE%" pip install --offline --no-index --find-links "%WHEELS_DIR%" --python "%PYTHON_EXE%" modelscope
) else (
    "%UV_EXE%" pip install --python "%PYTHON_EXE%" modelscope
)
if errorlevel 1 goto :fail

"%PYTHON_EXE%" -c "import modelscope" 2>nul
if errorlevel 1 goto :fail

:done
exit /b 0

:fail
if not defined MODELSCOPE_SAMPLE_SKIP_PAUSE (
    echo.
    echo Press any key to close...
    pause >nul
)
exit /b 1
