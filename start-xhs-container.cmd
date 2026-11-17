@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
echo Starting the approved local Xiaohongshu container. No posts will be published.
"%~dp0.venv\Scripts\python.exe" "%~dp0scripts\start_xhs_container.py"
echo.
pause
