@echo off
setlocal enableextensions
chcp 65001 >nul

cd /d "%~dp0"
set "PROJECT_DIR=%~dp0"
if "%PROJECT_DIR:~-1%"=="\" set "PROJECT_DIR=%PROJECT_DIR:~0,-1%"

REM ---- Runtime dependencies stay project-local; optional camera plugins write game dirs ----
set "TOOLS_DIR=%PROJECT_DIR%\.tools"
set "UV_DIR=%TOOLS_DIR%\uv"
set "UV_EXE=%UV_DIR%\uv.exe"
set "FFMPEG_DIR=%PROJECT_DIR%\ffmpeg"
set "FFMPEG_EXE=%FFMPEG_DIR%\bin\ffmpeg.exe"
set "VENV_DIR=%PROJECT_DIR%\.venv"
set "WHEELS_DIR=%PROJECT_DIR%\wheels"

REM ---- Redirect uv state into project dir (avoid %LOCALAPPDATA%, %APPDATA%) ----
set "UV_CACHE_DIR=%TOOLS_DIR%\uv-cache"
set "UV_PYTHON_INSTALL_DIR=%TOOLS_DIR%\python"
set "UV_TOOL_DIR=%TOOLS_DIR%\uv-tools"
set "UV_TOOL_BIN_DIR=%TOOLS_DIR%\uv-tools\bin"

REM ---- Detect offline portable bundle: presence of wheels/ + pre-baked uv/ffmpeg/python ----
REM  When this script is unzipped from a build_offline_bundle.bat artifact, every dependency
REM  is already on disk; we must NOT touch the network (the target box is typically a 网吧
REM  PC behind a firewall / no proxy).
set "OFFLINE_MODE=0"
if exist "%WHEELS_DIR%" if exist "%UV_EXE%" if exist "%FFMPEG_EXE%" if exist "%UV_PYTHON_INSTALL_DIR%" set "OFFLINE_MODE=1"
if "%OFFLINE_MODE%"=="1" (
    REM Tell uv: never reach out to PyPI or python-build-standalone
    set "UV_OFFLINE=1"
    set "UV_PYTHON_DOWNLOADS=never"
)

echo ============================================================
echo   游戏录制器 - Windows 一键安装
echo ============================================================
echo   安装位置     : %PROJECT_DIR%
echo   uv 缓存/Python: %TOOLS_DIR%
if "%OFFLINE_MODE%"=="1" (
    echo   模式         : 离线 ^(从本地 wheels\ 恢复^)
) else (
    echo   模式         : 在线  ^(将下载 uv / Python / FFmpeg / wheels^)
)
echo   ^(运行环境保存在项目目录；可选相机插件会写入对应游戏目录。^)
echo ============================================================
echo.

REM ---- Warn if installed on the system drive (网吧 still-restore wipes it on reboot) ----
set "PROJECT_DRIVE=%PROJECT_DIR:~0,1%"
set "SYS_DRIVE=%SystemDrive:~0,1%"
if /I "%PROJECT_DRIVE%"=="%SYS_DRIVE%" (
    echo [警告] 项目位于系统盘 ^(%SystemDrive%^)。
    echo        在网吧 / 共享电脑等启用系统还原的环境
    echo        ^(网吧还原系统 / 影子系统^) 中，每次重启都会清除
    echo        本目录下的安装与所有录制文件。
    echo        强烈建议将项目移至非系统盘
    echo        ^(例如 D:\game-recorder^) 后再继续。
    echo.
    if defined GAME_RECORDER_SKIP_PAUSE (
        echo       [自动] 打包/脚本模式，跳过确认并继续。
    ) else (
        choice /c YN /n /m "仍要继续？[Y/N] "
        if errorlevel 2 exit /b 1
    )
    echo.
)

if not exist "%TOOLS_DIR%"           mkdir "%TOOLS_DIR%"
if not exist "%UV_CACHE_DIR%"        mkdir "%UV_CACHE_DIR%"
if not exist "%UV_PYTHON_INSTALL_DIR%" mkdir "%UV_PYTHON_INSTALL_DIR%"

REM ---- Prefer the exact bundled Python in offline portable zips.
REM  uv normally maintains a minor-version link directory such as
REM  cpython-3.11-windows-x86_64-none. Zip extraction can turn that link into
REM  a normal directory, making `uv python install 3.11` fail with os error 4390.
set "MANAGED_PYTHON_EXE="
for /d %%D in ("%UV_PYTHON_INSTALL_DIR%\cpython-3.11.*-windows-*") do (
    if exist "%%D\python.exe" if not defined MANAGED_PYTHON_EXE set "MANAGED_PYTHON_EXE=%%D\python.exe"
)

