@echo off
setlocal EnableExtensions
chcp 65001 >nul

cd /d "%~dp0.."
set "PROJECT_DIR=%CD%"
set "SCRIPT=%PROJECT_DIR%\scripts\uninstall_wukong_camera.py"

if exist "%PROJECT_DIR%\.venv\Scripts\python.exe" goto :run_venv

if exist "%PROJECT_DIR%\.tools\uv\uv.exe" goto :run_uv

where py >nul 2>&1
if not errorlevel 1 goto :run_py

where python >nul 2>&1
if not errorlevel 1 goto :run_python

echo [错误] 未找到可用的 Python。请先运行项目根目录 install.bat。
exit /b 1

:run_venv
"%PROJECT_DIR%\.venv\Scripts\python.exe" "%SCRIPT%" %*
exit /b %ERRORLEVEL%

:run_uv
"%PROJECT_DIR%\.tools\uv\uv.exe" run --python 3.11 python "%SCRIPT%" %*
exit /b %ERRORLEVEL%

:run_py
py -3.11 "%SCRIPT%" %*
exit /b %ERRORLEVEL%

:run_python
python "%SCRIPT%" %*
exit /b %ERRORLEVEL%
