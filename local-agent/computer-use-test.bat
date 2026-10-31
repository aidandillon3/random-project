@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Run setup.bat first.
  pause
  exit /b 1
)
echo Computer use test: the model does small tasks with your mouse and keyboard.
echo Start the model server first. Don't touch the mouse; push it into a screen corner to stop.
echo Compare runs: computer-use-test.bat --compare helpers,thinking
echo.
.venv\Scripts\python -m localagent.cubench %*
pause
