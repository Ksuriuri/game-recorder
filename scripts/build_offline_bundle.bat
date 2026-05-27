@echo off
setlocal enableextensions enabledelayedexpansion
chcp 65001 >nul

REM ============================================================
REM   Game Recorder - Offline Portable Bundle Builder
REM
REM   Run this on a Windows box WITH internet (e.g. your dev laptop).
REM   Output: game-recorder-portable-YYYYMMDD.zip in the project root.
REM
REM   Workflow at the cafe:
REM     1) Copy the zip onto a USB stick.
REM     2) On the cafe PC, extract the zip into D:\game-recorder
REM        (or any folder NOT on the system drive — see install.bat warning).
REM     3) Double-click install.bat       (~10 s, no network).
REM     4) Double-click run.bat           (start recording).
REM
REM   What goes into the zip:
REM     .tools\          uv.exe + managed Python 3.11 + uv cache
REM     ffmpeg\          BtbN gpl FFmpeg (NVENC + libx264 + dshow)
REM     wheels\          pre-downloaded dependency wheels (numpy, opencv-headless,
REM                      dxcam, soundcard, cffi, pycparser …)
REM     src\, pyproject.toml, scripts\
REM     根目录全部 *.bat / *.vbs / *.md（install.bat、run.bat、录制操作手册.md 等）
REM
REM   What is NOT shipped:
REM     .venv\           path-bound; install.bat recreates it offline from wheels\
REM     recordings\      user data
REM     this script and any *.zip
REM ============================================================

cd /d "%~dp0\.."
set "PROJECT_DIR=%CD%"
set "WHEELS_DIR=%PROJECT_DIR%\wheels"
set "VENV_DIR=%PROJECT_DIR%\.venv"
set "TOOLS_DIR=%PROJECT_DIR%\.tools"
set "UV_EXE=%TOOLS_DIR%\uv\uv.exe"

echo ============================================================
echo   正在构建离线便携包
echo   项目: %PROJECT_DIR%
echo ============================================================
echo.

REM ----------------------------------------------------------------
REM  Step 1: Run install.bat in ONLINE mode to materialise:
REM    - .tools\uv\uv.exe
REM    - .tools\python\<managed cpython 3.11>\
REM    - ffmpeg\bin\ffmpeg.exe
REM    - .venv\ + populated .tools\uv-cache\
REM
REM  install.bat is idempotent: it skips any download whose target
REM  already exists, so re-running this script is cheap.
REM ----------------------------------------------------------------
echo [1/4] 正在运行 install.bat（在线）以填充 uv / Python / FFmpeg / 缓存 ...
if exist "%WHEELS_DIR%" (
    echo       正在删除旧的 wheels\ 以便重新下载。
    rmdir /s /q "%WHEELS_DIR%"
)
call "%PROJECT_DIR%\install.bat"
if errorlevel 1 (
    echo.
    echo [错误] install.bat 失败。中止打包。
    exit /b 1
)

if not exist "%UV_EXE%"           goto :missing_uv
if not exist "%VENV_DIR%\Scripts\python.exe" goto :missing_venv

REM ----------------------------------------------------------------
REM  Step 2: Pre-download every runtime wheel into wheels\ so the
REM  target machine can install fully offline.  We freeze the venv
REM  first to capture exact resolved versions (incl. transitive deps
REM  like cffi/pycparser pulled in by soundcard).
REM ----------------------------------------------------------------
echo.
echo [2/4] 正在锁定版本并下载 wheels ...
mkdir "%WHEELS_DIR%" >nul 2>&1

set "FREEZE_FILE=%PROJECT_DIR%\.tools\bundle-freeze.txt"
"%UV_EXE%" pip freeze --python "%VENV_DIR%\Scripts\python.exe" --exclude-editable > "%FREEZE_FILE%"
if errorlevel 1 (
    echo [错误] uv pip freeze 失败。
    exit /b 1
)

REM uv has no `pip download` (see uv pip --help). Bootstrap pip into the venv, then use pip.
"%UV_EXE%" pip install --python "%VENV_DIR%\Scripts\python.exe" pip
if errorlevel 1 (
    echo [错误] 无法在 venv 中安装 pip 以下载 wheel。
    exit /b 1
)
"%VENV_DIR%\Scripts\python.exe" -m pip download -d "%WHEELS_DIR%" -r "%FREEZE_FILE%"
if errorlevel 1 (
    echo [错误] pip download 失败；wheels\ 可能不完整。
    exit /b 1
)

