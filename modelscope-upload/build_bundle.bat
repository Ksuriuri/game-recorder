@echo off
setlocal EnableExtensions

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
set "REQUIREMENTS=%PACK_DIR%\requirements.txt"

if not exist "%UV_EXE%" (
    echo ERROR: Run install.bat in the game-recorder root first.
    goto :fail
)

echo ============================================================
echo   Build ModelScope upload offline bundle
echo   Pack dir: %PACK_DIR%
echo ============================================================
echo.

echo [1/3] Download pinned modelscope wheels ...
if not exist "%REQUIREMENTS%" (
    echo ERROR: Missing "%REQUIREMENTS%"
    goto :fail
)
if exist "%WHEELS_DIR%" rmdir /s /q "%WHEELS_DIR%"
mkdir "%WHEELS_DIR%"

set "MANAGED_PYTHON_EXE="
for /d %%D in ("%UV_PYTHON_INSTALL_DIR%\cpython-3.11.*-windows-*") do (
    if exist "%%D\python.exe" if not defined MANAGED_PYTHON_EXE set "MANAGED_PYTHON_EXE=%%D\python.exe"
)

if not defined MANAGED_PYTHON_EXE (
    echo ERROR: Python 3.11 not found under "%UV_PYTHON_INSTALL_DIR%"
    goto :fail
)

"%MANAGED_PYTHON_EXE%" -m pip download -r "%REQUIREMENTS%" -d "%WHEELS_DIR%"
if errorlevel 1 goto :fail_wheels

echo.
echo [2/3] Verify offline install ...
if exist "%VENV_DIR%" rmdir /s /q "%VENV_DIR%"
set "MODELSCOPE_UPLOAD_QUIET=1"
call "%PACK_DIR%\install.bat"
set "MODELSCOPE_UPLOAD_QUIET="
if errorlevel 1 goto :fail_install

echo.
echo [3/3] Create zip ...
if exist "%VENV_DIR%" rmdir /s /q "%VENV_DIR%"

for /f %%D in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set "STAMP=%%D"
set "ZIP_NAME=modelscope-upload-portable-%STAMP%.zip"
set "ZIP_PATH=%GAME_ROOT%\%ZIP_NAME%"
set "ZIP_TMP=%TOOLS_DIR%\modelscope-upload-%STAMP%.zip"

if exist "%ZIP_PATH%" del /q "%ZIP_PATH%" 2>nul
if exist "%ZIP_TMP%" del /q "%ZIP_TMP%" 2>nul

powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; Compress-Archive -LiteralPath '%PACK_DIR%' -DestinationPath '%ZIP_TMP%' -CompressionLevel Optimal -Force"
if errorlevel 1 goto :fail_zip

move /Y "%ZIP_TMP%" "%ZIP_PATH%" >nul
if errorlevel 1 goto :fail_move

echo.
echo ============================================================
echo   Done
echo ============================================================
echo   Zip: %ZIP_PATH%
echo.
echo   Extract zip into game-recorder root, then run:
echo   modelscope-upload\upload.bat
echo.
echo   Read the usage txt file in modelscope-upload folder.
echo ============================================================
echo.
pause
exit /b 0

:fail_wheels
echo ERROR: wheel download failed.
goto :fail

:fail_install
echo ERROR: offline install.bat verification failed.
goto :fail

:fail_zip
echo ERROR: Compress-Archive failed.
goto :fail

:fail_move
echo ERROR: could not move zip to: %ZIP_PATH%
echo Temp file: %ZIP_TMP%
goto :fail

:fail
echo.
pause
exit /b 1