REM ============================================================
REM  Step 1/4: Download uv (standalone, ~15MB)
REM ============================================================
if exist "%UV_EXE%" (
    echo [1/4] uv 已存在，跳过下载。
) else (
    echo [1/4] 正在下载 uv ...
    if not exist "%UV_DIR%" mkdir "%UV_DIR%"
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "$ProgressPreference='SilentlyContinue';" ^
        "Invoke-WebRequest -Uri 'https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip' -OutFile '%UV_DIR%\uv.zip'"
    if errorlevel 1 goto :fail_download_uv

    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "Expand-Archive -Path '%UV_DIR%\uv.zip' -DestinationPath '%UV_DIR%' -Force"
    del /q "%UV_DIR%\uv.zip" >nul 2>&1

    if not exist "%UV_EXE%" goto :fail_extract_uv
    echo       uv 已安装: "%UV_EXE%"
)

REM ============================================================
REM  Step 2/4: Install Python 3.11 (into project dir via uv)
REM ============================================================
echo.
if "%OFFLINE_MODE%"=="1" (
    if defined MANAGED_PYTHON_EXE (
        echo [2/4] 捆绑 Python 已存在，跳过安装。
        echo       Python: "%MANAGED_PYTHON_EXE%"
    ) else (
        goto :fail_missing_offline_python
    )
) else (
    echo [2/4] 正在安装托管 Python 3.11 ...
    "%UV_EXE%" python install 3.11
    if errorlevel 1 goto :fail_install_python
    set "MANAGED_PYTHON_EXE="
    for /d %%D in ("%UV_PYTHON_INSTALL_DIR%\cpython-3.11.*-windows-*") do (
        if exist "%%D\python.exe" if not defined MANAGED_PYTHON_EXE set "MANAGED_PYTHON_EXE=%%D\python.exe"
    )
)

REM ============================================================
REM  Step 3/4: Download FFmpeg (BtbN gpl — encoders: NVENC, libx264, dshow, …)
REM ============================================================
REM  Note: Upstream static win64 FFmpeg (incl. BtbN) often has NO wasapi
REM  *demuxer*; system audio is usually captured via DirectShow (Stereo Mix,
REM  VoiceMeeter route, etc.).  Gyan "essentials" is too stripped; gpl is full.
REM  URL: master-latest; replace with a release addin (e.g. n7.1) if you need
REM  a specific branch (folder name still ffmpeg-* after extract).
REM ============================================================
echo.
if exist "%FFMPEG_EXE%" (
    echo [3/4] FFmpeg 已存在，跳过下载。
) else (
    echo [3/4] 正在下载 FFmpeg ^(BtbN gpl，约 140MB，含 NVENC + dshow + libx264^) ...
    set "FFMPEG_ZIP=%TOOLS_DIR%\ffmpeg.zip"
    set "FFMPEG_TMP=%TOOLS_DIR%\ffmpeg-extract"

    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "$ProgressPreference='SilentlyContinue';" ^
        "Invoke-WebRequest -Uri 'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip' -OutFile '%TOOLS_DIR%\ffmpeg.zip'"
    if errorlevel 1 goto :fail_download_ffmpeg

    if exist "%TOOLS_DIR%\ffmpeg-extract" rmdir /s /q "%TOOLS_DIR%\ffmpeg-extract"
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "Expand-Archive -Path '%TOOLS_DIR%\ffmpeg.zip' -DestinationPath '%TOOLS_DIR%\ffmpeg-extract' -Force"

    if exist "%FFMPEG_DIR%" rmdir /s /q "%FFMPEG_DIR%"
    for /d %%D in ("%TOOLS_DIR%\ffmpeg-extract\ffmpeg-*") do (
        move "%%D" "%FFMPEG_DIR%" >nul
    )
    rmdir /s /q "%TOOLS_DIR%\ffmpeg-extract" >nul 2>&1
    del /q "%TOOLS_DIR%\ffmpeg.zip" >nul 2>&1

    if not exist "%FFMPEG_EXE%" goto :fail_extract_ffmpeg
    echo       FFmpeg 已安装: "%FFMPEG_EXE%"
)

