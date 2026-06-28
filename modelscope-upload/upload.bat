@echo off
setlocal EnableExtensions

cd /d "%~dp0"
set "PACK_DIR=%~dp0"
if "%PACK_DIR:~-1%"=="\" set "PACK_DIR=%PACK_DIR:~0,-1%"

for %%I in ("%PACK_DIR%\..") do set "GAME_ROOT=%%~fI"

set "RECORDINGS=%GAME_ROOT%\recordings"
set "PYTHON_EXE=%PACK_DIR%\.venv\Scripts\python.exe"
set "UPLOAD_SCRIPT=%PACK_DIR%\upload_recordings.py"
set "MODELSCOPE_LOG_LEVEL=40"

if not exist "%RECORDINGS%\" (
    echo ERROR: recordings not found: "%RECORDINGS%"
    goto :fail
)

if not exist "%UPLOAD_SCRIPT%" (
    echo ERROR: missing "%UPLOAD_SCRIPT%"
    goto :fail
)

set "MODELSCOPE_UPLOAD_QUIET=1"
call "%PACK_DIR%\install.bat"
set "INSTALL_CODE=%ERRORLEVEL%"
set "MODELSCOPE_UPLOAD_QUIET="
if not "%INSTALL_CODE%"=="0" goto :fail

if not exist "%PYTHON_EXE%" (
    echo ERROR: venv not created. install.bat failed.
    goto :fail
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
