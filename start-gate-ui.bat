@echo off
rem cc-rpm-gate UI (Python). Edit config, start/stop, watch live status.
rem Keep this file CRLF and pure ASCII: under chcp 65001 cmd.exe can split a
rem line of a .bat containing non-ASCII bytes and run half of it as a command.
rem Chinese text is printed by the Python side instead, which is unaffected.
chcp 65001 >nul
setlocal
cd /d "%~dp0python"

set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

rem Prefer pythonw.exe so no console window is left behind. Fall back to
rem python.exe (the window stays, which is also how errors stay visible).
rem Probe statements go straight on the command line: inside if ( ) blocks
rem the (3,8) would break cmd's paren matching, and >= would be a redirect.
set "PYEXE="
pyw -3 -c "import sys,tkinter;sys.exit(0 if sys.version_info[:2] >= (3,8) else 1)" >nul 2>nul
if not errorlevel 1 set "PYEXE=pyw -3"
if defined PYEXE goto run

pythonw -c "import sys,tkinter;sys.exit(0 if sys.version_info[:2] >= (3,8) else 1)" >nul 2>nul
if not errorlevel 1 set "PYEXE=pythonw"
if defined PYEXE goto run

py -3 -c "import sys,tkinter;sys.exit(0 if sys.version_info[:2] >= (3,8) else 1)" >nul 2>nul
if not errorlevel 1 set "PYEXE=py -3"
if defined PYEXE goto run

python -c "import sys,tkinter;sys.exit(0 if sys.version_info[:2] >= (3,8) else 1)" >nul 2>nul
if not errorlevel 1 set "PYEXE=python"
if defined PYEXE goto run

echo [cc-rpm-gate] Need Python 3.8+ with tkinter.
echo [cc-rpm-gate] Windows: use the python.org installer and keep "tcl/tk" checked.
echo [cc-rpm-gate] Or run the console version instead: start-gate-py.bat
pause
exit /b 1

:run
start "" %PYEXE% -m gate --ui %*
exit /b 0
