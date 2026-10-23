@echo off
rem Starts the JBeam Editor in its own window (no console window stays open).
rem If something goes wrong, see data\editor.log or run "python run.py" in a console.
cd /d "%~dp0"
where pyw >nul 2>nul
if %errorlevel%==0 (
  start "" pyw -3 run.py %*
  exit /b 0
)
where pythonw >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw run.py %*
  exit /b 0
)
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 run.py %*
  goto :done
)
where python >nul 2>nul
if %errorlevel%==0 (
  python run.py %*
  goto :done
)
echo The JBeam Editor needs Python 3.8 or newer.
echo Get it from https://www.python.org/downloads/ and tick "Add python.exe to PATH" when installing.
pause
exit /b 1
:done
if errorlevel 1 pause
