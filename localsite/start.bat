@echo off
rem Starts the JustSimple local test site. Double-click it. Close this window (or press Ctrl+C) to stop.
cd /d "%~dp0"
title JustSimple local test site
rem (the Python environment lives in a short path outside this folder: Windows limits path length, and OneDrive need not sync it)
set "VENV=%LOCALAPPDATA%\JustSimpleLocal\venv"

if exist "%VENV%\Scripts\python.exe" goto run

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY goto nopython
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" || goto oldpython

echo Setting up (first start only, a few minutes)...
%PY% -m venv "%VENV%" || goto failed
"%VENV%\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r "..\backend\requirements.txt" || goto failed

:run
"%VENV%\Scripts\python.exe" run_local.py --open %*
pause
exit /b

:nopython
echo Python is not installed. Install Python 3.12 or newer from https://www.python.org/downloads/
echo (tick "Add python.exe to PATH" in the installer), then double-click start.bat again.
pause
exit /b 1

:oldpython
echo This needs Python 3.12 or newer. Install it from https://www.python.org/downloads/ and double-click start.bat again.
pause
exit /b 1

:failed
echo Setting up failed (see the messages above). Check the internet connection and double-click start.bat again.
rmdir /s /q "%VENV%" 2>nul
pause
exit /b 1
