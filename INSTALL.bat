@echo off
setlocal
cd /d "%~dp0"

echo [1/4] Checking Python...
python --version >nul 2>&1
if errorlevel 1 (
  echo Python was not found in PATH.
  echo Install Python 3.11 or newer and run INSTALL.bat again.
  pause
  exit /b 1
)

echo [2/4] Installing dependencies...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo Failed to install dependencies.
  pause
  exit /b 1
)

echo [3/4] Preparing local config files...
if not exist ".env" (
  copy /y ".env.example" ".env" >nul
)
if not exist "wallets.txt" (
  (
    echo # One wallet address per line ^(0x...^)
    echo # wallet line N must match key/api/proxy line N
  ) > "wallets.txt"
)
if not exist "keys.txt" (
  (
    echo # One private key per line ^(0x...^)
    echo # wallet line N must match key/api/proxy line N
  ) > "keys.txt"
)
if not exist "api_keys.txt" (
  (
    echo # One DOMA API key per line ^(optional^)
  ) > "api_keys.txt"
)
if not exist "proxies.txt" (
  (
    echo # Optional proxy per line
    echo # format: ip:port or http://ip:port
  ) > "proxies.txt"
)
if not exist "allowlist_pools.txt" type nul > "allowlist_pools.txt"

echo [4/4] Done.
echo.
echo Next steps:
echo 1. Open .env and review the settings
echo 2. Fill wallets.txt and keys.txt line-by-line
echo 3. Optionally fill api_keys.txt and proxies.txt
echo 4. Keep all files aligned by line number
echo 5. Run START.bat
pause
exit /b 0
