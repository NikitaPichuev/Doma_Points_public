from __future__ import annotations

import random
import sys
import time
from datetime import datetime, timezone

from config import BotConfig
from doma_api import DomaApiClient


def main() -> None:
    cfg = BotConfig()
    cli_wallets = [w.strip() for w in sys.argv[1:] if w.strip().startswith("0x")]
    wallets = cli_wallets or cfg.points_wallets or ([cfg.account_address] if cfg.account_address else [])
    if not wallets:
        print("No wallets configured. Add wallets.txt or pass addresses in CLI.")
        return

    for idx, wallet in enumerate(wallets):
        api_key = cfg.file_api_keys[idx].strip() if idx < len(cfg.file_api_keys) else ""
        if not api_key and cfg.file_api_keys:
            print(f"Points check skipped for line={idx + 1}: no API key on same line")
            continue
        if not api_key and cfg.doma_api_key.strip():
            api_key = cfg.doma_api_key.strip()

        proxy = cfg.file_proxies[idx].strip() if idx < len(cfg.file_proxies) else ""
        if cfg.file_proxies and idx >= len(cfg.file_proxies):
            print(f"Points check skipped for line={idx + 1}: no proxy on same line")
            continue

        proxies = {"http": proxy, "https": proxy} if proxy else None
        api = DomaApiClient(
            cfg.doma_api_url,
            api_key=api_key,
            api_keys=[api_key] if api_key else [],
            proxies=proxies,
        )
        try:
            snapshot = api.fetch_points(wallet, cfg.leaderboard_rank_by)
        except Exception as exc:
            print(f"Points check failed for {wallet} [line={idx + 1}]: {exc}")
            continue
        if not snapshot:
            print(f"No leaderboard row found for wallet: {wallet}")
            continue
        print(
            f"[{datetime.now(timezone.utc).isoformat()}] "
            f"line={idx + 1} "
            f"wallet={snapshot.wallet_address} rank={snapshot.rank} "
            f"total_entries={snapshot.total_snapshot_entries} "
            f"weekly_points={snapshot.points} season_points={snapshot.trading_volume_usd} "
            f"meta={snapshot.snapshot_date}"
        )
        if idx < len(wallets) - 1 and cfg.wallet_delay_max_sec > 0:
            delay_sec = random.uniform(cfg.wallet_delay_min_sec, cfg.wallet_delay_max_sec)
            print(f"Delay before next wallet: {delay_sec:.2f} sec")
            time.sleep(delay_sec)


if __name__ == "__main__":
    main()
