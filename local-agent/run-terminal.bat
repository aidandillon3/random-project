@echo off
rem The same agent in a terminal window, without the app window.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Run setup.bat first.
  pause
  exit /b 1
)
.venv\Scripts\python -m localagent --cli %*
pause
