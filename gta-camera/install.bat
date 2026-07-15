@echo off
setlocal EnableExtensions
cd /d "%~dp0.."
set "PROJECT_DIR=%cd%"

set "PY="
if exist "%PROJECT_DIR%\.venv\Scripts\python.exe" set "PY=%PROJECT_DIR%\.venv\Scripts\python.exe"
if not defined PY (
  for /d %%D in ("%PROJECT_DIR%\.tools\python\cpython-3.11*-windows-*") do (
    if exist "%%D\python.exe" if not defined PY set "PY=%%D\python.exe"
  )
)
if not defined PY set "PY=python"

if "%~1"=="" (
  "%PY%" "%PROJECT_DIR%\scripts\install_gta_camera.py" --recordings-dir "%PROJECT_DIR%\recordings"
) else (
  "%PY%" "%PROJECT_DIR%\scripts\install_gta_camera.py" --recordings-dir "%PROJECT_DIR%\recordings" --gta-dir "%~1"
)
exit /b %ERRORLEVEL%
