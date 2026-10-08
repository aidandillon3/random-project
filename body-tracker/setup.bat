@echo off
rem One-time setup: makes a private Python environment and installs what the tracker needs.
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Install Python 3.12 from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^) and run this again.
  pause
  exit /b 1
)
py -3 -m venv .venv || goto :fail
.venv\Scripts\python -m pip install --upgrade pip || goto :fail
.venv\Scripts\python -m pip install -r requirements.txt || goto :fail
echo.
echo Setup done. Double-click run.bat to start the tracker.
pause
exit /b 0
:fail
echo Setup failed - see the messages above.
pause
exit /b 1
