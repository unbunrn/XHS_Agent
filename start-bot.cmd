@echo off
REM 双击启动本地 worker + 审批控制台。
REM 实际逻辑在 scripts\start.ps1；这里只负责用 PowerShell 5.1 调用它，
REM 并把命令行参数（%*）原样传过去。
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1" %*
exit /b %ERRORLEVEL%
