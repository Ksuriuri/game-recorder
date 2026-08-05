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
REM     .tools\uv\       uv.exe
REM     .tools\python\   managed Python 3.11
REM     .tools\uv-cache\ uv resolution cache (offline fallback)
REM     ffmpeg\          BtbN gpl FFmpeg (NVENC/AMF/QSV + libx264 + dshow)
REM     wheels\          pre-downloaded dependency wheels (numpy, opencv-headless,
REM                      dxcam, soundcard, cffi, pycparser …)
REM     src\, scripts\, gta-camera\, rdr2-camera\, wukong-camera\, cp2077-camera\,
REM     s3-upload\（不含 .venv / oss_credentials.json）, pyproject.toml
REM     根目录全部 *.bat / *.vbs / *.md / *.txt（install.bat、run.bat、录制操作手册.txt 等）
REM
REM   What is NOT shipped:
REM     .venv\           path-bound; install.bat recreates it offline from wheels\
REM     recordings\      user data
REM     .tools\llvm-mingw\ / vs2022-buildtools\ / *.zip / installer exes
REM                     (local ASI compile toolchains — not needed on cafe PCs)
REM     this script and any portable *.zip
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

echo.
echo [1b/4] 正在预下载赛博朋克 2077 相机依赖 ^(RED4ext + CET + ReShade^) ...
"%VENV_DIR%\Scripts\python.exe" "%PROJECT_DIR%\scripts\install_cp2077_camera.py" --prefetch-deps
if errorlevel 1 (
    echo [警告] CP2077 依赖预下载失败；离线包中赛博朋克相机可能无法一键安装。
) else (
    if not exist "%PROJECT_DIR%\cp2077-camera\vendor\RED4ext" mkdir "%PROJECT_DIR%\cp2077-camera\vendor\RED4ext"
    if not exist "%PROJECT_DIR%\cp2077-camera\vendor\CET" mkdir "%PROJECT_DIR%\cp2077-camera\vendor\CET"
    if not exist "%PROJECT_DIR%\cp2077-camera\vendor\ReShade" mkdir "%PROJECT_DIR%\cp2077-camera\vendor\ReShade"
    if exist "%PROJECT_DIR%\.tools\cp2077-camera-cache\red4ext-1.30.0.zip" (
        copy /Y "%PROJECT_DIR%\.tools\cp2077-camera-cache\red4ext-1.30.0.zip" "%PROJECT_DIR%\cp2077-camera\vendor\RED4ext\" >nul
    )
    if exist "%PROJECT_DIR%\.tools\cp2077-camera-cache\cet_1.37.1.zip" (
        copy /Y "%PROJECT_DIR%\.tools\cp2077-camera-cache\cet_1.37.1.zip" "%PROJECT_DIR%\cp2077-camera\vendor\CET\" >nul
    )
    if exist "%PROJECT_DIR%\.tools\cp2077-camera-cache\ReShade_Setup_6.7.3_Addon.exe" (
        copy /Y "%PROJECT_DIR%\.tools\cp2077-camera-cache\ReShade_Setup_6.7.3_Addon.exe" "%PROJECT_DIR%\cp2077-camera\vendor\ReShade\" >nul
    ) else (
        echo [警告] 缺少 ReShade_Setup_6.7.3_Addon.exe；离线安装 CP2077 深度捕获会失败。
    )
)

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

