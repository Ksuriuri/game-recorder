@echo off
setlocal EnableExtensions
REM Build CameraPoseLogger.dll for ScriptHookVDotNet3.
REM Usage:
REM   build.bat
REM   build.bat "D:\Steam\steamapps\common\Grand Theft Auto V"
REM Or set GTAV_DIR env var to the GTA V folder that contains ScriptHookVDotNet3.dll.

cd /d "%~dp0CameraPoseLogger"

if not "%~1"=="" set "GTAV_DIR=%~1"

if "%GTAV_DIR%"=="" (
  echo [error] Set GTAV_DIR or pass GTA V path as arg 1.
  echo Example: build.bat "D:\Steam\steamapps\common\Grand Theft Auto V"
  exit /b 1
)

if not exist "%GTAV_DIR%\ScriptHookVDotNet3.dll" (
  echo [error] ScriptHookVDotNet3.dll not found in:
  echo   %GTAV_DIR%
  echo Install ScriptHookV + ScriptHookVDotNet3 first.
  exit /b 1
)

where dotnet >nul 2>&1
if errorlevel 1 (
  echo [error] .NET SDK not found. Install from https://dotnet.microsoft.com/download
  exit /b 1
)

echo Building against: %GTAV_DIR%
dotnet build -c Release -p:GtaVDir="%GTAV_DIR%"
if errorlevel 1 exit /b 1

set "OUT=%~dp0CameraPoseLogger\bin\Release\CameraPoseLogger.dll"
set "CFG=%~dp0CameraPoseLogger\camera_pose_logger.config.json"
set "DEST=%GTAV_DIR%\scripts"

if not exist "%DEST%" mkdir "%DEST%"

copy /Y "%OUT%" "%DEST%\CameraPoseLogger.dll" >nul
copy /Y "%CFG%" "%DEST%\camera_pose_logger.config.json" >nul

echo.
echo Copied:
echo   %DEST%\CameraPoseLogger.dll
echo   %DEST%\camera_pose_logger.config.json
echo.
echo Prefer: gta-camera\install.bat  （auto detect GTA + download SHVDN + write follow_recorder config）
echo In-game: plugin follows game-recorder active_session.json （no F10 needed）.
endlocal
