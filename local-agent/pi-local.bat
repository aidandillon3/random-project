@echo off
rem The pi coding agent (pi.dev) with this app's models, without the app: starts llama.cpp with every
rem llama.cpp model the app has on this PC, then pi. Pick a model with /model in pi; it loads when picked.
rem Strata modes (Flash Next) run only in the app.
rem pi works in the folder this is started from (no cd here).
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)
set "PYTHONPATH=%~dp0."
"%~dp0.venv\Scripts\python" -m localagent.pi_router %*
if errorlevel 1 pause
