@echo off
setlocal EnableExtensions
chcp 65001 >nul

cd /d "%~dp0"
set "PACK_DIR=%~dp0"
if "%PACK_DIR:~-1%"=="\" set "PACK_DIR=%PACK_DIR:~0,-1%"

set "PYTHON_EXE=%PACK_DIR%\.venv\Scripts\python.exe"
set "CLEANUP_SCRIPT=%PACK_DIR%\cleanup_short_sessions.py"
set "MODELSCOPE_LOG_LEVEL=40"
set "MODELSCOPE_CACHE=%PACK_DIR%\.cache"
set "TMP=%PACK_DIR%\.cache\tmp"
set "TEMP=%PACK_DIR%\.cache\tmp"

if not exist "%MODELSCOPE_CACHE%\tmp" mkdir "%MODELSCOPE_CACHE%\tmp"

if not exist "%CLEANUP_SCRIPT%" (
    echo ERROR: missing "%CLEANUP_SCRIPT%"
    goto :fail
)

set "MODELSCOPE_SAMPLE_QUIET=1"
call "%PACK_DIR%\install.bat"
set "INSTALL_CODE=%ERRORLEVEL%"
set "MODELSCOPE_SAMPLE_QUIET="
if not "%INSTALL_CODE%"=="0" goto :fail

if not exist "%PYTHON_EXE%" (
    echo ERROR: venv not created. install.bat failed.
    goto :fail
)

where git >nul 2>&1
if errorlevel 1 (
    echo ERROR: Git not found. cleanup.bat needs Git for Windows with Git LFS.
    echo Download: https://git-scm.com/download/win
    goto :fail
)

git lfs version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Git LFS not found. Re-run Git installer and enable Git LFS.
    goto :fail
)

echo.
echo Remove sessions with mp4 total size below 10MB on ModelScope recordings/.
echo Default: delete directly. Preview only with --dry-run
echo Example: cleanup.bat --dry-run
echo.

"%PYTHON_EXE%" "%CLEANUP_SCRIPT%" %*
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
