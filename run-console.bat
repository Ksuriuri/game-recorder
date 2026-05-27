@echo off
REM 带黑色终端启动，便于查看日志与报错（等同 run.bat --console）
cd /d "%~dp0"
call "%~dp0run.bat" --console %*
