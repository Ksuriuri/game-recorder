@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul

set "ROOT=%~dp0.."
set "INSTALLER=%ROOT%\scripts\install_rdr2_camera.py"

if exist "%ROOT%\.venv\Scripts\python.exe" (
    "%ROOT%\.venv\Scripts\python.exe" "%INSTALLER%" %*
    exit /b !errorlevel!
)

where python.exe >nul 2>nul
if not errorlevel 1 (
    python.exe "%INSTALLER%" %*
    exit /b !errorlevel!
)

where py.exe >nul 2>nul
if not errorlevel 1 (
    py.exe -3 "%INSTALLER%" %*
    exit /b !errorlevel!
)

echo [错误] 未找到 Python。请先安装 Python 3，或创建项目 .venv。
exit /b 1
