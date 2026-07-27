@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
set "PYTHONUTF8=1"

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" creatorhub.py %*
    exit /b !errorlevel!
)

where py.exe >nul 2>nul
if not errorlevel 1 (
    py -3 creatorhub.py %*
    exit /b !errorlevel!
)

where python.exe >nul 2>nul
if not errorlevel 1 (
    python creatorhub.py %*
    exit /b !errorlevel!
)

echo [CreatorHub] Python 3.10 or newer is required.
echo [CreatorHub] Install Python, enable "Add Python to PATH", then run start.cmd again.
pause
exit /b 1
