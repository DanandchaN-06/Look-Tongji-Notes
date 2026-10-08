@echo off
rem Launcher for the Look Tongji Notes console (ASCII-only on purpose).
setlocal
set "PYTHONUTF8=1"
cd /d "%~dp0.."
set "SCRIPT=%~dp0start.py"

if defined LOOK_TONGJI_PYTHON (
  "%LOOK_TONGJI_PYTHON%" "%SCRIPT%" %*
  goto :done
)
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" "%SCRIPT%" %*
  goto :done
)

where py >nul 2>nul
if %errorlevel%==0 (
  py -3 "%SCRIPT%" %*
  goto :done
)

where python >nul 2>nul
if %errorlevel%==0 (
  python "%SCRIPT%" %*
  goto :done
)

echo Python 3 was not found in PATH. Install Python 3 first.
pause

:done
if errorlevel 1 pause
endlocal
