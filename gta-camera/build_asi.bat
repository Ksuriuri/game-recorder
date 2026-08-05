@echo off
setlocal EnableExtensions

rem Usage:
rem   build_asi.bat
rem   build_asi.bat "C:\path\to\MSBuild.exe"

if not "%~1"=="" set "MSBUILD=%~1"

if "%MSBUILD%"=="" (
  for /f "usebackq tokens=*" %%I in (`where msbuild 2^>nul`) do if not defined MSBUILD set "MSBUILD=%%I"
)

if "%MSBUILD%"=="" (
  set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
  if exist "%VSWHERE%" (
    for /f "usebackq tokens=*" %%I in (`"%VSWHERE%" -latest -products * -requires Microsoft.Component.MSBuild -find MSBuild\**\Bin\MSBuild.exe`) do if not defined MSBUILD set "MSBUILD=%%I"
  )
)

if "%MSBUILD%"=="" if exist "%~dp0..\.tools\vs2022-buildtools\MSBuild\Current\Bin\MSBuild.exe" (
  set "MSBUILD=%~dp0..\.tools\vs2022-buildtools\MSBuild\Current\Bin\MSBuild.exe"
)

set "OUT_DIR=%~dp0AsiCameraPoseLogger\bin\Release"
set "OUT=%OUT_DIR%\CameraPoseLogger.asi"
mkdir "%OUT_DIR%" 2>nul

if not "%MSBUILD%"=="" if exist "%MSBUILD%" (
  echo Building CameraPoseLogger.asi with MSBuild
  "%MSBUILD%" "%~dp0AsiCameraPoseLogger\CameraPoseLogger.vcxproj" ^
    /m /nologo /p:Configuration=Release /p:Platform=x64
  if not errorlevel 1 if exist "%OUT%" goto :publish
  echo [warn] MSBuild build failed, trying clang++ fallback...
)

set "CLANG=%~dp0..\.tools\llvm-mingw\bin\clang++.exe"
if not exist "%CLANG%" (
  for /f "usebackq tokens=*" %%I in (`where clang++ 2^>nul`) do if not defined CLANG_FOUND set "CLANG=%%I" & set "CLANG_FOUND=1"
)
if not exist "%CLANG%" (
  echo [error] Neither MSBuild nor clang++ is available.
  exit /b 1
)

echo Building CameraPoseLogger.asi with clang++
pushd "%~dp0AsiCameraPoseLogger"
"%CLANG%" -shared -O2 -std=c++17 -municode -o "%OUT%" main.cpp scripthookv.cpp -lkernel32 -luser32 -static
set "ERR=%ERRORLEVEL%"
popd
if not "%ERR%"=="0" exit /b %ERR%
if not exist "%OUT%" (
  echo [error] clang++ did not produce "%OUT%"
  exit /b 1
)

:publish
mkdir "%~dp0dist" 2>nul
copy /Y "%OUT%" "%~dp0dist\CameraPoseLogger.asi" >nul
echo.
echo Built:
echo   %OUT%
echo   %~dp0dist\CameraPoseLogger.asi
endlocal
