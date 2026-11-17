@echo off
REM 停止由 start-bot.cmd 启动的 worker 和审批控制台。
REM 只结束 var\run 里记录的那个 PID，不会误杀其他 Python 程序。
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop.ps1" %*
exit /b %ERRORLEVEL%