REM ============================================================
REM  Step 4/4: Create venv + install project (editable)
REM
REM  Online: uv resolves + downloads from PyPI, fills %UV_CACHE_DIR%.
REM  Offline (wheels/ present): uv resolves from --find-links wheels/ only,
REM  with --no-index --offline so a missing wheel fails loudly instead of
REM  silently hanging on a connect attempt.
REM ============================================================
echo.
echo [4/4] 正在创建虚拟环境并安装 game-recorder ...
if defined MANAGED_PYTHON_EXE (
    "%UV_EXE%" venv --clear --python "%MANAGED_PYTHON_EXE%" "%VENV_DIR%"
) else (
    "%UV_EXE%" venv --clear --python 3.11 "%VENV_DIR%"
)
if errorlevel 1 goto :fail_venv

if "%OFFLINE_MODE%"=="1" goto :install_offline
"%UV_EXE%" pip install --python "%VENV_DIR%\Scripts\python.exe" -e .
goto :install_done

:install_offline
REM Do not set PROJECT_WHEEL inside (...) — CMD expands %% vars at block parse time.
set "PROJECT_WHEEL="
for %%F in ("%WHEELS_DIR%\game_recorder-*.whl") do set "PROJECT_WHEEL=%%F"
if not defined PROJECT_WHEEL goto :install_offline_editable
echo       离线模式：从 wheel 安装 game-recorder ^(不访问 PyPI^)。
"%UV_EXE%" pip install --offline --no-index --find-links "%WHEELS_DIR%" --python "%VENV_DIR%\Scripts\python.exe" "%PROJECT_WHEEL%"
goto :install_done

:install_offline_editable
echo       离线模式：从 "%WHEELS_DIR%" editable 安装 ^(不访问 PyPI^)。
"%UV_EXE%" pip install --offline --no-index --find-links "%WHEELS_DIR%" --python "%VENV_DIR%\Scripts\python.exe" -e .

:install_done
if errorlevel 1 goto :fail_install

set "VERIFY_PY=%VENV_DIR%\Scripts\python.exe"
"%VERIFY_PY%" -c "import game_recorder" >nul 2>&1
if errorlevel 1 (
    set "PYTHONPATH=%PROJECT_DIR%\src"
    "%VERIFY_PY%" -c "import game_recorder" >nul 2>&1
    if errorlevel 1 goto :fail_import_check
    echo [警告] 当前文件夹路径含中文等特殊字符时，editable 安装可能异常。
    echo        run.bat 已自动加入 PYTHONPATH；建议解压到纯英文路径如 D:\game-recorder。
)

REM ============================================================
REM  Install launch scripts (copy templates; avoid fragile echo generation)
REM ============================================================
copy /Y "%PROJECT_DIR%\scripts\run.bat" "%PROJECT_DIR%\run.bat" >nul
if errorlevel 1 goto :fail_launchers
copy /Y "%PROJECT_DIR%\scripts\run-console.bat" "%PROJECT_DIR%\run-console.bat" >nul
if errorlevel 1 goto :fail_launchers

REM ============================================================
REM  Optional: GTA V camera pose logger (does not fail main install)
REM ============================================================
echo.
echo [可选] 正在尝试安装 GTA 相机轨迹插件 …
if defined GAME_RECORDER_SKIP_PAUSE (
    if defined GTAV_DIR (
        "%VERIFY_PY%" "%PROJECT_DIR%\scripts\install_gta_camera.py" --recordings-dir "%PROJECT_DIR%\recordings" --no-prompt --gta-dir "%GTAV_DIR%"
    ) else (
        "%VERIFY_PY%" "%PROJECT_DIR%\scripts\install_gta_camera.py" --recordings-dir "%PROJECT_DIR%\recordings" --no-prompt
    )
) else (
    if defined GTAV_DIR (
        "%VERIFY_PY%" "%PROJECT_DIR%\scripts\install_gta_camera.py" --recordings-dir "%PROJECT_DIR%\recordings" --gta-dir "%GTAV_DIR%"
    ) else (
        "%VERIFY_PY%" "%PROJECT_DIR%\scripts\install_gta_camera.py" --recordings-dir "%PROJECT_DIR%\recordings"
    )
)
if errorlevel 4 goto :gta_install_fail
if errorlevel 3 goto :gta_install_skip
if errorlevel 1 goto :gta_install_fail
echo       [完成] GTA 相机插件已安装，进故事模式录制即可采集相机参数。
goto :gta_install_done

