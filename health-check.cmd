@echo off
REM 只读体检：配置、依赖、数据库、外部账号、控制台健康页。
REM 不会批准任何内容，也不会发布。退出码 0 表示没有硬故障。
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\health.ps1" %*
exit /b %ERRORLEVEL%
