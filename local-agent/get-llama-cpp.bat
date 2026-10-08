@echo off
rem Downloads the newest llama.cpp for NVIDIA cards into the llama.cpp folder.
rem (The Model server page in the app has the same button.)
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Run setup.bat first.
  pause
  exit /b 1
)
.venv\Scripts\python -m localagent.launcher
pause
