@echo off
REM Create an ordinary Python 3.12 .venv and install pinned dependencies.
REM This does NOT use --system-site-packages and does not read any secrets.
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup.ps1" %*
exit /b %ERRORLEVEL%
