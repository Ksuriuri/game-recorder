@echo off
setlocal EnableExtensions

rem Usage:
rem   build.bat "C:\path\to\extracted\ScriptHookRDR2_SDK"
rem   build.bat "C:\path\to\SDK" "C:\path\to\MSBuild.exe"
rem Arguments override SDK_ROOT and MSBUILD environment variables.

if not "%~1"=="" set "SDK_ROOT=%~1"
if not "%~2"=="" set "MSBUILD=%~2"

if "%SDK_ROOT%"=="" (
  echo [error] Pass SDK_ROOT as argument 1 or set the SDK_ROOT environment variable.
  echo         Extract the official SDK outside this repository.
  exit /b 1
)

if not exist "%SDK_ROOT%\inc\main.h" (
  echo [error] SDK header not found: "%SDK_ROOT%\inc\main.h"
  exit /b 1
)
if not exist "%SDK_ROOT%\lib\ScriptHookRDR2.lib" (
  echo [error] SDK import library not found: "%SDK_ROOT%\lib\ScriptHookRDR2.lib"
  exit /b 1
)

if "%MSBUILD%"=="" (
  for /f "usebackq tokens=*" %%I in (`where msbuild 2^>nul`) do if not defined MSBUILD set "MSBUILD=%%I"
)

if "%MSBUILD%"=="" (
  set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
  if exist "%VSWHERE%" (
    for /f "usebackq tokens=*" %%I in (`"%VSWHERE%" -latest -products * -requires Microsoft.Component.MSBuild -find MSBuild\**\Bin\MSBuild.exe`) do if not defined MSBUILD set "MSBUILD=%%I"
  )
)

if "%MSBUILD%"=="" (
  echo [error] MSBuild.exe was not found. Pass it as argument 2 or set MSBUILD.
  exit /b 1
)
if not exist "%MSBUILD%" (
  echo [error] MSBuild does not exist: "%MSBUILD%"
  exit /b 1
)

echo Building RDR2CameraPoseLogger.asi
echo SDK_ROOT=%SDK_ROOT%
"%MSBUILD%" "%~dp0CameraPoseLogger\CameraPoseLogger.vcxproj" ^
  /m /nologo /p:Configuration=Release /p:Platform=x64 /p:SDK_ROOT="%SDK_ROOT%"
if errorlevel 1 exit /b 1

echo.
echo Built:
echo   %~dp0CameraPoseLogger\bin\Release\RDR2CameraPoseLogger.asi
endlocal
