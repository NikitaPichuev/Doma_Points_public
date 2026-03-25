# Doma Swap Bot Public

Public sanitized copy of a Doma network bot for swaps, bridge actions, points checks, position closing, token sweeping, and volume farming.

## Included Modes

1. `Bridge`
2. `Check points`
3. `Close all positions`
4. `Swap domain token`
5. `Swap ETH <-> USDC.E`
6. `Sell all tokens -> USDC.E`
7. `Farm 250+ volume ETH <-> USDC.E`

## Requirements

- Windows
- Python 3.11+
- Access to Doma RPC
- Your own wallets, private keys, API keys, and proxies

## Quick Start

1. Copy `.env.example` to `.env`
2. Fill your own values in `.env`
3. Fill these files line-by-line:
   - `wallets.txt`
   - `keys.txt`
   - `api_keys.txt`
   - `proxies.txt`
4. Keep wallet, key, API key, and proxy lines aligned by the same wallet index
5. Run `INSTALL.bat`
6. Run `START.bat`

## Important Notes

- This is a public sanitized copy
- No real secrets should be included
- Contract addresses and token addresses in the repo are public on-chain data
- Volume and points on Doma can update with delay
- Proxy quality directly affects stability

## Files

- `main.py` - main menu and mode execution
- `doma_api.py` - Doma API, quote, router, and chain execution helpers
- `config.py` - config loading
- `contracts.json` - contract addresses
- `points_checker.py` - points-related helpers
- `position_manager.py` - position closing logic
- `relay_bridge.py` - bridge logic
- `strategy.py` - strategy helpers

## Safety

Read [SANITIZED_PUBLIC_COPY.txt](SANITIZED_PUBLIC_COPY.txt) before use.

You are responsible for:

- your private keys
- your proxies
- your RPC/provider stability
- gas costs
- slippage
- point farming profitability

## Disclaimer

Use at your own risk. Test with small balances first.
