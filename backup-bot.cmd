@echo off
REM 在线备份 SQLite 数据库，并用 PRAGMA integrity_check 校验备份文件。
REM 不停服务、不改原库；备份默认写到 var\backups\。
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\backup.ps1" %*
exit /b %ERRORLEVEL%
