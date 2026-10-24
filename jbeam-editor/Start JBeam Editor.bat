@echo off
rem Starts the JBeam Editor in its own window (no console window stays open).
rem If something goes wrong, see data\editor.log or run "python run.py" in a console.
cd /d "%~dp0"
if not exist "run.py" goto :notextracted
if not exist "editor\server.py" goto :notextracted

rem Find a Python 3.8+ that really runs (the Microsoft Store "python" shortcut does not).
set "PYCHECK=import sys; sys.exit(0 if sys.version_info[:2] >= (3, 8) else 1)"
py -3 -c "%PYCHECK%" >nul 2>nul && goto :usepy
python -c "%PYCHECK%" >nul 2>nul && goto :usepython
python3 -c "%PYCHECK%" >nul 2>nul && goto :usepython3
goto :nopython

:usepy
echo Starting the JBeam Editor...
where pyw >nul 2>nul || goto :visiblepy
start "" pyw -3 run.py %*
exit /b 0
:visiblepy
py -3 run.py %*
goto :done

:usepython
set "PYEXE=python"
goto :usefound
:usepython3
set "PYEXE=python3"
:usefound
echo Starting the JBeam Editor...
set "PYW="
for /f "delims=" %%i in ('%PYEXE% -c "import os, sys; print(os.path.join(os.path.dirname(sys.executable), 'pythonw.exe'))"') do set "PYW=%%i"
if defined PYW if exist "%PYW%" (
  start "" "%PYW%" run.py %*
  exit /b 0
)
%PYEXE% run.py %*
goto :done

:notextracted
echo The JBeam Editor's files are not next to this launcher.
echo.
echo If you opened it from inside the zip, extract the zip first:
echo right-click jbeam-editor.zip, choose Extract All, then open the extracted
echo folder and double-click Start JBeam Editor.bat there.
echo.
pause
exit /b 1

:nopython
echo The JBeam Editor needs Python 3.8 or newer, and none was found.
echo.
echo Get it from https://www.python.org/downloads/ and tick
echo "Add python.exe to PATH" on the first page of the installer.
echo Then double-click Start JBeam Editor.bat again.
echo.
echo (The "python" that opens the Microsoft Store does not count.)
echo.
pause
exit /b 1

:done
if errorlevel 1 pause
