@echo off
setlocal EnableExtensions

cd /d "%~dp0"
set "PACK_DIR=%~dp0"
if "%PACK_DIR:~-1%"=="\" set "PACK_DIR=%PACK_DIR:~0,-1%"

for %%I in ("%PACK_DIR%\..") do set "GAME_ROOT=%%~fI"

set "RECORDINGS=%GAME_ROOT%\recordings"
set "PYTHON_EXE=%PACK_DIR%\.venv\Scripts\python.exe"
set "UPLOAD_SCRIPT=%PACK_DIR%\upload_recordings.py"
set "CRED_ZIP=%PACK_DIR%\oss_credentials.zip"
set "CRED_JSON=%PACK_DIR%\oss_credentials.json"

if not exist "%RECORDINGS%\" (
    echo ERROR: recordings not found: "%RECORDINGS%"
    goto :fail
)

if not exist "%UPLOAD_SCRIPT%" (
    echo ERROR: missing "%UPLOAD_SCRIPT%"
    goto :fail
)

REM Extract OSS keys from zip next to this script (zip is not committed to git).
if exist "%CRED_ZIP%" (
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
      "Expand-Archive -LiteralPath '%CRED_ZIP%' -DestinationPath '%PACK_DIR%' -Force"
    if errorlevel 1 (
        echo ERROR: failed to extract "%CRED_ZIP%"
        goto :fail
    )
)

if not exist "%CRED_JSON%" (
    echo ERROR: missing OSS credentials.
    echo Put oss_credentials.zip in this folder, then run upload.bat again.
    goto :fail
)

set "S3_UPLOAD_QUIET=1"
call "%PACK_DIR%\install.bat"
set "INSTALL_CODE=%ERRORLEVEL%"
set "S3_UPLOAD_QUIET="
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
