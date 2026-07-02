@echo off
setlocal EnableExtensions
chcp 65001 >nul

cd /d "%~dp0"
set "PACK_DIR=%~dp0"
if "%PACK_DIR:~-1%"=="\" set "PACK_DIR=%PACK_DIR:~0,-1%"

set "PYTHON_EXE=%PACK_DIR%\.venv\Scripts\python.exe"
set "SAMPLE_SCRIPT=%PACK_DIR%\sample_recordings.py"
set "MODELSCOPE_LOG_LEVEL=40"
set "MODELSCOPE_CACHE=%PACK_DIR%\.cache"
set "TMP=%PACK_DIR%\.cache\tmp"
set "TEMP=%PACK_DIR%\.cache\tmp"

if not exist "%MODELSCOPE_CACHE%\tmp" mkdir "%MODELSCOPE_CACHE%\tmp"

if not exist "%SAMPLE_SCRIPT%" (
    echo ERROR: missing "%SAMPLE_SCRIPT%"
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

echo.
echo Note: meta.json reads use more threads; video downloads use 6 by default.
echo       Override: sample.bat 20260701 --download-workers 4
echo.

"%PYTHON_EXE%" "%SAMPLE_SCRIPT%" %*
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
