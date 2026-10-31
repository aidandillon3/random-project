@echo off
rem The pi coding agent (pi.dev) with this app's models, without the app: starts llama.cpp with every
rem llama.cpp model the app has on this PC, then pi. Pick a model with /model in pi; it loads when picked.
rem Strata modes (Flash Next) run only in the app.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Run setup.bat first.
  pause
  exit /b 1
)
.venv\Scripts\python -m localagent.pi_router %*
if errorlevel 1 pause
