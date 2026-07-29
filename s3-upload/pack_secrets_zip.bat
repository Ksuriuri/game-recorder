@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

REM Build s3-upload-secrets.zip in the project root (gitignored).
REM Run on a machine that already has s3-upload\oss_credentials.json.

set "PACK_DIR=%~dp0"
if "%PACK_DIR:~-1%"=="\" set "PACK_DIR=%PACK_DIR:~0,-1%"
for %%I in ("%PACK_DIR%\..") do set "ROOT=%%~fI"

set "CRED=%PACK_DIR%\oss_credentials.json"
set "ZIP=%ROOT%\s3-upload-secrets.zip"
set "TMP=%ROOT%\.tools\s3-upload-secrets-pack"

if not exist "%CRED%" (
    echo ERROR: missing "%CRED%"
    echo Copy oss_credentials.example.json to oss_credentials.json and fill in keys first.
    exit /b 1
)

if exist "%TMP%" rmdir /s /q "%TMP%"
mkdir "%ROOT%\.tools" >nul 2>&1
mkdir "%TMP%" >nul 2>&1
copy /Y "%CRED%" "%TMP%\oss_credentials.json" >nul

if exist "%ZIP%" del /f /q "%ZIP%"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Compress-Archive -LiteralPath '%TMP%\oss_credentials.json' -DestinationPath '%ZIP%' -Force"
if errorlevel 1 (
    echo ERROR: failed to create zip
    exit /b 1
)

rmdir /s /q "%TMP%"
echo Created: %ZIP%
echo Distribute this zip with the project, then run update.bat on target PCs.
exit /b 0