:gta_install_skip
echo       [跳过] 未安装 GTA 相机插件。需要相机时请再运行 gta-camera\install.bat 并输入 GTA 主目录。
goto :gta_install_done

:gta_install_fail
echo       [失败] GTA 相机插件未装好。请再运行 gta-camera\install.bat 并输入 GTA 主目录。
goto :gta_install_done

:gta_install_done

REM ============================================================
REM  Optional: Black Myth: Wukong camera logger (does not fail main install)
REM ============================================================
echo.
echo [可选] 正在尝试安装黑神话：悟空相机插件 …
if defined GAME_RECORDER_SKIP_PAUSE (
    if defined WUKONG_DIR (
        "%VERIFY_PY%" "%PROJECT_DIR%\scripts\install_wukong_camera.py" --recordings-dir "%PROJECT_DIR%\recordings" --no-prompt --wukong-dir "%WUKONG_DIR%"
    ) else (
        "%VERIFY_PY%" "%PROJECT_DIR%\scripts\install_wukong_camera.py" --recordings-dir "%PROJECT_DIR%\recordings" --no-prompt
    )
) else (
    if defined WUKONG_DIR (
        "%VERIFY_PY%" "%PROJECT_DIR%\scripts\install_wukong_camera.py" --recordings-dir "%PROJECT_DIR%\recordings" --wukong-dir "%WUKONG_DIR%"
    ) else (
        "%VERIFY_PY%" "%PROJECT_DIR%\scripts\install_wukong_camera.py" --recordings-dir "%PROJECT_DIR%\recordings"
    )
)
if errorlevel 4 goto :wukong_install_fail
if errorlevel 3 goto :wukong_install_skip
if errorlevel 1 goto :wukong_install_fail
echo       [完成] 黑神话相机插件已安装，录制时会同步输出相机参数。
goto :wukong_install_done

:wukong_install_skip
echo       [跳过] 未安装黑神话相机插件。需要时请再运行 wukong-camera\install.bat。
goto :wukong_install_done

:wukong_install_fail
echo       [失败] 黑神话相机插件未装好，但不影响录制器主程序。
echo              请关闭游戏后再运行 wukong-camera\install.bat。
goto :wukong_install_done

:wukong_install_done

echo.
echo ============================================================
echo   安装完成！
echo ============================================================
echo   开始录制      :  run.bat
echo   显示控制台    :  run-console.bat  或  run.bat --console
echo   无热键模式    :  run.bat --no-hotkey
echo   低延迟回退    :  run.bat --fps 20 --quality 28 --x264-threads 1
echo   GTA 相机插件  :  gta-camera\install.bat
echo   黑神话相机插件:  wukong-camera\install.bat
echo ============================================================
echo.
call :wait_key
exit /b 0


:fail_download_uv
echo.
echo [错误] 下载 uv 失败。请检查网络/代理后重试。
call :wait_key
exit /b 1

:fail_extract_uv
echo.
echo [错误] uv 压缩包已解压但未找到 uv.exe。
call :wait_key
exit /b 1

:fail_install_python
echo.
echo [错误] uv python install 失败。
call :wait_key
exit /b 1

:fail_missing_offline_python
echo.
echo [错误] 离线包缺少 ".tools\python" 下的托管 Python。
call :wait_key
exit /b 1

:fail_download_ffmpeg
echo.
echo [错误] 从 github.com/BtbN 下载 FFmpeg 失败。请检查网络/代理后重试。
call :wait_key
exit /b 1

:fail_extract_ffmpeg
echo.
echo [错误] FFmpeg 已解压但未找到 ffmpeg.exe。
call :wait_key
exit /b 1

:fail_venv
echo.
echo [错误] 创建虚拟环境失败。
call :wait_key
exit /b 1

:fail_install
echo.
echo [错误] uv pip install 失败。
call :wait_key
exit /b 1

:fail_import_check
echo.
echo [错误] 安装后无法导入 game_recorder。
echo        若路径含中文，请改解压到纯英文目录 ^(如 D:\game-recorder^) 后删除 .venv 再运行本脚本。
call :wait_key
exit /b 1

:fail_launchers
echo.
echo [错误] 无法生成 run.bat / run-console.bat，请确认 scripts 目录完整且项目目录可写。
call :wait_key
exit /b 1

:wait_key
if defined GAME_RECORDER_SKIP_PAUSE exit /b 0
echo 按任意键继续...
pause >nul
exit /b 0