REM Write to .tools\ first, then move — avoids zip failing when an older portable
REM zip in the project root is open in Explorer or the IDE.
REM Pack selectively: only runtime .tools subdirs (skip local ASI compile toolchains).
REM s3-upload is included with .venv / __pycache__ / oss_credentials.json skipped.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ErrorActionPreference='Stop';" ^
    "Add-Type -AssemblyName System.IO.Compression;" ^
    "Add-Type -AssemblyName System.IO.Compression.FileSystem;" ^
    "function Add-Tree([System.IO.Compression.ZipArchive]$zip, [string]$dirPath, [string]$entryPrefix, [string[]]$skipDirs=@(), [string[]]$skipFiles=@()) {" ^
    "  if (-not (Test-Path -LiteralPath $dirPath)) { return };" ^
    "  $rootPath = (Resolve-Path -LiteralPath $dirPath).Path;" ^
    "  Get-ChildItem -LiteralPath $dirPath -Recurse -Force -File | ForEach-Object {" ^
    "    $rel = $_.FullName.Substring($rootPath.Length).TrimStart('\','/');" ^
    "    $parts = $rel -split '[\\/]';" ^
    "    if ($parts | Where-Object { $_ -in $skipDirs }) { return };" ^
    "    if ($_.Name -in $skipFiles) { return };" ^
    "    $entry = ($entryPrefix + ($rel -replace '\\','/'));" ^
    "    [void][System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $_.FullName, $entry, [System.IO.Compression.CompressionLevel]::Optimal);" ^
    "  };" ^
    "};" ^
    "if (Test-Path -LiteralPath '%BUNDLE_TMP%') { Remove-Item -LiteralPath '%BUNDLE_TMP%' -Force };" ^
    "$zip = [System.IO.Compression.ZipFile]::Open('%BUNDLE_TMP%', [System.IO.Compression.ZipArchiveMode]::Create);" ^
    "try {" ^
    "  foreach ($name in @('uv','python','uv-cache')) {" ^
    "    Add-Tree $zip (Join-Path '.tools' $name) ('.tools/' + $name + '/') ;" ^
    "  };" ^
    "  foreach ($name in @('ffmpeg','wheels','src','scripts')) {" ^
    "    Add-Tree $zip $name ($name + '/') @('__pycache__','.venv');" ^
    "  };" ^
    "  $camSkip = @('__pycache__','.venv','bin','obj','.vs');" ^
    "  foreach ($name in @('gta-camera','rdr2-camera','wukong-camera','cp2077-camera')) {" ^
    "    Add-Tree $zip $name ($name + '/') $camSkip;" ^
    "  };" ^
    "  if (Test-Path -LiteralPath 'pyproject.toml') {" ^
    "    [void][System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, (Resolve-Path -LiteralPath 'pyproject.toml').Path, 'pyproject.toml', [System.IO.Compression.CompressionLevel]::Optimal);" ^
    "  };" ^
    "  Get-ChildItem -LiteralPath '.' -File | Where-Object { $_.Extension -in @('.bat','.vbs','.md','.txt') } | ForEach-Object {" ^
    "    [void][System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $_.FullName, $_.Name, [System.IO.Compression.CompressionLevel]::Optimal);" ^
    "  };" ^
    "  Add-Tree $zip 's3-upload' 's3-upload/' @('.venv','__pycache__') @('oss_credentials.json');" ^
    "} finally { $zip.Dispose() }"
if errorlevel 1 (
    echo [错误] 压缩打包失败。
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
echo   Cafe deploy:
echo     1. Copy the zip to D: on the target PC ^(not C:^)
echo     2. Extract to an ASCII-only path, e.g. D:\game-recorder
echo     3. Double-click "install.bat" ^(offline venv rebuild^)
echo     4. Double-click "run.bat", then enter the game to record
echo.
echo   Notes:
echo     - Target PC needs GTA V; ScriptHookV must match the game build
echo     - After a big GTA update, refresh gta-camera\vendor\ScriptHookV
echo       then re-run "gta-camera\install.bat"
echo     - Wukong plugin is fully offline; close the game before install/uninstall
echo.
echo   After this build:
echo     Local .venv was removed ^(not shipped; path-bound^).
echo     On the target PC, run "install.bat" to rebuild .venv from wheels.
echo ============================================================
exit /b 0


:missing_uv
echo [错误] install.bat 之后缺少 %UV_EXE%。中止。
exit /b 1

:missing_venv
echo [错误] install.bat 之后缺少 %VENV_DIR%\Scripts\python.exe。中止。
exit /b 1
