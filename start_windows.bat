@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

where py >nul 2>nul
if not errorlevel 1 (
  py -3 bootstrap.py %*
  set "EXIT_CODE=!errorlevel!"
) else (
  where python >nul 2>nul
  if errorlevel 1 (
    echo Python 3.10+ was not found. Install it from https://www.python.org/downloads/windows/
    pause
    exit /b 1
  )
  python bootstrap.py %*
  set "EXIT_CODE=!errorlevel!"
)

if not "%EXIT_CODE%"=="0" pause
exit /b %EXIT_CODE%
