@echo off
setlocal EnableExtensions

cd /d "%~dp0"
set "PACK_DIR=%~dp0"
if "%PACK_DIR:~-1%"=="\" set "PACK_DIR=%PACK_DIR:~0,-1%"

for %%I in ("%PACK_DIR%\..") do set "GAME_ROOT=%%~fI"

set "GAME_TOOLS=%GAME_ROOT%\.tools"
set "PACK_TOOLS=%PACK_DIR%\.tools"
set "UV_EXE=%GAME_TOOLS%\uv\uv.exe"
set "UV_PYTHON_INSTALL_DIR=%GAME_TOOLS%\python"
set "WHEELS_DIR=%PACK_DIR%\wheels"
set "VENV_DIR=%PACK_DIR%\.venv"

if not exist "%UV_EXE%" (
    echo ERROR: Run install.bat in the game-recorder root first.
    goto :fail
)

echo ============================================================
echo   Build ModelScope sample offline bundle
echo   Pack dir: %PACK_DIR%
echo ============================================================
echo.
echo Before building: close sample.bat and any window using this folder.
echo.

echo [1/4] Download modelscope wheels ...
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

"%MANAGED_PYTHON_EXE%" -m pip download modelscope -d "%WHEELS_DIR%"
if errorlevel 1 goto :fail_wheels

echo.
echo [2/4] Copy portable uv + Python into pack ...

REM .venv locks files under pack\.tools\python - remove it first
if exist "%VENV_DIR%" (
    echo Removing old .venv ...
    rmdir /s /q "%VENV_DIR%"
    if exist "%VENV_DIR%" (
        echo ERROR: Cannot remove "%VENV_DIR%"
        echo Close sample.bat, then retry build_bundle.bat.
        goto :fail
    )
)

if exist "%PACK_TOOLS%" (
    echo Removing old .tools ...
    rmdir /s /q "%PACK_TOOLS%"
    if exist "%PACK_TOOLS%" (
        echo ERROR: Cannot remove "%PACK_TOOLS%"
        echo Close sample.bat / Python windows using this folder, then retry.
        goto :fail
    )
)

mkdir "%PACK_TOOLS%"
if errorlevel 1 goto :fail_tools

robocopy "%GAME_TOOLS%\uv" "%PACK_TOOLS%\uv" /E /NFL /NDL /NJH /NJS /NC /NS /NP >nul
if errorlevel 8 goto :fail_tools
robocopy "%GAME_TOOLS%\python" "%PACK_TOOLS%\python" /E /NFL /NDL /NJH /NJS /NC /NS /NP >nul
if errorlevel 8 goto :fail_tools

echo Copied uv + Python into pack\.tools\

echo.
echo [3/4] Verify offline install ...
set "MODELSCOPE_SAMPLE_QUIET=1"
call "%PACK_DIR%\install.bat"
set "MODELSCOPE_SAMPLE_QUIET="
if errorlevel 1 goto :fail_install

if not exist "%PACK_DIR%\cleanup.bat" goto :fail_missing
if not exist "%PACK_DIR%\cleanup_short_sessions.py" goto :fail_missing
if not exist "%PACK_DIR%\sample.bat" goto :fail_missing
if not exist "%PACK_DIR%\sample_recordings.py" goto :fail_missing

echo.
echo [4/4] Create zip ...
if exist "%VENV_DIR%" rmdir /s /q "%VENV_DIR%"

REM uv install cache is not needed in the portable zip (deep paths break Compress-Archive)
if exist "%PACK_TOOLS%\uv-cache" (
    echo Removing build cache: .tools\uv-cache
    rmdir /s /q "%PACK_TOOLS%\uv-cache"
)

for /f %%D in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set "STAMP=%%D"
set "ZIP_NAME=modelscope-sample-portable-%STAMP%.zip"
set "ZIP_PATH=%GAME_ROOT%\%ZIP_NAME%"
set "ZIP_TMP=%GAME_TOOLS%\modelscope-sample-%STAMP%.zip"
set "STAGING=%TEMP%\modelscope-sample-pack-%STAMP%"

if exist "%ZIP_PATH%" del /q "%ZIP_PATH%" 2>nul
if exist "%ZIP_TMP%" del /q "%ZIP_TMP%" 2>nul
if exist "%STAGING%" rmdir /s /q "%STAGING%" 2>nul

echo Packing offline bundle (scripts + wheels + .tools) ...
echo Excluding: data\  .venv\  uv-cache\  .cache\
if exist "%PACK_DIR%\data\" (
    echo NOTE: data\ contains downloaded samples and is NOT included in the zip.
)

robocopy "%PACK_DIR%" "%STAGING%\modelscope-sample" /E /XD data .venv uv-cache .cache /NFL /NDL /NJH /NJS /NC /NS /NP >nul
if errorlevel 8 goto :fail_zip

echo Compressing about 250 MB, please wait ...
tar -a -cf "%ZIP_TMP%" -C "%STAGING%" modelscope-sample
if errorlevel 1 goto :fail_zip

if exist "%STAGING%" rmdir /s /q "%STAGING%" 2>nul

move /Y "%ZIP_TMP%" "%ZIP_PATH%" >nul
if errorlevel 1 goto :fail_move

echo.
echo ============================================================
echo   Done
echo ============================================================
echo   Zip: %ZIP_PATH%
echo.
echo   On another Windows PC:
echo     1. Extract zip anywhere (e.g. D:\modelscope-sample\)
echo     2. sample.bat   - sample videos to data\YYYYMMDD\
echo     3. cleanup.bat  - remove sessions with mp4 total below 10MB
echo        cleanup.bat needs Git + Git LFS installed on that PC
echo.
echo   Read the usage txt file in modelscope-sample folder.
echo ============================================================
echo.
pause
exit /b 0

:fail_wheels
echo ERROR: wheel download failed.
goto :fail

:fail_tools
echo ERROR: failed to copy uv/python into .tools\
echo If you see "Access denied": close sample.bat and retry.
goto :fail

:fail_install
echo ERROR: offline install.bat verification failed.
goto :fail

:fail_missing
echo ERROR: required pack file missing (sample.bat / cleanup.bat / scripts).
goto :fail

:fail_zip
echo ERROR: tar zip failed.
echo If tar is missing, use Windows 10+ or install tar.exe.
goto :fail

:fail_move
echo ERROR: could not move zip to: %ZIP_PATH%
echo Temp file: %ZIP_TMP%
goto :fail

:fail
echo.
pause
exit /b 1