REM Offline `uv pip install -e .` needs pyproject build-system deps plus uv's editable helper.
REM   hatchling -> packaging, pathspec, pluggy, trove-classifiers
REM   editables   -> required by uv when installing -e from a local path in isolation
echo       同时下载 hatchling + editables ^(+ 依赖^) 以供离线 editable 安装 ...
"%VENV_DIR%\Scripts\python.exe" -m pip download -d "%WHEELS_DIR%" hatchling "editables>=0.3,<1"
if errorlevel 1 (
    echo [错误] pip download hatchling/editables 失败。
    exit /b 1
)

REM Sanity check: must contain at least one wheel for each direct dep.
for %%P in (numpy opencv_python_headless dxcam soundcard hatchling editables) do (
    dir /b "%WHEELS_DIR%\%%P-*.whl" >nul 2>&1 || (
        echo [错误] wheels\ 中未找到 %%P 的 wheel。打包将不可用。
        exit /b 1
    )
)
echo       Wheels 已暂存于: %WHEELS_DIR%

REM ----------------------------------------------------------------
REM  Step 3: Drop the path-bound venv.  install.bat on the target
REM  machine will recreate it from wheels\ in a few seconds.
REM ----------------------------------------------------------------
echo.
echo [3/4] 正在删除路径绑定的 .venv\（目标机器将离线重建） ...
if exist "%VENV_DIR%" rmdir /s /q "%VENV_DIR%"

REM ----------------------------------------------------------------
REM  Step 4: Pack everything we need into a single zip.
REM
REM  Compress-Archive is built into PowerShell 5.1+ on every Windows
REM  10/11 box, so this script needs no extra tooling.  It does NOT
REM  preserve permissions, but for our payload (binaries + scripts)
REM  Windows doesn't need exec bits anyway.
REM ----------------------------------------------------------------
echo.
echo [4/4] 正在压缩打包 ...

for /f %%D in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set "DATESTAMP=%%D"
set "BUNDLE=%PROJECT_DIR%\game-recorder-portable-%DATESTAMP%.zip"
if exist "%BUNDLE%" del /q "%BUNDLE%"

REM Per-item array because Compress-Archive otherwise drags in the project root
REM as a parent directory, which makes the unzipped layout one level too deep.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ErrorActionPreference='Stop';" ^
    "$core = @('.tools','ffmpeg','wheels','src','scripts','pyproject.toml');" ^
    "$root = Get-ChildItem -LiteralPath '.' -File | Where-Object { $_.Extension -in @('.bat','.vbs','.md') } | ForEach-Object { $_.Name };" ^
    "$items = ($core + $root) | Select-Object -Unique | Where-Object { Test-Path $_ };" ^
    "Compress-Archive -Path $items -DestinationPath '%BUNDLE%' -CompressionLevel Optimal -Force"
if errorlevel 1 (
    echo [错误] Compress-Archive 失败。
    exit /b 1
)

for %%S in ("%BUNDLE%") do set "BUNDLE_SIZE=%%~zS"
set /a BUNDLE_MB=%BUNDLE_SIZE% / 1048576

echo.
echo ============================================================
echo   打包成功
echo ============================================================
echo   文件 : %BUNDLE%
echo   大小 : %BUNDLE_MB% MB
echo.
echo   网吧部署：
echo     1) 将 zip 复制到目标 PC 的 D:\ ^(不要用 C:\^)
echo     2) 右键 -^> 全部提取
echo     3) 双击 install.bat   ^(约 10 秒，无需联网^)
echo     4) 双击 run.bat        ^(连按两次大写键切换录制，悬浮窗「退出」结束^)
echo.
echo   本机构建后：
echo     .venv\ 已删除，zip 中不包含路径绑定的 venv。
echo     运行一次 install.bat 可从 wheels\ 离线重建 .venv\。
echo ============================================================
exit /b 0


:missing_uv
echo [错误] install.bat 之后缺少 %UV_EXE%。中止。
exit /b 1

:missing_venv
echo [错误] install.bat 之后缺少 %VENV_DIR%\Scripts\python.exe。中止。
exit /b 1
