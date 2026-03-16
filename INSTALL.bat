@echo off
setlocal
cd /d "%~dp0"

echo [1/3] Checking Python...
python --version >nul 2>&1
if errorlevel 1 (
  echo Python not found in PATH.
  echo Install Python 3.11+ and re-run INSTALL.bat
  pause
  exit /b 1
)

echo [2/3] Installing dependencies...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo Failed to install requirements.
  pause
  exit /b 1
)

echo [3/3] Preparing local files...
if not exist ".env" (
  (
    echo RPC_URL=https://doma.drpc.org
    echo SUBGRAPH_URL=https://graph.doma.xyz/subgraphs/name/uniswap-v3-doma-mainnet
    echo CHAIN_ID=97477
    echo KEYS_FILE=keys.txt
    echo API_KEYS_FILE=api_keys.txt
    echo PROXY_FILE=proxies.txt
    echo CONTRACTS_FILE=contracts.json
    echo ALLOWLIST_POOLS_FILE=allowlist_pools.txt
    echo ACCOUNT_ADDRESS=
    echo PRIVATE_KEY=
    echo PAPER_MODE=true
    echo DRY_RUN=true
    echo ENABLE_EXECUTION=false
  ) > ".env"
)
if not exist "keys.txt" type nul > "keys.txt"
if not exist "api_keys.txt" type nul > "api_keys.txt"
if not exist "proxies.txt" type nul > "proxies.txt"
if not exist "allowlist_pools.txt" type nul > "allowlist_pools.txt"
if not exist "contracts.json" (
  (
    echo {
    echo   "router_address": "",
    echo   "quoter_address": "",
    echo   "router_variant": "with_deadline",
    echo   "default_fee_tier": 500,
    echo   "tokens": {
    echo     "WETH": "0x4200000000000000000000000000000000000006",
    echo     "USDC.E": "0x31eef89d5215c305304a2fa5376a1f1b6c5dc477"
    echo   },
    echo   "allowlist_pools": [],
    echo   "bootstrap_swaps": ["ETH^>USDC.E", "USDC.E^>ETH"]
    echo }
  ) > "contracts.json"
)

echo.
echo Install complete.
echo Next:
echo 1) Fill .env (ACCOUNT_ADDRESS)
echo 2) Put private key in first line of keys.txt
echo 3) Put DOMA API key in first line of api_keys.txt
echo 4) Optional proxy in first line of proxies.txt
echo 5) Run START.bat
pause
exit /b 0
