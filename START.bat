@echo off
setlocal
cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
  echo Python not found in PATH.
  pause
  exit /b 1
)

if not exist ".env" (
  echo .env not found. Run INSTALL.bat first.
  pause
  exit /b 1
)

echo Starting bot...
python main.py
echo.
echo Process exited with code %errorlevel%
pause
exit /b %errorlevel%

