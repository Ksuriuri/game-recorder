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
REM     4) Double-click run.bat, then start GTA V or Black Myth: Wukong.
REM
REM   What goes into the zip:
REM     .tools\          uv.exe + managed Python 3.11 + uv cache
REM     ffmpeg\          BtbN gpl FFmpeg (NVENC + libx264 + dshow)
REM     wheels\          pre-downloaded dependency wheels (numpy, opencv-headless,
REM                      dxcam, soundcard, cffi, pycparser …)
REM     src\, scripts\, gta-camera\, rdr2-camera\, wukong-camera\, pyproject.toml
REM     根目录全部 *.bat / *.vbs / *.md / *.txt（install.bat、run.bat、录制操作手册.txt 等）
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

if /I "%~1"=="--pack-only" (
    set "PROJECT_WHEEL="
    for %%F in ("%WHEELS_DIR%\game_recorder-*.whl") do set "PROJECT_WHEEL=%%F"
    if not defined PROJECT_WHEEL (
        echo [错误] wheels\ 中无 game_recorder-*.whl，请先完整运行本脚本。
        exit /b 1
    )
    echo [pack-only] 已有 wheels\，跳过 install，仅重新压缩 ...
    goto :step4_pack
)

echo ============================================================
echo   正在构建离线便携包
echo   项目目录 %PROJECT_DIR%
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
set "GAME_RECORDER_SKIP_PAUSE=1"
call "%PROJECT_DIR%\install.bat"
set "GAME_RECORDER_SKIP_PAUSE="
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

REM Bake game-recorder itself as a wheel so offline install does not rely on
REM editable .pth files (they break when the extract path contains non-ASCII chars).
echo       正在构建 game_recorder wheel ...
"%UV_EXE%" build --wheel -o "%WHEELS_DIR%"
if errorlevel 1 (
    echo [错误] uv build --wheel 失败。
    exit /b 1
)

REM Sanity check: must contain at least one wheel for each direct dep.
for %%P in (numpy opencv_python_headless dxcam soundcard modelscope game_recorder) do (
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
:step4_pack
REM Keep root launchers in sync with scripts\ templates (install.bat does this too;
REM --pack-only skips install, so sync here before zipping).
copy /Y "%PROJECT_DIR%\scripts\run.bat" "%PROJECT_DIR%\run.bat" >nul
if errorlevel 1 (
    echo [错误] 缺少或无法复制 scripts\run.bat。
    exit /b 1
)
copy /Y "%PROJECT_DIR%\scripts\run-console.bat" "%PROJECT_DIR%\run-console.bat" >nul
if errorlevel 1 (
    echo [错误] 缺少或无法复制 scripts\run-console.bat。
    exit /b 1
)

echo.
echo [4/4] 正在压缩打包 ...

for /f %%D in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "DATESTAMP=%%D"
set "BUNDLE=%PROJECT_DIR%\game-recorder-portable-%DATESTAMP%.zip"
set "BUNDLE_TMP=%TOOLS_DIR%\bundle-%DATESTAMP%.zip"
if exist "%BUNDLE_TMP%" del /q "%BUNDLE_TMP%" 2>nul

REM Write to .tools\ first, then move — avoids Compress-Archive failing when an
REM older portable zip in the project root is open in Explorer or the IDE.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ErrorActionPreference='Stop';" ^
    "$core = @('.tools','ffmpeg','wheels','src','scripts','gta-camera','rdr2-camera','wukong-camera','pyproject.toml');" ^
    "$root = Get-ChildItem -LiteralPath '.' -File | Where-Object { $_.Extension -in @('.bat','.vbs','.md','.txt') } | ForEach-Object { $_.Name };" ^
    "$items = ($core + $root) | Select-Object -Unique | Where-Object { Test-Path $_ };" ^
    "Compress-Archive -Path $items -DestinationPath '%BUNDLE_TMP%' -CompressionLevel Optimal -Force"
if errorlevel 1 (
    echo [错误] Compress-Archive 失败。
    exit /b 1
)
move /Y "%BUNDLE_TMP%" "%BUNDLE%" >nul
if errorlevel 1 (
    echo [错误] 无法将压缩包移动到项目根目录，临时文件保留在:
    echo        %BUNDLE_TMP%
    exit /b 1
)
if exist "%BUNDLE_TMP%" del /q "%BUNDLE_TMP%" 2>nul

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
echo     1. 将 zip 复制到目标 PC 的 D 盘，不要用 C 盘
echo     2. 右键 - 全部提取到纯英文目录，如 D:\game-recorder
echo     3. 双击 install.bat（离线重建环境；可自动发现或提示输入 GTA/黑神话目录）
echo     4. 双击 run.bat 后再进入对应游戏录制；session 内应有 camera.jsonl
echo.
echo   注意：
echo     - 目标机需已安装 GTA V；ScriptHookV 版本需匹配当前游戏版本
echo     - 游戏大更新后若进故事模式报 Unknown game version，需更新
echo       gta-camera\vendor\ScriptHookV\ 后再跑一次 gta-camera\install.bat
echo     - 黑神话插件完全离线分发；安装/卸载时必须先关闭游戏，写入受保护目录
echo       时会请求 UAC。版本不匹配时请先验证兼容性，不要盲目强制安装
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
