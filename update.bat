@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

set "ZIP=%ROOT%\s3-upload-secrets.zip"
set "DEST=%ROOT%\s3-upload"
set "TMP=%ROOT%\.tools\s3-upload-secrets-extract"

if not exist "%ZIP%" (
    echo ERROR: 找不到密钥包 "%ZIP%"
    echo.
    echo 请把 s3-upload-secrets.zip 放到项目根目录后再运行本脚本。
    echo 该压缩包不进 git，需单独拷贝分发。
    goto :fail
)

if not exist "%DEST%\" (
    echo ERROR: 找不到 s3-upload 目录："%DEST%"
    echo 请先从仓库拉取/解压完整项目。
    goto :fail
)

echo 正在解压 OSS 密钥到 s3-upload\ ...
if exist "%TMP%" rmdir /s /q "%TMP%"
mkdir "%ROOT%\.tools" >nul 2>&1
mkdir "%TMP%" >nul 2>&1

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Expand-Archive -LiteralPath '%ZIP%' -DestinationPath '%TMP%' -Force"
if errorlevel 1 (
    echo ERROR: 解压失败
    goto :fail
)

set "FOUND="
if exist "%TMP%\oss_credentials.json" (
    copy /Y "%TMP%\oss_credentials.json" "%DEST%\oss_credentials.json" >nul
    set "FOUND=1"
)
if exist "%TMP%\s3-upload\oss_credentials.json" (
    copy /Y "%TMP%\s3-upload\oss_credentials.json" "%DEST%\oss_credentials.json" >nul
    set "FOUND=1"
)

if not defined FOUND (
    echo ERROR: 压缩包内未找到 oss_credentials.json
    goto :fail
)

if exist "%TMP%" rmdir /s /q "%TMP%"

echo 完成：已写入 "%DEST%\oss_credentials.json"
echo 现在可以运行 s3-upload\upload.bat
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
