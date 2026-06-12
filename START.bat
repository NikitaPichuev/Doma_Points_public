@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1

python --version >nul 2>&1
if errorlevel 1 (
  echo Python was not found in PATH.
  echo Install Python 3.11 or newer first.
  pause
  exit /b 1
)

if not exist ".env" (
  echo .env was not found.
  echo Run INSTALL.bat first.
  pause
  exit /b 1
)

echo Starting bot...
python main.py
set EXIT_CODE=%errorlevel%
echo.
echo Process exited with code %EXIT_CODE%
pause
exit /b %EXIT_CODE%
