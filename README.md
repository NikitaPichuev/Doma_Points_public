# DOMA Swap Bot Public

Public-safe CLI build for working with Doma.

This copy is prepared for open publication:

- no private keys
- no wallet addresses
- no API keys
- no proxy credentials
- no local cache or runtime artifacts

## Features

- bridge `Base -> Doma`
- bridge `ETH -> ETH`
- bridge `ETH -> USDC.E`
- swap `ETH <-> USDC.E`
- swap domain tokens on Doma
- round-trip flows: `forward -> back`
- close all LP positions
- multi-wallet processing
- line-by-line matching for `wallets.txt`, `keys.txt`, `api_keys.txt`, `proxies.txt`
- random delays between wallets and actions
- final colored summary after run

## Folder layout

- `.env.example` - safe environment template
- `wallets.txt` - one wallet per line
- `keys.txt` - one private key per line
- `api_keys.txt` - one API key per line
- `proxies.txt` - one proxy per line
- `contracts.json` - public chain/router/token config
- `INSTALL.bat` - dependency install and local file bootstrap
- `START.bat` - bot launcher
- `SANITIZED_PUBLIC_COPY.txt` - note about what was removed from this release

## Quick start

1. Run `INSTALL.bat`
2. Open `.env`
3. Fill your own `ACCOUNT_ADDRESS` if needed
4. Fill `wallets.txt` and `keys.txt` line-by-line
5. Optionally fill `api_keys.txt` and `proxies.txt`
6. Keep the same line order across all files
7. Run `START.bat`

## Safety notes

- This public copy no longer forces `PAPER_MODE` / `DRY_RUN`.
- Check `.env` before running and make sure execution flags match what you actually want.
- Live execution should be used only after you understand the routes, balances, RPC behavior, proxy behavior, and gas costs.
- Always test with small amounts first.

## Notes

- On-chain execution depends on liquidity, gas, RPC stability, and token/pool availability.
- If one wallet fails in supported modes, the bot continues with the next wallet and prints a final summary at the end.
- For line-based wallet usage, keep `wallets.txt`, `keys.txt`, `api_keys.txt`, and `proxies.txt` aligned by row number.
