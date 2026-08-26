from __future__ import annotations

import csv
import base64
import json
import logging
import os
import random
import re
import requests
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from eth_account import Account
from eth_account.messages import encode_defunct
from web3 import Web3

from config import BotConfig
from galxe_api import (
    GALXE_DEFAULT_CAMPAIGN_ID,
    GALXE_DEFAULT_CHAIN,
    GALXE_DEFAULT_QUEST_URL,
    GalxeApiClient,
    get_galxe_captcha_input,
)
from doma_api import (
    DomainListing,
    DomainOfferCandidate,
    DomainReceivedOffer,
    DomaApiClient,
    DomaSubgraphClient,
    DOMA_INTERFACE_PORTION_BIPS,
    DOMA_INTERFACE_PORTION_RECIPIENT,
    DOMA_NATIVE_TOKEN_SENTINEL,
    ERC20_ABI,
    EvmExecutionClient,
    LaunchpadTokenInfo,
    OwnedDomain,
    PointsSnapshot,
    Pool,
    QuestStatus,
    StakedSubdomain,
    Token,
    decimal_to_raw,
    pick_token_usd_price,
    raw_to_decimal,
)
from okx_api import OkxApiClient
from relay_bridge import NATIVE_ETH, execute_relay_swap, run_bridge_tasks
from strategy import StrategyEngine
from position_manager import PositionManagerClient


@dataclass
class BotState:
    day_utc: str
    daily_volume_usd: Decimal
    last_tx_hash: str = ""
    last_error: str = ""
    bootstrap_completed: List[str] = field(default_factory=list)
    last_points_check_ts: int = 0
    last_bridge_ts: int = 0
    used_bridge_amounts: List[str] = field(default_factory=list)

    @classmethod
    def create_default(cls) -> "BotState":
        return cls(day_utc=datetime.now(timezone.utc).strftime("%Y-%m-%d"), daily_volume_usd=Decimal("0"))


WALLET_LOG_NAMES: Dict[str, str] = {}
ANSI_RESET = "\033[0m"
ANSI_DIM = "\033[90m"
ANSI_GREEN = "\033[92m"
ANSI_RED = "\033[91m"
ANSI_YELLOW = "\033[93m"
ANSI_CYAN = "\033[96m"
ANSI_WHITE = "\033[97m"


def _redact_wallet_addresses(text: str) -> str:
    out = str(text)
    for address, label in WALLET_LOG_NAMES.items():
        out = re.sub(re.escape(address), label, out, flags=re.IGNORECASE)
    return out


class WalletAddressRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not WALLET_LOG_NAMES:
            return True
        record.msg = _redact_wallet_addresses(record.getMessage())
        record.args = ()
        return True


class ColoredConsoleFormatter(logging.Formatter):
    LEVEL_COLORS = {
        logging.DEBUG: ANSI_DIM,
        logging.INFO: ANSI_WHITE,
        logging.WARNING: ANSI_YELLOW,
        logging.ERROR: ANSI_RED,
        logging.CRITICAL: ANSI_RED,
    }

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        color = self.LEVEL_COLORS.get(record.levelno, ANSI_WHITE)
        return f"{color}{line}{ANSI_RESET}"


def _enable_windows_ansi_colors() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


def install_wallet_log_names(logger: logging.Logger, wallets: List[str]) -> None:
    WALLET_LOG_NAMES.clear()
    for idx, wallet in enumerate(wallets, start=1):
        if _is_valid_evm_address(wallet):
            WALLET_LOG_NAMES[wallet.lower()] = f"wallet#{idx}"
    redaction_filter = WalletAddressRedactionFilter()
    logger.addFilter(redaction_filter)
    for handler in logger.handlers:
        handler.addFilter(redaction_filter)


def setup_logger(log_path: Path) -> logging.Logger:
    _enable_windows_ansi_colors()
    logger = logging.getLogger("doma_swap_bot")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    console_fmt = ColoredConsoleFormatter("%(asctime)s | %(levelname)s | %(message)s")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(console_fmt)
    logger.addHandler(sh)
    return logger


MIN_EXECUTABLE_TRADE_USD = Decimal("0.10")
DOMA_QUOTE_RETRY_DELAYS_SEC = (0.0, 2.0, 5.0, 10.0)
DOMAIN_QUEST_COMPLETION_THRESHOLD_USD = Decimal("25")
DOMAIN_QUEST_TOKENS = [
    "agenticconsultant.ai",
    "alert.ai",
    "bipod.ai",
    "bitcoinhalvingcycles.com",
    "boner.com",
    "brag.com",
    "buyhigh.xyz",
    "closingbells.com",
    "coinlogic.ai",
    "continents.ai",
    "depin.ai",
    "exemption.ai",
    "fyi.xyz",
    "get.cash",
    "gobitcoin.xyz",
    "hightech.xyz",
    "investors.xyz",
    "itprojects.ai",
    "lifeadvice.ai",
    "loancrypto.ai",
    "mishka.ai",
    "onlineadvisor.ai",
    "payasap.xyz",
    "playonline.ai",
    "pointrogram.xyz",
    "rides.com",
    "riskvault.xyz",
    "seedfunding.ai",
    "smoothie.com",
    "software.ai",
    "swimsuits.ai",
    "terabytes.ai",
    "trenches.ai",
    "wines.xyz",
    "worldtravels.com",
]
DOMAIN_QUEST_MIN_BUY_USD = {
    "fyi.xyz": Decimal("5"),
    "smoothie.com": Decimal("5"),
    "wines.xyz": Decimal("5"),
}
CHEAP_BUY_TOKEN_BLOCKLIST = {
    "agenttoken.com",
    "barbeque.io",
    "discordwallets.com",
    "digitalcreate.art",
    "escalations.com",
    "favours.ai",
    "foundations.xyz",
    "ilovepunch.xyz",
    "mishka.ai",
    "onlineadvisor.ai",
    "overweights.xyz",
    "payportal.ai",
    "stackfour.com",
    "superbowl.world",
    "tradetheinternet.com",
    "trenches.ai",
    "wines.xyz",
    "yearofthefirehorse.com",
}
DOMAIN_LISTING_CSV = Path("domain_listings.csv")
DOMAIN_DELISTING_CSV = Path("domain_delistings.csv")
DOMAIN_BRIDGE_CSV = Path("domain_bridges.csv")
DOMAIN_OFFERS_CSV = Path("domain_offers.csv")
DOMAIN_ACCEPTED_OFFERS_CSV = Path("domain_accepted_offers.csv")
DOMAIN_PURCHASES_CSV = Path("domain_purchases.csv")
DOMAIN_QUESTS_CSV = Path("quests.csv")
DOMAIN_LIQUIDITY_CSV = Path("domain_liquidity_positions.csv")
DOMAIN_COM_DAILY_CSV = Path("domain_com_daily_swaps.csv")
DOMAIN_BONDING_BUYS_CSV = Path("domain_bonding_buys.csv")
DOMA_DAILY_ROLLCALL_CSV = Path("doma_daily_rollcall.csv")
GALXE_CLAIMS_CSV = Path("galxe_claims.csv")
DOMA_COST_REPORT_CSV = Path("doma_cost_report.csv")
OKX_WITHDRAWALS_CSV = Path("okx_withdrawals.csv")
EXCHANGE_DEPOSITS_CSV = Path("exchange_deposits.csv")
PRIVY_APP_ID = "cm9jd3vun03ptju0knmkls1zp"
PRIVY_CLIENT_ID = "client-WY5iumRYaVR7RsCRhMD2ACF3MssqVB59ebRgJbpZwUxZd"
PRIVY_API_BASE_URL = "https://auth.privy.io"
DOMAIN_LIQUIDITY_MINT_BUFFER = Decimal("1.06")
DOMAIN_LIQUIDITY_MIN_EXTRA_USD = Decimal("0.25")
DOMAIN_LIQUIDITY_SWAP_BUFFER = Decimal("1.03")
DOMAIN_LIQUIDITY_MIN_BALANCE_RATIO = Decimal("0.97")
NATIVE_GAS_RESERVE_USD = Decimal("0.03")
NATIVE_GAS_RESERVE_FALLBACK_ETH = Decimal("0.00002")
BONDING_DAILY_GAS_RESERVE_USD = Decimal("0.10")
BONDING_DAILY_BOOTSTRAP_GAS_BUFFER_USD = Decimal("0.03")
BONDING_DAILY_INITIAL_MIN_USDCE = Decimal("1")
SWEEP_MIN_TOKEN_VALUE_USD = Decimal("0.01")
DOMAIN_PURCHASE_RELIST_MARKUP_MIN = Decimal("0.02")
DOMAIN_PURCHASE_RELIST_MARKUP_MAX = Decimal("0.05")
DOMAIN_PURCHASE_CLAIM_WAIT_TIMEOUT_SEC = 45 * 60
DOMAIN_PURCHASE_CLAIM_WAIT_INTERVAL_SEC = 30
WEEKLY_VOLUME_TOPUP_BUFFER_USD = Decimal("1")


def _load_sweep_token_exclusions(path: Path) -> set[str]:
    if not path.exists():
        return set()
    exclusions: set[str] = set()
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        value = raw_line.split("#", 1)[0].strip().lower()
        if value:
            exclusions.add(value)
    return exclusions


def _is_sweep_token_excluded(info: LaunchpadTokenInfo, exclusions: set[str]) -> bool:
    if not exclusions:
        return False
    identifiers = {
        str(info.name or "").strip().lower(),
        str(info.symbol or "").strip().lower(),
        str(info.address or "").strip().lower(),
    }
    identifiers.discard("")
    return bool(identifiers & exclusions)


KNOWN_ETH_USDCE_POOL_ADDRESSES = [
    "0xd604c96e51DF995bb46FAb0E3FC1b18d985AA8f5",
    "0xe8E9ca039B1a9467eD32E8B2337f657c8c794754",
    "0x8db8e9a11c37544e1b274452f932835f4aab6db2",
]
DOMAIN_LISTING_DEFAULT_DURATION_DAYS = 90
DOMAIN_LISTING_DEFAULT_DELAY_MIN_SEC = Decimal("4")
DOMAIN_LISTING_DEFAULT_DELAY_MAX_SEC = Decimal("10")
DOMAIN_LISTING_SOURCE = "doma-swap-bot-public"
PROXY_DOMA_RECORD_ADDRESS = "0xd0000000000067CB44aE7b6aC3AB5764dE20A3E2"
BASE_CHAIN_ID = 8453
BASE_CHAIN_CAIP2 = "eip155:8453"
BASE_RPC_FALLBACK_URLS = [
    "https://mainnet.base.org",
    "https://base-rpc.publicnode.com",
    "https://base.llamarpc.com",
]
DOMAIN_OFFER_MIN_ETH_EQUIVALENT = Decimal("0.0001")


def _native_gas_reserve_eth(eth_price: Decimal) -> Decimal:
    if eth_price > 0:
        return NATIVE_GAS_RESERVE_USD / eth_price
    return NATIVE_GAS_RESERVE_FALLBACK_ETH


def _spendable_native_eth(exec_client: EvmExecutionClient, eth_price: Decimal) -> Decimal:
    return max(
        Decimal("0"),
        exec_client.get_native_balance() - _native_gas_reserve_eth(eth_price),
    )


def _color(text: str, code: str) -> str:
    return f"{code}{text}{ANSI_RESET}"


def _wallet_progress_label(idx: int, total: int, wallet: str) -> str:
    return f"{idx + 1}/{total} - {wallet}"


def _wallet_record_progress_label(process_idx: int, selected_total: int, line_idx: int, total_wallets: int, wallet: str) -> str:
    return f"{process_idx + 1}/{selected_total} | wallet#{line_idx + 1}/{total_wallets} - {wallet}"


def _current_week_start_utc() -> datetime:
    now = datetime.now(timezone.utc)
    today_utc = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    return today_utc - timedelta(days=now.weekday())


def _fetch_public_eth_price_usd(proxies: Optional[Dict[str, str]] = None) -> Decimal:
    errors: List[str] = []
    try:
        resp = requests.get(
            "https://api.coinbase.com/v2/exchange-rates",
            params={"currency": "ETH"},
            timeout=15,
            proxies=proxies,
        )
        resp.raise_for_status()
        usd_raw = ((resp.json().get("data") or {}).get("rates") or {}).get("USD")
        price = Decimal(str(usd_raw or "0"))
        if price > 0:
            return price
    except Exception as exc:
        errors.append(f"coinbase: {exc}")

    try:
        resp = requests.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": "ETHUSDT"},
            timeout=15,
            proxies=proxies,
        )
        resp.raise_for_status()
        price = Decimal(str(resp.json().get("price") or "0"))
        if price > 0:
            return price
    except Exception as exc:
        errors.append(f"binance: {exc}")

    raise RuntimeError("Failed to resolve public ETH/USD price: " + " | ".join(errors))


def _is_proxy_connectivity_error(exc: Exception) -> bool:
    message = str(exc).lower()
    markers = [
        "proxyerror",
        "cannot connect to proxy",
        "failed to establish a new connection",
        "max retries exceeded",
        "winerror 10065",
        "proxy authentication required",
        "tunnel connection failed",
        "failed to parse",
        "squid software foundation",
        "service unavailable",
        "status 503",
        "timeout",
        "timed out",
    ]
    return any(marker in message for marker in markers)


def _is_proxy_connectivity_text(text: str) -> bool:
    message = str(text or "").lower()
    markers = [
        "proxyerror",
        "cannot connect to proxy",
        "failed to establish a new connection",
        "max retries exceeded",
        "winerror 10065",
        "proxy authentication required",
        "tunnel connection failed",
        "failed to parse",
        "squid software foundation",
        "service unavailable",
        "status 503",
        "timeout",
        "timed out",
    ]
    return any(marker in message for marker in markers)


def _random_swap_delay_sec() -> float:
    return random.uniform(4, 10)


def _pick_delay_from_range(delay_range: Optional[Tuple[float, float]] = None) -> float:
    if delay_range is None:
        return _random_swap_delay_sec()
    delay_min, delay_max = delay_range
    return random.uniform(delay_min, delay_max)


def _doma_rpc_candidates(cfg: BotConfig) -> List[str]:
    candidates: List[str] = []
    for rpc_url in [cfg.rpc_url, *getattr(cfg, "doma_rpc_urls", []), "https://rpc.doma.xyz/", "https://doma.drpc.org/"]:
        normalized = (rpc_url or "").strip()
        if normalized and normalized not in candidates:
            candidates.append(normalized)
    return candidates


def _l2_deposit_networks() -> List[Tuple[str, int, str, List[str], Decimal]]:
    return [
        ("Base | native ETH", 8453, "ETH", ["https://mainnet.base.org", "https://base-rpc.publicnode.com", "https://base.llamarpc.com"], Decimal("0.00002")),
        ("Arbitrum | native ETH", 42161, "ETH", ["https://arbitrum-one.publicnode.com", "https://arb1.arbitrum.io/rpc"], Decimal("0.00002")),
        ("Optimism | native ETH", 10, "ETH", ["https://optimism-rpc.publicnode.com", "https://mainnet.optimism.io"], Decimal("0.00002")),
        ("Blast | native ETH", 81457, "ETH", ["https://rpc.blast.io", "https://blast-rpc.publicnode.com"], Decimal("0.00002")),
        ("Mantle | native MNT", 5000, "MNT", ["https://rpc.mantle.xyz", "https://mantle-rpc.publicnode.com"], Decimal("0.2")),
    ]


def _build_exec_client_for_chain_rpcs(
    cfg: BotConfig,
    logger: logging.Logger,
    wallet: str,
    private_key: str,
    chain_id: int,
    rpc_urls: List[str],
    proxies: Optional[Dict[str, str]],
    log_prefix: str,
) -> EvmExecutionClient:
    errors: List[str] = []
    proxy_variants: List[Tuple[str, Optional[Dict[str, str]]]] = [("proxy", proxies)] if proxies else [("direct", None)]
    for rpc_url in rpc_urls:
        for proxy_label, request_proxies in proxy_variants:
            try:
                client = EvmExecutionClient(
                    rpc_url=rpc_url,
                    chain_id=chain_id,
                    account_address=wallet,
                    private_key=private_key,
                    router_address="",
                    quoter_address="",
                    router_variant=cfg.router_variant,
                    request_proxies=request_proxies,
                )
                actual_chain_id = client.get_chain_id()
                if actual_chain_id != chain_id:
                    raise RuntimeError(f"chain_id mismatch: rpc={actual_chain_id} expected={chain_id}")
                logger.info("%s wallet=%s RPC selected | url=%s | mode=%s", log_prefix, wallet, rpc_url, proxy_label)
                return client
            except Exception as exc:
                errors.append(f"{rpc_url} ({proxy_label}): {exc}")
    raise RuntimeError("All RPC attempts failed: " + " | ".join(errors))


def _build_exec_client_with_rpc_fallback(
    cfg: BotConfig,
    logger: logging.Logger,
    wallet: str,
    private_key: str,
    proxies: Optional[Dict[str, str]],
    log_prefix: str,
) -> EvmExecutionClient:
    errors: List[str] = []
    proxy_variants: List[Tuple[str, Optional[Dict[str, str]]]]
    if proxies:
        proxy_variants = [("proxy", proxies)]
    else:
        proxy_variants = [("direct", None)]

    for rpc_url in _doma_rpc_candidates(cfg):
        for proxy_label, request_proxies in proxy_variants:
            try:
                client = EvmExecutionClient(
                    rpc_url=rpc_url,
                    chain_id=cfg.chain_id,
                    account_address=wallet,
                    private_key=private_key,
                    router_address=cfg.router_address,
                    quoter_address=cfg.quoter_address,
                    router_variant=cfg.router_variant,
                    request_proxies=request_proxies,
                )
                actual_chain_id = client.get_chain_id()
                if actual_chain_id != cfg.chain_id:
                    raise RuntimeError(f"chain_id mismatch: rpc={actual_chain_id} cfg={cfg.chain_id}")
                if rpc_url != cfg.rpc_url or proxy_label == "direct":
                    logger.info(
                        "%s wallet=%s RPC selected | url=%s | mode=%s",
                        log_prefix,
                        wallet,
                        rpc_url,
                        proxy_label,
                    )
                return client
            except Exception as exc:
                errors.append(f"{rpc_url} ({proxy_label}): {exc}")
    raise RuntimeError("All RPC attempts failed: " + " | ".join(errors))


def _build_position_manager_client_with_rpc_fallback(
    cfg: BotConfig,
    logger: logging.Logger,
    wallet: str,
    private_key: str,
    proxies: Optional[Dict[str, str]],
    log_prefix: str,
) -> PositionManagerClient:
    errors: List[str] = []
    proxy_variants: List[Tuple[str, Optional[Dict[str, str]]]]
    if proxies:
        proxy_variants = [("proxy", proxies)]
    else:
        proxy_variants = [("direct", None)]

    for rpc_url in _doma_rpc_candidates(cfg):
        for proxy_label, request_proxies in proxy_variants:
            try:
                client = PositionManagerClient(
                    rpc_url=rpc_url,
                    chain_id=cfg.chain_id,
                    account_address=wallet,
                    private_key=private_key,
                    position_manager_address=cfg.position_manager_address,
                    request_proxies=request_proxies,
                )
                actual_chain_id = int(client.web3.eth.chain_id)
                if actual_chain_id != cfg.chain_id:
                    raise RuntimeError(f"chain_id mismatch: rpc={actual_chain_id} cfg={cfg.chain_id}")
                if rpc_url != cfg.rpc_url or proxy_label == "direct":
                    logger.info(
                        "%s wallet=%s position RPC selected | url=%s | mode=%s",
                        log_prefix,
                        wallet,
                        rpc_url,
                        proxy_label,
                    )
                return client
            except Exception as exc:
                errors.append(f"{rpc_url} ({proxy_label}): {exc}")
    raise RuntimeError("All position manager RPC attempts failed: " + " | ".join(errors))


def _cleanup_weth_balance(
    logger: logging.Logger,
    exec_client: EvmExecutionClient,
    weth_token: Token,
    label: str,
    reason: str,
    wait_for_receipt: bool = True,
) -> bool:
    weth_balance = exec_client.get_erc20_balance(weth_token.address, weth_token.decimals)
    weth_raw = decimal_to_raw(weth_balance, weth_token.decimals)
    if weth_raw <= 0:
        return True

    logger.info(
        "[%s] %s | found leftover WETH=%.8f, unwrapping to ETH",
        label,
        reason,
        float(weth_balance),
    )
    unwrap_tx = exec_client.unwrap_weth(weth_token.address, weth_raw)
    if not unwrap_tx:
        return True
    logger.info("[%s] Cleanup WETH->ETH tx sent: %s", label, unwrap_tx)
    if wait_for_receipt:
        ok = _wait_tx_receipt(exec_client, unwrap_tx, timeout_sec=180)
        if not ok:
            logger.warning("[%s] Cleanup WETH->ETH tx failed or timed out", label)
            return False
        delay_sec = _random_swap_delay_sec()
        logger.info("[%s] delay after cleanup: %.2f sec", label, delay_sec)
        time.sleep(delay_sec)
    return True


def _print_mode_summary(
    mode: str,
    total: int,
    success: int,
    failed: int,
    skipped: int = 0,
    failed_wallets: Optional[List[str]] = None,
) -> None:
    processed = success + failed + skipped
    parts = [
        _color(f"[{mode}] Итог", ANSI_CYAN),
        f"кошельков: {total}",
        _color(f"выполнено: {success}", ANSI_GREEN),
        _color(f"ошибки: {failed}", ANSI_RED),
    ]
    if skipped:
        parts.append(_color(f"пропущено: {skipped}", ANSI_YELLOW))
    parts.append(f"обработано: {processed}")
    print("\n" + " | ".join(parts))
    if failed_wallets:
        print(_color(f"[{mode}] кошельки с ошибками: {'; '.join(failed_wallets)}", ANSI_RED))


def ensure_csv(path: Path, header: List[str], delimiter: str = ",") -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=delimiter)
        w.writerow(header)


def append_csv(path: Path, row: List[object], delimiter: str = ",") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=delimiter)
        w.writerow(row)


def load_state(path: Path) -> BotState:
    if not path.exists():
        return BotState.create_default()
    raw = json.loads(path.read_text(encoding="utf-8"))
    return BotState(
        day_utc=raw["day_utc"],
        daily_volume_usd=Decimal(raw["daily_volume_usd"]),
        last_tx_hash=raw.get("last_tx_hash", ""),
        bootstrap_completed=raw.get("bootstrap_completed", []),
        last_points_check_ts=int(raw.get("last_points_check_ts", 0)),
        last_bridge_ts=int(raw.get("last_bridge_ts", 0)),
        used_bridge_amounts=raw.get("used_bridge_amounts", []),
    )


def save_state(path: Path, state: BotState) -> None:
    raw = {
        "day_utc": state.day_utc,
        "daily_volume_usd": str(state.daily_volume_usd),
        "last_tx_hash": state.last_tx_hash,
        "bootstrap_completed": state.bootstrap_completed,
        "last_points_check_ts": state.last_points_check_ts,
        "last_bridge_ts": state.last_bridge_ts,
        "used_bridge_amounts": state.used_bridge_amounts[-500:],
    }
    path.write_text(json.dumps(raw, indent=2), encoding="utf-8")


def rotate_daily_state(state: BotState) -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.day_utc != today:
        state.day_utc = today
        state.daily_volume_usd = Decimal("0")
        state.last_tx_hash = ""


def should_stop(cfg: BotConfig) -> bool:
    return cfg.stop_file.exists()


def _read_nonempty_lines(path: Path) -> List[str]:
    if not path.exists():
        return []
    out: List[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.replace("\ufeff", "").strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def _build_wallet_key_records(cfg: BotConfig, logger: logging.Logger, prefix: str) -> List[Tuple[int, str, str]]:
    key_lines = _read_nonempty_lines(cfg.keys_file)
    if not key_lines and cfg.private_key:
        key_lines = [cfg.private_key]

    wallets = [w for w in cfg.points_wallets if _is_valid_evm_address(w)]
    if not wallets and _is_valid_evm_address(cfg.account_address):
        wallets = [cfg.account_address]

    records: List[Tuple[int, str, str]] = []
    for i, wallet in enumerate(wallets):
        if i < len(key_lines) and key_lines[i].strip():
            records.append((i, wallet.lower(), key_lines[i].strip()))
        elif i == 0 and cfg.private_key:
            records.append((i, wallet.lower(), cfg.private_key))
        else:
            logger.warning("[%s] skip wallet %s (line %s): no private key on matching line", prefix, wallet, i + 1)
    return records


def _prompt_start_wallet_number(total_wallets: int) -> int:
    if total_wallets <= 1:
        return 1
    while True:
        raw = input(f"Start from wallet number [1-{total_wallets}, default 1]: ").strip()
        if not raw:
            return 1
        try:
            value = int(raw)
        except ValueError:
            print(f"Enter a number from 1 to {total_wallets}.")
            continue
        if 1 <= value <= total_wallets:
            return value
        print(f"Enter a number from 1 to {total_wallets}.")


def _prompt_end_wallet_number(total_wallets: int, start_number: int) -> int:
    if total_wallets <= 1:
        return total_wallets
    while True:
        raw = input(f"End at wallet number [{start_number}-{total_wallets}, default {total_wallets}]: ").strip()
        if not raw:
            return total_wallets
        try:
            value = int(raw)
        except ValueError:
            print(f"Enter a number from {start_number} to {total_wallets}.")
            continue
        if start_number <= value <= total_wallets:
            return value
        print(f"Enter a number from {start_number} to {total_wallets}.")


def _prompt_wallet_order(default_random: bool = True) -> str:
    default = "1" if default_random else "2"
    while True:
        print("Wallet order:")
        print("1) Random")
        print("2) In order")
        raw = input(f"Select [1-2, default {default}]: ").strip() or default
        if raw == "1":
            return "random"
        if raw == "2":
            return "ordered"
        print("Enter 1 or 2.")


def _apply_wallet_order(records: List[Tuple[int, str, str]], order: str) -> List[Tuple[int, str, str]]:
    selected = list(records)
    if order == "random":
        random.shuffle(selected)
    return selected


def _apply_wallet_start_selection(records: List[Tuple[int, str, str]]) -> Tuple[List[Tuple[int, str, str]], int, int]:
    total_wallets = len(records)
    start_number = _prompt_start_wallet_number(total_wallets)
    end_number = _prompt_end_wallet_number(total_wallets, start_number)
    order = _prompt_wallet_order(default_random=True)
    start_offset = start_number - 1
    return _apply_wallet_order(records[start_offset:end_number], order), start_offset, total_wallets


def _proxy_for_line(
    cfg: BotConfig,
    line_idx: int,
    logger: Optional[logging.Logger] = None,
    prefix: str = "",
) -> Tuple[Optional[Dict[str, str]], bool]:
    if cfg.file_proxies:
        if line_idx >= len(cfg.file_proxies) or not cfg.file_proxies[line_idx].strip():
            if logger:
                logger.warning("[%s] skip line %s: no proxy on matching line", prefix, line_idx + 1)
            return None, True
        proxy = cfg.file_proxies[line_idx].strip()
        return {"http": proxy, "https": proxy}, False
    if cfg.http_proxy or cfg.https_proxy:
        return {
            "http": cfg.http_proxy or cfg.https_proxy,
            "https": cfg.https_proxy or cfg.http_proxy,
        }, False
    return None, False


def _doma_api_client_for_proxies(cfg: BotConfig, proxies: Optional[Dict[str, str]] = None) -> DomaApiClient:
    return DomaApiClient(
        cfg.doma_api_url,
        api_keys=[cfg.doma_api_key, *cfg.doma_api_keys, *cfg.file_api_keys],
        proxies=proxies,
    )


def _fetch_fractional_tokens_with_wallet_proxy_fallback(
    cfg: BotConfig,
    logger: logging.Logger,
    wallet_records: List[Tuple[int, str, str]],
    mode_tag: str,
    take: int = 100,
    max_pages: int = 10,
) -> Tuple[DomaApiClient, List[LaunchpadTokenInfo], Optional[Dict[str, str]]]:
    last_error: Optional[Exception] = None
    tried_proxy_keys: set[str] = set()
    for line_idx, _, _ in wallet_records:
        candidate_proxies, skip_proxy = _proxy_for_line(cfg, line_idx, None, f"{mode_tag}_METADATA")
        if skip_proxy:
            continue
        proxy_key = json.dumps(candidate_proxies or {}, sort_keys=True)
        if proxy_key in tried_proxy_keys:
            continue
        tried_proxy_keys.add(proxy_key)
        api = _doma_api_client_for_proxies(cfg, candidate_proxies)
        try:
            return api, api.fetch_fractional_tokens(take=take, max_pages=max_pages), candidate_proxies
        except Exception as exc:
            last_error = exc
            logger.warning(
                "[%s] metadata catalog fetch failed via wallet#%s proxy, trying next | %s",
                mode_tag,
                line_idx + 1,
                exc,
            )
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"[{mode_tag}] no usable proxy for selected wallets")


def _is_transient_catalog_fetch_error(exc: Exception) -> bool:
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    if isinstance(exc, requests.HTTPError):
        response = exc.response
        return response is not None and response.status_code in {408, 425, 429, 500, 502, 503, 504}
    return False


def _fetch_fractional_tokens_with_same_proxy_retry(
    api: DomaApiClient,
    logger: logging.Logger,
    mode_tag: str,
    take: int = 100,
    max_pages: int = 10,
    retry_delay: float = 2.0,
) -> List[LaunchpadTokenInfo]:
    attempt = 0
    while True:
        attempt += 1
        try:
            return api.fetch_fractional_tokens(take=take, max_pages=max_pages)
        except Exception as exc:
            if not _is_transient_catalog_fetch_error(exc):
                raise
            logger.warning(
                "[%s] metadata catalog request failed via bound proxy | attempt=%s | "
                "retrying in %.2f sec | %s",
                mode_tag,
                attempt,
                retry_delay,
                exc,
            )
            time.sleep(retry_delay)


def _is_valid_evm_address(addr: str) -> bool:
    return bool(re.fullmatch(r"0x[a-fA-F0-9]{40}", (addr or "").strip()))


def canonical_symbol(symbol: str) -> str:
    s = symbol.strip().upper()
    return "WETH" if s == "ETH" else s


def parse_trade_amount_expression(expr: str) -> Tuple[str, Decimal]:
    s = (expr or "").strip().lower()
    if not s:
        raise ValueError("Empty trade amount expression")
    if s.endswith("%"):
        return "percent", Decimal(s[:-1].strip())
    if s.startswith("$"):
        return "usd", Decimal(s[1:].strip())
    if s.endswith("usd"):
        return "usd", Decimal(s[:-3].strip())
    return "token", Decimal(s)


def resolve_trade_amount(expr: str, wallet_token_balance: Decimal, token_usd_price: Decimal) -> Tuple[Decimal, Decimal]:
    mode, value = parse_trade_amount_expression(expr)
    if value <= 0:
        raise ValueError("Trade amount must be > 0")
    if mode == "percent":
        if value > 100:
            raise ValueError("Percent trade amount cannot be > 100")
        amount_in_dec = wallet_token_balance * value / Decimal("100")
        return amount_in_dec, amount_in_dec * token_usd_price
    if mode == "usd":
        if token_usd_price <= 0:
            raise ValueError("Cannot convert USD amount: token USD price is unknown")
        return value / token_usd_price, value
    amount_in_dec = value
    return amount_in_dec, amount_in_dec * token_usd_price


def choose_proxy(cfg: BotConfig, state: BotState) -> Optional[Dict[str, str]]:
    _ = state
    if cfg.proxy_candidates:
        p = cfg.proxy_candidates[0]
        return {"http": p, "https": p}
    if cfg.http_proxy or cfg.https_proxy:
        return {"http": cfg.http_proxy or cfg.https_proxy, "https": cfg.https_proxy or cfg.http_proxy}
    return None


def apply_filters(cfg: BotConfig, pools: List[Pool]) -> List[Pool]:
    out: List[Pool] = []
    for p in pools:
        if cfg.allowlist_pool_addresses and p.address.lower() not in cfg.allowlist_pool_addresses:
            continue
        if cfg.allowed_fee_tiers and p.fee_tier not in cfg.allowed_fee_tiers:
            continue
        if cfg.token_address_overrides:
            expected0 = cfg.token_address_overrides.get(p.token0.symbol.upper())
            expected1 = cfg.token_address_overrides.get(p.token1.symbol.upper())
            if expected0 and expected0 != p.token0.address.lower():
                continue
            if expected1 and expected1 != p.token1.address.lower():
                continue
        out.append(p)
    return out


def estimate_impact_bps(trade_usd: Decimal, pool_tvl_usd: Decimal) -> Decimal:
    if pool_tvl_usd <= 0:
        return Decimal("99999")
    return (trade_usd / pool_tvl_usd) * Decimal("10000") * Decimal("10")


def risk_checks(cfg: BotConfig, state: BotState, trade_usd: Decimal, est_impact_bps: Decimal, quote_out_dec: Decimal, expected_out_usd: Decimal) -> Optional[str]:
    if state.daily_volume_usd + trade_usd > cfg.max_daily_volume_usd:
        return "daily_volume_limit_exceeded"
    if est_impact_bps > Decimal(cfg.max_slippage_bps):
        return "estimated_price_impact_too_high"
    if expected_out_usd <= 0:
        return "invalid_quote_out"
    if expected_out_usd < trade_usd * Decimal("0.7"):
        return "quote_too_bad"
    if quote_out_dec <= 0:
        return "zero_quote"
    return None


def current_ts() -> int:
    return int(time.time())


def log_trade(
    cfg: BotConfig,
    status: str,
    label: str,
    pool: Pool,
    token_in: Token,
    token_out: Token,
    amount_in_dec: Decimal,
    quote_out_dec: Decimal,
    trade_usd: Decimal,
    expected_out_usd: Decimal,
    tx_hash: str,
    reason: str,
) -> None:
    pnl = expected_out_usd - trade_usd
    append_csv(
        cfg.trades_csv_file,
        [
            datetime.now(timezone.utc).isoformat(),
            status,
            label,
            pool.address,
            pool.fee_tier,
            token_in.symbol,
            token_out.symbol,
            str(amount_in_dec),
            str(quote_out_dec),
            str(trade_usd),
            str(expected_out_usd),
            str(pnl),
            tx_hash,
            reason,
        ],
    )


def _find_tokens_for_direction(pool: Pool, symbol_in: str, symbol_out: str) -> Optional[Tuple[Token, Token]]:
    s_in = canonical_symbol(symbol_in)
    s_out = canonical_symbol(symbol_out)
    if pool.token0.symbol == s_in and pool.token1.symbol == s_out:
        return pool.token0, pool.token1
    if pool.token1.symbol == s_in and pool.token0.symbol == s_out:
        return pool.token1, pool.token0
    return None


def _find_best_pool_for_symbols(
    cfg: BotConfig,
    pools: List[Pool],
    symbol_in: str,
    symbol_out: str,
    ignore_limits: bool = False,
) -> Optional[Pool]:
    s_in = canonical_symbol(symbol_in)
    s_out = canonical_symbol(symbol_out)
    cands: List[Pool] = []
    for p in pools:
        if {p.token0.symbol, p.token1.symbol} != {s_in, s_out}:
            continue
        if not ignore_limits:
            if p.tvl_usd < cfg.min_pool_tvl_usd or p.volume_24h_usd < cfg.min_pool_volume_24h_usd:
                continue
        cands.append(p)
    if not cands:
        return None
    exact_fee = [p for p in cands if p.fee_tier == cfg.default_fee_tier]
    chosen = exact_fee if exact_fee else cands
    chosen.sort(key=lambda p: p.tvl_usd, reverse=True)
    return chosen[0]


def _pool_looks_dead(pool: Pool) -> bool:
    return (
        pool.tvl_usd <= 0
        or (
            pool.volume_24h_usd <= 0
            and pool.token0_price <= 0
            and pool.token1_price <= 0
        )
    )


def check_gas_guards(cfg: BotConfig, exec_client: EvmExecutionClient, eth_price: Decimal) -> Optional[str]:
    gas_gwei = exec_client.get_gas_price_gwei()
    if gas_gwei > cfg.max_gas_gwei:
        return f"gas_gwei_too_high:{gas_gwei}"
    if cfg.skip_if_basefee_high:
        # Approximate max tx cost in USD using conservative 350k gas.
        approx_gas_units = Decimal("350000")
        gas_price_eth = Decimal(exec_client.get_gas_price_wei()) / Decimal(10**18)
        max_fee_usd = approx_gas_units * gas_price_eth * eth_price
        if max_fee_usd > cfg.max_tx_fee_usd:
            return f"max_tx_fee_usd_exceeded:{max_fee_usd}"
    return None


def _execute_trade_for_pair(
    cfg: BotConfig,
    logger: logging.Logger,
    state: BotState,
    exec_client: EvmExecutionClient,
    pool: Pool,
    symbol_in: str,
    symbol_out: str,
    trade_amount_expr: str,
    eth_price: Decimal,
    label: str,
    bypass_risk_checks: bool = False,
    allow_no_quoter_execution: bool = False,
    wait_for_pre_tx: bool = False,
    post_approve_delay_range: Optional[Tuple[float, float]] = None,
) -> bool:
    pair = _find_tokens_for_direction(pool, symbol_in, symbol_out)
    if not pair:
        logger.warning("[%s] Direction %s>%s not found in selected pool.", label, symbol_in, symbol_out)
        return False
    if _pool_looks_dead(pool):
        logger.warning(
            "[%s] Skipping execution: dead pool %s (TVL=%s, volume24h=%s).",
            label,
            pool.address,
            pool.tvl_usd,
            pool.volume_24h_usd,
        )
        return False
    token_in, token_out = pair
    if symbol_in.strip().upper() == "ETH" and token_in.symbol == "WETH":
        _cleanup_weth_balance(
            logger=logger,
            exec_client=exec_client,
            weth_token=token_in,
            label=label,
            reason="pre-swap cleanup",
            wait_for_receipt=wait_for_pre_tx,
        )

    try:
        amount_mode, _ = parse_trade_amount_expression(trade_amount_expr)
    except Exception as exc:
        logger.warning("[%s] Invalid trade amount '%s': %s", label, trade_amount_expr, exc)
        return False

    token_in_usd = pick_token_usd_price(token_in, eth_price)
    if token_in_usd <= 0 and (amount_mode == "usd" or not bypass_risk_checks):
        logger.warning("[%s] Unknown token USD price for %s.", label, token_in.symbol)
        return False

    is_eth_source = symbol_in.strip().upper() == "ETH" and token_in.symbol == "WETH"
    if is_eth_source:
        wallet_balance_in = _spendable_native_eth(exec_client, eth_price) + exec_client.get_erc20_balance(
            token_in.address,
            token_in.decimals,
        )
    else:
        wallet_balance_in = exec_client.get_erc20_balance(token_in.address, token_in.decimals)
    try:
        amount_in_dec, trade_usd = resolve_trade_amount(trade_amount_expr, wallet_balance_in, token_in_usd)
    except Exception as exc:
        logger.warning("[%s] Invalid trade amount '%s': %s", label, trade_amount_expr, exc)
        return False

    if amount_in_dec > wallet_balance_in:
        logger.warning("[%s] Insufficient balance for %s: need %.8f, have %.8f", label, token_in.symbol, float(amount_in_dec), float(wallet_balance_in))
        return False

    amount_in_raw = decimal_to_raw(amount_in_dec, token_in.decimals)
    if amount_in_raw <= 0:
        logger.warning("[%s] Amount too small after decimal conversion.", label)
        return False

    unwrap_to_native = symbol_out.strip().upper() == "ETH" and token_out.symbol == "WETH"
    before_weth_out_balance = Decimal("0")
    if unwrap_to_native:
        before_weth_out_balance = exec_client.get_erc20_balance(token_out.address, token_out.decimals)

    quote_out_raw = None
    quote_out_dec = Decimal("0")
    try:
        quote_out_raw = exec_client.quote_exact_input_single(
            token_in=token_in.address,
            token_out=token_out.address,
            fee_tier=pool.fee_tier,
            amount_in_raw=amount_in_raw,
        )
        quote_out_dec = raw_to_decimal(quote_out_raw, token_out.decimals)
    except Exception:
        rate = pool.token1_price if token_in == pool.token0 else pool.token0_price
        quote_out_dec = amount_in_dec * rate

    token_out_usd = pick_token_usd_price(token_out, eth_price)
    expected_out_usd = quote_out_dec * token_out_usd
    impact = estimate_impact_bps(trade_usd, pool.tvl_usd)
    block_reason = None if bypass_risk_checks else risk_checks(cfg, state, trade_usd, impact, quote_out_dec, expected_out_usd)

    logger.info(
        "[%s] %s -> %s | pool=%s | fee=%s | in=%.8f %s (~$%.2f) | out=%.8f %s (~$%.2f)",
        label,
        token_in.symbol,
        token_out.symbol,
        pool.address,
        pool.fee_tier,
        float(amount_in_dec),
        token_in.symbol,
        float(trade_usd),
        float(quote_out_dec),
        token_out.symbol,
        float(expected_out_usd),
    )

    if (quote_out_raw is not None and int(quote_out_raw) <= 0) or quote_out_dec <= 0:
        logger.warning("[%s] Skipping execution: zero output quote for %s.", label, token_out.symbol)
        return False

    if block_reason:
        log_trade(cfg, "BLOCKED", label, pool, token_in, token_out, amount_in_dec, quote_out_dec, trade_usd, expected_out_usd, "", block_reason)
        logger.warning("[%s] Trade blocked: %s", label, block_reason)
        return False

    if quote_out_raw is None and not allow_no_quoter_execution:
        log_trade(cfg, "BLOCKED", label, pool, token_in, token_out, amount_in_dec, quote_out_dec, trade_usd, expected_out_usd, "", "no_quoter")
        logger.warning("[%s] Skipping execution: no on-chain quoter configured.", label)
        return False

    if cfg.paper_mode or cfg.dry_run or not cfg.enable_execution:
        log_trade(cfg, "PAPER", label, pool, token_in, token_out, amount_in_dec, quote_out_dec, trade_usd, expected_out_usd, "", "paper_or_dry")
        logger.info("[%s] PAPER/DRY mode active. No transaction sent.", label)
        return True

    gas_reason = check_gas_guards(cfg, exec_client, eth_price)
    if gas_reason:
        log_trade(cfg, "BLOCKED", label, pool, token_in, token_out, amount_in_dec, quote_out_dec, trade_usd, expected_out_usd, "", gas_reason)
        logger.warning("[%s] Gas guard blocked trade: %s", label, gas_reason)
        return False

    min_out_raw = 0
    if quote_out_raw is not None:
        min_out_raw = int(quote_out_raw * (10_000 - cfg.max_slippage_bps) / 10_000)
    elif allow_no_quoter_execution:
        logger.warning("[%s] Executing without quoter: amountOutMinimum=0", label)
    if is_eth_source:
        wrap_tx = exec_client.ensure_weth_balance(token_in.address, amount_in_raw)
        if wrap_tx:
            logger.info("[%s] Wrap ETH->WETH tx sent: %s", label, wrap_tx)
            if wait_for_pre_tx:
                ok = _wait_tx_receipt(exec_client, wrap_tx, timeout_sec=180)
                if not ok:
                    raise RuntimeError("Wrap ETH->WETH tx failed or timed out")
                delay_sec = _pick_delay_from_range(post_approve_delay_range)
                logger.info("[%s] delay after wrap: %.2f sec", label, delay_sec)
                time.sleep(delay_sec)

    approve_hash = exec_client.ensure_allowance(token_in.address, amount_in_raw)
    if approve_hash:
        logger.info("[%s] Approve tx sent: %s", label, approve_hash)
        if wait_for_pre_tx:
            ok = _wait_tx_receipt(exec_client, approve_hash, timeout_sec=180)
            if not ok:
                raise RuntimeError("Approve tx failed or timed out")
            delay_sec = _pick_delay_from_range(post_approve_delay_range)
            logger.info("[%s] delay after approve: %.2f sec", label, delay_sec)
            time.sleep(delay_sec)

    try:
        tx_hash = exec_client.execute_swap_exact_input_single(
            token_in=token_in.address,
            token_out=token_out.address,
            fee_tier=pool.fee_tier,
            amount_in_raw=amount_in_raw,
            min_amount_out_raw=min_out_raw,
            recipient=exec_client.account_address,
            ttl_sec=180,
        )
    except Exception as exc:
        # STF often appears when wrap/approve tx is not yet reflected for estimate_gas.
        if "STF" in str(exc):
            retry_delay = _random_swap_delay_sec()
            logger.warning("[%s] Swap reverted with STF, retrying after %.2f sec", label, retry_delay)
            time.sleep(retry_delay)
            if is_eth_source:
                exec_client.ensure_weth_balance(token_in.address, amount_in_raw)
            exec_client.ensure_allowance(token_in.address, amount_in_raw)
            tx_hash = exec_client.execute_swap_exact_input_single(
                token_in=token_in.address,
                token_out=token_out.address,
                fee_tier=pool.fee_tier,
                amount_in_raw=amount_in_raw,
                min_amount_out_raw=min_out_raw,
                recipient=exec_client.account_address,
                ttl_sec=180,
            )
        else:
            logger.warning("[%s] Swap execution failed: %s", label, exc)
            if is_eth_source:
                _cleanup_weth_balance(
                    logger=logger,
                    exec_client=exec_client,
                    weth_token=token_in,
                    label=label,
                    reason="post-failed-swap cleanup",
                    wait_for_receipt=wait_for_pre_tx,
                )
            return False
    state.daily_volume_usd += trade_usd
    state.last_tx_hash = tx_hash
    log_trade(cfg, "EXECUTED", label, pool, token_in, token_out, amount_in_dec, quote_out_dec, trade_usd, expected_out_usd, tx_hash, "")
    logger.info("[%s] Swap tx sent: %s", label, tx_hash)

    if unwrap_to_native:
        ok = _wait_tx_receipt(exec_client, tx_hash, timeout_sec=180)
        if not ok:
            raise RuntimeError("Swap tx failed or timed out before WETH->ETH unwrap")
        after_weth_out_balance = exec_client.get_erc20_balance(token_out.address, token_out.decimals)
        received_weth = after_weth_out_balance - before_weth_out_balance
        unwrap_amount_raw = decimal_to_raw(received_weth, token_out.decimals)
        if unwrap_amount_raw <= 0 and quote_out_raw is not None and quote_out_raw > 0:
            current_weth_raw = decimal_to_raw(after_weth_out_balance, token_out.decimals)
            unwrap_amount_raw = min(current_weth_raw, int(quote_out_raw))
        if unwrap_amount_raw > 0:
            unwrap_tx = exec_client.unwrap_weth(token_out.address, unwrap_amount_raw)
            if unwrap_tx:
                state.last_tx_hash = unwrap_tx
                logger.info("[%s] Unwrap WETH->ETH tx sent: %s", label, unwrap_tx)
        else:
            logger.warning("[%s] Swap finished, but no WETH amount detected for unwrap.", label)
    return True


def _execute_trade_via_doma_ui_route(
    cfg: BotConfig,
    logger: logging.Logger,
    state: BotState,
    doma_api: DomaApiClient,
    exec_client: EvmExecutionClient,
    token_in: Token,
    token_out: Token,
    display_in_symbol: str,
    display_out_symbol: str,
    trade_amount_expr: str,
    eth_price: Decimal,
    label: str,
    is_eth_source: bool = False,
    unwrap_to_native: bool = False,
    wait_for_pre_tx: bool = False,
    cleanup_weth_before_eth_source: bool = True,
    post_approve_delay_range: Optional[Tuple[float, float]] = None,
) -> bool:
    if is_eth_source and cleanup_weth_before_eth_source:
        _cleanup_weth_balance(
            logger=logger,
            exec_client=exec_client,
            weth_token=token_in,
            label=label,
            reason="pre-swap cleanup",
            wait_for_receipt=wait_for_pre_tx,
        )

    token_in_usd = pick_token_usd_price(token_in, eth_price)
    if token_in_usd <= 0:
        logger.warning("[%s] Unknown token USD price for %s.", label, token_in.symbol)
        return False

    wallet_balance_in = (
        _spendable_native_eth(exec_client, eth_price)
        if is_eth_source
        else exec_client.get_erc20_balance(token_in.address, token_in.decimals)
    )
    try:
        amount_in_dec, trade_usd = resolve_trade_amount(trade_amount_expr, wallet_balance_in, token_in_usd)
    except Exception as exc:
        logger.warning("[%s] Invalid trade amount '%s': %s", label, trade_amount_expr, exc)
        return False

    if amount_in_dec > wallet_balance_in:
        logger.warning(
            "[%s] Insufficient balance for %s: need %.8f, have %.8f",
            label,
            display_in_symbol,
            float(amount_in_dec),
            float(wallet_balance_in),
        )
        return False

    amount_in_raw = decimal_to_raw(amount_in_dec, token_in.decimals)
    if amount_in_raw <= 0:
        logger.warning("[%s] Amount too small after decimal conversion.", label)
        return False

    before_weth_out_balance = Decimal("0")
    quote_token_in_address = DOMA_NATIVE_TOKEN_SENTINEL if is_eth_source else token_in.address
    quote_token_out_address = DOMA_NATIVE_TOKEN_SENTINEL if unwrap_to_native else token_out.address

    slippage_pct = Decimal("2.5")

    def _fetch_doma_ui_quote() -> Any:
        return doma_api.fetch_universal_router_quote(
            token_in_address=quote_token_in_address,
            token_out_address=quote_token_out_address,
            amount_raw=amount_in_raw,
            chain_id=cfg.chain_id,
            trade_type="exactIn",
            slippage_tolerance_pct=slippage_pct,
            portion_bips=DOMA_INTERFACE_PORTION_BIPS,
            portion_recipient=DOMA_INTERFACE_PORTION_RECIPIENT,
        )

    def _fetch_doma_ui_quote_with_retries(context: str = "") -> Optional[Any]:
        last_exc: Optional[Exception] = None
        suffix = f" {context}" if context else ""
        for attempt, delay_sec in enumerate(DOMA_QUOTE_RETRY_DELAYS_SEC, start=1):
            if delay_sec > 0:
                time.sleep(delay_sec)
            try:
                return _fetch_doma_ui_quote()
            except Exception as exc:
                last_exc = exc
                if attempt < len(DOMA_QUOTE_RETRY_DELAYS_SEC):
                    logger.warning(
                        "[%s] Doma UI quote failed%s | attempt=%s/%s | retry_in=%.2f sec | %s",
                        label,
                        suffix,
                        attempt,
                        len(DOMA_QUOTE_RETRY_DELAYS_SEC),
                        DOMA_QUOTE_RETRY_DELAYS_SEC[attempt],
                        exc,
                    )
                else:
                    logger.warning(
                        "[%s] Doma UI quote failed%s after %s attempts: %s",
                        label,
                        suffix,
                        attempt,
                        exc,
                    )
        if last_exc:
            state.last_error = str(last_exc)
        return None

    quote = _fetch_doma_ui_quote_with_retries()
    if quote is None:
        return False

    token_out_usd = pick_token_usd_price(token_out, eth_price)
    expected_out_usd = quote.quote_decimals * token_out_usd
    logger.info(
        "[%s] via Doma UI route | router=%s | in=%.8f %s (~$%.2f) | out=%.8f %s (~$%.2f) | impact=%s%% | route=%s",
        label,
        Web3.to_checksum_address(quote.to),
        float(amount_in_dec),
        display_in_symbol,
        float(trade_usd),
        float(quote.quote_decimals),
        display_out_symbol,
        float(expected_out_usd),
        _format_decimal_plain(quote.price_impact_pct),
        quote.route_string or "n/a",
    )

    if quote.quote_raw <= 0 or quote.quote_decimals <= 0:
        logger.warning("[%s] Skipping execution: zero output quote.", label)
        return False

    if cfg.paper_mode or cfg.dry_run or not cfg.enable_execution:
        logger.info("[%s] PAPER/DRY mode active. No transaction sent.", label)
        return True

    gas_reason = check_gas_guards(cfg, exec_client, eth_price)
    if gas_reason:
        logger.warning("[%s] Gas guard blocked trade: %s", label, gas_reason)
        return False

    approvals_sent = False
    if not is_eth_source:
        token_approve_hash = exec_client.ensure_allowance(
            token_in.address,
            amount_in_raw,
            spender_address=exec_client.permit2_address,
            approve_max=True,
        )
        if token_approve_hash:
            approvals_sent = True
            logger.info("[%s] Approve token->Permit2 tx sent: %s", label, token_approve_hash)
            if wait_for_pre_tx:
                ok = _wait_tx_receipt(exec_client, token_approve_hash, timeout_sec=180)
                if not ok:
                    raise RuntimeError("Approve token->Permit2 tx failed or timed out")
                delay_sec = _pick_delay_from_range(post_approve_delay_range)
                logger.info("[%s] delay after approve: %.2f sec", label, delay_sec)
                time.sleep(delay_sec)

        permit2_approve_hash = exec_client.ensure_permit2_allowance(token_in.address, quote.to, amount_in_raw)
        if permit2_approve_hash:
            approvals_sent = True
            logger.info("[%s] Approve Permit2->UniversalRouter tx sent: %s", label, permit2_approve_hash)
            if wait_for_pre_tx:
                ok = _wait_tx_receipt(exec_client, permit2_approve_hash, timeout_sec=180)
                if not ok:
                    raise RuntimeError("Approve Permit2->UniversalRouter tx failed or timed out")
                delay_sec = _pick_delay_from_range(post_approve_delay_range)
                logger.info("[%s] delay after approve: %.2f sec", label, delay_sec)
                time.sleep(delay_sec)

        if approvals_sent:
            refreshed_quote = _fetch_doma_ui_quote_with_retries("refresh after approve")
            if refreshed_quote is not None and refreshed_quote.quote_raw > 0 and refreshed_quote.calldata:
                quote = refreshed_quote
                permit2_refresh_hash = exec_client.ensure_permit2_allowance(token_in.address, quote.to, amount_in_raw)
                if permit2_refresh_hash:
                    logger.info("[%s] Approve Permit2->UniversalRouter refresh tx sent: %s", label, permit2_refresh_hash)
                    if wait_for_pre_tx:
                        ok = _wait_tx_receipt(exec_client, permit2_refresh_hash, timeout_sec=180)
                        if not ok:
                            raise RuntimeError("Approve Permit2 refresh tx failed or timed out")
                        delay_sec = _pick_delay_from_range(post_approve_delay_range)
                        logger.info("[%s] delay after approve: %.2f sec", label, delay_sec)
                        time.sleep(delay_sec)
                logger.info("[%s] refreshed Doma UI quote after approve", label)
            elif refreshed_quote is None:
                logger.warning("[%s] Doma UI quote refresh after approve failed, using previous quote", label)

    try:
        tx_hash = exec_client.execute_prebuilt_transaction(
            to_address=quote.to,
            calldata=quote.calldata,
            value_raw=quote.value_raw,
        )
    except Exception as exc:
        message = str(exc)
        if "STF" in message or "AllowanceExpired" in message or "d81b2f2e" in message or "675cae38" in message:
            retry_delay = _pick_delay_from_range(post_approve_delay_range)
            logger.warning("[%s] Swap reverted, refreshing quote and retrying after %.2f sec: %s", label, retry_delay, message)
            time.sleep(retry_delay)
            retry_quote = _fetch_doma_ui_quote_with_retries("execution retry")
            if retry_quote is None:
                return False
            if not is_eth_source:
                exec_client.ensure_allowance(
                    token_in.address,
                    amount_in_raw,
                    spender_address=exec_client.permit2_address,
                    approve_max=True,
                )
                exec_client.ensure_permit2_allowance(token_in.address, retry_quote.to, amount_in_raw)
            tx_hash = exec_client.execute_prebuilt_transaction(
                to_address=retry_quote.to,
                calldata=retry_quote.calldata,
                value_raw=retry_quote.value_raw,
            )
            quote = retry_quote
        else:
            logger.warning("[%s] Doma UI route execution failed: %s", label, exc)
            if is_eth_source:
                _cleanup_weth_balance(
                    logger=logger,
                    exec_client=exec_client,
                    weth_token=token_in,
                    label=label,
                    reason="post-failed-swap cleanup",
                    wait_for_receipt=wait_for_pre_tx,
                )
            return False

    state.daily_volume_usd += trade_usd
    state.last_tx_hash = tx_hash
    logger.info("[%s] UniversalRouter tx sent: %s", label, tx_hash)
    return True


def _execute_trade_for_path(
    cfg: BotConfig,
    logger: logging.Logger,
    state: BotState,
    exec_client: EvmExecutionClient,
    token_in: Token,
    token_out: Token,
    path_tokens: List[Token],
    path_token_addresses: List[str],
    path_fee_tiers: List[int],
    trade_amount_expr: str,
    eth_price: Decimal,
    label: str,
    is_eth_source: bool = False,
    wait_for_pre_tx: bool = False,
    post_approve_delay_range: Optional[Tuple[float, float]] = None,
) -> bool:
    token_in_usd = pick_token_usd_price(token_in, eth_price)
    try:
        amount_in_dec, trade_usd = resolve_trade_amount(
            trade_amount_expr,
            exec_client.get_native_balance() + exec_client.get_erc20_balance(token_in.address, token_in.decimals)
            if is_eth_source
            else exec_client.get_erc20_balance(token_in.address, token_in.decimals),
            token_in_usd,
        )
    except Exception as exc:
        logger.warning("[%s] Invalid trade amount '%s': %s", label, trade_amount_expr, exc)
        return False

    amount_in_raw = decimal_to_raw(amount_in_dec, token_in.decimals)
    if amount_in_raw <= 0:
        logger.warning("[%s] Amount too small after decimal conversion.", label)
        return False

    unwrap_to_native = token_out.symbol == "WETH" and label.upper().endswith(">ETH")
    before_weth_out_balance = Decimal("0")
    if unwrap_to_native:
        before_weth_out_balance = exec_client.get_erc20_balance(token_out.address, token_out.decimals)

    if len(path_tokens) < 2 or len(path_fee_tiers) != len(path_tokens) - 1:
        logger.warning("[%s] Invalid path metadata.", label)
        return False

    quote_raw = amount_in_raw
    for i, fee in enumerate(path_fee_tiers):
        t_in = path_tokens[i]
        t_out = path_tokens[i + 1]
        try:
            quote_raw = exec_client.quote_exact_input_single(
                token_in=t_in.address,
                token_out=t_out.address,
                fee_tier=fee,
                amount_in_raw=quote_raw,
            )
        except Exception as exc:
            logger.warning("[%s] No on-chain quote for path leg %s (%s>%s): %s", label, i + 1, t_in.symbol, t_out.symbol, exc)
            quote_raw = 0
            break

    quote_out_dec = raw_to_decimal(quote_raw, token_out.decimals) if quote_raw > 0 else Decimal("0")

    logger.info(
        "[%s] path swap | in=%.8f %s (~$%.2f) | out=%.8f %s | route=%s",
        label,
        float(amount_in_dec),
        token_in.symbol,
        float(trade_usd),
        float(quote_out_dec),
        token_out.symbol,
        " -> ".join([Web3.to_checksum_address(a) for a in path_token_addresses]),
    )

    if quote_raw <= 0 or quote_out_dec <= 0:
        logger.warning("[%s] Skipping execution: zero output quote for path swap.", label)
        return False

    if cfg.paper_mode or cfg.dry_run or not cfg.enable_execution:
        logger.info("[%s] PAPER/DRY mode active. No transaction sent.", label)
        return True

    gas_reason = check_gas_guards(cfg, exec_client, eth_price)
    if gas_reason:
        logger.warning("[%s] Gas guard blocked trade: %s", label, gas_reason)
        return False

    if is_eth_source:
        wrap_tx = exec_client.ensure_weth_balance(token_in.address, amount_in_raw)
        if wrap_tx:
            logger.info("[%s] Wrap ETH->WETH tx sent: %s", label, wrap_tx)
            if wait_for_pre_tx:
                ok = _wait_tx_receipt(exec_client, wrap_tx, timeout_sec=180)
                if not ok:
                    raise RuntimeError("Wrap ETH->WETH tx failed or timed out")
                delay_sec = _pick_delay_from_range(post_approve_delay_range)
                logger.info("[%s] delay after wrap: %.2f sec", label, delay_sec)
                time.sleep(delay_sec)

    approve_hash = exec_client.ensure_allowance(token_in.address, amount_in_raw)
    if approve_hash:
        logger.info("[%s] Approve tx sent: %s", label, approve_hash)
        if wait_for_pre_tx:
            ok = _wait_tx_receipt(exec_client, approve_hash, timeout_sec=180)
            if not ok:
                raise RuntimeError("Approve tx failed or timed out")
            delay_sec = _pick_delay_from_range(post_approve_delay_range)
            logger.info("[%s] delay after approve: %.2f sec", label, delay_sec)
            time.sleep(delay_sec)

    try:
        min_out_raw = int(quote_raw * (10_000 - cfg.max_slippage_bps) / 10_000) if quote_raw > 0 else 0
        tx_hash = exec_client.execute_swap_exact_input_path(
            token_addresses=path_token_addresses,
            fee_tiers=path_fee_tiers,
            amount_in_raw=amount_in_raw,
            min_amount_out_raw=min_out_raw,
            recipient=exec_client.account_address,
            ttl_sec=180,
        )
    except Exception as exc:
        if "STF" in str(exc):
            retry_delay = _random_swap_delay_sec()
            logger.warning("[%s] Swap reverted with STF, retrying after %.2f sec", label, retry_delay)
            time.sleep(retry_delay)
            if is_eth_source:
                exec_client.ensure_weth_balance(token_in.address, amount_in_raw)
            exec_client.ensure_allowance(token_in.address, amount_in_raw)
            tx_hash = exec_client.execute_swap_exact_input_path(
                token_addresses=path_token_addresses,
                fee_tiers=path_fee_tiers,
                amount_in_raw=amount_in_raw,
                min_amount_out_raw=min_out_raw,
                recipient=exec_client.account_address,
                ttl_sec=180,
            )
        else:
            logger.warning("[%s] Path swap execution failed: %s", label, exc)
            return False
    state.daily_volume_usd += trade_usd
    state.last_tx_hash = tx_hash
    logger.info("[%s] Swap tx sent: %s", label, tx_hash)

    if unwrap_to_native:
        ok = _wait_tx_receipt(exec_client, tx_hash, timeout_sec=180)
        if not ok:
            raise RuntimeError("Swap tx failed or timed out before WETH->ETH unwrap")
        after_weth_out_balance = exec_client.get_erc20_balance(token_out.address, token_out.decimals)
        received_weth = after_weth_out_balance - before_weth_out_balance
        unwrap_amount_raw = decimal_to_raw(received_weth, token_out.decimals)
        if unwrap_amount_raw <= 0 and quote_raw > 0:
            current_weth_raw = decimal_to_raw(after_weth_out_balance, token_out.decimals)
            unwrap_amount_raw = min(current_weth_raw, int(quote_raw))
        if unwrap_amount_raw > 0:
            unwrap_tx = exec_client.unwrap_weth(token_out.address, unwrap_amount_raw)
            if unwrap_tx:
                state.last_tx_hash = unwrap_tx
                logger.info("[%s] Unwrap WETH->ETH tx sent: %s", label, unwrap_tx)
        else:
            logger.warning("[%s] Swap finished, but no WETH amount detected for unwrap.", label)
    return True


def run_bootstrap_swaps(cfg: BotConfig, logger: logging.Logger, state: BotState, pools: List[Pool], eth_price: Decimal, exec_client: EvmExecutionClient) -> None:
    if not cfg.run_bootstrap_swaps:
        return
    for pair in cfg.bootstrap_swaps:
        if pair in state.bootstrap_completed:
            continue
        if ">" not in pair:
            logger.warning("Invalid bootstrap pair format: %s", pair)
            state.bootstrap_completed.append(pair)
            continue
        symbol_in, symbol_out = [x.strip().upper() for x in pair.split(">", 1)]
        pool = _find_best_pool_for_symbols(cfg, pools, symbol_in, symbol_out)
        if not pool:
            logger.warning("[BOOTSTRAP] No pool found for %s.", pair)
            continue
        ok = _execute_trade_for_pair(
            cfg=cfg,
            logger=logger,
            state=state,
            exec_client=exec_client,
            pool=pool,
            symbol_in=symbol_in,
            symbol_out=symbol_out,
            trade_amount_expr=cfg.bootstrap_trade_amount,
            eth_price=eth_price,
            label=f"BOOTSTRAP {pair}",
        )
        if ok:
            state.bootstrap_completed.append(pair)


def run_points_check(cfg: BotConfig, logger: logging.Logger, state: BotState, api: DomaApiClient) -> None:
    if not cfg.points_check_enabled:
        return
    now = current_ts()
    if now - state.last_points_check_ts < cfg.points_check_interval_sec:
        return
    state.last_points_check_ts = now
    try:
        snapshot: Optional[PointsSnapshot] = api.fetch_points(cfg.account_address, cfg.leaderboard_rank_by)
        if not snapshot:
            logger.info("Points check: no leaderboard row for wallet.")
            return
        append_csv(
            cfg.points_csv_file,
            [
                datetime.now(timezone.utc).isoformat(),
                snapshot.wallet_address,
                snapshot.rank,
                str(snapshot.points),
                str(snapshot.trading_volume_usd),
                str(snapshot.liquid_amount_usd),
                snapshot.referral_count,
                snapshot.total_snapshot_entries,
                snapshot.snapshot_date,
            ],
        )
        logger.info(
            "Points: rank=%s/%s weekly_pts=%s season_pts=%s meta=%s",
            snapshot.rank,
            snapshot.total_snapshot_entries,
            snapshot.points,
            snapshot.trading_volume_usd,
            snapshot.snapshot_date,
        )
    except Exception as exc:
        logger.warning("Points check failed: %s", exc)


def run_replay_report(cfg: BotConfig, logger: logging.Logger) -> None:
    if not cfg.replay_csv_file.exists():
        logger.warning("Replay file not found: %s", cfg.replay_csv_file)
        return
    rows = 0
    wins = 0
    total_pnl = Decimal("0")
    with cfg.replay_csv_file.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            status = (row.get("status") or "").upper()
            if status not in {"PAPER", "EXECUTED"}:
                continue
            pnl = Decimal(row.get("pnl_estimate_usd", "0"))
            rows += 1
            total_pnl += pnl
            if pnl > 0:
                wins += 1
    wr = (Decimal(wins) / Decimal(rows) * Decimal("100")) if rows else Decimal("0")
    logger.info("Replay report | trades=%s winrate=%.2f%% pnl_estimate_usd=%s", rows, float(wr), total_pnl)


def preflight_check(cfg: BotConfig, logger: logging.Logger, subgraph: DomaSubgraphClient, exec_client: EvmExecutionClient) -> None:
    if exec_client.get_chain_id() != cfg.chain_id:
        raise RuntimeError(f"chain_id mismatch: rpc={exec_client.get_chain_id()} cfg={cfg.chain_id}")

    pools = subgraph.fetch_top_pools(limit=10)
    pools = apply_filters(cfg, pools)
    if not pools:
        raise RuntimeError("No pools available after allowlist/filters")

    native_balance = exec_client.get_native_balance()
    logger.info("Preflight: native balance=%s", native_balance)

    if cfg.enable_execution and not cfg.dry_run and not cfg.paper_mode:
        if not cfg.router_address or not cfg.quoter_address:
            raise RuntimeError("Missing ROUTER_ADDRESS/QUOTER_ADDRESS for live execution")
        if not exec_client.has_contract_code(cfg.router_address):
            raise RuntimeError("Router address has no contract code")
        if not exec_client.has_contract_code(cfg.quoter_address):
            raise RuntimeError("Quoter address has no contract code")
    logger.info("Preflight OK")


def validate_config(cfg: BotConfig) -> None:
    if not _is_valid_evm_address(cfg.account_address):
        raise ValueError(
            "ACCOUNT_ADDRESS must be a valid EVM address (0x + 40 hex chars), "
            "for example: 0x1234...abcd"
        )
    mode, value = parse_trade_amount_expression(cfg.trade_amount)
    if value <= 0:
        raise ValueError("TRADE_AMOUNT must be > 0")
    if mode == "percent" and value > 100:
        raise ValueError("TRADE_AMOUNT percent cannot be > 100")
    b_mode, b_value = parse_trade_amount_expression(cfg.bootstrap_trade_amount)
    if b_value <= 0:
        raise ValueError("BOOTSTRAP_TRADE_AMOUNT must be > 0")
    if b_mode == "percent" and b_value > 100:
        raise ValueError("BOOTSTRAP_TRADE_AMOUNT percent cannot be > 100")

    if cfg.enable_execution and not cfg.private_key:
        raise ValueError("PRIVATE_KEY is required when ENABLE_EXECUTION=true")

    if cfg.enable_execution and not cfg.dry_run and not cfg.paper_mode and cfg.require_live_confirmation:
        if not sys.stdin.isatty():
            raise RuntimeError("Live mode confirmation requires interactive terminal")
        print(f"Type exact phrase to confirm LIVE mode: {cfg.live_confirm_phrase}")
        typed = input("Confirm: ").strip()
        if typed != cfg.live_confirm_phrase:
            raise RuntimeError("Live mode confirmation failed")


def run_once(cfg: BotConfig, logger: logging.Logger, state: BotState, pools: List[Pool], eth_price: Decimal, exec_client: EvmExecutionClient, strategy: StrategyEngine) -> None:
    rotate_daily_state(state)
    token_map = {}
    for p in pools:
        token_map[p.token0.address] = p.token0
        token_map[p.token1.address] = p.token1
    balances = {addr: exec_client.get_erc20_balance(addr, tok.decimals) for addr, tok in token_map.items()}
    best = strategy.choose_best(pools, balances, eth_price)
    if not best:
        logger.info("No suitable candidate found under current filters.")
        return
    ok = _execute_trade_for_pair(
        cfg=cfg,
        logger=logger,
        state=state,
        exec_client=exec_client,
        pool=best.pool,
        symbol_in=best.token_in.symbol,
        symbol_out=best.token_out.symbol,
        trade_amount_expr=cfg.trade_amount,
        eth_price=eth_price,
        label="STRATEGY",
    )
    _ = ok


def build_clients(cfg: BotConfig, state: BotState, create_exec: bool = True):
    proxies = choose_proxy(cfg, state)
    subgraph = DomaSubgraphClient(cfg.subgraph_url, proxies=proxies)
    points_api = DomaApiClient(
        cfg.doma_api_url,
        api_key=cfg.doma_api_key,
        api_keys=cfg.doma_api_keys,
        proxies=proxies,
    )
    exec_client = None
    if create_exec:
        exec_client = EvmExecutionClient(
            rpc_url=cfg.rpc_url,
            chain_id=cfg.chain_id,
            account_address=cfg.account_address,
            private_key=cfg.private_key,
            router_address=cfg.router_address,
            quoter_address=cfg.quoter_address,
            router_variant=cfg.router_variant,
            request_proxies=proxies,
        )
    return proxies, subgraph, points_api, exec_client


def _wallet_api_context(
    cfg: BotConfig,
    idx: int,
    logger: logging.Logger,
    mode: str,
) -> Optional[Tuple[str, Optional[Dict[str, str]]]]:
    api_key = cfg.file_api_keys[idx].strip() if idx < len(cfg.file_api_keys) else ""
    if not api_key and cfg.file_api_keys:
        logger.warning("%s skipped for line %s: no API key on same line", mode, idx + 1)
        return None
    if not api_key and cfg.doma_api_key.strip():
        api_key = cfg.doma_api_key.strip()

    proxy = cfg.file_proxies[idx].strip() if idx < len(cfg.file_proxies) else ""
    if cfg.file_proxies and idx >= len(cfg.file_proxies):
        logger.warning("%s skipped for line %s: no proxy on same line", mode, idx + 1)
        return None
    proxies = {"http": proxy, "https": proxy} if proxy else None
    return api_key, proxies


def _write_points_snapshot(cfg: BotConfig, logger: logging.Logger, wallet: str, line_no: int, api: DomaApiClient) -> None:
    snapshot: Optional[PointsSnapshot] = api.fetch_points(wallet, cfg.leaderboard_rank_by)
    if not snapshot:
        logger.info("Points: no leaderboard row for wallet %s", wallet)
        return
    append_csv(
        cfg.points_csv_file,
        [
            datetime.now(timezone.utc).isoformat(),
            snapshot.wallet_address,
            snapshot.rank,
            str(snapshot.points),
            str(snapshot.trading_volume_usd),
            str(snapshot.liquid_amount_usd),
            snapshot.referral_count,
            snapshot.total_snapshot_entries,
            snapshot.snapshot_date,
        ],
        delimiter=cfg.csv_delimiter,
    )
    logger.info(
        "Points [%s] [line=%s]: rank=%s/%s weekly_pts=%s season_pts=%s meta=%s",
        snapshot.wallet_address,
        line_no,
        snapshot.rank,
        snapshot.total_snapshot_entries,
        snapshot.points,
        snapshot.trading_volume_usd,
        snapshot.snapshot_date,
    )


def _log_quests_column(logger: logging.Logger, wallet: str, line_no: int, quests: List[QuestStatus]) -> None:
    lines = [f"Quests [{wallet}] [line={line_no}]"]
    for period in ("DAILY", "WEEKLY", "SEASON"):
        period_quests = sorted(
            [q for q in quests if q.reset_period.upper() == period],
            key=lambda q: (q.priority, q.quest_id),
        )
        done = sum(1 for q in period_quests if q.completed)
        lines.append(f"  {period}: {done}/{len(period_quests)}")
        for quest in period_quests:
            marker = "DONE" if quest.completed else "MISS"
            completed_at = f" | {quest.completed_at}" if quest.completed_at else ""
            lines.append(f"    [{marker}] {quest.description} (+{_format_decimal_plain(quest.points_to_award)} pts){completed_at}")
    logger.info("\n".join(lines))


def _apply_local_rollcall_status(
    cfg: BotConfig,
    wallet: str,
    quests: List[QuestStatus],
) -> None:
    rollcall_csv = cfg.points_csv_file.parent / DOMA_DAILY_ROLLCALL_CSV.name
    if not rollcall_csv.exists():
        return
    wallet_lower = wallet.strip().lower()
    today_utc = datetime.now(timezone.utc).date().isoformat()
    completed_at = ""
    try:
        with rollcall_csv.open("r", newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle, delimiter=cfg.csv_delimiter):
                if str(row.get("status") or "").strip().lower() != "success":
                    continue
                if str(row.get("wallet") or "").strip().lower() != wallet_lower:
                    continue
                check_in_date = str(row.get("check_in_date") or "").strip()
                timestamp_utc = str(row.get("timestamp_utc") or "").strip()
                row_date = check_in_date[:10] or timestamp_utc[:10]
                if row_date != today_utc:
                    continue
                completed_at = timestamp_utc or check_in_date
    except (OSError, csv.Error):
        return
    if not completed_at:
        return
    for quest in quests:
        if "daily rollcall" not in " ".join(quest.description.lower().split()):
            continue
        quest.completed = True
        quest.completed_at = completed_at
        return


def _write_quest_statuses(
    cfg: BotConfig,
    logger: logging.Logger,
    wallet: str,
    line_no: int,
    api: DomaApiClient,
) -> None:
    quests_csv = cfg.points_csv_file.parent / DOMAIN_QUESTS_CSV.name
    ensure_csv(
        quests_csv,
        [
            "timestamp_utc",
            "wallet",
            "line",
            "reset_period",
            "quest_type",
            "description",
            "points",
            "completed",
            "completed_at",
            "available_at",
        ],
        delimiter=cfg.csv_delimiter,
    )
    quests = api.fetch_quests(wallet, cfg.chain_id)
    _apply_local_rollcall_status(cfg, wallet, quests)
    now_iso = datetime.now(timezone.utc).isoformat()
    for quest in sorted(quests, key=lambda q: (q.reset_period, q.priority, q.quest_id)):
        append_csv(
            quests_csv,
            [
                now_iso,
                wallet,
                line_no,
                quest.reset_period,
                quest.quest_type,
                quest.description,
                str(quest.points_to_award),
                "yes" if quest.completed else "no",
                quest.completed_at,
                quest.available_at,
            ],
            delimiter=cfg.csv_delimiter,
        )
    _log_quests_column(logger, wallet, line_no, quests)


def _balance_reader_web3(cfg: BotConfig, proxies: Optional[Dict[str, str]]) -> Web3:
    request_kwargs: Dict[str, Any] = {"timeout": 20}
    if proxies:
        request_kwargs["proxies"] = proxies
    errors: List[str] = []
    for rpc_url in _doma_rpc_candidates(cfg):
        try:
            web3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs=request_kwargs))
            if web3.is_connected():
                return web3
            errors.append(f"{rpc_url}: not connected")
        except Exception as exc:
            errors.append(f"{rpc_url}: {exc}")
    raise RuntimeError("All balance RPC attempts failed: " + " | ".join(errors))


def _erc20_balance_readonly(web3: Web3, wallet: str, token_address: str, decimals: int) -> Decimal:
    token = web3.eth.contract(address=Web3.to_checksum_address(token_address), abi=ERC20_ABI)
    raw = token.functions.balanceOf(Web3.to_checksum_address(wallet)).call()
    return Decimal(int(raw)) / (Decimal(10) ** int(decimals))


def _log_wallet_balances_column(
    cfg: BotConfig,
    logger: logging.Logger,
    wallet: str,
    line_no: int,
    api: DomaApiClient,
    proxies: Optional[Dict[str, str]],
    token_catalog: List[LaunchpadTokenInfo],
) -> None:
    min_value_usd = Decimal("0.01")
    web3 = _balance_reader_web3(cfg, proxies)
    quote_token = _usdce_token_from_config(cfg)
    weth_token = _token_from_config_override(cfg, "WETH", 18)
    eth_price = _fetch_eth_price_via_doma_quote(cfg, api, quote_token)
    balances: List[Tuple[str, Decimal, Decimal]] = []

    native_eth = Decimal(web3.eth.get_balance(Web3.to_checksum_address(wallet))) / Decimal(10**18)
    native_usd = native_eth * eth_price
    if native_usd > min_value_usd:
        balances.append(("ETH", native_eth, native_usd))

    seen_addresses: set[str] = set()

    def _add_erc20(symbol: str, address: str, decimals: int, price_usd: Decimal) -> None:
        addr = (address or "").strip().lower()
        if not addr or addr in seen_addresses or price_usd <= 0:
            return
        seen_addresses.add(addr)
        try:
            balance = _erc20_balance_readonly(web3, wallet, addr, decimals)
        except Exception as exc:
            logger.warning("Balances [%s] [line=%s]: token=%s read failed: %s", wallet, line_no, symbol, exc)
            return
        value_usd = balance * price_usd
        if value_usd > min_value_usd:
            balances.append((symbol, balance, value_usd))

    _add_erc20("WETH", weth_token.address, weth_token.decimals, eth_price)
    _add_erc20("USDC.E", quote_token.address, quote_token.decimals, Decimal("1"))
    for info in token_catalog:
        _add_erc20(canonical_symbol(info.symbol or info.name), info.address, info.decimals, info.price_usd)

    balances.sort(key=lambda item: item[2], reverse=True)
    if not balances:
        logger.info("Balances [%s] [line=%s]: no tokens above $0.01", wallet, line_no)
        return
    lines = [f"Balances [{wallet}] [line={line_no}] > $0.01"]
    total_usd = sum((value for _, _, value in balances), Decimal("0"))
    for symbol, amount, value_usd in balances:
        lines.append(f"  {symbol}: {_format_decimal_plain(amount)} (~${_format_decimal_plain(value_usd)})")
    lines.append(f"  TOTAL: ~${_format_decimal_plain(total_usd)}")
    logger.info("\n".join(lines))


def run_points_once(cfg: BotConfig, logger: logging.Logger, state: BotState) -> None:
    _ = state
    wallets = cfg.points_wallets or ([cfg.account_address] if cfg.account_address else [])
    if not wallets:
        logger.warning("Points check skipped: no wallets configured")
        return
    start_number = _prompt_start_wallet_number(len(wallets))
    end_number = _prompt_end_wallet_number(len(wallets), start_number)
    start_offset = start_number - 1
    wallet_records = list(enumerate(wallets))[start_offset:end_number]
    points_wallet_order = _prompt_wallet_order(default_random=True)
    if points_wallet_order == "random":
        random.shuffle(wallet_records)
    for order_idx, (idx, wallet) in enumerate(wallet_records):
        ctx = _wallet_api_context(cfg, idx, logger, "Points/quests check")
        if ctx is None:
            continue
        api_key, proxies = ctx
        api = DomaApiClient(
            cfg.doma_api_url,
            api_key=api_key,
            api_keys=[api_key] if api_key else [],
            proxies=proxies,
        )
        try:
            _write_points_snapshot(cfg, logger, wallet, idx + 1, api)
        except Exception as exc:
            logger.warning("Points check failed for %s [line=%s]: %s", wallet, idx + 1, exc)
        try:
            _write_quest_statuses(cfg, logger, wallet, idx + 1, api)
        except Exception as exc:
            logger.warning("Quest check failed for %s [line=%s]: %s", wallet, idx + 1, exc)
        if order_idx < len(wallet_records) - 1:
            delay_sec = random.uniform(2, 5)
            logger.info("Delay before next wallet: %.2f sec", delay_sec)
            time.sleep(delay_sec)

    try:
        cost_since = datetime.now(timezone.utc) - timedelta(days=7)
        run_doma_cost_report_once(
            cfg,
            logger,
            state,
            preset=(cost_since, start_offset, end_number),
            report_label="last_7d",
            wallet_order=points_wallet_order,
        )
        for period_label, period_since, period_until in _derive_current_season_ranges(cfg, logger, wallets):
            run_doma_cost_report_once(
                cfg,
                logger,
                state,
                preset=(period_since, start_offset, end_number),
                report_label=period_label,
                until=period_until,
                wallet_order=points_wallet_order,
            )
    except Exception as exc:
        logger.warning("Doma cost report failed during points/quests check: %s", exc)


def _get_privy_access_token_for_wallet(
    wallet: str,
    private_key: str,
    chain_id: int,
    proxies: Optional[Dict[str, str]] = None,
) -> str:
    checksum_wallet = Web3.to_checksum_address(wallet)
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "origin": "https://app.doma.xyz",
        "referer": "https://app.doma.xyz/",
        "privy-app-id": PRIVY_APP_ID,
        "privy-client-id": PRIVY_CLIENT_ID,
        "privy-client": "react-auth:2.17.3",
    }
    init_resp = requests.post(
        f"{PRIVY_API_BASE_URL}/api/v1/siwe/init",
        json={"address": checksum_wallet},
        headers=headers,
        timeout=20,
        proxies=proxies,
    )
    if init_resp.status_code == 401 and "invalid_credentials" in init_resp.text.lower():
        raise RuntimeError(
            "Privy SIWE init rejected the request: captcha/access-token gate is required for Doma daily rollcall"
        )
    init_resp.raise_for_status()
    nonce = str(init_resp.json().get("nonce") or "").strip()
    if not nonce:
        raise RuntimeError("Privy SIWE nonce is missing")

    issued_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    message = (
        "app.doma.xyz wants you to sign in with your Ethereum account:\n"
        f"{checksum_wallet}\n\n"
        "By signing, you are proving you own this wallet and logging in. "
        "This does not initiate a transaction or cost any fees.\n\n"
        "URI: https://app.doma.xyz\n"
        "Version: 1\n"
        f"Chain ID: {int(chain_id)}\n"
        f"Nonce: {nonce}\n"
        f"Issued At: {issued_at}\n"
        "Resources:\n"
        "- https://privy.io"
    )
    signed = Account.sign_message(encode_defunct(text=message), private_key=private_key)
    signature = Web3.to_hex(signed.signature)
    auth_resp = requests.post(
        f"{PRIVY_API_BASE_URL}/api/v1/siwe/authenticate",
        json={
            "signature": signature,
            "message": message,
            "chainId": f"eip155:{int(chain_id)}",
            "walletClientType": None,
            "connectorType": None,
            "mode": "login-or-sign-up",
        },
        headers=headers,
        timeout=20,
        proxies=proxies,
    )
    if auth_resp.status_code == 401 and "invalid_credentials" in auth_resp.text.lower():
        raise RuntimeError("Privy SIWE authenticate rejected the signed login")
    auth_resp.raise_for_status()
    data = auth_resp.json()
    token = str(data.get("token") or data.get("access_token") or data.get("accessToken") or "").strip()
    if not token:
        raise RuntimeError(f"Privy access token is missing in response keys: {', '.join(sorted(data.keys()))}")
    return token


def _decode_jwt_payload(token: str) -> Dict[str, Any]:
    parts = (token or "").strip().split(".")
    if len(parts) < 2:
        return {}
    try:
        payload = parts[1]
        payload += "=" * ((4 - len(payload) % 4) % 4)
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        data = json.loads(decoded.decode("utf-8", errors="ignore"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _extract_wallet_from_token_payload(payload: Dict[str, Any]) -> str:
    if not payload:
        return ""
    raw = json.dumps(payload, ensure_ascii=False)
    match = re.search(r"0x[a-fA-F0-9]{40}", raw)
    return match.group(0).lower() if match else ""


def _read_rollcall_access_tokens(path: Path) -> Tuple[Dict[str, str], Dict[int, str]]:
    wallet_tokens: Dict[str, str] = {}
    positional_tokens: Dict[int, str] = {}
    if not path.exists():
        return wallet_tokens, positional_tokens

    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8-sig").splitlines(),
        start=1,
    ):
        line = raw_line.replace("\ufeff", "").strip()
        if not line or line.startswith("#"):
            continue
        wallet = ""
        token = ""
        if ";" in line:
            left, right = line.split(";", 1)
            wallet, token = left.strip().lower(), right.strip()
        elif "," in line:
            left, right = line.split(",", 1)
            wallet, token = left.strip().lower(), right.strip()
        else:
            parts = line.split()
            if len(parts) >= 2 and _is_valid_evm_address(parts[0]):
                wallet, token = parts[0].strip().lower(), parts[1].strip()
            else:
                token = line

        if token.lower().startswith("bearer "):
            token = token.split(None, 1)[1].strip()
        if not token:
            continue
        if wallet and _is_valid_evm_address(wallet):
            wallet_tokens[wallet.lower()] = token
            continue
        payload_wallet = _extract_wallet_from_token_payload(_decode_jwt_payload(token))
        if payload_wallet and _is_valid_evm_address(payload_wallet):
            wallet_tokens[payload_wallet.lower()] = token
        else:
            positional_tokens[line_number] = token
    return wallet_tokens, positional_tokens


def _read_rollcall_token_lines(path: Path) -> List[str]:
    if not path.exists():
        return []
    return [
        line.replace("\ufeff", "").strip()
        for line in path.read_text(encoding="utf-8-sig").splitlines()
    ]


def _write_rollcall_token_lines(path: Path, token_lines: List[str], total_wallets: int) -> None:
    normalized = list(token_lines[:total_wallets])
    if len(normalized) < total_wallets:
        normalized.extend([""] * (total_wallets - len(normalized)))
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text("\n".join(normalized) + "\n", encoding="utf-8")
    temp_path.replace(path)


def _save_env_value(env_path: Path, key: str, value: str) -> None:
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    replaced = False
    for idx, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[idx] = f"{key}={value}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _pick_rollcall_browser_token(
    wallet: str,
    wallet_number: int,
    wallet_tokens: Dict[str, str],
    positional_tokens: Dict[int, str],
) -> str:
    token = wallet_tokens.get(wallet.lower(), "")
    if token:
        return token
    token = positional_tokens.get(wallet_number, "")
    if token:
        payload_wallet = _extract_wallet_from_token_payload(_decode_jwt_payload(token))
        if payload_wallet and payload_wallet.lower() != wallet.lower():
            raise RuntimeError(f"browser token belongs to {payload_wallet}, not wallet#{wallet_number}")
        return token
    return ""


def _save_rollcall_access_token(
    path: Path,
    wallet_number: int,
    total_wallets: int,
    access_token: str,
) -> None:
    token_lines = _read_rollcall_token_lines(path)
    if len(token_lines) < total_wallets:
        token_lines.extend([""] * (total_wallets - len(token_lines)))
    token_lines[wallet_number - 1] = access_token
    _write_rollcall_token_lines(path, token_lines, total_wallets)


def _is_privy_auth_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "failed to verify privy token",
            "access token is missing",
            "unauthenticated",
            "statuscode': 401",
            "401 client error",
        )
    )


def run_rollcall_token_generation_once(cfg: BotConfig, logger: logging.Logger, state: BotState) -> None:
    _ = state
    import privy_auth

    configured_proxies = [proxy.strip() for proxy in (cfg.file_proxies or []) if proxy.strip()]
    if not configured_proxies:
        raise RuntimeError("Point 23 requires proxies.txt: one proxy per wallet line; direct mode is disabled")
    config_proxy = {"http": configured_proxies[0], "https": configured_proxies[0]}
    captcha_type, _captcha_sitekey = privy_auth.fetch_privy_captcha_config(proxies=config_proxy)
    twocaptcha_key = (cfg.twocaptcha_api_key or "").strip()
    captchasonic_key = (cfg.captchasonic_api_key or "").strip()
    if captcha_type == "hcaptcha" and not captchasonic_key:
        if not sys.stdin.isatty():
            raise RuntimeError("CAPTCHASONIC_API_KEY is missing. Put it into .env.")
        captchasonic_key = input("CaptchaSonic API key [blank = cancel]: ").strip()
        if not captchasonic_key:
            raise RuntimeError("CAPTCHASONIC_API_KEY is missing. Put it into .env.")
        _save_env_value(Path(".env"), "CAPTCHASONIC_API_KEY", captchasonic_key)
        cfg.captchasonic_api_key = captchasonic_key
        logger.info("[ROLLCALL_TOKEN] CAPTCHASONIC_API_KEY saved to .env")
    elif captcha_type == "turnstile" and not twocaptcha_key:
        if not sys.stdin.isatty():
            raise RuntimeError("TWOCAPTCHA_API_KEY is missing. Put it into .env.")
        twocaptcha_key = input("2captcha API key [blank = cancel]: ").strip()
        if not twocaptcha_key:
            raise RuntimeError("TWOCAPTCHA_API_KEY is missing. Put it into .env.")
        _save_env_value(Path(".env"), "TWOCAPTCHA_API_KEY", twocaptcha_key)
        cfg.twocaptcha_api_key = twocaptcha_key
        logger.info("[ROLLCALL_TOKEN] TWOCAPTCHA_API_KEY saved to .env")

    wallet_key_records = _build_wallet_key_records(cfg, logger, "ROLLCALL_TOKEN")
    if not wallet_key_records:
        logger.warning("[ROLLCALL_TOKEN] skipped: no wallet/private-key pairs configured")
        return

    total_wallets = len(cfg.points_wallets or wallet_key_records)
    start_number = _prompt_start_wallet_number(total_wallets)
    end_number = _prompt_end_wallet_number(total_wallets, start_number)
    wallet_records = [
        record for record in wallet_key_records
        if start_number <= record[0] + 1 <= end_number
    ]
    wallet_order = _prompt_wallet_order(default_random=False)
    if wallet_order == "random":
        random.shuffle(wallet_records)

    refresh_store = privy_auth.RefreshTokenStore(cfg.privy_refresh_tokens_file)
    token_lines = _read_rollcall_token_lines(cfg.privy_access_tokens_file)
    if len(token_lines) < total_wallets:
        token_lines.extend([""] * (total_wallets - len(token_lines)))

    success = 0
    failed = 0
    failed_wallets: List[str] = []
    logger.info(
        "[ROLLCALL_TOKEN] mode started | wallets=%s | start_wallet=%s | end_wallet=%s | output=%s | refresh_store=%s",
        len(wallet_records),
        start_number,
        end_number,
        cfg.privy_access_tokens_file,
        cfg.privy_refresh_tokens_file,
    )

    for process_idx, (line_idx, wallet, private_key) in enumerate(wallet_records):
        wallet_number = line_idx + 1
        logger.info(
            "[ROLLCALL_TOKEN] wallet %s",
            _wallet_record_progress_label(process_idx, len(wallet_records), line_idx, total_wallets, wallet),
        )
        bound_proxy = cfg.file_proxies[line_idx].strip() if line_idx < len(cfg.file_proxies) else ""
        try:
            if not bound_proxy:
                raise RuntimeError(f"missing proxy on line {wallet_number}; direct mode is disabled")
            proxies = {"http": bound_proxy, "https": bound_proxy}
            token_lines[wallet_number - 1] = ""
            _write_rollcall_token_lines(cfg.privy_access_tokens_file, token_lines, total_wallets)
            _proxy_label, work_proxies = privy_auth.find_working_proxy(
                [(f"bound#{wallet_number}", proxies)],
                logger=logger,
            )
            if work_proxies is None:
                raise RuntimeError(f"proxy on line {wallet_number} is unavailable; direct mode is disabled")
            access_token = privy_auth.mint_access_token(
                wallet,
                private_key,
                twocaptcha_key=twocaptcha_key,
                captchasonic_key=captchasonic_key,
                chain_id=cfg.chain_id,
                proxies=work_proxies,
                refresh_store=refresh_store,
                logger=logger,
            )
            token_lines[wallet_number - 1] = access_token
            _write_rollcall_token_lines(cfg.privy_access_tokens_file, token_lines, total_wallets)
            saved_lines = _read_rollcall_token_lines(cfg.privy_access_tokens_file)
            saved_token = saved_lines[wallet_number - 1] if wallet_number <= len(saved_lines) else ""
            if saved_token != access_token:
                raise RuntimeError(f"token verification failed after saving line {wallet_number}")
            success += 1
            logger.info("[ROLLCALL_TOKEN] wallet=wallet#%s token saved | line=%s", wallet_number, wallet_number)
        except Exception as exc:
            failed += 1
            failed_wallets.append(f"wallet#{wallet_number}")
            logger.warning("[ROLLCALL_TOKEN] wallet=wallet#%s failed: %s", wallet_number, exc)

        if process_idx < len(wallet_records) - 1:
            delay_sec = random.uniform(2, 5)
            logger.info("[ROLLCALL_TOKEN] delay before next wallet: %.2f sec", delay_sec)
            time.sleep(delay_sec)

    _print_mode_summary("ROLLCALL_TOKEN", len(wallet_records), success, failed, 0, failed_wallets)


GALXE_SPACE_STATION_ABI = [
    {
        "inputs": [
            {"internalType": "uint256", "name": "campaignID", "type": "uint256"},
            {"internalType": "address", "name": "nftCore", "type": "address"},
            {"internalType": "uint256", "name": "verifyID", "type": "uint256"},
            {"internalType": "uint256", "name": "powah", "type": "uint256"},
            {"internalType": "uint256", "name": "claimFeeAmount", "type": "uint256"},
            {"internalType": "bytes", "name": "signature", "type": "bytes"},
        ],
        "name": "claim",
        "outputs": [],
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "uint256", "name": "campaignID", "type": "uint256"},
            {"internalType": "address", "name": "nftCore", "type": "address"},
            {"internalType": "uint256", "name": "verifyID", "type": "uint256"},
            {"internalType": "uint256", "name": "powah", "type": "uint256"},
            {"internalType": "uint256", "name": "cap", "type": "uint256"},
            {"internalType": "uint256", "name": "claimFeeAmount", "type": "uint256"},
            {"internalType": "bytes", "name": "signature", "type": "bytes"},
        ],
        "name": "claimCapped",
        "outputs": [],
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "uint256", "name": "campaignID", "type": "uint256"},
            {"internalType": "address", "name": "nftCore", "type": "address"},
            {"internalType": "uint256[]", "name": "verifyIDs", "type": "uint256[]"},
            {"internalType": "uint256[]", "name": "powahs", "type": "uint256[]"},
            {"internalType": "uint256", "name": "claimFeeAmount", "type": "uint256"},
            {"internalType": "bytes", "name": "signature", "type": "bytes"},
        ],
        "name": "claimBatch",
        "outputs": [],
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "uint256", "name": "campaignID", "type": "uint256"},
            {"internalType": "address", "name": "nftCore", "type": "address"},
            {"internalType": "uint256[]", "name": "verifyIDs", "type": "uint256[]"},
            {"internalType": "uint256[]", "name": "powahs", "type": "uint256[]"},
            {"internalType": "uint256", "name": "cap", "type": "uint256"},
            {"internalType": "uint256", "name": "claimFeeAmount", "type": "uint256"},
            {"internalType": "bytes", "name": "signature", "type": "bytes"},
        ],
        "name": "claimBatchCapped",
        "outputs": [],
        "stateMutability": "payable",
        "type": "function",
    },
]


def _parse_galxe_campaign_id(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        return GALXE_DEFAULT_CAMPAIGN_ID
    if "/" in value:
        return value.rstrip("/").split("/")[-1].split("?")[0].strip() or GALXE_DEFAULT_CAMPAIGN_ID
    return value


def _send_galxe_claim_tx(
    *,
    cfg: BotConfig,
    private_key: str,
    campaign_number_id: int,
    prepare: Dict[str, Any],
) -> str:
    rpc_url = (cfg.galxe_rpc_url or "").strip()
    if not rpc_url:
        raise RuntimeError("GALXE_RPC_URL is empty")
    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 45}))
    if not w3.is_connected():
        raise RuntimeError(f"Galxe RPC is unavailable: {rpc_url}")

    account = Account.from_key(private_key)
    space_station = (prepare.get("spaceStationInfo") or {}).get("address") or prepare.get("spaceStation")
    mint_info = prepare.get("mintFuncInfo") or {}
    signature = str(prepare.get("signature") or "")
    if not space_station or not mint_info or not signature:
        raise RuntimeError("prepareParticipate did not return SpaceStation claim data")

    contract = w3.eth.contract(address=Web3.to_checksum_address(space_station), abi=GALXE_SPACE_STATION_ABI)
    nft_core = Web3.to_checksum_address(str(mint_info.get("nftCoreAddress") or "0x0000000000000000000000000000000000000000"))
    verify_ids = [int(x) for x in (mint_info.get("verifyIDs") or [])]
    powahs = [int(x) for x in (mint_info.get("powahs") or [])]
    claim_fee = int(mint_info.get("claimFeeAmount") or 0)
    cap = int(mint_info.get("cap") or 0)
    signature_bytes = Web3.to_bytes(hexstr=signature) if signature.startswith("0x") else signature.encode()
    value = claim_fee

    if len(verify_ids) <= 1:
        verify_id = verify_ids[0] if verify_ids else 0
        powah = powahs[0] if powahs else 0
        if cap > 0:
            fn = contract.functions.claimCapped(
                int(campaign_number_id),
                nft_core,
                int(verify_id),
                int(powah),
                cap,
                claim_fee,
                signature_bytes,
            )
        else:
            fn = contract.functions.claim(
                int(campaign_number_id),
                nft_core,
                int(verify_id),
                int(powah),
                claim_fee,
                signature_bytes,
            )
    else:
        if cap > 0:
            fn = contract.functions.claimBatchCapped(
                int(campaign_number_id),
                nft_core,
                verify_ids,
                powahs,
                cap,
                claim_fee,
                signature_bytes,
            )
        else:
            fn = contract.functions.claimBatch(
                int(campaign_number_id),
                nft_core,
                verify_ids,
                powahs,
                claim_fee,
                signature_bytes,
            )

    tx = fn.build_transaction(
        {
            "from": account.address,
            "value": value,
            "nonce": w3.eth.get_transaction_count(account.address),
            "chainId": int(w3.eth.chain_id),
        }
    )
    tx.setdefault("gas", int(w3.eth.estimate_gas(tx) * 1.25))
    if "maxFeePerGas" not in tx and "gasPrice" not in tx:
        tx["gasPrice"] = w3.eth.gas_price
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    if int(receipt.status) != 1:
        raise RuntimeError(f"Galxe claim tx failed: {tx_hash.hex()}")
    return tx_hash.hex()


def run_galxe_quest_claim_once(cfg: BotConfig, logger: logging.Logger, state: BotState) -> None:
    _ = state
    campaign_raw = input(f"Galxe quest URL or campaign ID [{GALXE_DEFAULT_QUEST_URL}]: ").strip()
    campaign_id = _parse_galxe_campaign_id(campaign_raw)
    quest_url = (
        campaign_raw.strip()
        if campaign_raw.strip().startswith("http")
        else f"https://app.galxe.com/quest/D3/{campaign_id}"
    )

    delay_min = _prompt_positive_decimal("Minimum delay between wallets sec", "3")
    delay_max = _prompt_positive_decimal("Maximum delay between wallets sec", "8")
    if delay_max < delay_min:
        raise ValueError("Maximum delay must be >= minimum delay")

    wallet_key_records = _build_wallet_key_records(cfg, logger, "GALXE")
    if not wallet_key_records:
        logger.warning("[GALXE] skipped: no wallet/private-key pairs configured")
        return
    wallet_records, start_offset, total_wallets = _apply_wallet_start_selection(wallet_key_records)

    claims_csv = cfg.points_csv_file.parent / GALXE_CLAIMS_CSV.name
    success = 0
    failed = 0
    skipped = 0
    failed_wallets: List[str] = []
    logger.info(
        "[GALXE] mode started | wallets=%s | start_wallet=%s | campaign=%s | url=%s",
        len(wallet_records),
        start_offset + 1,
        campaign_id,
        quest_url,
    )

    for process_idx, (line_idx, wallet, private_key) in enumerate(wallet_records):
        wallet_number = line_idx + 1
        logger.info("[GALXE] wallet %s", _wallet_record_progress_label(process_idx, len(wallet_records), line_idx, total_wallets, wallet))
        proxies, skip_proxy = _proxy_for_line(cfg, line_idx, logger, "GALXE")
        if skip_proxy:
            skipped += 1
            reason = "missing proxy on matching line"
            failed_wallets.append(f"wallet#{wallet_number} | skipped: {reason}")
            append_csv(
                claims_csv,
                [datetime.now(timezone.utc).isoformat(), "skipped", f"wallet#{wallet_number}", wallet_number, campaign_id, "", "", "", "", "", reason],
                delimiter=cfg.csv_delimiter,
            )
            continue

        try:
            client = GalxeApiClient(proxies=proxies)
            galxe_token = client.signin(private_key, chain_id=1)
            status = client.campaign_status(campaign_id, wallet, token=galxe_token)
            logger.info(
                "[GALXE] wallet=wallet#%s campaign=%s | claimed=%s | participation=%s | eligible=%s",
                wallet_number,
                status.name or campaign_id,
                status.claimed_times,
                status.participation_status or "unknown",
                status.eligible,
            )
            if status.claimed_times > 0 or status.participation_status.lower() in {"participated", "completed", "claimed"}:
                skipped += 1
                reason = "already claimed"
                logger.info("[GALXE] wallet=wallet#%s skipped | %s", wallet_number, reason)
                append_csv(
                    claims_csv,
                    [datetime.now(timezone.utc).isoformat(), "skipped", f"wallet#{wallet_number}", wallet_number, campaign_id, status.name, "", "", "", "", reason],
                    delimiter=cfg.csv_delimiter,
                )
                continue
            if not status.eligible:
                skipped += 1
                missing = "; ".join(f"{c.name or c.cred_id} ({c.source})" for c in status.missing_conditions) or "unknown missing conditions"
                reason = f"not eligible: {missing}"
                logger.warning("[GALXE] wallet=wallet#%s skipped | %s", wallet_number, reason)
                append_csv(
                    claims_csv,
                    [datetime.now(timezone.utc).isoformat(), "skipped", f"wallet#{wallet_number}", wallet_number, campaign_id, status.name, "", "", "", "", reason],
                    delimiter=cfg.csv_delimiter,
                )
                continue

            proxy_url = (proxies or {}).get("https") or (proxies or {}).get("http") or ""
            captcha = get_galxe_captcha_input(quest_url=quest_url, proxy_url=proxy_url)
            prepare = client.prepare_participate(
                campaign_id=campaign_id,
                address=wallet,
                token=galxe_token,
                captcha=captcha,
                chain=GALXE_DEFAULT_CHAIN,
            )
            if not prepare.get("allow"):
                raise RuntimeError(str(prepare.get("disallowReason") or "prepareParticipate disallowed claim"))
            tx_hash = _send_galxe_claim_tx(
                cfg=cfg,
                private_key=private_key,
                campaign_number_id=status.number_id,
                prepare=prepare,
            )
            mint_info = prepare.get("mintFuncInfo") or {}
            verify_ids = [str(x) for x in (mint_info.get("verifyIDs") or [])]
            participated = client.participate(
                campaign_id=campaign_id,
                address=wallet,
                token=galxe_token,
                signature=str(prepare.get("signature") or ""),
                tx_hash=tx_hash,
                verify_ids=verify_ids,
                nonce=str(prepare.get("nonce") or ""),
                chain=GALXE_DEFAULT_CHAIN,
            )
            if not participated.get("participated"):
                raise RuntimeError(str(participated.get("failReason") or "participate returned false"))
            success += 1
            logger.info("[GALXE] wallet=wallet#%s claimed | tx=%s", wallet_number, tx_hash)
            append_csv(
                claims_csv,
                [datetime.now(timezone.utc).isoformat(), "success", f"wallet#{wallet_number}", wallet_number, campaign_id, status.name, tx_hash, ",".join(verify_ids), prepare.get("nonce") or "", prepare.get("signature") or "", ""],
                delimiter=cfg.csv_delimiter,
            )
        except Exception as exc:
            failed += 1
            reason = str(exc)
            failed_wallets.append(f"wallet#{wallet_number} | {reason}")
            logger.warning("[GALXE] wallet=wallet#%s failed: %s", wallet_number, reason)
            append_csv(
                claims_csv,
                [datetime.now(timezone.utc).isoformat(), "failed", f"wallet#{wallet_number}", wallet_number, campaign_id, "", "", "", "", "", reason],
                delimiter=cfg.csv_delimiter,
            )

        if process_idx < len(wallet_records) - 1:
            delay_sec = random.uniform(float(delay_min), float(delay_max))
            logger.info("[GALXE] delay before next wallet: %.2f sec", delay_sec)
            time.sleep(delay_sec)

    _print_mode_summary("GALXE", len(wallet_records), success, failed, skipped, failed_wallets)


def run_daily_rollcall_once(cfg: BotConfig, logger: logging.Logger, state: BotState) -> None:
    _ = state
    wallet_key_records = _build_wallet_key_records(cfg, logger, "ROLLCALL")
    if not wallet_key_records:
        logger.warning("[ROLLCALL] skipped: no wallet/private-key pairs configured")
        return
    total_wallets = len(cfg.points_wallets or wallet_key_records)
    start_number = _prompt_start_wallet_number(total_wallets)
    end_number = _prompt_end_wallet_number(total_wallets, start_number)
    delay_min = _prompt_positive_decimal("Minimum delay between wallets sec", "2")
    delay_max = _prompt_positive_decimal("Maximum delay between wallets sec", "5")
    if delay_max < delay_min:
        raise ValueError("Maximum delay must be >= minimum delay")
    wallet_records = [
        record for record in wallet_key_records
        if start_number <= record[0] + 1 <= end_number
    ]
    wallet_order = _prompt_wallet_order(default_random=True)
    if wallet_order == "random":
        random.shuffle(wallet_records)

    selected_total = len(wallet_records)
    success_count = 0
    failed_count = 0
    skipped_count = 0
    failed_wallets: List[str] = []
    skipped_details: List[str] = []
    rollcall_csv = cfg.points_csv_file.parent / DOMA_DAILY_ROLLCALL_CSV.name
    wallet_tokens, positional_tokens = _read_rollcall_access_tokens(cfg.privy_access_tokens_file)
    import privy_auth

    refresh_store = privy_auth.RefreshTokenStore(cfg.privy_refresh_tokens_file)
    twocaptcha_key = (cfg.twocaptcha_api_key or "").strip()
    captchasonic_key = (cfg.captchasonic_api_key or "").strip()
    has_captcha_solver = bool(twocaptcha_key or captchasonic_key)
    fatal_auth_error = False

    logger.info(
        "[ROLLCALL] mode started | wallets=%s | start_wallet=%s | end_wallet=%s | order=%s | delay=%s-%s sec | wallet_tokens=%s | positional_tokens=%s",
        selected_total,
        start_number,
        end_number,
        wallet_order,
        _format_decimal_plain(delay_min),
        _format_decimal_plain(delay_max),
        len(wallet_tokens),
        len(positional_tokens),
    )

    for process_idx, (line_idx, wallet, private_key) in enumerate(wallet_records):
        wallet_number = line_idx + 1
        logger.info(
            "[ROLLCALL] wallet %s",
            _wallet_record_progress_label(process_idx, selected_total, line_idx, total_wallets, wallet),
        )
        proxy = cfg.file_proxies[line_idx].strip() if line_idx < len(cfg.file_proxies) else ""
        if not proxy:
            skipped_count += 1
            reason = "missing proxy on matching line; direct mode is disabled"
            skipped_details.append(f"wallet#{wallet_number}: {reason}")
            append_csv(
                rollcall_csv,
                [datetime.now(timezone.utc).isoformat(), "skipped", wallet, wallet_number, "", "", "", reason],
                delimiter=cfg.csv_delimiter,
            )
            continue
        proxies = {"http": proxy, "https": proxy}
        api = DomaApiClient(
            cfg.doma_api_url,
            api_key="",
            api_keys=[],
            proxies=proxies,
        )
        try:
            stored_access_token = _pick_rollcall_browser_token(
                wallet,
                wallet_number,
                wallet_tokens,
                positional_tokens,
            )
            has_refresh = bool(refresh_store.get_refresh(wallet) and refresh_store.get_access(wallet))
            access_token = stored_access_token
            if has_refresh or (not access_token and has_captcha_solver):
                auth_method = "refresh" if has_refresh else "full login"
                logger.info("[ROLLCALL] wallet=wallet#%s obtaining fresh Privy token via %s", wallet_number, auth_method)
                access_token = privy_auth.mint_access_token(
                    wallet,
                    private_key,
                    twocaptcha_key=twocaptcha_key,
                    captchasonic_key=captchasonic_key,
                    chain_id=cfg.chain_id,
                    proxies=proxies,
                    refresh_store=refresh_store,
                    logger=logger,
                )
                _save_rollcall_access_token(
                    cfg.privy_access_tokens_file,
                    wallet_number,
                    total_wallets,
                    access_token,
                )
            elif access_token:
                logger.info("[ROLLCALL] wallet=wallet#%s using saved Privy token (no refresh token available)", wallet_number)
            else:
                raise RuntimeError(
                    "Privy access and refresh tokens are missing; run point 23 or configure a CAPTCHA API key"
                )
            try:
                result = api.check_in(wallet, cfg.chain_id, access_token=access_token)
            except Exception as first_exc:
                if not _is_privy_auth_error(first_exc) or not has_captcha_solver:
                    raise
                logger.warning(
                    "[ROLLCALL] wallet=wallet#%s Privy token rejected; re-authenticating once",
                    wallet_number,
                )
                refresh_store.drop(wallet)
                access_token = privy_auth.mint_access_token(
                    wallet,
                    private_key,
                    twocaptcha_key=twocaptcha_key,
                    captchasonic_key=captchasonic_key,
                    chain_id=cfg.chain_id,
                    proxies=proxies,
                    refresh_store=refresh_store,
                    logger=logger,
                )
                _save_rollcall_access_token(
                    cfg.privy_access_tokens_file,
                    wallet_number,
                    total_wallets,
                    access_token,
                )
                result = api.check_in(wallet, cfg.chain_id, access_token=access_token)
            points_awarded = result["points_awarded"]
            check_in_date = result["check_in_date"]
            ok = bool(result["success"])
            status = "success" if ok else "skipped"
            reason = "" if ok else "checkIn returned success=false"
            if ok:
                success_count += 1
                logger.info(
                    "[ROLLCALL] wallet=wallet#%s claimed | points=%s | date=%s",
                    wallet_number,
                    _format_decimal_plain(points_awarded),
                    check_in_date or "unknown",
                )
            else:
                skipped_count += 1
                skipped_details.append(f"wallet#{wallet_number}: {reason}")
                logger.warning("[ROLLCALL] wallet=wallet#%s skipped | %s", wallet_number, reason)
            append_csv(
                rollcall_csv,
                [
                    datetime.now(timezone.utc).isoformat(),
                    status,
                    wallet,
                    wallet_number,
                    result["wallet_address"],
                    str(points_awarded),
                    check_in_date,
                    reason,
                ],
                delimiter=cfg.csv_delimiter,
            )
        except Exception as exc:
            failed_count += 1
            failed_wallets.append(f"wallet#{wallet_number}")
            reason = str(exc)
            logger.warning("[ROLLCALL] wallet=wallet#%s failed: %s", wallet_number, exc)
            append_csv(
                rollcall_csv,
                [datetime.now(timezone.utc).isoformat(), "failed", wallet, wallet_number, "", "", "", reason],
                delimiter=cfg.csv_delimiter,
            )
            if "captcha/access-token gate" in reason:
                fatal_auth_error = True
                logger.error(
                    "[ROLLCALL] stopped: Doma rollcall requires browser Privy tokens. "
                    "Put tokens into %s as wallet;token, or run one selected wallet with a single raw token.",
                    cfg.privy_access_tokens_file,
                )
                break

        if process_idx < selected_total - 1:
            delay_sec = random.uniform(float(delay_min), float(delay_max))
            logger.info("[ROLLCALL] delay before next wallet: %.2f sec", delay_sec)
            time.sleep(delay_sec)

    _print_mode_summary("ROLLCALL", selected_total, success_count, failed_count, skipped_count, failed_wallets)
    if skipped_details:
        print(_color(f"[ROLLCALL] wallets skipped: {'; '.join(skipped_details)}", ANSI_YELLOW))


def run_balances_once(cfg: BotConfig, logger: logging.Logger, state: BotState) -> None:
    _ = state
    wallets = cfg.points_wallets or ([cfg.account_address] if cfg.account_address else [])
    if not wallets:
        logger.warning("Balance check skipped: no wallets configured")
        return
    start_number = _prompt_start_wallet_number(len(wallets))
    end_number = _prompt_end_wallet_number(len(wallets), start_number)
    wallet_records = list(enumerate(wallets))[start_number - 1:end_number]
    wallet_order = _prompt_wallet_order(default_random=True)
    if wallet_order == "random":
        random.shuffle(wallet_records)

    balance_token_catalog: Optional[List[LaunchpadTokenInfo]] = None
    logger.info("[BALANCES] mode started | wallets=%s | start_wallet=%s | end_wallet=%s | min_value=$0.01", len(wallets), start_number, end_number)
    for order_idx, (idx, wallet) in enumerate(wallet_records):
        logger.info("[BALANCES] wallet %s/%s - %s", idx + 1, len(wallets), wallet)
        ctx = _wallet_api_context(cfg, idx, logger, "Balance check")
        if ctx is None:
            continue
        api_key, proxies = ctx
        api = DomaApiClient(
            cfg.doma_api_url,
            api_key=api_key,
            api_keys=[api_key] if api_key else [],
            proxies=proxies,
        )
        try:
            if balance_token_catalog is None:
                balance_token_catalog = api.fetch_fractional_tokens(take=100, max_pages=20)
            _log_wallet_balances_column(cfg, logger, wallet, idx + 1, api, proxies, balance_token_catalog)
        except Exception as exc:
            logger.warning("Balance check failed for %s [line=%s]: %s", wallet, idx + 1, exc)
        if order_idx < len(wallet_records) - 1:
            delay_sec = random.uniform(2, 5)
            logger.info("[BALANCES] delay before next wallet: %.2f sec", delay_sec)
            time.sleep(delay_sec)


def _fetch_wallet_points_snapshot(
    cfg: BotConfig,
    wallet: str,
    proxies: Optional[Dict[str, str]],
    logger: logging.Logger,
    mode: str,
) -> Optional[PointsSnapshot]:
    try:
        points_api = DomaApiClient(
            cfg.doma_api_url,
            api_key=cfg.doma_api_key,
            api_keys=cfg.doma_api_keys,
            proxies=proxies,
        )
        return points_api.fetch_points(wallet, cfg.leaderboard_rank_by)
    except Exception as exc:
        logger.warning("[%s] wallet=%s points snapshot fetch failed: %s", mode, wallet, exc)
        return None


def _log_wallet_points_delta(
    logger: logging.Logger,
    mode: str,
    wallet: str,
    before_snapshot: Optional[PointsSnapshot],
    after_snapshot: Optional[PointsSnapshot],
) -> None:
    def _fmt(value: Optional[Decimal]) -> str:
        return "n/a" if value is None else _format_decimal_plain(value)

    before_points = before_snapshot.points if before_snapshot else None
    after_points = after_snapshot.points if after_snapshot else None
    before_volume = before_snapshot.trading_volume_usd if before_snapshot else None
    after_volume = after_snapshot.trading_volume_usd if after_snapshot else None
    delta_points = (
        after_points - before_points if before_points is not None and after_points is not None else None
    )
    delta_volume = (
        after_volume - before_volume if before_volume is not None and after_volume is not None else None
    )

    logger.info(
        "[%s] wallet=%s leaderboard | points before=%s after=%s delta=%s | volume before=%s after=%s delta=%s",
        mode,
        wallet,
        _fmt(before_points),
        _fmt(after_points),
        _fmt(delta_points),
        _fmt(before_volume),
        _fmt(after_volume),
        _fmt(delta_volume),
    )


def _derive_current_season_ranges(
    cfg: BotConfig,
    logger: logging.Logger,
    wallets: List[str],
) -> List[Tuple[str, Optional[datetime], Optional[datetime]]]:
    for idx, wallet in enumerate(wallets):
        ctx = _wallet_api_context(cfg, idx, logger, "Season range")
        if ctx is None:
            continue
        api_key, proxies = ctx
        api = DomaApiClient(
            cfg.doma_api_url,
            api_key=api_key,
            api_keys=[api_key] if api_key else [],
            proxies=proxies,
        )
        snapshot = api.fetch_points(wallet, cfg.leaderboard_rank_by)
        meta = snapshot.snapshot_date if snapshot else ""
        week_match = re.search(r"week=(\d+)", meta)
        season_match = re.search(r"season=(\d+)", meta)
        if not week_match or not season_match:
            continue
        week_number = int(week_match.group(1))
        season_number = int(season_match.group(1))
        if season_number != 1 or week_number < 1:
            logger.warning("Season range: expected current season=1, got meta=%s; season cost split skipped", meta)
            return []
        season_1_start = _current_week_start_utc() - timedelta(weeks=week_number - 1)
        logger.info(
            "Season range derived from leaderboard meta=%s | season_1_start=%s",
            meta,
            season_1_start.isoformat(),
        )
        return [
            ("season_0", None, season_1_start),
            ("season_1", season_1_start, None),
        ]
    logger.warning("Season range: unable to derive season/week from leaderboard; season cost split skipped")
    return []


def run_bridge_once(cfg: BotConfig, logger: logging.Logger, state: BotState) -> None:
    bridge_tasks = cfg.bridge_tasks
    success_wallets = 0
    failed_wallets = 0
    skipped_wallets = 0
    failed_wallet_addresses: List[str] = []

    def _fail_wallet() -> None:
        nonlocal failed_wallets
        failed_wallets += 1
        wallet_label = f"wallet#{line_idx + 1}"
        if wallet_label not in failed_wallet_addresses:
            failed_wallet_addresses.append(wallet_label)

    need_eth_price = False
    for raw in bridge_tasks:
        try:
            _left, _pair, amount_expr = [x.strip() for x in raw.split(":", 2)]
            amount_expr_lc = amount_expr.strip().lower()
            if amount_expr_lc.startswith(("rand_percent(", "rand_token(")):
                continue
            mode, _ = parse_trade_amount_expression(amount_expr)
            if mode == "usd":
                need_eth_price = True
                break
        except Exception:
            need_eth_price = True
            break

    def _resolve_eth_price(active_proxies: Optional[Dict[str, str]]) -> Decimal:
        if not need_eth_price:
            return Decimal("0")
        errors: List[str] = []
        try:
            subgraph = DomaSubgraphClient(cfg.subgraph_url, proxies=active_proxies)
            price = subgraph.fetch_eth_price_usd()
            if price > 0:
                return price
        except Exception as exc:
            errors.append(f"doma_subgraph: {exc}")
        try:
            price = _fetch_public_eth_price_usd(active_proxies)
            if price > 0:
                logger.info("[BRIDGE] ETH/USD resolved via public fallback: %s", _format_decimal_plain(price))
                return price
        except Exception as exc:
            errors.append(str(exc))
        raise RuntimeError("Failed to resolve ETH/USD for bridge: " + " | ".join(errors))

    def _expand_bridge_tasks_for_wallet(tasks: List[str]) -> List[str]:
        expanded: List[str] = []
        for raw_task in tasks:
            left, pair, amount_expr = [x.strip() for x in raw_task.split(":", 2)]
            expr = amount_expr.strip()
            if expr.startswith("rand_token(") and expr.endswith(")"):
                inside = expr[len("rand_token(") : -1]
                min_raw, max_raw = [x.strip() for x in inside.split("|", 1)]
                min_value = _parse_decimal_input(min_raw)
                max_value = _parse_decimal_input(max_raw)
                expr = _pick_random_amount_expr(
                    "number",
                    min_value,
                    max_value,
                    state,
                    min_raw=min_raw,
                    max_raw=max_raw,
                )
            elif expr.startswith("rand_percent(") and expr.endswith(")"):
                inside = expr[len("rand_percent(") : -1]
                min_raw, max_raw = [x.strip() for x in inside.split("|", 1)]
                min_value = _parse_decimal_input(min_raw)
                max_value = _parse_decimal_input(max_raw)
                expr = _pick_random_amount_expr(
                    "percent",
                    min_value,
                    max_value,
                    state,
                    min_raw=min_raw,
                    max_raw=max_raw,
                )
            expanded.append(f"{left}:{pair}:{expr}")
        return expanded

    wallet_key_records = _build_wallet_key_records(cfg, logger, "BRIDGE")

    if not wallet_key_records:
        raise ValueError(
            "No wallet/private-key pairs available for bridge "
            "(fill wallets.txt + keys.txt line-by-line or set valid PRIVATE_KEY in .env)"
        )
    wallet_key_records, wallet_start_offset, total_loaded_wallets = _apply_wallet_start_selection(wallet_key_records)

    logger.info(
        "[BRIDGE] mode started | wallets=%s | start_wallet=%s",
        len(wallet_key_records),
        wallet_start_offset + 1,
    )
    for idx, (line_idx, wallet, private_key) in enumerate(wallet_key_records):
        proxies, skip_wallet = _proxy_for_line(cfg, line_idx, logger, "BRIDGE")
        if skip_wallet:
            skipped_wallets += 1
            continue
        logger.info("[BRIDGE] wallet %s", _wallet_record_progress_label(idx, len(wallet_key_records), line_idx, total_loaded_wallets, wallet))
        wallet_tasks = bridge_tasks
        try:
            wallet_tasks = _expand_bridge_tasks_for_wallet(bridge_tasks)
            eth_price = _resolve_eth_price(proxies)
            if need_eth_price and eth_price <= 0:
                raise RuntimeError("Failed to resolve ETH/USD for bridge")
            run_bridge_tasks(
                cfg=cfg,
                logger=logger,
                wallet=wallet,
                private_key=private_key,
                tasks=wallet_tasks,
                proxies=proxies,
                eth_price_usd=eth_price,
                force_run=True,
            )
        except Exception as exc:
            # Common failure mode: broken proxy returns non-JSON RPC body.
            if proxies:
                logger.warning("[BRIDGE] wallet %s via proxy failed, retrying without proxy: %s", wallet, exc)
                eth_price = _resolve_eth_price(None)
                if need_eth_price and eth_price <= 0:
                    raise RuntimeError("Failed to resolve ETH/USD for bridge")
                run_bridge_tasks(
                    cfg=cfg,
                    logger=logger,
                    wallet=wallet,
                    private_key=private_key,
                    tasks=wallet_tasks,
                    proxies=None,
                    eth_price_usd=eth_price,
                    force_run=True,
                )
            else:
                raise
        except Exception as exc:
            _fail_wallet()
            logger.warning("[BRIDGE] wallet %s failed: %s", wallet, exc)
            continue
        else:
            success_wallets += 1

        if idx < len(wallet_key_records) - 1 and cfg.wallet_delay_max_sec > 0:
            delay_sec = random.uniform(cfg.wallet_delay_min_sec, cfg.wallet_delay_max_sec)
            logger.info("[BRIDGE] delay before next wallet: %.2f sec", delay_sec)
            time.sleep(delay_sec)
    state.last_bridge_ts = current_ts()
    _print_mode_summary(
        "BRIDGE",
        len(wallet_key_records),
        success_wallets,
        failed_wallets,
        skipped_wallets,
        failed_wallet_addresses,
    )


def run_close_position_once(cfg: BotConfig, logger: logging.Logger, state: BotState) -> None:
    _ = state
    success_wallets = 0
    failed_wallets = 0
    skipped_wallets = 0
    failed_wallet_addresses: List[str] = []
    weth_token = _token_from_config_override(cfg, "WETH", 18)

    def _fail_wallet() -> None:
        nonlocal failed_wallets
        failed_wallets += 1
        wallet_label = f"wallet#{line_idx + 1}"
        if wallet_label not in failed_wallet_addresses:
            failed_wallet_addresses.append(wallet_label)
    if not cfg.position_manager_address or cfg.position_manager_address == "0x0000000000000000000000000000000000000000":
        raise ValueError("Set position_manager_address in contracts.json")

    wallet_key_records = _build_wallet_key_records(cfg, logger, "POSITION")

    if not wallet_key_records:
        fallback_pk = (cfg.private_key or "").strip()
        if fallback_pk:
            try:
                fallback_wallet = cfg.account_address if _is_valid_evm_address(cfg.account_address) else ""
                if not fallback_wallet:
                    fallback_wallet = Web3().eth.account.from_key(fallback_pk).address
                wallet_key_records.append((0, fallback_wallet.lower(), fallback_pk))
                logger.info("[POSITION] fallback to .env credentials for close-all mode")
            except Exception as exc:
                raise ValueError(
                    "No wallet/private-key pairs available for close positions "
                    "(fill wallets.txt + keys.txt line-by-line or set valid PRIVATE_KEY in .env)"
                ) from exc
        else:
            raise ValueError(
                "No wallet/private-key pairs available for close positions "
                "(fill wallets.txt + keys.txt line-by-line or set valid PRIVATE_KEY in .env)"
            )
    wallet_key_records, wallet_start_offset, total_loaded_wallets = _apply_wallet_start_selection(wallet_key_records)

    logger.info(
        "[POSITION] close-all mode | wallets=%s | start_wallet=%s",
        len(wallet_key_records),
        wallet_start_offset + 1,
    )
    rpc_candidates: List[str] = []
    for rpc in [cfg.rpc_url, "https://rpc.doma.xyz/", "https://doma.drpc.org/"]:
        r = (rpc or "").strip()
        if r and r not in rpc_candidates:
            rpc_candidates.append(r)

    for idx, (line_idx, wallet, private_key) in enumerate(wallet_key_records):
        proxies, skip_wallet = _proxy_for_line(cfg, line_idx, logger, "POSITION")
        if skip_wallet:
            skipped_wallets += 1
            continue
        logger.info("[POSITION] wallet %s", _wallet_record_progress_label(idx, len(wallet_key_records), line_idx, total_loaded_wallets, wallet))
        wallet_failed = False
        client: Optional[PositionManagerClient] = None
        init_errors: List[str] = []
        for rpc_url in rpc_candidates:
            for try_proxies in [proxies, None]:
                try:
                    candidate = PositionManagerClient(
                        rpc_url=rpc_url,
                        chain_id=cfg.chain_id,
                        account_address=wallet,
                        private_key=private_key,
                        position_manager_address=cfg.position_manager_address,
                        request_proxies=try_proxies,
                    )
                    # Verify that reads work on this endpoint before using it.
                    _ = candidate.list_owner_token_ids(owner=wallet, limit=1)
                    client = candidate
                    if rpc_url != cfg.rpc_url:
                        logger.info("[POSITION] wallet %s switched RPC to %s", wallet, rpc_url)
                    break
                except Exception as exc:
                    init_errors.append(f"{rpc_url}: {exc}")
            if client is not None:
                break
        if client is None:
            logger.warning(
                "[POSITION] wallet %s init failed: %s",
                wallet,
                " | ".join(init_errors),
            )
            _fail_wallet()
            continue

        try:
            owner_positions = client.list_owner_positions(owner=wallet, limit=200, only_active=False)
        except Exception as exc:
            logger.warning("[POSITION] wallet %s: failed to read positions: %s", wallet, exc)
            _fail_wallet()
            continue
        if not owner_positions:
            logger.info("[POSITION] wallet %s: no positions", wallet)
        else:
            logger.info("[POSITION] wallet %s: positions=%s", wallet, len(owner_positions))

        for p_idx, p in enumerate(owner_positions):
            token_id = int(p.token_id)
            liq_to_remove = int(p.liquidity)
            logger.info(
                "[POSITION] wallet=%s tokenId=%s liquidity=%s remove=100%%",
                wallet,
                token_id,
                p.liquidity,
            )
            if cfg.paper_mode or cfg.dry_run or not cfg.enable_execution:
                logger.info("[POSITION] PAPER/DRY mode: wallet=%s tokenId=%s no tx sent", wallet, token_id)
                continue

            try:
                if liq_to_remove > 0:
                    tx1 = client.decrease_liquidity(
                        token_id=token_id,
                        liquidity_to_remove=liq_to_remove,
                        deadline_sec=600,
                    )
                    logger.info("[POSITION] decreaseLiquidity wallet=%s tokenId=%s tx=%s", wallet, token_id, tx1)
                    if not _wait_tx_receipt(client, tx1, timeout_sec=240):
                        raise RuntimeError(f"decreaseLiquidity tx failed or timed out: {tx1}")
                try:
                    fresh = client.get_position_info(token_id)
                    if fresh.tokens_owed0 > 0 or fresh.tokens_owed1 > 0:
                        tx2 = client.collect_all(token_id=token_id, recipient=wallet)
                        logger.info("[POSITION] collect wallet=%s tokenId=%s tx=%s", wallet, token_id, tx2)
                        if not _wait_tx_receipt(client, tx2, timeout_sec=240):
                            raise RuntimeError(f"collect tx failed or timed out: {tx2}")
                        fresh = client.get_position_info(token_id)
                    if fresh.tokens_owed0 > 0 or fresh.tokens_owed1 > 0:
                        logger.info(
                            "[POSITION] tokenId=%s has owed tokens after collect | owed0=%s owed1=%s | collecting again",
                            token_id,
                            fresh.tokens_owed0,
                            fresh.tokens_owed1,
                        )
                        tx2b = client.collect_all(token_id=token_id, recipient=wallet)
                        logger.info("[POSITION] collect retry wallet=%s tokenId=%s tx=%s", wallet, token_id, tx2b)
                        if not _wait_tx_receipt(client, tx2b, timeout_sec=240):
                            raise RuntimeError(f"collect retry tx failed or timed out: {tx2b}")
                        fresh = client.get_position_info(token_id)
                    if fresh.liquidity > 0:
                        logger.info("[POSITION] burn skipped wallet=%s tokenId=%s | remaining liquidity=%s", wallet, token_id, fresh.liquidity)
                    elif fresh.tokens_owed0 > 0 or fresh.tokens_owed1 > 0:
                        logger.info(
                            "[POSITION] burn skipped wallet=%s tokenId=%s | owed0=%s owed1=%s",
                            wallet,
                            token_id,
                            fresh.tokens_owed0,
                            fresh.tokens_owed1,
                        )
                    else:
                        tx3 = client.burn(token_id=token_id)
                        logger.info("[POSITION] burn wallet=%s tokenId=%s tx=%s", wallet, token_id, tx3)
                        if not _wait_tx_receipt(client, tx3, timeout_sec=240):
                            raise RuntimeError(f"burn tx failed or timed out: {tx3}")
                except Exception as exc:
                    logger.warning("[POSITION] burn skipped/failed wallet=%s tokenId=%s: %s", wallet, token_id, exc)
            except Exception as exc:
                logger.warning("[POSITION] close failed wallet=%s tokenId=%s: %s", wallet, token_id, exc)
                wallet_failed = True

            if p_idx < len(owner_positions) - 1:
                pos_delay_sec = random.uniform(10, 20)
                logger.info("[POSITION] delay before next position: %.2f sec", pos_delay_sec)
                time.sleep(pos_delay_sec)

        if not (cfg.paper_mode or cfg.dry_run or not cfg.enable_execution):
            try:
                weth_raw = client.weth_balance_raw(weth_token.address)
                if weth_raw > 0:
                    weth_amount = Decimal(weth_raw) / (Decimal(10) ** weth_token.decimals)
                    logger.info(
                        "[POSITION] wallet=%s final cleanup | WETH->ETH amount=%s WETH",
                        wallet,
                        _format_decimal_plain(weth_amount),
                    )
                    unwrap_tx = client.unwrap_weth(weth_token.address, weth_raw)
                    if unwrap_tx:
                        logger.info("[POSITION] WETH->ETH wallet=%s tx=%s", wallet, unwrap_tx)
                        if not _wait_tx_receipt(client, unwrap_tx, timeout_sec=240):
                            raise RuntimeError(f"WETH->ETH tx failed or timed out: {unwrap_tx}")
            except Exception as exc:
                logger.warning("[POSITION] wallet=%s WETH->ETH cleanup failed: %s", wallet, exc)
                wallet_failed = True

        if wallet_failed:
            _fail_wallet()
        else:
            success_wallets += 1

        if idx < len(wallet_key_records) - 1 and cfg.wallet_delay_max_sec > 0:
            delay_sec = random.uniform(cfg.wallet_delay_min_sec, cfg.wallet_delay_max_sec)
            logger.info("[POSITION] delay before next wallet: %.2f sec", delay_sec)
            time.sleep(delay_sec)
    _print_mode_summary(
        "POSITION",
        len(wallet_key_records),
        success_wallets,
        failed_wallets,
        skipped_wallets,
        failed_wallet_addresses,
    )


def _decimal_places_from_raw(raw: str) -> int:
    s = (raw or "").strip().replace(" ", "").replace(",", ".")
    if "." not in s:
        return 0
    return len(s.split(".", 1)[1])


def _parse_decimal_input(raw: str) -> Decimal:
    s = (raw or "").strip().replace(" ", "").replace(",", ".")
    if not s:
        raise ValueError("Empty number")
    return Decimal(s)


def _format_decimal_plain(value: Decimal) -> str:
    s = format(value, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def _format_decimal_for_api(value: Decimal) -> str:
    return _format_decimal_plain(value.normalize())


def _random_okx_decimal_between(min_value: Decimal, max_value: Decimal, precision: int = 8) -> Decimal:
    if max_value < min_value:
        raise ValueError("Maximum amount must be >= minimum amount")
    step = Decimal(1).scaleb(-precision)
    min_units = int((min_value / step).to_integral_value(rounding=ROUND_CEILING))
    max_units = int((max_value / step).to_integral_value(rounding=ROUND_FLOOR))
    if max_units < min_units:
        max_units = min_units
    return (Decimal(random.randint(min_units, max_units)) * step).quantize(step)


def _prompt_positive_decimal(prompt: str, default: str = "") -> Decimal:
    suffix = f" [{default}]" if default else ""
    while True:
        raw = input(f"{prompt}{suffix}: ").strip() or default
        try:
            value = _parse_decimal_input(raw)
        except Exception:
            print("Enter a positive number.")
            continue
        if value > 0:
            return value
        print("Enter a positive number.")


def _load_okx_withdraw_addresses(cfg: BotConfig, logger: logging.Logger) -> List[Tuple[int, str]]:
    lines = _read_nonempty_lines(cfg.okx_withdraw_addresses_file)
    records: List[Tuple[int, str]] = []
    for line_idx, raw in enumerate(lines):
        address = raw.split(";", 1)[0].split(",", 1)[0].strip()
        if not _is_valid_evm_address(address):
            logger.warning("[OKX_WITHDRAW] skip address line %s: invalid EVM address", line_idx + 1)
            continue
        records.append((line_idx, address.lower()))
    return records


def _apply_address_order(records: List[Tuple[int, str]], order: str) -> List[Tuple[int, str]]:
    selected = list(records)
    if order == "random":
        random.shuffle(selected)
    return selected


def _append_okx_withdraw_csv(
    cfg: BotConfig,
    status: str,
    address: str,
    line_idx: int,
    ccy: str,
    chain: str,
    amount: Decimal,
    fee: Decimal,
    withdraw_id: str,
    client_id: str,
    reason: str,
) -> None:
    append_csv(
        cfg.points_csv_file.parent / OKX_WITHDRAWALS_CSV.name,
        [
            datetime.now(timezone.utc).isoformat(),
            status,
            address,
            line_idx + 1,
            ccy,
            chain,
            _format_decimal_for_api(amount),
            _format_decimal_for_api(fee),
            withdraw_id,
            client_id,
            reason,
        ],
        delimiter=cfg.csv_delimiter,
    )


def _okx_decimal_field(item: Dict[str, Any], names: List[str]) -> Optional[Decimal]:
    for name in names:
        raw = str(item.get(name) or "").strip()
        if not raw:
            continue
        try:
            return Decimal(raw)
        except Exception:
            continue
    return None


def _okx_chain_limits(item: Dict[str, Any]) -> Tuple[Optional[Decimal], Optional[Decimal]]:
    min_withdraw = _okx_decimal_field(
        item,
        ["minWd", "minWdAmt", "minWdAmount", "minWithdraw", "minWithdrawAmt", "minWithdrawalAmt"],
    )
    fee = _okx_decimal_field(item, ["minFee", "minWdFee"])
    return min_withdraw, fee


def _prompt_okx_withdraw_amount(prompt: str, min_withdraw: Optional[Decimal], default: str = "") -> Decimal:
    while True:
        value = _prompt_positive_decimal(prompt, default)
        if min_withdraw is None or value >= min_withdraw:
            return value
        print(f"Minimum withdrawal amount is {_format_decimal_plain(min_withdraw)}.")


def _prompt_okx_currency_and_chain(
    client: OkxApiClient,
    logger: logging.Logger,
) -> Tuple[str, str, Optional[Decimal], Optional[Decimal]]:
    currency_options = ["ETH", "USDT", "USDC"]
    print("Currency:")
    for idx, currency in enumerate(currency_options, start=1):
        print(f"{idx}) {currency}")
    custom_currency_idx = len(currency_options) + 1
    print(f"{custom_currency_idx}) Custom")
    while True:
        raw = input(f"Select [1-{custom_currency_idx}, default 1]: ").strip() or "1"
        if raw.isdigit() and 1 <= int(raw) <= custom_currency_idx:
            currency_idx = int(raw)
            break
        print("Invalid selection.")
    if currency_idx == custom_currency_idx:
        ccy = input("Currency: ").strip().upper()
        if not ccy:
            raise ValueError("Currency is required")
    else:
        ccy = currency_options[currency_idx - 1]

    chain_items: List[Dict[str, Any]] = []
    try:
        chain_items = client.get_currencies(ccy)
    except Exception as exc:
        logger.warning("[OKX_WITHDRAW] failed to load OKX chains for %s, use Custom chain: %s", ccy, exc)

    chain_options: List[Tuple[str, str, Optional[Decimal], Optional[Decimal]]] = []
    seen_chains = set()
    for item in chain_items:
        chain = str(item.get("chain") or "").strip()
        chain_key = chain.lower()
        if not chain or chain_key in seen_chains:
            continue
        seen_chains.add(chain_key)
        can_withdraw = str(item.get("canWd") or item.get("canWithdraw") or "true").strip().lower()
        if can_withdraw in {"false", "0", "no"}:
            continue
        min_withdraw, fee = _okx_chain_limits(item)
        label_parts = [chain]
        if min_withdraw is not None:
            label_parts.append(f"min={_format_decimal_plain(min_withdraw)}")
        if fee is not None:
            label_parts.append(f"fee={_format_decimal_plain(fee)}")
        chain_options.append((chain, " | ".join(label_parts), min_withdraw, fee))
    chain_options = sorted(chain_options, key=lambda pair: pair[0].lower())

    print("OKX chain:")
    for idx, option in enumerate(chain_options, start=1):
        label = option[1]
        print(f"{idx}) {label}")
    custom_chain_idx = len(chain_options) + 1
    print(f"{custom_chain_idx}) Custom")
    while True:
        raw = input(f"Select [1-{custom_chain_idx}]: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= custom_chain_idx:
            chain_idx = int(raw)
            break
        print("Invalid selection.")
    if chain_idx == custom_chain_idx:
        chain = input("OKX chain, e.g. ETH-Base / USDT-Polygon / ETH-Arbitrum One: ").strip()
        if not chain:
            raise ValueError("OKX chain is required")
        min_withdraw = None
        fee = None
        chain_key = chain.lower()
        for item in chain_items:
            if str(item.get("chain") or "").strip().lower() == chain_key:
                min_withdraw, fee = _okx_chain_limits(item)
                break
    else:
        chain, _, min_withdraw, fee = chain_options[chain_idx - 1]
    return ccy, chain, min_withdraw, fee


def run_okx_withdrawals_once(cfg: BotConfig, logger: logging.Logger, state: BotState) -> None:
    _ = state
    if not (cfg.okx_api_key and cfg.okx_secret_key and cfg.okx_passphrase):
        raise RuntimeError(
            f"OKX credentials are missing. Put api_key, secret_key and passphrase into {cfg.okx_api_keys_file} "
            "on separate lines, or set OKX_API_KEY/OKX_SECRET_KEY/OKX_PASSPHRASE."
        )

    records = _load_okx_withdraw_addresses(cfg, logger)
    if not records:
        raise RuntimeError(f"No valid EVM addresses found in {cfg.okx_withdraw_addresses_file}")

    print("\nWithdraw from OKX to addresses:")
    client = OkxApiClient(
        cfg.okx_api_key,
        cfg.okx_secret_key,
        cfg.okx_passphrase,
        base_url=cfg.okx_base_url,
    )
    ccy, chain, min_withdraw_amount, fee = _prompt_okx_currency_and_chain(client, logger)
    if min_withdraw_amount is None:
        logger.warning("[OKX_WITHDRAW] minimum withdrawal amount not found in OKX metadata | ccy=%s | chain=%s", ccy, chain)
    else:
        logger.info(
            "[OKX_WITHDRAW] minimum withdrawal amount resolved from OKX | ccy=%s | chain=%s | min=%s",
            ccy,
            chain,
            _format_decimal_plain(min_withdraw_amount),
        )

    print("Amount mode:")
    print("1) Fixed amount")
    print("2) Random amount from min/max")
    amount_mode = input("Select [1-2, default 1]: ").strip() or "1"
    if amount_mode == "2":
        min_amount = _prompt_okx_withdraw_amount("Minimum amount per address", min_withdraw_amount)
        max_amount = _prompt_okx_withdraw_amount("Maximum amount per address", min_withdraw_amount)
        if max_amount < min_amount:
            raise ValueError("Maximum amount must be >= minimum amount")
        fixed_amount = Decimal("0")
    else:
        fixed_amount = _prompt_okx_withdraw_amount("Amount per address", min_withdraw_amount)
        min_amount = fixed_amount
        max_amount = fixed_amount

    if fee is None:
        fee = client.get_min_withdraw_fee(ccy, chain)
    logger.info("[OKX_WITHDRAW] fee resolved from OKX | ccy=%s | chain=%s | fee=%s", ccy, chain, fee)

    delay_min = _prompt_positive_decimal("Minimum delay between withdrawals sec", "10")
    delay_max = _prompt_positive_decimal("Maximum delay between withdrawals sec", "30")
    if delay_max < delay_min:
        raise ValueError("Maximum delay must be >= minimum delay")

    start_number = _prompt_start_wallet_number(len(records))
    end_number = _prompt_end_wallet_number(len(records), start_number)
    order = _prompt_wallet_order(default_random=True)
    selected = _apply_address_order(records[start_number - 1 : end_number], order)

    live = True

    logger.info(
        "[OKX_WITHDRAW] mode started | addresses=%s | start_address=%s | end_address=%s | order=%s | ccy=%s | chain=%s | amount=%s-%s | fee=%s | live=%s",
        len(records),
        start_number,
        end_number,
        order,
        ccy,
        chain,
        _format_decimal_for_api(min_amount),
        _format_decimal_for_api(max_amount),
        _format_decimal_for_api(fee),
        live,
    )

    success = 0
    failed = 0
    failed_addresses: List[str] = []
    selected_count = len(selected)
    for offset, (line_idx, address) in enumerate(selected):
        amount = fixed_amount if amount_mode != "2" else _random_okx_decimal_between(min_amount, max_amount)
        client_id = f"doma{line_idx + 1}{int(time.time() * 1000)}"[:32]
        try:
            logger.info(
                "[OKX_WITHDRAW] address %s/%s line=%s to=%s | %s %s | fee=%s",
                offset + 1,
                selected_count,
                line_idx + 1,
                address,
                _format_decimal_for_api(amount),
                ccy,
                _format_decimal_for_api(fee),
            )
            result = client.withdraw(ccy, chain, amount, fee, address, client_id=client_id)
            withdraw_id = str(result.get("wdId") or result.get("id") or "")
            logger.info("[OKX_WITHDRAW] sent | address=%s | withdraw_id=%s | client_id=%s", address, withdraw_id, client_id)
            _append_okx_withdraw_csv(cfg, "sent", address, line_idx, ccy, chain, amount, fee, withdraw_id, client_id, "")
            success += 1
        except Exception as exc:
            failed += 1
            failed_addresses.append(address)
            logger.warning("[OKX_WITHDRAW] address=%s failed: %s", address, exc)
            _append_okx_withdraw_csv(cfg, "failed", address, line_idx, ccy, chain, amount, fee, "", client_id, str(exc))

        if offset < selected_count - 1:
            delay_sec = random.uniform(float(delay_min), float(delay_max))
            logger.info("[OKX_WITHDRAW] delay before next address: %.2f sec", delay_sec)
            time.sleep(delay_sec)

    _print_mode_summary("OKX_WITHDRAW", len(selected), success, failed, 0, failed_addresses)


def _load_exchange_deposit_addresses(cfg: BotConfig) -> List[str]:
    return [line.split(";", 1)[0].split(",", 1)[0].strip().lower() for line in _read_nonempty_lines(cfg.exchange_deposit_addresses_file)]


def _deposit_address_for_wallet(deposit_addresses: List[str], wallet_idx: int) -> str:
    if not deposit_addresses:
        raise RuntimeError("No deposit addresses loaded")
    if len(deposit_addresses) == 1:
        return deposit_addresses[0]
    if wallet_idx < len(deposit_addresses):
        return deposit_addresses[wallet_idx]
    return ""


def _append_exchange_deposit_csv(
    cfg: BotConfig,
    status: str,
    wallet_idx: int,
    deposit_address: str,
    chain_label: str,
    symbol: str,
    amount: Decimal,
    tx_hash: str,
    reason: str,
) -> None:
    append_csv(
        cfg.points_csv_file.parent / EXCHANGE_DEPOSITS_CSV.name,
        [
            datetime.now(timezone.utc).isoformat(),
            status,
            wallet_idx + 1,
            deposit_address,
            chain_label,
            symbol,
            _format_decimal_for_api(amount),
            tx_hash,
            reason,
        ],
        delimiter=cfg.csv_delimiter,
    )


def run_exchange_deposit_once(cfg: BotConfig, logger: logging.Logger, state: BotState) -> None:
    _ = state
    wallet_key_records = _build_wallet_key_records(cfg, logger, "EXCHANGE_DEPOSIT")
    if not wallet_key_records:
        raise RuntimeError("No wallets with private keys found")
    deposit_addresses = _load_exchange_deposit_addresses(cfg)
    if not any(_is_valid_evm_address(address) for address in deposit_addresses):
        raise RuntimeError(f"No valid EVM deposit addresses found in {cfg.exchange_deposit_addresses_file}")

    networks = _l2_deposit_networks()
    print("\nDeposit to exchange from L2:")
    print("Network:")
    for idx, (label, *_rest) in enumerate(networks, start=1):
        print(f"{idx}) {label}")
    network_raw = input(f"Select [1-{len(networks)}]: ").strip()
    if not network_raw.isdigit() or not 1 <= int(network_raw) <= len(networks):
        raise ValueError("Invalid network selection")
    chain_label, chain_id, symbol, rpc_urls, native_reserve = networks[int(network_raw) - 1]

    print("Amount mode:")
    print(f"1) Fixed amount ({symbol})")
    print("2) Random amount from min/max")
    print("3) Percent of spendable balance")
    amount_mode = input("Select [1-3, default 1]: ").strip() or "1"
    if amount_mode == "2":
        min_amount = _prompt_positive_decimal(f"Minimum amount per wallet {symbol}")
        max_amount = _prompt_positive_decimal(f"Maximum amount per wallet {symbol}")
        if max_amount < min_amount:
            raise ValueError("Maximum amount must be >= minimum amount")
        fixed_amount = Decimal("0")
        percent_amount = Decimal("0")
    elif amount_mode == "3":
        percent_amount = _prompt_positive_decimal("Percent of spendable balance", "100")
        if percent_amount > 100:
            raise ValueError("Percent cannot be > 100")
        fixed_amount = Decimal("0")
        min_amount = Decimal("0")
        max_amount = Decimal("0")
    else:
        fixed_amount = _prompt_positive_decimal(f"Amount per wallet {symbol}")
        min_amount = fixed_amount
        max_amount = fixed_amount
        percent_amount = Decimal("0")

    delay_min = _prompt_positive_decimal("Minimum delay between deposits sec", "10")
    delay_max = _prompt_positive_decimal("Maximum delay between deposits sec", "30")
    if delay_max < delay_min:
        raise ValueError("Maximum delay must be >= minimum delay")

    start_number = _prompt_start_wallet_number(len(wallet_key_records))
    end_number = _prompt_end_wallet_number(len(wallet_key_records), start_number)
    order = _prompt_wallet_order(default_random=True)
    selected = _apply_wallet_order(wallet_key_records[start_number - 1 : end_number], order)

    logger.info(
        "[EXCHANGE_DEPOSIT] mode started | wallets=%s | start_wallet=%s | end_wallet=%s | order=%s | network=%s | symbol=%s | deposit_addresses=%s",
        len(wallet_key_records),
        start_number,
        end_number,
        order,
        chain_label,
        symbol,
        len(deposit_addresses),
    )

    success = 0
    failed = 0
    failed_wallets: List[str] = []
    for order_idx, (wallet_idx, wallet, private_key) in enumerate(selected):
        wallet_number = wallet_idx + 1
        deposit_address = _deposit_address_for_wallet(deposit_addresses, wallet_idx)
        if not _is_valid_evm_address(deposit_address):
            failed += 1
            failed_wallets.append(wallet)
            reason = f"missing/invalid deposit address for wallet line {wallet_number}"
            logger.warning("[EXCHANGE_DEPOSIT] wallet %s skipped | %s", wallet_number, reason)
            _append_exchange_deposit_csv(cfg, "failed", wallet_idx, deposit_address, chain_label, symbol, Decimal("0"), "", reason)
            continue

        try:
            proxies, skip_wallet = _proxy_for_line(cfg, wallet_idx, logger, "EXCHANGE_DEPOSIT")
            if skip_wallet:
                failed += 1
                failed_wallets.append(wallet)
                reason = "missing proxy on matching line"
                _append_exchange_deposit_csv(cfg, "failed", wallet_idx, deposit_address, chain_label, symbol, Decimal("0"), "", reason)
                continue
            exec_client = _build_exec_client_for_chain_rpcs(
                cfg,
                logger,
                wallet,
                private_key,
                chain_id,
                rpc_urls,
                proxies=proxies,
                log_prefix="[EXCHANGE_DEPOSIT]",
            )
            balance = exec_client.get_native_balance()
            spendable = max(Decimal("0"), balance - native_reserve)
            if amount_mode == "2":
                amount = _random_okx_decimal_between(min_amount, max_amount)
            elif amount_mode == "3":
                amount = (spendable * percent_amount / Decimal("100")).quantize(Decimal("0.00000001"))
            else:
                amount = fixed_amount
            if amount <= 0 or amount > spendable:
                raise RuntimeError(
                    f"insufficient spendable balance: amount={_format_decimal_plain(amount)} {symbol}, "
                    f"spendable={_format_decimal_plain(spendable)} {symbol}, reserve={_format_decimal_plain(native_reserve)} {symbol}"
                )
            amount_raw = decimal_to_raw(amount, 18)
            logger.info(
                "[EXCHANGE_DEPOSIT] wallet %s/%s | to=%s | %s %s on %s",
                wallet_number,
                len(wallet_key_records),
                deposit_address,
                _format_decimal_plain(amount),
                symbol,
                chain_label,
            )
            tx_hash = exec_client.send_native(deposit_address, amount_raw)
            logger.info("[EXCHANGE_DEPOSIT] wallet %s tx sent: %s", wallet_number, tx_hash)
            _append_exchange_deposit_csv(cfg, "sent", wallet_idx, deposit_address, chain_label, symbol, amount, tx_hash, "")
            success += 1
        except Exception as exc:
            failed += 1
            failed_wallets.append(wallet)
            logger.warning("[EXCHANGE_DEPOSIT] wallet %s failed: %s", wallet_number, exc)
            _append_exchange_deposit_csv(cfg, "failed", wallet_idx, deposit_address, chain_label, symbol, Decimal("0"), "", str(exc))
        if order_idx < len(selected) - 1:
            delay_sec = random.uniform(float(delay_min), float(delay_max))
            logger.info("[EXCHANGE_DEPOSIT] delay before next wallet: %.2f sec", delay_sec)
            time.sleep(delay_sec)

    _print_mode_summary("EXCHANGE_DEPOSIT", len(selected), success, failed, 0, failed_wallets)


def _volume_added_from_usdc_balance_change(before_usdc: Decimal, after_usdc: Decimal, input_symbol: str) -> Decimal:
    if input_symbol == "USDC.E":
        return max(Decimal("0"), before_usdc - after_usdc)
    return max(Decimal("0"), after_usdc - before_usdc)


def _pick_random_amount_expr(
    mode: str,
    min_value: Decimal,
    max_value: Decimal,
    state: BotState,
    min_raw: str = "",
    max_raw: str = "",
) -> str:
    if mode == "percent":
        precision = max(2, _decimal_places_from_raw(min_raw), _decimal_places_from_raw(max_raw))
        precision = min(precision, 4)
    else:
        # For number mode, use one extra decimal place vs user input to increase randomness.
        # Example: 0.0001..0.0002 => random values like 0.00019 / 0.00018 / ...
        src_precision = max(_decimal_places_from_raw(min_raw), _decimal_places_from_raw(max_raw))
        precision = max(5, src_precision + 1)
        precision = min(precision, 8)
    step = Decimal(1).scaleb(-precision)
    if max_value < min_value:
        raise ValueError("Maximum must be >= minimum")
    if min_value <= 0:
        raise ValueError("Minimum must be > 0")
    if mode == "percent" and max_value > 100:
        raise ValueError("Percent maximum cannot be > 100")

    min_units = int((min_value / step).to_integral_value(rounding=ROUND_CEILING))
    max_units = int((max_value / step).to_integral_value(rounding=ROUND_FLOOR))
    if max_units < min_units:
        # If range is tighter than 0.01, fallback to exact min.
        amount = min_value
    else:
        used = set(state.used_bridge_amounts)
        amount = None
        for _ in range(200):
            units = random.randint(min_units, max_units)
            candidate = (Decimal(units) * step).quantize(step)
            if mode == "percent":
                candidate_str = f"{candidate:.{precision}f}%"
            else:
                candidate_str = _format_decimal_plain(candidate)
            key = f"{mode}:{candidate_str.strip()}"
            if key not in used:
                amount = candidate
                state.used_bridge_amounts.append(key)
                break
        if amount is None:
            units = random.randint(min_units, max_units)
            amount = (Decimal(units) * step).quantize(step)

    if mode == "percent":
        amount_str = f"{amount:.{precision}f}%"
    else:
        amount_str = _format_decimal_plain(amount)
    state.used_bridge_amounts = state.used_bridge_amounts[-500:]
    return amount_str


def _normalize_domain_token_symbol(raw: str) -> str:
    s = (raw or "").strip().strip(" \t\r\n,;")
    if not s:
        raise ValueError("Empty domain token input")
    low = s.lower()
    marker = "/domain/"
    if marker in low:
        idx = low.index(marker) + len(marker)
        tail = s[idx:]
        tail = tail.split("?", 1)[0].split("#", 1)[0].strip("/")
        if tail:
            s = tail
    return s.strip().strip(" \t\r\n,;").upper()


def _wait_tx_receipt(exec_client: EvmExecutionClient, tx_hash: str, timeout_sec: int = 180) -> bool:
    return _wait_tx_receipt_result(exec_client, tx_hash, timeout_sec=timeout_sec) == "success"


def _wait_tx_receipt_result(
    exec_client: EvmExecutionClient,
    tx_hash: str,
    timeout_sec: int = 180,
    poll_latency: float = 2.0,
) -> str:
    """Return success, reverted, or pending for a submitted transaction."""
    deadline = time.time() + max(1, timeout_sec)
    while time.time() < deadline:
        try:
            receipt = exec_client.web3.eth.wait_for_transaction_receipt(
                tx_hash,
                timeout=min(15, max(1, int(deadline - time.time()))),
                poll_latency=max(0.05, float(poll_latency)),
            )
            status = int(getattr(receipt, "status", 0))
            return "success" if status == 1 else "reverted"
        except Exception:
            time.sleep(max(0.05, float(poll_latency)))
    return "pending"


def _find_token_by_address(pools: List[Pool], address: str) -> Optional[Token]:
    target = (address or "").strip().lower()
    for pool in pools:
        if pool.token0.address == target:
            return pool.token0
        if pool.token1.address == target:
            return pool.token1
    return None


def _find_pool_by_address(pools: List[Pool], address: str) -> Optional[Pool]:
    target = (address or "").strip().lower()
    if not target:
        return None
    for pool in pools:
        if pool.address == target:
            return pool
    return None


def _token_from_launchpad(info: LaunchpadTokenInfo) -> Token:
    return Token(
        address=info.address,
        symbol=canonical_symbol(info.symbol or info.name),
        decimals=info.decimals,
        derived_eth=Decimal("0"),
    )


def _token_from_launchpad_price(info: LaunchpadTokenInfo, eth_price: Decimal) -> Token:
    derived_eth = Decimal("0")
    if info.price_usd > 0 and eth_price > 0:
        derived_eth = info.price_usd / eth_price
    return Token(
        address=info.address,
        symbol=canonical_symbol(info.symbol or info.name),
        decimals=info.decimals,
        derived_eth=derived_eth,
    )


def _token_from_config_override(cfg: BotConfig, symbol: str, decimals: int) -> Token:
    token_symbol = canonical_symbol(symbol)
    address = cfg.token_address_overrides.get(token_symbol)
    if not address:
        raise RuntimeError(f"{token_symbol} token address not found in contracts.json")
    return Token(
        address=address,
        symbol=token_symbol,
        decimals=decimals,
        derived_eth=Decimal("1") if token_symbol == "WETH" else Decimal("0"),
    )




def _usdce_token_from_config(cfg: BotConfig) -> Token:
    address = cfg.token_address_overrides.get("USDC.E") or cfg.token_address_overrides.get("USDC")
    if not address:
        raise RuntimeError("USDC.E token address not found in contracts.json")
    return Token(
        address=address.lower(),
        symbol="USDC.E",
        decimals=6,
        derived_eth=Decimal("0"),
    )

def _fetch_eth_price_via_doma_quote(cfg: BotConfig, doma_api: DomaApiClient, quote_token: Token) -> Decimal:
    last_exc: Optional[Exception] = None
    for delay_sec in DOMA_QUOTE_RETRY_DELAYS_SEC:
        if delay_sec > 0:
            time.sleep(delay_sec)
        try:
            quote = doma_api.fetch_universal_router_quote(
                token_in_address=DOMA_NATIVE_TOKEN_SENTINEL,
                token_out_address=quote_token.address,
                amount_raw=decimal_to_raw(Decimal("1"), 18),
                chain_id=cfg.chain_id,
                trade_type="exactIn",
                portion_bips=0,
                portion_recipient="",
            )
            if quote.quote_decimals <= 0:
                raise RuntimeError("Doma ETH/USD quote returned zero output")
            return quote.quote_decimals
        except Exception as exc:
            last_exc = exc
    if last_exc:
        raise last_exc
    raise RuntimeError("Doma ETH/USD quote failed")


def _fallback_eth_usdce_pool_for_ui_route(cfg: BotConfig, doma_api: DomaApiClient) -> Tuple[Pool, Decimal]:
    usdc_token = _usdce_token_from_config(cfg)
    weth_token = _token_from_config_override(cfg, "WETH", 18)
    eth_price = _fetch_eth_price_via_doma_quote(cfg, doma_api, usdc_token)
    if eth_price <= 0:
        raise RuntimeError("Failed to resolve ETH/USD via Doma quote")
    return (
        Pool(
            address="",
            fee_tier=0,
            tvl_usd=Decimal("0"),
            volume_24h_usd=Decimal("0"),
            token0=usdc_token,
            token1=weth_token,
            token0_price=Decimal("1"),
            token1_price=eth_price,
        ),
        eth_price,
    )


def _execute_launchpad_buy(
    cfg: BotConfig,
    logger: logging.Logger,
    state: BotState,
    exec_client: EvmExecutionClient,
    launchpad: LaunchpadTokenInfo,
    quote_token: Token,
    trade_amount_expr: str,
    eth_price: Decimal,
    label: str,
    wait_for_pre_tx: bool = False,
    post_approve_delay_range: Optional[Tuple[float, float]] = None,
    skip_allowance_check: bool = False,
    probe_before_send: bool = False,
    quiet_probe_fail: bool = False,
) -> bool:
    state.last_error = ""
    quote_price_usd = pick_token_usd_price(quote_token, eth_price)
    if quote_token.symbol == "USDC.E" and quote_price_usd <= 0:
        quote_price_usd = Decimal("1")
    try:
        balance_dec = exec_client.get_erc20_balance(quote_token.address, quote_token.decimals)
        amount_in_dec, trade_usd = resolve_trade_amount(trade_amount_expr, balance_dec, quote_price_usd)
    except Exception as exc:
        logger.warning("[%s] Invalid launchpad buy amount '%s': %s", label, trade_amount_expr, exc)
        return False
    amount_in_raw = decimal_to_raw(amount_in_dec, quote_token.decimals)
    if amount_in_raw <= 0:
        logger.warning("[%s] Amount too small after decimal conversion.", label)
        return False
    if cfg.paper_mode or cfg.dry_run or not cfg.enable_execution:
        logger.info("[%s] PAPER/DRY mode active. No transaction sent.", label)
        return True
    expected_out_dec = Decimal("0")
    if launchpad.price_usd > 0:
        expected_out_dec = trade_usd / launchpad.price_usd
    min_out_raw = max(1, decimal_to_raw(expected_out_dec * Decimal("0.7"), launchpad.decimals)) if expected_out_dec > 0 else 1
    logger.info(
        "[%s] %s -> %s | launchpad=%s | in=%.8f %s (~$%.2f) | out≈%.8f %s",
        label,
        quote_token.symbol,
        canonical_symbol(launchpad.symbol or launchpad.name),
        launchpad.launchpad_address,
        float(amount_in_dec),
        quote_token.symbol,
        float(trade_usd),
        float(expected_out_dec),
        canonical_symbol(launchpad.symbol or launchpad.name),
    )
    if not skip_allowance_check:
        approve_hash = exec_client.ensure_allowance(
            quote_token.address,
            amount_in_raw,
            spender_address=launchpad.launchpad_address,
        )
        if approve_hash:
            logger.info("[%s] Approve tx sent: %s", label, approve_hash)
            if wait_for_pre_tx:
                ok = _wait_tx_receipt(exec_client, approve_hash, timeout_sec=180)
                if not ok:
                    raise RuntimeError("Launchpad approve tx failed or timed out")
                delay_sec = _pick_delay_from_range(post_approve_delay_range)
                logger.info("[%s] delay after approve: %.2f sec", label, delay_sec)
                time.sleep(delay_sec)
    if probe_before_send:
        try:
            exec_client.call_launchpad_buy(
                launchpad_address=launchpad.launchpad_address,
                amount_in_raw=amount_in_raw,
                min_amount_out_raw=min_out_raw,
            )
        except Exception as exc:
            state.last_error = str(exc)
            if not quiet_probe_fail:
                logger.warning("[%s] Launchpad buy probe failed: %s", label, exc)
            return False
    try:
        tx_hash = exec_client.execute_launchpad_buy(
            launchpad_address=launchpad.launchpad_address,
            amount_in_raw=amount_in_raw,
            min_amount_out_raw=min_out_raw,
        )
    except Exception as exc:
        state.last_error = str(exc)
        logger.warning("[%s] Launchpad buy failed: %s", label, exc)
        return False
    state.daily_volume_usd += trade_usd
    state.last_tx_hash = tx_hash
    logger.info("[%s] Buy tx sent: %s", label, tx_hash)
    return True


def _execute_launchpad_sell(
    cfg: BotConfig,
    logger: logging.Logger,
    state: BotState,
    exec_client: EvmExecutionClient,
    launchpad: LaunchpadTokenInfo,
    quote_token: Token,
    trade_amount_expr: str,
    eth_price: Decimal,
    label: str,
    wait_for_pre_tx: bool = False,
    post_approve_delay_range: Optional[Tuple[float, float]] = None,
    failed_launch_min_out_retry: bool = False,
) -> bool:
    state.last_error = ""
    launchpad_token = _token_from_launchpad(launchpad)
    token_price_usd = launchpad.price_usd
    try:
        balance_dec = exec_client.get_erc20_balance(launchpad_token.address, launchpad_token.decimals)
        amount_in_dec, trade_usd = resolve_trade_amount(trade_amount_expr, balance_dec, token_price_usd if token_price_usd > 0 else Decimal("1"))
    except Exception as exc:
        logger.warning("[%s] Invalid launchpad sell amount '%s': %s", label, trade_amount_expr, exc)
        return False
    amount_in_raw = decimal_to_raw(amount_in_dec, launchpad_token.decimals)
    if amount_in_raw <= 0:
        logger.warning("[%s] Amount too small after decimal conversion.", label)
        return False
    if cfg.paper_mode or cfg.dry_run or not cfg.enable_execution:
        logger.info("[%s] PAPER/DRY mode active. No transaction sent.", label)
        return True
    quote_price_usd = pick_token_usd_price(quote_token, eth_price)
    if quote_token.symbol == "USDC.E" and quote_price_usd <= 0:
        quote_price_usd = Decimal("1")
    expected_out_dec = (amount_in_dec * token_price_usd / quote_price_usd) if token_price_usd > 0 and quote_price_usd > 0 else Decimal("0")
    min_out_raw = max(1, decimal_to_raw(expected_out_dec * Decimal("0.7"), quote_token.decimals)) if expected_out_dec > 0 else 1
    logger.info(
        "[%s] %s -> %s | launchpad=%s | in=%.8f %s (~$%.2f) | out≈%.8f %s",
        label,
        launchpad_token.symbol,
        quote_token.symbol,
        launchpad.launchpad_address,
        float(amount_in_dec),
        launchpad_token.symbol,
        float(trade_usd),
        float(expected_out_dec),
        quote_token.symbol,
    )
    approve_hash = exec_client.ensure_allowance(
        launchpad_token.address,
        amount_in_raw,
        spender_address=launchpad.launchpad_address,
    )
    if approve_hash:
        logger.info("[%s] Approve tx sent: %s", label, approve_hash)
        if wait_for_pre_tx:
            ok = _wait_tx_receipt(exec_client, approve_hash, timeout_sec=180)
            if not ok:
                raise RuntimeError("Launchpad approve tx failed or timed out")
            delay_sec = _pick_delay_from_range(post_approve_delay_range)
            logger.info("[%s] delay after approve: %.2f sec", label, delay_sec)
            time.sleep(delay_sec)
    try:
        if failed_launch_min_out_retry:
            logger.info("[%s] failed launch detected, using sellOnFail", label)
            tx_hash = exec_client.execute_launchpad_sell_on_fail(
                launchpad_address=launchpad.launchpad_address,
                amount_in_raw=amount_in_raw,
            )
        else:
            tx_hash = exec_client.execute_launchpad_sell(
                launchpad_address=launchpad.launchpad_address,
                amount_in_raw=amount_in_raw,
                min_amount_out_raw=min_out_raw,
            )
    except Exception as exc:
        state.last_error = str(exc)
        if failed_launch_min_out_retry and min_out_raw > 1:
            retry_min_out_raw = max(1, decimal_to_raw(expected_out_dec * Decimal("0.5"), quote_token.decimals)) if expected_out_dec > 0 else 1
            logger.warning(
                "[%s] sellOnFail failed, retrying ordinary launchpad sell with min_out=%s: %s",
                label,
                retry_min_out_raw,
                exc,
            )
            try:
                tx_hash = exec_client.execute_launchpad_sell(
                    launchpad_address=launchpad.launchpad_address,
                    amount_in_raw=amount_in_raw,
                    min_amount_out_raw=retry_min_out_raw,
                )
            except Exception as retry_exc:
                state.last_error = str(retry_exc)
                logger.warning("[%s] Launchpad sell retry failed: %s", label, retry_exc)
                return False
        else:
            logger.warning("[%s] Launchpad sell failed: %s", label, exc)
            return False
    state.daily_volume_usd += trade_usd
    state.last_tx_hash = tx_hash
    logger.info("[%s] Sell tx sent: %s", label, tx_hash)
    return True


def _execute_bonding_sell_with_route_fallback(
    cfg: BotConfig,
    logger: logging.Logger,
    state: BotState,
    doma_api: DomaApiClient,
    exec_client: EvmExecutionClient,
    launchpad: LaunchpadTokenInfo,
    quote_token: Token,
    trade_amount_expr: str,
    eth_price: Decimal,
    label: str,
    wait_for_pre_tx: bool = False,
    post_approve_delay_range: Optional[Tuple[float, float]] = None,
    refresh_launchpad: Optional[Callable[[], Optional[LaunchpadTokenInfo]]] = None,
) -> bool:
    current = launchpad

    def _sell_via_doma_route(reason: str) -> bool:
        route_token = _token_from_launchpad_price(current, eth_price)
        logger.warning(
            "[%s] Launchpad sell unavailable, trying Doma UI route | reason=%s",
            label,
            reason,
        )
        return _execute_trade_via_doma_ui_route(
            cfg=cfg,
            logger=logger,
            state=state,
            doma_api=doma_api,
            exec_client=exec_client,
            token_in=route_token,
            token_out=quote_token,
            display_in_symbol=route_token.symbol,
            display_out_symbol=quote_token.symbol,
            trade_amount_expr=trade_amount_expr,
            eth_price=eth_price,
            label=f"{label} ROUTE",
            wait_for_pre_tx=wait_for_pre_tx,
            post_approve_delay_range=post_approve_delay_range,
        )

    if current.pool_address:
        return _sell_via_doma_route(f"token has pool={current.pool_address}")

    ok = _execute_launchpad_sell(
        cfg=cfg,
        logger=logger,
        state=state,
        exec_client=exec_client,
        launchpad=current,
        quote_token=quote_token,
        trade_amount_expr=trade_amount_expr,
        eth_price=eth_price,
        label=label,
        wait_for_pre_tx=wait_for_pre_tx,
        post_approve_delay_range=post_approve_delay_range,
        failed_launch_min_out_retry=current.status.strip().upper() == "GRADUATION_FAILED",
    )
    if ok:
        return True

    last_error = str(getattr(state, "last_error", "") or "")
    if "0xa1fa02b3" not in last_error:
        return False
    if refresh_launchpad is None:
        return False
    refreshed = refresh_launchpad()
    if refreshed is None:
        return False
    current = refreshed
    if current.pool_address:
        return _sell_via_doma_route(f"launchpad rejected sell and token now has pool={current.pool_address}")
    return False


def _execute_ui_route_with_fallback(
    cfg: BotConfig,
    logger: logging.Logger,
    state: BotState,
    doma_api: DomaApiClient,
    exec_client: EvmExecutionClient,
    token_in: Token,
    token_out: Token,
    display_in_symbol: str,
    display_out_symbol: str,
    trade_amount_expr: str,
    eth_price: Decimal,
    label: str,
    is_eth_source: bool = False,
    unwrap_to_native: bool = False,
    wait_for_pre_tx: bool = False,
) -> Tuple[bool, str, Decimal]:
    token_in_price_usd = pick_token_usd_price(token_in, eth_price)
    if display_in_symbol == "USDC.E" and token_in_price_usd <= 0:
        token_in_price_usd = Decimal("1")

    try:
        if is_eth_source:
            balance_dec = exec_client.get_native_balance()
        else:
            balance_dec = exec_client.get_erc20_balance(token_in.address, token_in.decimals)
        _amount_in_dec, initial_trade_usd = resolve_trade_amount(trade_amount_expr, balance_dec, token_in_price_usd)
    except Exception as exc:
        logger.warning("[%s] Invalid route swap amount '%s': %s", label, trade_amount_expr, exc)
        return False, trade_amount_expr, Decimal("0")

    candidate_exprs: List[str] = []
    candidate_usd_map: Dict[str, Decimal] = {}
    seen: set[str] = set()

    def _push_expr(expr: str, usd_value: Optional[Decimal] = None) -> None:
        expr_norm = str(expr).strip()
        if not expr_norm or expr_norm in seen:
            return
        seen.add(expr_norm)
        candidate_exprs.append(expr_norm)
        if usd_value is not None:
            candidate_usd_map[expr_norm] = usd_value

    _push_expr(trade_amount_expr, initial_trade_usd)
    if initial_trade_usd >= (MIN_EXECUTABLE_TRADE_USD * Decimal("2")):
        for ratio in (Decimal("0.5"), Decimal("0.25")):
            fallback_usd = (initial_trade_usd * ratio)
            if fallback_usd < MIN_EXECUTABLE_TRADE_USD:
                continue
            _push_expr(f"${_format_decimal_plain(fallback_usd)}", fallback_usd)

    for idx, candidate_expr in enumerate(candidate_exprs):
        if idx > 0:
            logger.info(
                "[%s] Retrying route swap with fallback amount=%s",
                label,
                candidate_expr,
            )
        ok = _execute_trade_via_doma_ui_route(
            cfg=cfg,
            logger=logger,
            state=state,
            doma_api=doma_api,
            exec_client=exec_client,
            token_in=token_in,
            token_out=token_out,
            display_in_symbol=display_in_symbol,
            display_out_symbol=display_out_symbol,
            trade_amount_expr=candidate_expr,
            eth_price=eth_price,
            label=label,
            is_eth_source=is_eth_source,
            unwrap_to_native=unwrap_to_native,
            wait_for_pre_tx=wait_for_pre_tx,
        )
        if ok:
            return True, candidate_expr, candidate_usd_map.get(candidate_expr, initial_trade_usd)

    return False, trade_amount_expr, initial_trade_usd

    if refresh_launchpad:
        try:
            refreshed = refresh_launchpad()
            if refreshed:
                current = refreshed
        except Exception as exc:
            logger.warning("[%s] Launchpad metadata refresh before route fallback failed: %s", label, exc)

    return _sell_via_doma_route(last_error or "0xa1fa02b3")


def _execute_ui_route_with_fallback(
    cfg: BotConfig,
    logger: logging.Logger,
    state: BotState,
    doma_api: DomaApiClient,
    exec_client: EvmExecutionClient,
    token_in: Token,
    token_out: Token,
    display_in_symbol: str,
    display_out_symbol: str,
    trade_amount_expr: str,
    eth_price: Decimal,
    label: str,
    is_eth_source: bool = False,
    unwrap_to_native: bool = False,
    wait_for_pre_tx: bool = False,
) -> Tuple[bool, str, Decimal]:
    token_in_price_usd = pick_token_usd_price(token_in, eth_price)
    if display_in_symbol == "USDC.E" and token_in_price_usd <= 0:
        token_in_price_usd = Decimal("1")

    try:
        if is_eth_source:
            balance_dec = exec_client.get_native_balance()
        else:
            balance_dec = exec_client.get_erc20_balance(token_in.address, token_in.decimals)
        _amount_in_dec, initial_trade_usd = resolve_trade_amount(trade_amount_expr, balance_dec, token_in_price_usd)
    except Exception as exc:
        logger.warning("[%s] Invalid route swap amount '%s': %s", label, trade_amount_expr, exc)
        return False, trade_amount_expr, Decimal("0")

    candidate_exprs: List[str] = []
    candidate_usd_map: Dict[str, Decimal] = {}
    seen: set[str] = set()

    def _push_expr(expr: str, usd_value: Optional[Decimal] = None) -> None:
        expr_norm = str(expr).strip()
        if not expr_norm or expr_norm in seen:
            return
        seen.add(expr_norm)
        candidate_exprs.append(expr_norm)
        if usd_value is not None:
            candidate_usd_map[expr_norm] = usd_value

    _push_expr(trade_amount_expr, initial_trade_usd)
    if initial_trade_usd >= (MIN_EXECUTABLE_TRADE_USD * Decimal("2")):
        for ratio in (Decimal("0.5"), Decimal("0.25")):
            fallback_usd = (initial_trade_usd * ratio)
            if fallback_usd < MIN_EXECUTABLE_TRADE_USD:
                continue
            _push_expr(f"${_format_decimal_plain(fallback_usd)}", fallback_usd)

    for idx, candidate_expr in enumerate(candidate_exprs):
        if idx > 0:
            logger.info(
                "[%s] Retrying route swap with fallback amount=%s",
                label,
                candidate_expr,
            )
        ok = _execute_trade_via_doma_ui_route(
            cfg=cfg,
            logger=logger,
            state=state,
            doma_api=doma_api,
            exec_client=exec_client,
            token_in=token_in,
            token_out=token_out,
            display_in_symbol=display_in_symbol,
            display_out_symbol=display_out_symbol,
            trade_amount_expr=candidate_expr,
            eth_price=eth_price,
            label=label,
            is_eth_source=is_eth_source,
            unwrap_to_native=unwrap_to_native,
            wait_for_pre_tx=wait_for_pre_tx,
        )
        if ok:
            return True, candidate_expr, candidate_usd_map.get(candidate_expr, initial_trade_usd)

    return False, trade_amount_expr, initial_trade_usd


def get_domain_swap_menu_input(state: BotState) -> Optional[Tuple[str, str, str, str, str, str]]:
    _ = state
    print("\nDomain token swap (Doma):")
    print("1) Source ETH")
    print("2) Source USDC.E")
    print("3) Back")
    src_raw = input("Select [1-3]: ").strip()
    if src_raw == "3":
        return None
    if src_raw not in {"1", "2"}:
        raise ValueError("Invalid source selection")
    src_symbol = "ETH" if src_raw == "1" else "USDC.E"

    domain_raw = input("Domain token (e.g. regionalcrypto.com or full URL): ").strip()
    dst_symbol = _normalize_domain_token_symbol(domain_raw)

    print("\nAmount mode:")
    print(f"1) Number ({src_symbol})")
    print("2) Percent (%)")
    mode_raw = input("Select [1-2]: ").strip()
    if mode_raw not in {"1", "2"}:
        raise ValueError("Invalid amount mode selection")
    amount_mode = "number" if mode_raw == "1" else "percent"

    min_raw = input("Minimum: ").strip()
    max_raw = input("Maximum: ").strip()
    _ = _parse_decimal_input(min_raw)
    _ = _parse_decimal_input(max_raw)
    delay_min_raw = input("Minimum delay between swaps sec [4]: ").strip() or "4"
    delay_max_raw = input("Maximum delay between swaps sec [10]: ").strip() or "10"
    delay_min = _parse_decimal_input(delay_min_raw)
    delay_max = _parse_decimal_input(delay_max_raw)
    if delay_min < 0 or delay_max < 0:
        raise ValueError("Delay between swaps must be >= 0")
    if delay_min > delay_max:
        raise ValueError("Minimum delay between swaps cannot be greater than maximum")
    return src_symbol, dst_symbol, amount_mode, f"{min_raw}|{max_raw}", delay_min_raw, delay_max_raw


def _normalize_domain_swap_asset(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        raise ValueError("Empty token input")
    upper = value.upper()
    if upper in {"ETH", "WETH", "USDC", "USDC.E", "USDC.E"}:
        return "USDC.E" if upper == "USDC" else upper
    return _normalize_domain_token_symbol(value)


def get_domain_single_swap_menu_input(state: BotState) -> Optional[Tuple[str, str, str, str, str, str]]:
    _ = state
    print("\nOne domain token swap (Doma):")
    print("Examples: ETH, WETH, USDC.E, warriors.xyz, full Doma URL")
    src_symbol = _normalize_domain_swap_asset(input("From token: ").strip())
    dst_symbol = _normalize_domain_swap_asset(input("To token: ").strip())
    if src_symbol == dst_symbol:
        raise ValueError("From token and To token cannot be the same")

    print("\nAmount mode:")
    print(f"1) Number ({src_symbol})")
    print("2) Percent (%)")
    mode_raw = input("Select [1-2]: ").strip()
    if mode_raw not in {"1", "2"}:
        raise ValueError("Invalid amount mode selection")
    amount_mode = "number" if mode_raw == "1" else "percent"

    if amount_mode == "percent":
        percent_raw = input("Percent: ").strip()
        _ = _parse_decimal_input(percent_raw)
        return src_symbol, dst_symbol, amount_mode, percent_raw

    min_raw = input("Minimum: ").strip()
    max_raw = input("Maximum: ").strip()
    _ = _parse_decimal_input(min_raw)
    _ = _parse_decimal_input(max_raw)
    delay_min_raw = input("Minimum delay after approve sec [4]: ").strip() or "4"
    delay_max_raw = input("Maximum delay after approve sec [10]: ").strip() or "10"
    delay_min = _parse_decimal_input(delay_min_raw)
    delay_max = _parse_decimal_input(delay_max_raw)
    if delay_min < 0 or delay_max < 0:
        raise ValueError("Delay after approve must be >= 0")
    if delay_min > delay_max:
        raise ValueError("Minimum delay after approve cannot be greater than maximum")
    return src_symbol, dst_symbol, amount_mode, f"{min_raw}|{max_raw}", delay_min_raw, delay_max_raw


def get_domain_mode_menu_choice() -> Optional[str]:
    print("\nDomain token mode:")
    print("1) Single round-trip")
    print("2) Domain quest volume")
    print("3) One swap")
    print("4) Back")
    raw = input("Select [1-4]: ").strip()
    if raw == "4":
        return None
    if raw == "1":
        return "single"
    if raw == "2":
        return "quest"
    if raw == "3":
        return "one_swap"
    raise ValueError("Invalid domain mode selection")


def get_domain_quest_token_choice() -> Optional[str]:
    print("\nQuest token:")
    for idx, domain_name in enumerate(DOMAIN_QUEST_TOKENS, start=1):
        print(f"{idx}) {domain_name}")
    back_index = len(DOMAIN_QUEST_TOKENS) + 1
    print(f"{back_index}) Back")
    raw = input(f"Select [1-{back_index}]: ").strip()
    if raw == str(back_index):
        return None
    try:
        picked_idx = int(raw)
    except ValueError as exc:
        raise ValueError("Invalid quest token selection") from exc
    if not 1 <= picked_idx <= len(DOMAIN_QUEST_TOKENS):
        raise ValueError("Invalid quest token selection")
    return DOMAIN_QUEST_TOKENS[picked_idx - 1]


def get_domain_quest_menu_input(state: BotState) -> Optional[Tuple[str, str, str, str, str, str, str]]:
    _ = state
    domain_name = get_domain_quest_token_choice()
    if not domain_name:
        return None
    required_buy_usd = DOMAIN_QUEST_MIN_BUY_USD.get(domain_name.lower())
    if required_buy_usd is not None:
        print(f"\n{domain_name} quest buy:")
        print(f"Target: buy {domain_name} with USDC.E (no return swap)")
        default_target = _format_decimal_plain(required_buy_usd)
        target_raw = input(f"Buy amount in USDC.E [{default_target}]: ").strip() or default_target
        target = _parse_decimal_input(target_raw)
        if target < required_buy_usd:
            raise ValueError(f"Buy amount must be at least {default_target} USDC.E for {domain_name}")
        return domain_name, "100", "100", target_raw, "DOMAIN_TOKEN", "single_swap", "ignore"
    print(f"\n{domain_name} quest volume:")
    print(f"Target: USDC.E <-> {domain_name}")
    print("\nPartial return percent range:")
    min_raw = input("Minimum percent [95]: ").strip() or "95"
    max_raw = input("Maximum percent [99]: ").strip() or "99"
    _ = _parse_decimal_input(min_raw)
    _ = _parse_decimal_input(max_raw)

    default_target = "25"
    target_raw = input(f"Target volume in USDC.E [{default_target}]: ").strip() or default_target
    _ = _parse_decimal_input(target_raw)
    print("\nVolume mode:")
    print("1) Minimum single swap")
    print("2) Total volume only")
    default_volume_mode = "2"
    volume_mode_raw = input(f"Select [1-2, default {default_volume_mode}]: ").strip() or default_volume_mode
    if volume_mode_raw not in {"1", "2"}:
        raise ValueError("Invalid volume mode selection")
    volume_mode = "single_swap" if volume_mode_raw == "1" else "volume_only"
    print("\nExisting volume check:")
    print("1) Check current UTC week and skip completed wallets")
    print("2) Ignore history and run selected wallets")
    default_history_mode = "1"
    history_mode_raw = input(f"Select [1-2, default {default_history_mode}]: ").strip() or default_history_mode
    if history_mode_raw not in {"1", "2"}:
        raise ValueError("Invalid existing volume check selection")
    history_mode = "check_skip" if history_mode_raw == "1" else "ignore"
    print("\nFinal asset:")
    print("1) USDC.E")
    print("2) ETH")
    final_raw = input("Select [1-2, default 1]: ").strip() or "1"
    if final_raw not in {"1", "2"}:
        raise ValueError("Invalid final asset selection")
    final_asset = "USDC.E" if final_raw == "1" else "ETH"
    return domain_name, min_raw, max_raw, target_raw, final_asset, volume_mode, history_mode


def _run_domain_min_buy_quest(
    cfg: BotConfig,
    logger: logging.Logger,
    state: BotState,
    *,
    domain_name: str,
    buy_amount_usdc: Decimal,
    launchpad_info: LaunchpadTokenInfo,
    quote_token: Token,
    weth_token: Token,
    eth_price: Decimal,
    wallet_key_records: List[Tuple[int, str, str]],
    wallet_start_offset: int,
    total_loaded_wallets: int,
) -> None:
    mode_tag = f"QUEST {domain_name}"
    domain_token = _token_from_launchpad_price(launchpad_info, eth_price)
    success_wallets = 0
    failed_wallets = 0
    skipped_wallets = 0
    failed_entries: List[str] = []

    logger.info(
        "[%s] buy-only mode started | wallets=%s | start_wallet=%s | amount=%s USDC.E | route=%s | final=%s",
        mode_tag,
        len(wallet_key_records),
        wallet_start_offset + 1,
        _format_decimal_plain(buy_amount_usdc),
        "pool" if launchpad_info.pool_address else "bonding-launchpad",
        domain_token.symbol,
    )

    for position, (line_idx, wallet, private_key) in enumerate(wallet_key_records, start=1):
        wallet_number = line_idx + 1
        logger.info(
            "[%s] wallet %s",
            mode_tag,
            _wallet_record_progress_label(position - 1, len(wallet_key_records), line_idx, total_loaded_wallets, wallet),
        )
        proxies, skip_wallet = _proxy_for_line(cfg, line_idx, logger, "QUEST_BUY")
        if skip_wallet:
            skipped_wallets += 1
            failed_entries.append(f"wallet#{wallet_number} | skipped: proxy missing")
            continue

        try:
            doma_api = DomaApiClient(
                cfg.doma_api_url,
                api_keys=[cfg.doma_api_key, *cfg.doma_api_keys, *cfg.file_api_keys],
                proxies=proxies,
            )
            current = doma_api.fetch_fractional_token_by_name(domain_name)
            if current is None:
                raise RuntimeError(f"{domain_name} token metadata not found")
            if not current.pool_address and not _is_currently_bonding_token(current, quote_token):
                raise RuntimeError(f"{domain_name} has neither an active bonding launchpad nor a pool route")

            exec_client = _build_exec_client_with_rpc_fallback(
                cfg,
                logger,
                wallet,
                private_key,
                proxies=proxies,
                log_prefix=f"[{mode_tag}]",
            )
            can_fund, current_usdc, spendable_eth_usd, total_spendable_usd = _can_fully_fund_usdce_topup(
                exec_client,
                quote_token,
                eth_price,
                buy_amount_usdc,
            )
            if not can_fund:
                skipped_wallets += 1
                reason = (
                    "insufficient combined balance before swaps: "
                    f"USDC.E={_format_decimal_plain(current_usdc)}, "
                    f"spendable_ETH=${_format_decimal_plain(spendable_eth_usd)}, "
                    f"total=${_format_decimal_plain(total_spendable_usd)}, "
                    f"required={_format_decimal_plain(buy_amount_usdc)} USDC.E plus conversion buffer"
                )
                failed_entries.append(f"wallet#{wallet_number} | skipped: {reason}")
                logger.warning("[%s] wallet=%s skipped | %s", mode_tag, wallet, reason)
                continue

            topup_ok, topup_reason, available_usdc, _ = _top_up_usdce_from_eth_for_cheap_buy(
                cfg,
                logger,
                state,
                doma_api,
                exec_client,
                quote_token,
                weth_token,
                wallet,
                eth_price,
                buy_amount_usdc,
                log_prefix=mode_tag,
            )
            if not topup_ok:
                raise RuntimeError(
                    f"USDC.E top-up failed: {topup_reason} (available={_format_decimal_plain(available_usdc)})"
                )

            current = doma_api.fetch_fractional_token_by_name(domain_name) or current
            current_token = _token_from_launchpad_price(current, eth_price)
            if current.pool_address:
                ok = _execute_trade_via_doma_ui_route(
                    cfg=cfg,
                    logger=logger,
                    state=state,
                    doma_api=doma_api,
                    exec_client=exec_client,
                    token_in=quote_token,
                    token_out=current_token,
                    display_in_symbol="USDC.E",
                    display_out_symbol=current_token.symbol,
                    trade_amount_expr=f"${_format_decimal_plain(buy_amount_usdc)}",
                    eth_price=eth_price,
                    label=f"{mode_tag} {wallet} USDC.E>{current_token.symbol} BUY_ONLY",
                    wait_for_pre_tx=True,
                )
            else:
                ok = _execute_launchpad_buy(
                    cfg=cfg,
                    logger=logger,
                    state=state,
                    exec_client=exec_client,
                    launchpad=current,
                    quote_token=quote_token,
                    trade_amount_expr=f"${_format_decimal_plain(buy_amount_usdc)}",
                    eth_price=eth_price,
                    label=f"{mode_tag} {wallet} USDC.E>{current_token.symbol} BUY_ONLY",
                    wait_for_pre_tx=True,
                )
            tx_hash = state.last_tx_hash if ok else ""
            if not ok or not tx_hash or not _wait_tx_receipt(exec_client, tx_hash, timeout_sec=180):
                raise RuntimeError("buy transaction failed or timed out")
            success_wallets += 1
            logger.info(
                "[%s] wallet=%s quest buy sent | amount=%s USDC.E | final_asset=%s | tx=%s",
                mode_tag,
                wallet,
                _format_decimal_plain(buy_amount_usdc),
                current_token.symbol,
                tx_hash,
            )
        except Exception as exc:
            failed_wallets += 1
            failed_entries.append(f"wallet#{wallet_number} | {exc}")
            logger.warning("[%s] wallet=%s failed: %s", mode_tag, wallet, exc)

        if position < len(wallet_key_records):
            delay_sec = _random_swap_delay_sec()
            logger.info("[%s] delay before next wallet: %.2f sec", mode_tag, delay_sec)
            time.sleep(delay_sec)

    _print_mode_summary(
        "QUEST_BUY",
        total=len(wallet_key_records),
        success=success_wallets,
        failed=failed_wallets,
        skipped=skipped_wallets,
        failed_wallets=failed_entries,
    )


def run_domain_quest_volume_once(
    cfg: BotConfig,
    logger: logging.Logger,
    state: BotState,
    preset: Optional[Tuple[str, ...]] = None,
) -> None:
    success_wallets = 0
    failed_wallets = 0
    skipped_wallets = 0
    failed_wallet_labels: List[str] = []
    current_wallet_number = 0

    def _fail_wallet() -> None:
        nonlocal failed_wallets
        failed_wallets += 1
        wallet_label = f"wallet#{current_wallet_number}" if current_wallet_number > 0 else "unknown wallet"
        if wallet_label not in failed_wallet_labels:
            failed_wallet_labels.append(wallet_label)

    picked = preset or get_domain_quest_menu_input(state)
    if not picked:
        logger.info("Domain quest volume canceled by user.")
        return
    if len(picked) == 5:
        domain_name, min_raw, max_raw, target_raw, final_asset = picked
        volume_mode = "volume_only"
        history_mode = "check_skip"
    elif len(picked) == 6:
        domain_name, min_raw, max_raw, target_raw, final_asset, volume_mode = picked
        history_mode = "check_skip"
    else:
        domain_name, min_raw, max_raw, target_raw, final_asset, volume_mode, history_mode = picked
    if volume_mode not in {"single_swap", "volume_only"}:
        raise ValueError("Invalid quest volume mode")
    if history_mode not in {"check_skip", "ignore"}:
        raise ValueError("Invalid quest history mode")
    required_quest_buy_usd = DOMAIN_QUEST_MIN_BUY_USD.get(domain_name.lower(), Decimal("0"))
    require_min_single_swap = volume_mode == "single_swap" or required_quest_buy_usd > 0
    check_existing_volume = history_mode == "check_skip"
    target_volume = _parse_decimal_input(target_raw)
    quest_target_volume = min(target_volume, DOMAIN_QUEST_COMPLETION_THRESHOLD_USD)
    execution_buffer_volume = min(Decimal("0.20"), max(Decimal("0.10"), quest_target_volume * Decimal("0.10")))
    execution_target_volume = quest_target_volume + execution_buffer_volume
    min_single_swap_usd = (
        required_quest_buy_usd
        if required_quest_buy_usd > 0
        else quest_target_volume if require_min_single_swap else Decimal("0")
    )
    partial_min = _parse_decimal_input(min_raw)
    partial_max = _parse_decimal_input(max_raw)
    if partial_min <= 0 or partial_max <= 0:
        raise ValueError("Partial return percent must be > 0")
    if partial_max > 100:
        raise ValueError("Partial return percent cannot be > 100")

    mode_label = f"QUEST {domain_name}"

    def _quest_log(message: str) -> str:
        return f"[{mode_label}] {message}"

    volume_since = _current_week_start_utc()

    wallet_key_records = _build_wallet_key_records(cfg, logger, "QUEST")
    if not wallet_key_records:
        raise ValueError(
            f"No wallet/private-key pairs available for {domain_name} quest "
            "(fill wallets.txt + keys.txt line-by-line or set valid PRIVATE_KEY in .env)"
        )
    wallet_key_records, wallet_start_offset, total_loaded_wallets = _apply_wallet_start_selection(wallet_key_records)

    metadata_proxies: Optional[Dict[str, str]] = None
    for metadata_line_idx, _, _ in wallet_key_records:
        candidate_proxies, skip_metadata_proxy = _proxy_for_line(cfg, metadata_line_idx, None, "QUEST_METADATA")
        if not skip_metadata_proxy:
            metadata_proxies = candidate_proxies
            break
    if metadata_proxies:
        logger.info(_quest_log("metadata API will use proxy from first available wallet after start"))
    else:
        logger.info(_quest_log("metadata API will use direct connection"))

    shared_doma_api = DomaApiClient(
        cfg.doma_api_url,
        api_keys=[cfg.doma_api_key, *cfg.doma_api_keys, *cfg.file_api_keys],
        proxies=metadata_proxies,
    )
    launchpad_info = shared_doma_api.fetch_fractional_token_by_name(domain_name)
    if not launchpad_info:
        raise RuntimeError(f"{domain_name} launchpad token not found")
    quest_symbol = canonical_symbol(launchpad_info.symbol or launchpad_info.name)
    quote_token = Token(
        address=launchpad_info.quote_token_address,
        symbol="USDC.E",
        decimals=6,
        derived_eth=Decimal("0"),
    )
    weth_token = _token_from_config_override(cfg, "WETH", 18)
    eth_price = _fetch_eth_price_via_doma_quote(cfg, shared_doma_api, quote_token)
    if required_quest_buy_usd > 0:
        _run_domain_min_buy_quest(
            cfg,
            logger,
            state,
            domain_name=domain_name,
            buy_amount_usdc=max(target_volume, required_quest_buy_usd),
            launchpad_info=launchpad_info,
            quote_token=quote_token,
            weth_token=weth_token,
            eth_price=eth_price,
            wallet_key_records=wallet_key_records,
            wallet_start_offset=wallet_start_offset,
            total_loaded_wallets=total_loaded_wallets,
        )
        return
    if not launchpad_info.pool_address:
        raise RuntimeError(f"{domain_name} pool route not found")
    rides_token = _token_from_launchpad_price(launchpad_info, eth_price)
    rides_pool_addresses = [launchpad_info.pool_address]

    logger.info(
        _quest_log("mode started | source=AUTO pair=USDC.E<->%s wallets=%s | start_wallet=%s | current_week_since=%s | history_mode=%s | target=%s USDC.E | quest_target=%s USDC.E | volume_mode=%s | min_single_swap=%s USDC.E | execution_target=%s USDC.E | pattern=auto-100%%->%s-%s%% | final=%s"),
        domain_name,
        len(wallet_key_records),
        wallet_start_offset + 1,
        volume_since.isoformat(),
        history_mode,
        _format_decimal_plain(target_volume),
        _format_decimal_plain(quest_target_volume),
        volume_mode,
        _format_decimal_plain(min_single_swap_usd),
        _format_decimal_plain(execution_target_volume),
        min_raw,
        max_raw,
        final_asset,
    )
    logger.info(
        _quest_log("metadata loaded | symbol=%s token=%s pool=%s token_price=%s eth_price=%s"),
        quest_symbol,
        rides_token.address,
        launchpad_info.pool_address,
        _format_decimal_plain(launchpad_info.price_usd),
        _format_decimal_plain(eth_price),
    )

    def _sleep_between_swaps() -> None:
        delay_sec = _random_swap_delay_sec()
        logger.info(_quest_log("delay between swaps: %.2f sec"), delay_sec)
        time.sleep(delay_sec)

    def _best_effort_failed_rides_cleanup(
        *,
        wallet: str,
        exec_client: EvmExecutionClient,
        doma_api: DomaApiClient,
        quote_token: Token,
        rides_token: Token,
        weth_token: Token,
        eth_price: Decimal,
        final_asset: str,
        protected_rides_balance: Decimal = Decimal("0"),
    ) -> None:
        logger.info(
            _quest_log("wallet=%s failed before target reached | quest volume not finalized, running cleanup to %s"),
            wallet,
            final_asset,
        )
        try:
            rides_balance = exec_client.get_erc20_balance(rides_token.address, rides_token.decimals)
            sellable_rides_balance = max(Decimal("0"), rides_balance - protected_rides_balance)
            rides_price_usd = pick_token_usd_price(rides_token, eth_price)
            if rides_price_usd <= 0:
                rides_price_usd = Decimal("0")
            rides_usd = sellable_rides_balance * rides_price_usd
            if sellable_rides_balance > 0 and rides_usd >= MIN_EXECUTABLE_TRADE_USD:
                logger.info(
                    _quest_log("wallet=%s failed-run cleanup | %s->USDC.E amount=%s %s | protected_existing=%s"),
                    wallet,
                    rides_token.symbol,
                    _format_decimal_plain(sellable_rides_balance),
                    rides_token.symbol,
                    _format_decimal_plain(protected_rides_balance),
                )
                ok_sell = _execute_trade_via_doma_ui_route(
                    cfg=cfg,
                    logger=logger,
                    state=state,
                    doma_api=doma_api,
                    exec_client=exec_client,
                    token_in=rides_token,
                        token_out=quote_token,
                        display_in_symbol=rides_token.symbol,
                        display_out_symbol="USDC.E",
                        trade_amount_expr=_format_decimal_plain(sellable_rides_balance),
                        eth_price=eth_price,
                        label=f"{mode_label} {wallet} {rides_token.symbol}>USDC.E FAIL-CLEANUP",
                        wait_for_pre_tx=True,
                        is_eth_source=False,
                        unwrap_to_native=False,
                    )
                if ok_sell and state.last_tx_hash and _wait_tx_receipt(exec_client, state.last_tx_hash, timeout_sec=180):
                    _sleep_between_swaps()
                else:
                    logger.warning(_quest_log("wallet=%s failed-run cleanup sell did not confirm"), wallet)
            elif sellable_rides_balance > 0:
                logger.info(
                    _quest_log("wallet=%s failed-run cleanup skipped | %s dust below $0.10 (%s)"),
                    wallet,
                    rides_token.symbol,
                    _format_decimal_plain(rides_usd),
                )
            elif rides_balance > 0:
                logger.info(
                    _quest_log("wallet=%s failed-run cleanup skipped | existing %s balance protected=%s"),
                    wallet,
                    rides_token.symbol,
                    _format_decimal_plain(protected_rides_balance),
                )

            if final_asset == "ETH":
                usdc_balance = exec_client.get_erc20_balance(quote_token.address, quote_token.decimals)
                if usdc_balance >= MIN_EXECUTABLE_TRADE_USD:
                    logger.info(
                        _quest_log("wallet=%s failed-run cleanup | USDC.E->ETH amount=100%% of USDC.E"),
                        wallet,
                    )
                    ok_exit = _execute_trade_via_doma_ui_route(
                        cfg=cfg,
                        logger=logger,
                        state=state,
                        doma_api=doma_api,
                        exec_client=exec_client,
                        token_in=quote_token,
                        token_out=weth_token,
                        display_in_symbol="USDC.E",
                        display_out_symbol="ETH",
                        trade_amount_expr="100%",
                        eth_price=eth_price,
                        label=f"{mode_label} {wallet} USDC.E>ETH FAIL-CLEANUP",
                        is_eth_source=False,
                        unwrap_to_native=True,
                        wait_for_pre_tx=True,
                    )
                    if ok_exit and state.last_tx_hash and _wait_tx_receipt(exec_client, state.last_tx_hash, timeout_sec=180):
                        _cleanup_weth_balance(
                            logger=logger,
                            exec_client=exec_client,
                            weth_token=weth_token,
                            label=f"{mode_label} {wallet} FAIL-CLEANUP",
                            reason="failed-run cleanup",
                            wait_for_receipt=True,
                        )
                    else:
                        logger.warning(_quest_log("wallet=%s failed-run cleanup exit to ETH did not confirm"), wallet)
                elif usdc_balance > 0:
                    logger.info(
                        _quest_log("wallet=%s failed-run cleanup skipped | USDC.E dust below $0.10 (%s)"),
                        wallet,
                        _format_decimal_plain(usdc_balance),
                    )
        except Exception as cleanup_exc:
            logger.warning(_quest_log("wallet=%s failed-run cleanup error: %s"), wallet, cleanup_exc)

    def _fetch_recent_quest_volume(doma_api: DomaApiClient, wallet: str) -> Decimal:
        launchpad_volume = doma_api.fetch_wallet_fractional_token_volume_usd(
            wallet_address=wallet,
            fractional_token_id=launchpad_info.token_id,
            since=volume_since,
        )
        pool_volume = doma_api.fetch_wallet_pool_volume_usd(
            wallet_address=wallet,
            pool_address=launchpad_info.pool_address,
            pool_addresses=rides_pool_addresses,
            tracked_token_symbol=rides_token.symbol,
            since=volume_since,
        )
        return launchpad_volume + pool_volume

    for idx, (line_idx, wallet, private_key) in enumerate(wallet_key_records):
        current_wallet_number = line_idx + 1
        proxies, skip_wallet = _proxy_for_line(cfg, line_idx, logger, "QUEST")
        if skip_wallet:
            skipped_wallets += 1
            continue
        logger.info(_quest_log("wallet %s"), _wallet_record_progress_label(idx, len(wallet_key_records), line_idx, total_loaded_wallets, wallet))
        before_points_snapshot: Optional[PointsSnapshot] = None
        try:
            try:
                doma_api = DomaApiClient(
                    cfg.doma_api_url,
                    api_key=cfg.doma_api_key,
                    api_keys=cfg.doma_api_keys,
                    proxies=proxies,
                )
            except Exception as exc:
                _fail_wallet()
                logger.warning(_quest_log("wallet=%s init failed: %s"), wallet, exc)
                continue

            exec_client = _build_exec_client_with_rpc_fallback(
                cfg=cfg,
                logger=logger,
                wallet=wallet,
                private_key=private_key,
                proxies=proxies,
                log_prefix=_quest_log(""),
            )
            protected_rides_balance = exec_client.get_erc20_balance(rides_token.address, rides_token.decimals)
            if protected_rides_balance > 0:
                logger.info(
                    _quest_log("wallet=%s existing %s balance protected from quest sells: %s"),
                    wallet,
                    rides_token.symbol,
                    _format_decimal_plain(protected_rides_balance),
                )

            if check_existing_volume:
                try:
                    accumulated_volume = _fetch_recent_quest_volume(doma_api, wallet)
                except Exception as exc:
                    logger.warning(
                        _quest_log("wallet=%s recent %s volume fetch failed, assuming 0: %s"),
                        wallet,
                        domain_name,
                        exc,
                    )
                    accumulated_volume = Decimal("0")
            else:
                accumulated_volume = Decimal("0")
                logger.info(
                    _quest_log("wallet=%s existing %s volume check disabled | starting from 0/%s"),
                    wallet,
                    domain_name,
                    _format_decimal_plain(target_volume),
                )
            rides_completion_threshold = quest_target_volume
            if check_existing_volume and accumulated_volume >= rides_completion_threshold:
                logger.info(
                    _quest_log("wallet=%s already has %s volume for current UTC week since %s = %s/%s | completion_threshold=%s | skipping wallet"),
                    wallet,
                    domain_name,
                    volume_since.isoformat(),
                    _format_decimal_plain(accumulated_volume),
                    _format_decimal_plain(target_volume),
                    _format_decimal_plain(rides_completion_threshold),
                )
                skipped_wallets += 1
                continue
            if check_existing_volume and accumulated_volume > 0:
                logger.info(
                    _quest_log("wallet=%s current UTC week %s volume since %s = %s/%s | remaining_to_target=%s | planned_topup_to=%s"),
                    wallet,
                    domain_name,
                    volume_since.isoformat(),
                    _format_decimal_plain(accumulated_volume),
                    _format_decimal_plain(target_volume),
                    _format_decimal_plain(target_volume - accumulated_volume),
                    _format_decimal_plain(execution_target_volume),
                )
            elif check_existing_volume:
                logger.info(
                    _quest_log("wallet=%s current UTC week %s volume since %s = 0/%s | planned_topup_to=%s"),
                    wallet,
                    domain_name,
                    volume_since.isoformat(),
                    _format_decimal_plain(target_volume),
                    _format_decimal_plain(execution_target_volume),
                )
            cycle = 0
            wallet_failed = False
            wallet_trades_started = False
            min_single_swap_done = True if not require_min_single_swap else accumulated_volume >= quest_target_volume

            # The buffer sizes the next trade, but it must not force extra swaps
            # after the actual quest target has already been reached.
            while accumulated_volume < quest_target_volume:
                cycle += 1
                logger.info(
                    _quest_log("wallet=%s cycle=%s | progress=%s/%s"),
                    wallet,
                    cycle,
                    _format_decimal_plain(accumulated_volume),
                    _format_decimal_plain(quest_target_volume),
                )

                full_balance_usdc = exec_client.get_erc20_balance(quote_token.address, quote_token.decimals)
                full_balance_rides = exec_client.get_erc20_balance(rides_token.address, rides_token.decimals)
                rides_price_usd = pick_token_usd_price(rides_token, eth_price, launchpad_info.price_usd)
                has_usable_usdc = full_balance_usdc >= MIN_EXECUTABLE_TRADE_USD
                reserve_eth = _native_gas_reserve_eth(eth_price)
                spendable_eth = exec_client.get_native_balance() - reserve_eth
                spendable_eth = spendable_eth if spendable_eth > 0 else Decimal("0")
                spendable_eth_usd = spendable_eth * eth_price

                if require_min_single_swap and not min_single_swap_done:
                    required_usdc = max(
                        min_single_swap_usd,
                        execution_target_volume - accumulated_volume,
                    )
                    if full_balance_usdc < required_usdc:
                        missing_usdc = required_usdc - full_balance_usdc
                        bootstrap_input_usd = (missing_usdc * Decimal("1.01")).quantize(Decimal("0.000001"))
                        combined_funding_usd = full_balance_usdc + (spendable_eth_usd / Decimal("1.01"))
                        if combined_funding_usd < required_usdc or bootstrap_input_usd > spendable_eth_usd:
                            logger.warning(
                                _quest_log(
                                    "wallet=%s skipped before swaps | insufficient combined Doma balance for single swap | "
                                    "need=%s USDC.E | USDC.E=%s | spendable_ETH~=%s USDC.E | combined~=%s USDC.E"
                                ),
                                wallet,
                                _format_decimal_plain(required_usdc),
                                _format_decimal_plain(full_balance_usdc),
                                _format_decimal_plain(spendable_eth_usd),
                                _format_decimal_plain(full_balance_usdc + spendable_eth_usd),
                            )
                            wallet_failed = True
                            break
                        bootstrap_eth = bootstrap_input_usd / eth_price
                        logger.info(
                            _quest_log(
                                "wallet=%s combined funding | USDC.E=%s + ETH topup~=%s USDC.E | "
                                "target_before_buy=%s USDC.E"
                            ),
                            wallet,
                            _format_decimal_plain(full_balance_usdc),
                            _format_decimal_plain(bootstrap_input_usd),
                            _format_decimal_plain(required_usdc),
                        )
                        ok_bootstrap = _execute_trade_via_doma_ui_route(
                            cfg=cfg,
                            logger=logger,
                            state=state,
                            doma_api=doma_api,
                            exec_client=exec_client,
                            token_in=weth_token,
                            token_out=quote_token,
                            display_in_symbol="ETH",
                            display_out_symbol="USDC.E",
                            trade_amount_expr=_format_decimal_plain(bootstrap_eth),
                            eth_price=eth_price,
                            label=f"{mode_label} {wallet} ETH>USDC.E COMBINED-TOPUP",
                            is_eth_source=True,
                            unwrap_to_native=False,
                            wait_for_pre_tx=True,
                        )
                        if not ok_bootstrap or not state.last_tx_hash or not _wait_tx_receipt(exec_client, state.last_tx_hash, timeout_sec=180):
                            wallet_failed = True
                            break
                        wallet_trades_started = True
                        _sleep_between_swaps()
                        continue
                elif not has_usable_usdc:
                    if spendable_eth <= 0:
                        logger.warning(
                            _quest_log("wallet=%s no usable ETH/USDC.E for buy-first quest cycle | existing %s balance is protected"),
                            wallet,
                            rides_token.symbol,
                        )
                        wallet_failed = True
                        break
                    bootstrap_eth = spendable_eth * Decimal("0.95")
                    bootstrap_trade_usd = bootstrap_eth * eth_price
                    if bootstrap_trade_usd < MIN_EXECUTABLE_TRADE_USD:
                        logger.warning(
                            _quest_log("wallet=%s bootstrap skipped | ETH->USDC.E input below $0.10 (%s)"),
                            wallet,
                            _format_decimal_plain(bootstrap_trade_usd),
                        )
                        wallet_failed = True
                        break
                    logger.info(
                        _quest_log("wallet=%s bootstrap | ETH->USDC.E amount=95%% spendable ETH"),
                        wallet,
                    )
                    ok_bootstrap = _execute_trade_via_doma_ui_route(
                        cfg=cfg,
                        logger=logger,
                        state=state,
                        doma_api=doma_api,
                        exec_client=exec_client,
                        token_in=weth_token,
                        token_out=quote_token,
                        display_in_symbol="ETH",
                        display_out_symbol="USDC.E",
                        trade_amount_expr=_format_decimal_plain(bootstrap_eth),
                        eth_price=eth_price,
                        label=f"{mode_label} {wallet} ETH>USDC.E BOOTSTRAP",
                        is_eth_source=True,
                        unwrap_to_native=False,
                        wait_for_pre_tx=True,
                    )
                    if not ok_bootstrap or not state.last_tx_hash or not _wait_tx_receipt(exec_client, state.last_tx_hash, timeout_sec=180):
                        wallet_failed = True
                        break
                    wallet_trades_started = True
                    _sleep_between_swaps()
                    continue
                elif full_balance_usdc > 0 and spendable_eth_usd >= MIN_EXECUTABLE_TRADE_USD and spendable_eth_usd > full_balance_usdc:
                    bootstrap_eth = spendable_eth * Decimal("0.95")
                    bootstrap_trade_usd = bootstrap_eth * eth_price
                    logger.info(
                        _quest_log("wallet=%s source selected | ETH balance larger than USDC.E (%s > %s), using ETH"),
                        wallet,
                        _format_decimal_plain(spendable_eth_usd),
                        _format_decimal_plain(full_balance_usdc),
                    )
                    logger.info(
                        _quest_log("wallet=%s bootstrap | ETH->USDC.E amount=95%% spendable ETH"),
                        wallet,
                    )
                    ok_bootstrap = _execute_trade_via_doma_ui_route(
                        cfg=cfg,
                        logger=logger,
                        state=state,
                        doma_api=doma_api,
                        exec_client=exec_client,
                        token_in=weth_token,
                        token_out=quote_token,
                        display_in_symbol="ETH",
                        display_out_symbol="USDC.E",
                        trade_amount_expr=_format_decimal_plain(bootstrap_eth),
                        eth_price=eth_price,
                        label=f"{mode_label} {wallet} ETH>USDC.E BOOTSTRAP",
                        is_eth_source=True,
                        unwrap_to_native=False,
                        wait_for_pre_tx=True,
                    )
                    if not ok_bootstrap or not state.last_tx_hash or not _wait_tx_receipt(exec_client, state.last_tx_hash, timeout_sec=180):
                        wallet_failed = True
                        break
                    wallet_trades_started = True
                    _sleep_between_swaps()
                    continue

                full_in_symbol = "USDC.E"
                full_out_symbol = rides_token.symbol
                partial_in_symbol = rides_token.symbol
                partial_out_symbol = "USDC.E"
                full_balance = full_balance_usdc
                full_trade_usd = full_balance_usdc
                full_trade_expr = "100%"

                remaining_volume = execution_target_volume - accumulated_volume
                if full_balance <= 0 or full_trade_usd <= 0:
                    logger.warning(_quest_log("wallet=%s no usable USDC.E/%s balance for quest cycle"), wallet, rides_token.symbol)
                    wallet_failed = True
                    break
                if full_trade_usd < MIN_EXECUTABLE_TRADE_USD:
                    logger.warning(
                        _quest_log("wallet=%s full step skipped before execution target | %s balance below $0.10 (%s)"),
                        wallet,
                        full_in_symbol,
                        _format_decimal_plain(full_trade_usd),
                    )
                    wallet_failed = True
                    break
                if require_min_single_swap and not min_single_swap_done and full_trade_usd < min_single_swap_usd:
                    logger.warning(
                        _quest_log("wallet=%s cannot satisfy quest single-swap requirement | need >=%s USDC.E in one swap, available %s via %s"),
                        wallet,
                        _format_decimal_plain(min_single_swap_usd),
                        _format_decimal_plain(full_trade_usd),
                        full_in_symbol,
                    )
                    wallet_failed = True
                    break
                if remaining_volume > 0 and remaining_volume < (full_trade_usd * Decimal("2")):
                    if require_min_single_swap and not min_single_swap_done:
                        min_full_step_usd = min_single_swap_usd
                        target_full_step_usd = max(min_full_step_usd, execution_target_volume - accumulated_volume)
                    else:
                        min_full_step_usd = MIN_EXECUTABLE_TRADE_USD
                        target_full_step_usd = remaining_volume / Decimal("2")
                    capped_full_usd = min(
                        full_trade_usd,
                        max(min_full_step_usd, target_full_step_usd),
                    )
                    if capped_full_usd >= MIN_EXECUTABLE_TRADE_USD:
                        full_trade_usd = capped_full_usd
                        full_trade_expr = f"${_format_decimal_plain(capped_full_usd)}"
                        logger.info(
                            _quest_log("wallet=%s near target | capping full step to %s"),
                            wallet,
                            full_trade_expr,
                        )

                logger.info(
                    _quest_log("wallet=%s full step | %s->%s amount=%s"),
                    wallet,
                    full_in_symbol,
                    full_out_symbol,
                    full_trade_expr if full_trade_expr != "100%" else f"100% of {full_in_symbol}",
                )
                full_before_usdc_balance = exec_client.get_erc20_balance(quote_token.address, quote_token.decimals)
                if full_in_symbol == "USDC.E":
                    ok_full, executed_full_expr, executed_full_trade_usd = _execute_ui_route_with_fallback(
                        cfg=cfg,
                        logger=logger,
                        state=state,
                        doma_api=doma_api,
                        exec_client=exec_client,
                        token_in=quote_token,
                        token_out=rides_token,
                        display_in_symbol="USDC.E",
                        display_out_symbol=rides_token.symbol,
                        trade_amount_expr=full_trade_expr,
                        eth_price=eth_price,
                        label=f"{mode_label} {wallet} USDC.E>{rides_token.symbol}",
                        wait_for_pre_tx=True,
                    )
                    if ok_full:
                        full_trade_expr = executed_full_expr
                        full_trade_usd = executed_full_trade_usd
                else:
                    ok_full = _execute_trade_via_doma_ui_route(
                        cfg=cfg,
                        logger=logger,
                        state=state,
                        doma_api=doma_api,
                        exec_client=exec_client,
                        token_in=rides_token,
                        token_out=quote_token,
                        display_in_symbol=rides_token.symbol,
                        display_out_symbol="USDC.E",
                        trade_amount_expr=full_trade_expr,
                        eth_price=eth_price,
                        label=f"{mode_label} {wallet} {rides_token.symbol}>USDC.E",
                        wait_for_pre_tx=True,
                    )
                if not ok_full or not state.last_tx_hash:
                    wallet_failed = True
                    break
                if not _wait_tx_receipt(exec_client, state.last_tx_hash, timeout_sec=180):
                    logger.warning(_quest_log("wallet=%s full step tx not confirmed before timeout | tx=%s"), wallet, state.last_tx_hash)
                    wallet_failed = True
                    break
                wallet_trades_started = True
                if require_min_single_swap and not min_single_swap_done and full_trade_usd < min_single_swap_usd:
                    logger.warning(
                        _quest_log("wallet=%s executed swap below quest single-swap requirement | swap=%s/%s USDC.E"),
                        wallet,
                        _format_decimal_plain(full_trade_usd),
                        _format_decimal_plain(min_single_swap_usd),
                    )
                    wallet_failed = True
                    break
                if require_min_single_swap and not min_single_swap_done and full_trade_usd >= min_single_swap_usd:
                    min_single_swap_done = True
                    logger.info(
                        _quest_log("wallet=%s single-swap requirement met | swap=%s/%s USDC.E"),
                        wallet,
                        _format_decimal_plain(full_trade_usd),
                        _format_decimal_plain(min_single_swap_usd),
                    )
                full_after_usdc_balance = exec_client.get_erc20_balance(quote_token.address, quote_token.decimals)
                full_added_volume = _volume_added_from_usdc_balance_change(
                    full_before_usdc_balance,
                    full_after_usdc_balance,
                    full_in_symbol,
                )
                if full_added_volume <= 0:
                    logger.warning(
                        _quest_log("wallet=%s full step volume fallback used | before_usdc=%s after_usdc=%s input=%s"),
                        wallet,
                        _format_decimal_plain(full_before_usdc_balance),
                        _format_decimal_plain(full_after_usdc_balance),
                        full_in_symbol,
                    )
                    full_added_volume = full_trade_usd
                accumulated_volume += full_added_volume
                logger.info(
                    _quest_log("wallet=%s full_added_volume=%s | total_volume=%s/%s"),
                    wallet,
                    _format_decimal_plain(full_added_volume),
                    _format_decimal_plain(accumulated_volume),
                    _format_decimal_plain(quest_target_volume),
                )
                if accumulated_volume >= quest_target_volume:
                    break
                if require_min_single_swap and min_single_swap_done and accumulated_volume >= quest_target_volume:
                    logger.info(
                        _quest_log("wallet=%s single-swap target reached | skipping partial top-up below minimum swap"),
                        wallet,
                    )
                    break
                _sleep_between_swaps()

                partial_expr = _pick_random_amount_expr(
                    "percent",
                    partial_min,
                    partial_max,
                    state,
                    min_raw=min_raw,
                    max_raw=max_raw,
                )
                if partial_in_symbol == "USDC.E":
                    partial_balance = exec_client.get_erc20_balance(quote_token.address, quote_token.decimals)
                    _, partial_trade_usd = resolve_trade_amount(partial_expr, partial_balance, Decimal("1"))
                else:
                    partial_balance = exec_client.get_erc20_balance(rides_token.address, rides_token.decimals)
                    sellable_partial_balance = max(Decimal("0"), partial_balance - protected_rides_balance)
                    if sellable_partial_balance <= 0:
                        logger.warning(
                            _quest_log("wallet=%s partial step skipped | no newly bought %s to sell; existing balance protected=%s"),
                            wallet,
                            rides_token.symbol,
                            _format_decimal_plain(protected_rides_balance),
                        )
                        wallet_failed = True
                        break
                    partial_amount_dec, partial_trade_usd = resolve_trade_amount(partial_expr, sellable_partial_balance, rides_price_usd)
                    partial_expr = _format_decimal_plain(partial_amount_dec)
                    partial_balance = sellable_partial_balance

                remaining_volume = execution_target_volume - accumulated_volume
                if partial_balance <= 0 or partial_trade_usd <= 0:
                    logger.warning(_quest_log("wallet=%s no balance for partial %s step"), wallet, partial_in_symbol)
                    wallet_failed = True
                    break
                if partial_trade_usd < MIN_EXECUTABLE_TRADE_USD:
                    logger.warning(
                        _quest_log("wallet=%s partial step skipped before execution target | %s input below $0.10 (%s)"),
                        wallet,
                        partial_in_symbol,
                        _format_decimal_plain(partial_trade_usd),
                    )
                    wallet_failed = True
                    break
                if remaining_volume > 0 and partial_trade_usd > remaining_volume:
                    capped_partial_usd = min(
                        partial_trade_usd,
                        max(MIN_EXECUTABLE_TRADE_USD, remaining_volume),
                    )
                    if capped_partial_usd >= MIN_EXECUTABLE_TRADE_USD:
                        partial_trade_usd = capped_partial_usd
                        partial_expr = f"${_format_decimal_plain(capped_partial_usd)}"
                        logger.info(
                            _quest_log("wallet=%s near target | capping partial step to %s"),
                            wallet,
                            partial_expr,
                        )

                logger.info(
                    _quest_log("wallet=%s partial step | %s->%s amount=%s"),
                    wallet,
                    partial_in_symbol,
                    partial_out_symbol,
                    partial_expr,
                )
                partial_before_usdc_balance = exec_client.get_erc20_balance(quote_token.address, quote_token.decimals)
                if partial_in_symbol == "USDC.E":
                    ok_partial, executed_partial_expr, executed_partial_trade_usd = _execute_ui_route_with_fallback(
                        cfg=cfg,
                        logger=logger,
                        state=state,
                        doma_api=doma_api,
                        exec_client=exec_client,
                        token_in=quote_token,
                        token_out=rides_token,
                        display_in_symbol="USDC.E",
                        display_out_symbol=rides_token.symbol,
                        trade_amount_expr=partial_expr,
                        eth_price=eth_price,
                        label=f"{mode_label} {wallet} USDC.E>{rides_token.symbol}",
                        wait_for_pre_tx=True,
                    )
                    if ok_partial:
                        partial_expr = executed_partial_expr
                        partial_trade_usd = executed_partial_trade_usd
                else:
                    ok_partial = _execute_trade_via_doma_ui_route(
                        cfg=cfg,
                        logger=logger,
                        state=state,
                        doma_api=doma_api,
                        exec_client=exec_client,
                        token_in=rides_token,
                        token_out=quote_token,
                        display_in_symbol=rides_token.symbol,
                        display_out_symbol="USDC.E",
                        trade_amount_expr=partial_expr,
                        eth_price=eth_price,
                        label=f"{mode_label} {wallet} {rides_token.symbol}>USDC.E",
                        wait_for_pre_tx=True,
                    )
                if not ok_partial or not state.last_tx_hash:
                    wallet_failed = True
                    break
                if not _wait_tx_receipt(exec_client, state.last_tx_hash, timeout_sec=180):
                    logger.warning(_quest_log("wallet=%s partial step tx not confirmed before timeout | tx=%s"), wallet, state.last_tx_hash)
                    wallet_failed = True
                    break
                wallet_trades_started = True
                partial_after_usdc_balance = exec_client.get_erc20_balance(quote_token.address, quote_token.decimals)
                partial_added_volume = _volume_added_from_usdc_balance_change(
                    partial_before_usdc_balance,
                    partial_after_usdc_balance,
                    partial_in_symbol,
                )
                if partial_added_volume <= 0:
                    logger.warning(
                        _quest_log("wallet=%s partial step volume fallback used | before_usdc=%s after_usdc=%s input=%s"),
                        wallet,
                        _format_decimal_plain(partial_before_usdc_balance),
                        _format_decimal_plain(partial_after_usdc_balance),
                        partial_in_symbol,
                    )
                    partial_added_volume = partial_trade_usd
                accumulated_volume += partial_added_volume
                logger.info(
                    _quest_log("wallet=%s partial_added_volume=%s | total_volume=%s/%s"),
                    wallet,
                    _format_decimal_plain(partial_added_volume),
                    _format_decimal_plain(accumulated_volume),
                    _format_decimal_plain(quest_target_volume),
                )
                if accumulated_volume < quest_target_volume:
                    _sleep_between_swaps()

            if not wallet_failed and accumulated_volume < quest_target_volume:
                logger.warning(
                    _quest_log("wallet=%s quest target not reached | local_volume=%s/%s"),
                    wallet,
                    _format_decimal_plain(accumulated_volume),
                    _format_decimal_plain(quest_target_volume),
                )
                wallet_failed = True

            if wallet_failed:
                if wallet_trades_started:
                    _best_effort_failed_rides_cleanup(
                        wallet=wallet,
                        exec_client=exec_client,
                        doma_api=doma_api,
                        quote_token=quote_token,
                        rides_token=rides_token,
                        weth_token=weth_token,
                        eth_price=eth_price,
                        final_asset=final_asset,
                        protected_rides_balance=protected_rides_balance,
                    )
                else:
                    logger.info(_quest_log("wallet=%s cleanup skipped | no quest swaps were sent"), wallet)
                _fail_wallet()
            elif accumulated_volume >= quest_target_volume:
                final_rides_balance = exec_client.get_erc20_balance(rides_token.address, rides_token.decimals)
                sellable_final_rides_balance = max(Decimal("0"), final_rides_balance - protected_rides_balance)
                if sellable_final_rides_balance > 0:
                    final_rides_usd = sellable_final_rides_balance * rides_price_usd
                    if final_rides_usd < MIN_EXECUTABLE_TRADE_USD:
                        logger.info(
                            _quest_log("wallet=%s final settle skipped | %s dust below $0.10 (%s)"),
                            wallet,
                            rides_token.symbol,
                            _format_decimal_plain(final_rides_usd),
                        )
                    else:
                        logger.info(
                            _quest_log("wallet=%s final settle | %s->USDC.E amount=%s %s | protected_existing=%s"),
                            wallet,
                            rides_token.symbol,
                            _format_decimal_plain(sellable_final_rides_balance),
                            rides_token.symbol,
                            _format_decimal_plain(protected_rides_balance),
                        )
                        final_before_usdc_balance = exec_client.get_erc20_balance(quote_token.address, quote_token.decimals)
                        ok_settle = _execute_trade_via_doma_ui_route(
                            cfg=cfg,
                            logger=logger,
                            state=state,
                            doma_api=doma_api,
                            exec_client=exec_client,
                            token_in=rides_token,
                            token_out=quote_token,
                            display_in_symbol=rides_token.symbol,
                            display_out_symbol="USDC.E",
                            trade_amount_expr=_format_decimal_plain(sellable_final_rides_balance),
                            eth_price=eth_price,
                            label=f"{mode_label} {wallet} {rides_token.symbol}>USDC.E FINAL",
                            wait_for_pre_tx=True,
                        )
                        if not ok_settle or not state.last_tx_hash or not _wait_tx_receipt(exec_client, state.last_tx_hash, timeout_sec=180):
                            wallet_failed = True
                        else:
                            final_after_usdc_balance = exec_client.get_erc20_balance(quote_token.address, quote_token.decimals)
                            final_added_volume = _volume_added_from_usdc_balance_change(
                                final_before_usdc_balance,
                                final_after_usdc_balance,
                                rides_token.symbol,
                            )
                            if final_added_volume <= 0:
                                logger.warning(
                                    _quest_log("wallet=%s final settle volume fallback used | before_usdc=%s after_usdc=%s"),
                                    wallet,
                                    _format_decimal_plain(final_before_usdc_balance),
                                    _format_decimal_plain(final_after_usdc_balance),
                                )
                                final_added_volume = max(Decimal("0"), final_after_usdc_balance - final_before_usdc_balance)
                            accumulated_volume += final_added_volume
                            logger.info(
                                _quest_log("wallet=%s final_settle_added_volume=%s | total_volume=%s/%s"),
                                wallet,
                                _format_decimal_plain(final_added_volume),
                                _format_decimal_plain(accumulated_volume),
                                _format_decimal_plain(quest_target_volume),
                            )

                if wallet_failed:
                    _best_effort_failed_rides_cleanup(
                        wallet=wallet,
                        exec_client=exec_client,
                        doma_api=doma_api,
                        quote_token=quote_token,
                        rides_token=rides_token,
                        weth_token=weth_token,
                        eth_price=eth_price,
                        final_asset=final_asset,
                        protected_rides_balance=protected_rides_balance,
                    )
                    _fail_wallet()
                else:
                    if final_asset == "ETH":
                        final_usdc_balance = exec_client.get_erc20_balance(quote_token.address, quote_token.decimals)
                        if final_usdc_balance >= MIN_EXECUTABLE_TRADE_USD:
                            logger.info(
                                _quest_log("wallet=%s final exit | USDC.E->ETH amount=100%% of USDC.E"),
                                wallet,
                            )
                            ok_exit = _execute_trade_via_doma_ui_route(
                                cfg=cfg,
                                logger=logger,
                                state=state,
                                doma_api=doma_api,
                                exec_client=exec_client,
                                token_in=quote_token,
                                token_out=weth_token,
                                display_in_symbol="USDC.E",
                                display_out_symbol="ETH",
                                trade_amount_expr="100%",
                                eth_price=eth_price,
                                label=f"{mode_label} {wallet} USDC.E>ETH EXIT",
                                is_eth_source=False,
                                unwrap_to_native=True,
                                wait_for_pre_tx=True,
                            )
                            if not ok_exit or not state.last_tx_hash or not _wait_tx_receipt(exec_client, state.last_tx_hash, timeout_sec=180):
                                wallet_failed = True
                            else:
                                _cleanup_weth_balance(
                                    logger=logger,
                                    exec_client=exec_client,
                                    weth_token=weth_token,
                                    label=f"{mode_label} {wallet} FINAL",
                                    reason="final cleanup",
                                    wait_for_receipt=True,
                                )
                        elif final_usdc_balance > 0:
                            logger.info(
                                _quest_log("wallet=%s final exit skipped | USDC.E dust below $0.10 (%s)"),
                                wallet,
                                _format_decimal_plain(final_usdc_balance),
                            )
                    if wallet_failed:
                        _fail_wallet()
                        continue
                    success_wallets += 1
                    logger.info(
                        _quest_log("wallet=%s target reached | total_volume=%s USDC.E | final_asset=%s"),
                        wallet,
                        _format_decimal_plain(accumulated_volume),
                        final_asset,
                    )
            else:
                skipped_wallets += 1
        except Exception as exc:
            _fail_wallet()
            if _is_proxy_connectivity_error(exc):
                logger.warning(_quest_log("wallet=%s proxy/RPC failed during run, skipping wallet: %s"), wallet, exc)
            else:
                logger.warning(_quest_log("wallet=%s runtime failed, skipping wallet: %s"), wallet, exc)
        if idx < len(wallet_key_records) - 1 and cfg.wallet_delay_max_sec > 0:
            delay_sec = random.uniform(cfg.wallet_delay_min_sec, cfg.wallet_delay_max_sec)
            logger.info(_quest_log("delay before next wallet: %.2f sec"), delay_sec)
            time.sleep(delay_sec)

    _print_mode_summary(
        mode_label,
        len(wallet_key_records),
        success_wallets,
        failed_wallets,
        skipped_wallets,
        failed_wallet_labels,
    )


def get_domain_listing_menu_input() -> Optional[Tuple[str, str, str, str, str, str, str, str, str]]:
    print("\nList unlisted domains for sale:")
    min_raw = input("Minimum price in USDC.E: ").strip()
    max_raw = input("Maximum price in USDC.E: ").strip()
    if not min_raw or not max_raw:
        raise ValueError("Minimum and maximum listing prices are required")
    min_price = _parse_decimal_input(min_raw)
    max_price = _parse_decimal_input(max_raw)
    if min_price <= 0 or max_price <= 0:
        raise ValueError("Listing prices must be > 0")
    if max_price < min_price:
        raise ValueError("Maximum price cannot be lower than minimum price")
    duration_raw = input(f"Listing duration days [{DOMAIN_LISTING_DEFAULT_DURATION_DAYS}]: ").strip()
    if not duration_raw:
        duration_raw = str(DOMAIN_LISTING_DEFAULT_DURATION_DAYS)
    duration_days = _parse_decimal_input(duration_raw)
    if duration_days <= 0:
        raise ValueError("Listing duration must be > 0")
    print("Domain network:")
    print("1) Doma")
    print("2) Base")
    print("3) Any available")
    network_mode_raw = input("Select [1-3, default 3]: ").strip()
    if not network_mode_raw:
        network_mode_raw = "3"
    if network_mode_raw not in {"1", "2", "3"}:
        raise ValueError("Invalid domain network mode")
    print("Listing count:")
    print("1) All unlisted domains")
    print("2) Random amount from min/max")
    count_mode_raw = input("Select [1-2, default 1]: ").strip()
    if not count_mode_raw:
        count_mode_raw = "1"
    if count_mode_raw not in {"1", "2"}:
        raise ValueError("Invalid listing count mode")
    count_min_raw = ""
    count_max_raw = ""
    if count_mode_raw == "2":
        count_min_raw = input("Minimum domains to list per wallet: ").strip()
        count_max_raw = input("Maximum domains to list per wallet: ").strip()
        if not count_min_raw or not count_max_raw:
            raise ValueError("Minimum and maximum listing count are required")
        count_min = int(_parse_decimal_input(count_min_raw))
        count_max = int(_parse_decimal_input(count_max_raw))
        if count_min <= 0 or count_max <= 0:
            raise ValueError("Listing count must be > 0")
        if count_max < count_min:
            raise ValueError("Maximum listing count cannot be lower than minimum count")
    delay_min_raw = input(f"Minimum delay between domains sec [{DOMAIN_LISTING_DEFAULT_DELAY_MIN_SEC}]: ").strip()
    delay_max_raw = input(f"Maximum delay between domains sec [{DOMAIN_LISTING_DEFAULT_DELAY_MAX_SEC}]: ").strip()
    if not delay_min_raw:
        delay_min_raw = _format_decimal_plain(DOMAIN_LISTING_DEFAULT_DELAY_MIN_SEC)
    if not delay_max_raw:
        delay_max_raw = _format_decimal_plain(DOMAIN_LISTING_DEFAULT_DELAY_MAX_SEC)
    delay_min = _parse_decimal_input(delay_min_raw)
    delay_max = _parse_decimal_input(delay_max_raw)
    if delay_min < 0 or delay_max < 0:
        raise ValueError("Domain listing delays cannot be negative")
    if delay_max < delay_min:
        raise ValueError("Maximum domain listing delay cannot be lower than minimum delay")
    return (
        min_raw,
        max_raw,
        duration_raw,
        network_mode_raw,
        count_mode_raw,
        count_min_raw,
        count_max_raw,
        delay_min_raw,
        delay_max_raw,
    )


def get_domain_purchase_menu_input() -> Optional[Tuple[str, str, str, str, str, str, str]]:
    print("\nBuy cheapest listed domains:")
    print("Action:")
    print("1) Buy only")
    print("2) Buy and list +$0.02-$0.05")
    action_raw = input("Select [1-2, default 1]: ").strip() or "1"
    if action_raw not in {"1", "2"}:
        raise ValueError("Invalid domain purchase action")
    max_price_raw = input("Buy cheapest domains up to USDC.E price [1]: ").strip() or "1"
    max_price = _parse_decimal_input(max_price_raw)
    if max_price <= 0:
        raise ValueError("Domain price filter must be > 0")
    network_mode_raw = "1"
    min_count_raw = input("Minimum domains to buy per wallet [1]: ").strip() or "1"
    max_count_raw = input("Maximum domains to buy per wallet [1]: ").strip() or "1"
    count_min = int(_parse_decimal_input(min_count_raw))
    count_max = int(_parse_decimal_input(max_count_raw))
    if count_min <= 0 or count_max <= 0:
        raise ValueError("Domain buy count must be > 0")
    if count_max < count_min:
        raise ValueError("Maximum buy count cannot be lower than minimum")
    delay_min_raw = input(f"Minimum delay between buys sec [{DOMAIN_LISTING_DEFAULT_DELAY_MIN_SEC}]: ").strip()
    delay_max_raw = input(f"Maximum delay between buys sec [{DOMAIN_LISTING_DEFAULT_DELAY_MAX_SEC}]: ").strip()
    if not delay_min_raw:
        delay_min_raw = _format_decimal_plain(DOMAIN_LISTING_DEFAULT_DELAY_MIN_SEC)
    if not delay_max_raw:
        delay_max_raw = _format_decimal_plain(DOMAIN_LISTING_DEFAULT_DELAY_MAX_SEC)
    delay_min = _parse_decimal_input(delay_min_raw)
    delay_max = _parse_decimal_input(delay_max_raw)
    if delay_min < 0 or delay_max < 0:
        raise ValueError("Domain buy delays cannot be negative")
    if delay_max < delay_min:
        raise ValueError("Maximum buy delay cannot be lower than minimum delay")
    return action_raw, max_price_raw, network_mode_raw, min_count_raw, max_count_raw, delay_min_raw, delay_max_raw


def get_domain_delisting_menu_input() -> Optional[Tuple[str, str, str, str]]:
    print("\nCancel active domain listings:")
    cancellation_type = "off-chain"
    print("Domain network:")
    print("1) Doma")
    print("2) Base")
    print("3) Any available")
    network_mode_raw = input("Select [1-3, default 3]: ").strip()
    if not network_mode_raw:
        network_mode_raw = "3"
    if network_mode_raw not in {"1", "2", "3"}:
        raise ValueError("Invalid domain network mode")
    delay_min_raw = input(f"Minimum delay between domains sec [{DOMAIN_LISTING_DEFAULT_DELAY_MIN_SEC}]: ").strip()
    delay_max_raw = input(f"Maximum delay between domains sec [{DOMAIN_LISTING_DEFAULT_DELAY_MAX_SEC}]: ").strip()
    if not delay_min_raw:
        delay_min_raw = _format_decimal_plain(DOMAIN_LISTING_DEFAULT_DELAY_MIN_SEC)
    if not delay_max_raw:
        delay_max_raw = _format_decimal_plain(DOMAIN_LISTING_DEFAULT_DELAY_MAX_SEC)
    delay_min = _parse_decimal_input(delay_min_raw)
    delay_max = _parse_decimal_input(delay_max_raw)
    if delay_min < 0 or delay_max < 0:
        raise ValueError("Domain delisting delays cannot be negative")
    if delay_max < delay_min:
        raise ValueError("Maximum domain delisting delay cannot be lower than minimum delay")
    return cancellation_type, network_mode_raw, delay_min_raw, delay_max_raw


def get_domain_bridge_to_base_menu_input() -> Optional[Tuple[str, str, str, str]]:
    print("\nBridge domain NFTs to Base:")
    print("Domain selection:")
    print("1) Listed domains")
    print("2) Unlisted domains")
    print("3) Any domains")
    selection_raw = input("Select [1-3, default 2]: ").strip()
    if not selection_raw:
        selection_raw = "2"
    selection_map = {
        "1": "listed",
        "2": "unlisted",
        "3": "any",
    }
    selection = selection_map.get(selection_raw)
    if not selection:
        raise ValueError("Invalid domain bridge selection")
    domains_raw = input("Domains to bridge per wallet [1]: ").strip()
    if not domains_raw:
        domains_raw = "1"
    domains_per_wallet = int(_parse_decimal_input(domains_raw))
    if domains_per_wallet <= 0:
        raise ValueError("Domains to bridge per wallet must be > 0")
    delay_min_raw = input(f"Minimum delay between domains sec [{DOMAIN_LISTING_DEFAULT_DELAY_MIN_SEC}]: ").strip()
    delay_max_raw = input(f"Maximum delay between domains sec [{DOMAIN_LISTING_DEFAULT_DELAY_MAX_SEC}]: ").strip()
    if not delay_min_raw:
        delay_min_raw = _format_decimal_plain(DOMAIN_LISTING_DEFAULT_DELAY_MIN_SEC)
    if not delay_max_raw:
        delay_max_raw = _format_decimal_plain(DOMAIN_LISTING_DEFAULT_DELAY_MAX_SEC)
    delay_min = _parse_decimal_input(delay_min_raw)
    delay_max = _parse_decimal_input(delay_max_raw)
    if delay_min < 0 or delay_max < 0:
        raise ValueError("Domain bridge delays cannot be negative")
    if delay_max < delay_min:
        raise ValueError("Maximum domain bridge delay cannot be lower than minimum delay")
    return selection, str(domains_per_wallet), delay_min_raw, delay_max_raw




def get_cheap_token_buy_menu_input() -> Optional[Tuple[str, str, str, str, str, str, str, str, str]]:
    print("\nBuy cheap domain tokens and claim subdomains:")
    max_price_raw = input("Maximum token price USD [0.01]: ").strip() or "0.01"
    buy_amount_min_raw = input("Minimum USDC.E amount per buy [0.01]: ").strip() or "0.01"
    buy_amount_max_raw = input("Maximum USDC.E amount per buy [0.01]: ").strip() or "0.01"
    tokens_min_raw = input("Minimum tokens to buy per wallet [1]: ").strip() or "1"
    tokens_max_raw = input("Maximum tokens to buy per wallet [1]: ").strip() or "1"
    subdomains_min_raw = input("Minimum subdomains to claim per token [1]: ").strip() or "1"
    subdomains_max_raw = input("Maximum subdomains to claim per token [1]: ").strip() or "1"
    delay_min_raw = input(f"Minimum delay between buys sec [{DOMAIN_LISTING_DEFAULT_DELAY_MIN_SEC}]: ").strip()
    delay_max_raw = input(f"Maximum delay between buys sec [{DOMAIN_LISTING_DEFAULT_DELAY_MAX_SEC}]: ").strip()
    if not delay_min_raw:
        delay_min_raw = _format_decimal_plain(DOMAIN_LISTING_DEFAULT_DELAY_MIN_SEC)
    if not delay_max_raw:
        delay_max_raw = _format_decimal_plain(DOMAIN_LISTING_DEFAULT_DELAY_MAX_SEC)
    max_price = _parse_decimal_input(max_price_raw)
    buy_amount_min = _parse_decimal_input(buy_amount_min_raw)
    buy_amount_max = _parse_decimal_input(buy_amount_max_raw)
    tokens_min = int(_parse_decimal_input(tokens_min_raw).to_integral_value(rounding=ROUND_FLOOR))
    tokens_max = int(_parse_decimal_input(tokens_max_raw).to_integral_value(rounding=ROUND_CEILING))
    subdomains_min = int(_parse_decimal_input(subdomains_min_raw).to_integral_value(rounding=ROUND_FLOOR))
    subdomains_max = int(_parse_decimal_input(subdomains_max_raw).to_integral_value(rounding=ROUND_CEILING))
    delay_min = _parse_decimal_input(delay_min_raw)
    delay_max = _parse_decimal_input(delay_max_raw)
    if max_price <= 0:
        raise ValueError("Maximum token price must be > 0")
    if buy_amount_min <= 0 or buy_amount_max <= 0:
        raise ValueError("USDC.E buy amounts must be > 0")
    if buy_amount_max < buy_amount_min:
        raise ValueError("Maximum USDC.E buy amount cannot be lower than minimum")
    if tokens_min <= 0 or tokens_max <= 0:
        raise ValueError("Token counts per wallet must be > 0")
    if tokens_max < tokens_min:
        raise ValueError("Maximum tokens per wallet cannot be lower than minimum")
    if subdomains_min <= 0 or subdomains_max <= 0:
        raise ValueError("Subdomain counts per token must be > 0")
    if subdomains_max < subdomains_min:
        raise ValueError("Maximum subdomains per token cannot be lower than minimum")
    if delay_min < 0 or delay_max < 0:
        raise ValueError("Buy delays cannot be negative")
    if delay_max < delay_min:
        raise ValueError("Maximum buy delay cannot be lower than minimum delay")
    return max_price_raw, buy_amount_min_raw, buy_amount_max_raw, str(tokens_min), str(tokens_max), str(subdomains_min), str(subdomains_max), delay_min_raw, delay_max_raw


def get_bonding_token_buy_menu_input() -> Tuple[str, str, str, str, str, str, str, str, str, str, str, str, str, str]:
    print("\nBuy domain tokens currently in bonding:")
    print("Token selection:")
    print("1) Choose active token by FDV")
    print("2) Wait for specific domain launch")
    print("3) Sell specific domain token")
    print("4) Sell specific token at bonding curve percent")
    selection_raw = input("Select [1-4, default 1]: ").strip() or "1"
    selection_map = {"1": "fdv", "2": "specific", "3": "sell_specific", "4": "sell_at_curve"}
    selection = selection_map.get(selection_raw)
    if not selection:
        raise ValueError("Invalid bonding token selection")
    domain_raw = ""
    poll_interval_raw = "5"
    max_wait_minutes_raw = "0"
    preapprove_raw = "0"
    fast_broadcast_raw = "0"
    launch_time_utc_raw = ""
    bonding_target_percent_raw = "0"
    if selection in {"specific", "sell_specific", "sell_at_curve"}:
        default_domain = "smoothie.com"
        prompt = "Domain or Doma URL to sell" if selection in {"sell_specific", "sell_at_curve"} else "Domain or Doma URL"
        domain_raw = input(f"{prompt} [{default_domain}]: ").strip() or default_domain
        domain_raw = _normalize_domain_token_symbol(domain_raw).lower()
    if selection == "specific":
        poll_interval_raw = input("Transaction rebroadcast interval sec [0.2]: ").strip() or "0.2"
        preapprove_raw = input("Pre-approve USDC.E before launch? 1=yes 2=no [1]: ").strip() or "1"
        fast_broadcast_raw = input("Fast broadcast buys after launch? 1=yes 2=no [1]: ").strip() or "1"
        print("Maximum wait: until launchpad buy is accepted")
        max_wait_minutes_raw = "0"
    elif selection == "sell_at_curve":
        bonding_target_percent_raw = input("Sell at bonding curve percent [90]: ").strip() or "90"
        poll_interval_raw = input("Bonding curve check interval sec [1]: ").strip() or "1"

    if selection == "specific":
        action = "buy"
        print("Action: Buy only")
    elif selection in {"sell_specific", "sell_at_curve"}:
        action = "sell"
        print("Action: Sell only")
    else:
        print("Action:")
        print("1) Buy only")
        print("2) Buy and sell")
        action_raw = input("Select [1-2, default 1]: ").strip() or "1"
        action_map = {"1": "buy", "2": "buy_sell"}
        action = action_map.get(action_raw)
        if not action:
            raise ValueError("Invalid bonding token action")
    if action == "sell":
        print("Sell amount mode:")
        print("1) Number (domain token)")
        print("2) Percent (%)")
        amount_mode_raw = input("Select [1-2, default 2]: ").strip() or "2"
        amount_mode_map = {"1": "sell_number", "2": "sell_percent"}
        amount_mode = amount_mode_map.get(amount_mode_raw)
        if not amount_mode:
            raise ValueError("Invalid bonding sell amount mode")
        if amount_mode == "sell_percent":
            buy_amount_min_raw = input("Minimum percent [100]: ").strip() or "100"
            buy_amount_max_raw = input("Maximum percent [100]: ").strip() or "100"
        else:
            buy_amount_min_raw = input("Minimum token amount: ").strip()
            buy_amount_max_raw = input("Maximum token amount: ").strip()
    else:
        print("Amount mode:")
        print("1) USDC.E amount range")
        print("2) All available USDC.E")
        amount_mode_raw = input("Select [1-2, default 1]: ").strip() or "1"
        amount_mode_map = {"1": "fixed", "2": "all_usdc"}
        amount_mode = amount_mode_map.get(amount_mode_raw)
        if not amount_mode:
            raise ValueError("Invalid bonding buy amount mode")
        if amount_mode == "fixed":
            buy_amount_min_raw = input("Minimum USDC.E amount per wallet [10]: ").strip() or "10"
            buy_amount_max_raw = input("Maximum USDC.E amount per wallet [10]: ").strip() or "10"
        else:
            buy_amount_min_raw = "0"
            buy_amount_max_raw = "0"
    delay_min_raw = input(f"Minimum delay between wallets sec [{DOMAIN_LISTING_DEFAULT_DELAY_MIN_SEC}]: ").strip()
    delay_max_raw = input(f"Maximum delay between wallets sec [{DOMAIN_LISTING_DEFAULT_DELAY_MAX_SEC}]: ").strip()
    if not delay_min_raw:
        delay_min_raw = _format_decimal_plain(DOMAIN_LISTING_DEFAULT_DELAY_MIN_SEC)
    if not delay_max_raw:
        delay_max_raw = _format_decimal_plain(DOMAIN_LISTING_DEFAULT_DELAY_MAX_SEC)

    buy_amount_min = _parse_decimal_input(buy_amount_min_raw)
    buy_amount_max = _parse_decimal_input(buy_amount_max_raw)
    delay_min = _parse_decimal_input(delay_min_raw)
    delay_max = _parse_decimal_input(delay_max_raw)
    poll_interval = _parse_decimal_input(poll_interval_raw)
    max_wait_minutes = _parse_decimal_input(max_wait_minutes_raw)
    bonding_target_percent = _parse_decimal_input(bonding_target_percent_raw)
    if amount_mode == "fixed" and (buy_amount_min <= 0 or buy_amount_max <= 0):
        raise ValueError("Bonding buy amounts must be > 0")
    if amount_mode == "fixed" and buy_amount_max < buy_amount_min:
        raise ValueError("Maximum bonding buy amount cannot be lower than minimum")
    if amount_mode in {"sell_number", "sell_percent"} and (buy_amount_min <= 0 or buy_amount_max <= 0):
        raise ValueError("Bonding sell amounts must be > 0")
    if amount_mode in {"sell_number", "sell_percent"} and buy_amount_max < buy_amount_min:
        raise ValueError("Maximum bonding sell amount cannot be lower than minimum")
    if amount_mode == "sell_percent" and buy_amount_max > 100:
        raise ValueError("Sell percent maximum cannot be > 100")
    if poll_interval <= 0:
        raise ValueError("Check interval must be > 0")
    if selection == "sell_at_curve" and not (Decimal("0") < bonding_target_percent < Decimal("100")):
        raise ValueError("Bonding curve sell target must be greater than 0 and lower than 100 percent")
    if max_wait_minutes < 0:
        raise ValueError("Maximum wait minutes cannot be negative")
    if preapprove_raw not in {"0", "1", "2"}:
        raise ValueError("Invalid pre-approve option")
    if fast_broadcast_raw not in {"0", "1", "2"}:
        raise ValueError("Invalid fast broadcast option")
    if delay_min < 0 or delay_max < 0:
        raise ValueError("Bonding buy delays cannot be negative")
    if delay_max < delay_min:
        raise ValueError("Maximum bonding buy delay cannot be lower than minimum delay")
    return selection, domain_raw, poll_interval_raw, max_wait_minutes_raw, action, amount_mode, buy_amount_min_raw, buy_amount_max_raw, delay_min_raw, delay_max_raw, preapprove_raw, fast_broadcast_raw, launch_time_utc_raw, bonding_target_percent_raw


def get_close_subdomains_menu_input() -> Optional[Tuple[str, str, str]]:
    print("\nClose/unstake staked subdomains:")
    max_raw = input("Maximum subdomains to close per wallet [all]: ").strip()
    delay_min_raw = input(f"Minimum delay between subdomains sec [{DOMAIN_LISTING_DEFAULT_DELAY_MIN_SEC}]: ").strip()
    delay_max_raw = input(f"Maximum delay between subdomains sec [{DOMAIN_LISTING_DEFAULT_DELAY_MAX_SEC}]: ").strip()
    if not delay_min_raw:
        delay_min_raw = _format_decimal_plain(DOMAIN_LISTING_DEFAULT_DELAY_MIN_SEC)
    if not delay_max_raw:
        delay_max_raw = _format_decimal_plain(DOMAIN_LISTING_DEFAULT_DELAY_MAX_SEC)
    max_to_close = 0
    if max_raw:
        max_to_close = int(_parse_decimal_input(max_raw).to_integral_value(rounding=ROUND_FLOOR))
        if max_to_close <= 0:
            raise ValueError("Maximum subdomains to close must be > 0 or empty")
    delay_min = _parse_decimal_input(delay_min_raw)
    delay_max = _parse_decimal_input(delay_max_raw)
    if delay_min < 0 or delay_max < 0:
        raise ValueError("Subdomain close delays cannot be negative")
    if delay_max < delay_min:
        raise ValueError("Maximum close delay cannot be lower than minimum delay")
    return str(max_to_close), delay_min_raw, delay_max_raw


def get_com_daily_swap_menu_input() -> Optional[Tuple[str, str, str, str, str, str]]:
    print("\nDaily quest: swap on top .com domain tokens:")
    swap_min_raw = input("Minimum swap amount USDC.E [1]: ").strip() or "1"
    swap_max_raw = input("Maximum swap amount USDC.E [1.2]: ").strip() or "1.2"
    domains_min_raw = input("Minimum .com domains per wallet [10]: ").strip() or "10"
    domains_max_raw = input("Maximum .com domains per wallet [10]: ").strip() or "10"
    delay_min_raw = input(f"Minimum delay between domain swaps sec [{DOMAIN_LISTING_DEFAULT_DELAY_MIN_SEC}]: ").strip()
    delay_max_raw = input(f"Maximum delay between domain swaps sec [{DOMAIN_LISTING_DEFAULT_DELAY_MAX_SEC}]: ").strip()
    if not delay_min_raw:
        delay_min_raw = _format_decimal_plain(DOMAIN_LISTING_DEFAULT_DELAY_MIN_SEC)
    if not delay_max_raw:
        delay_max_raw = _format_decimal_plain(DOMAIN_LISTING_DEFAULT_DELAY_MAX_SEC)
    swap_min = _parse_decimal_input(swap_min_raw)
    swap_max = _parse_decimal_input(swap_max_raw)
    domains_min = int(_parse_decimal_input(domains_min_raw).to_integral_value(rounding=ROUND_FLOOR))
    domains_max = int(_parse_decimal_input(domains_max_raw).to_integral_value(rounding=ROUND_CEILING))
    delay_min = _parse_decimal_input(delay_min_raw)
    delay_max = _parse_decimal_input(delay_max_raw)
    if swap_min <= 0 or swap_max <= 0:
        raise ValueError("Swap amounts must be > 0")
    if swap_max < swap_min:
        raise ValueError("Maximum swap amount cannot be lower than minimum")
    if domains_min <= 0 or domains_max <= 0:
        raise ValueError(".com domain counts must be > 0")
    if domains_max < domains_min:
        raise ValueError("Maximum .com domain count cannot be lower than minimum")
    if delay_min < 0 or delay_max < 0:
        raise ValueError("Swap delays cannot be negative")
    if delay_max < delay_min:
        raise ValueError("Maximum swap delay cannot be lower than minimum delay")
    return swap_min_raw, swap_max_raw, str(domains_min), str(domains_max), delay_min_raw, delay_max_raw


def get_domain_offer_menu_input() -> Optional[Tuple[str, str, str, str, str, str, str]]:
    print("\nPlace domain offers in USDC.E:")
    buffer_raw = input("Minimum offer amount USDC.E [0.23]: ").strip() or "0.23"
    max_offer_raw = input("Maximum offer amount USDC.E [0.25]: ").strip() or "0.25"
    duration_days_raw = input("Offer duration days [1]: ").strip() or "1"
    offers_min_raw = input("Minimum offers per wallet [1]: ").strip() or "1"
    offers_max_raw = input("Maximum offers per wallet [1]: ").strip() or "1"
    delay_min_raw = input(f"Minimum delay between offers sec [{DOMAIN_LISTING_DEFAULT_DELAY_MIN_SEC}]: ").strip()
    delay_max_raw = input(f"Maximum delay between offers sec [{DOMAIN_LISTING_DEFAULT_DELAY_MAX_SEC}]: ").strip()
    if not delay_min_raw:
        delay_min_raw = _format_decimal_plain(DOMAIN_LISTING_DEFAULT_DELAY_MIN_SEC)
    if not delay_max_raw:
        delay_max_raw = _format_decimal_plain(DOMAIN_LISTING_DEFAULT_DELAY_MAX_SEC)
    buffer_amount = _parse_decimal_input(buffer_raw)
    max_offer = _parse_decimal_input(max_offer_raw)
    duration_days = _parse_decimal_input(duration_days_raw)
    offers_min = int(_parse_decimal_input(offers_min_raw).to_integral_value(rounding=ROUND_FLOOR))
    offers_max = int(_parse_decimal_input(offers_max_raw).to_integral_value(rounding=ROUND_CEILING))
    delay_min = _parse_decimal_input(delay_min_raw)
    delay_max = _parse_decimal_input(delay_max_raw)
    if buffer_amount <= 0:
        raise ValueError("Minimum offer amount must be > 0")
    if max_offer <= 0:
        raise ValueError("Maximum offer amount must be > 0")
    if max_offer < buffer_amount:
        raise ValueError("Maximum offer amount cannot be lower than minimum")
    if duration_days <= 0:
        raise ValueError("Offer duration must be > 0")
    if offers_min <= 0 or offers_max <= 0:
        raise ValueError("Offer counts per wallet must be > 0")
    if offers_max < offers_min:
        raise ValueError("Maximum offers per wallet cannot be lower than minimum")
    if delay_min < 0 or delay_max < 0:
        raise ValueError("Offer delays cannot be negative")
    if delay_max < delay_min:
        raise ValueError("Maximum offer delay cannot be lower than minimum delay")
    return buffer_raw, max_offer_raw, duration_days_raw, str(offers_min), str(offers_max), delay_min_raw, delay_max_raw


def get_domain_accept_offer_menu_input() -> Optional[Tuple[str, str]]:
    print("\nAccept received top domain offers:")
    delay_min_raw = input(f"Minimum delay between accepts sec [{DOMAIN_LISTING_DEFAULT_DELAY_MIN_SEC}]: ").strip()
    delay_max_raw = input(f"Maximum delay between accepts sec [{DOMAIN_LISTING_DEFAULT_DELAY_MAX_SEC}]: ").strip()
    if not delay_min_raw:
        delay_min_raw = _format_decimal_plain(DOMAIN_LISTING_DEFAULT_DELAY_MIN_SEC)
    if not delay_max_raw:
        delay_max_raw = _format_decimal_plain(DOMAIN_LISTING_DEFAULT_DELAY_MAX_SEC)
    delay_min = _parse_decimal_input(delay_min_raw)
    delay_max = _parse_decimal_input(delay_max_raw)
    if delay_min < 0 or delay_max < 0:
        raise ValueError("Accept delays cannot be negative")
    if delay_max < delay_min:
        raise ValueError("Maximum accept delay cannot be lower than minimum delay")
    return delay_min_raw, delay_max_raw


def get_domain_liquidity_menu_input() -> Optional[Tuple[str, str, str, str, str]]:
    print("\nCreate full-range liquidity positions:")
    print("Pool selection:")
    print("1) Random from top 10 by TVL")
    print("2) WETH/USDC.E")
    pool_mode_raw = input("Select [1-2, default 1]: ").strip() or "1"
    if pool_mode_raw not in {"1", "2"}:
        raise ValueError("Invalid liquidity pool selection")
    min_usd_raw = input("Minimum total liquidity USD [5]: ").strip() or "5"
    max_usd_raw = input("Maximum total liquidity USD [5.5]: ").strip() or "5.5"
    delay_min_raw = input(f"Minimum delay between wallets sec [{DOMAIN_LISTING_DEFAULT_DELAY_MIN_SEC}]: ").strip()
    delay_max_raw = input(f"Maximum delay between wallets sec [{DOMAIN_LISTING_DEFAULT_DELAY_MAX_SEC}]: ").strip()
    if not delay_min_raw:
        delay_min_raw = _format_decimal_plain(DOMAIN_LISTING_DEFAULT_DELAY_MIN_SEC)
    if not delay_max_raw:
        delay_max_raw = _format_decimal_plain(DOMAIN_LISTING_DEFAULT_DELAY_MAX_SEC)
    min_usd = _parse_decimal_input(min_usd_raw)
    max_usd = _parse_decimal_input(max_usd_raw)
    delay_min = _parse_decimal_input(delay_min_raw)
    delay_max = _parse_decimal_input(delay_max_raw)
    if min_usd <= 0 or max_usd <= 0:
        raise ValueError("Liquidity USD amounts must be > 0")
    if max_usd < min_usd:
        raise ValueError("Maximum liquidity USD cannot be lower than minimum")
    if delay_min < 0 or delay_max < 0:
        raise ValueError("Liquidity delays cannot be negative")
    if delay_max < delay_min:
        raise ValueError("Maximum liquidity delay cannot be lower than minimum delay")
    return pool_mode_raw, min_usd_raw, max_usd_raw, delay_min_raw, delay_max_raw


def _random_decimal_between(min_value: Decimal, max_value: Decimal, min_raw: str, max_raw: str) -> Decimal:
    if min_value == max_value:
        return min_value
    precision = max(_decimal_places_from_raw(min_raw), _decimal_places_from_raw(max_raw), 2)
    precision = min(precision, 8)
    step = Decimal(1).scaleb(-precision)
    min_units = int((min_value / step).to_integral_value(rounding=ROUND_CEILING))
    max_units = int((max_value / step).to_integral_value(rounding=ROUND_FLOOR))
    if max_units < min_units:
        return min_value
    return (Decimal(random.randint(min_units, max_units)) * step).quantize(step)

def _random_listing_price(min_price: Decimal, max_price: Decimal) -> Decimal:
    if min_price == max_price:
        return min_price.quantize(Decimal("0.1"))
    spread = max_price - min_price
    price = min_price + spread * Decimal(str(random.random()))
    return price.quantize(Decimal("0.1"))


def _select_domains_for_listing(unlisted_domains: List[OwnedDomain], count_mode: str, count_min: int, count_max: int) -> List[OwnedDomain]:
    if count_mode == "1":
        return unlisted_domains
    count = random.randint(count_min, count_max)
    count = min(count, len(unlisted_domains))
    if count >= len(unlisted_domains):
        return unlisted_domains
    return random.sample(unlisted_domains, count)


def _doma_orderbook_base_url(cfg: BotConfig) -> str:
    base = (cfg.doma_api_url or "https://api.doma.xyz/graphql").strip().rstrip("/")
    if base.endswith("/graphql"):
        base = base[: -len("/graphql")]
    return base or "https://api.doma.xyz"


def _first_doma_api_key(cfg: BotConfig) -> str:
    for key in [cfg.doma_api_key, *cfg.doma_api_keys, *cfg.file_api_keys]:
        key = (key or "").strip()
        if key:
            return key
    return ""


def _listing_currency_address(cfg: BotConfig) -> str:
    address = cfg.token_address_overrides.get("USDC.E") or cfg.token_address_overrides.get("USDC")
    if not address:
        raise ValueError("USDC.E token address is missing in contracts.json")
    return Web3.to_checksum_address(address)


def _domain_listing_helper_path() -> Path:
    return Path(__file__).with_name("doma_list_domain.mjs")


def _domain_cancel_listing_helper_path() -> Path:
    return Path(__file__).with_name("doma_cancel_listing.mjs")


def _domain_place_offer_helper_path() -> Path:
    return Path(__file__).with_name("doma_place_offer.mjs")


def _domain_accept_offer_helper_path() -> Path:
    return Path(__file__).with_name("doma_accept_offer.mjs")


def _domain_buy_helper_path() -> Path:
    return Path(__file__).with_name("doma_buy_domain.mjs")


def _domain_listing_loader_path() -> Path:
    return Path(__file__).with_name("doma_node_esm_loader.mjs")


def _chain_id_from_network_id(network_id: str, fallback_chain_id: int) -> int:
    match = re.fullmatch(r"eip155:(\d+)", (network_id or "").strip())
    return int(match.group(1)) if match else fallback_chain_id


def _unique_nonempty(values: List[str]) -> List[str]:
    out: List[str] = []
    for value in values:
        item = (value or "").strip()
        if item and item not in out:
            out.append(item)
    return out


def _base_rpc_candidates() -> List[str]:
    env_values: List[str] = []
    for key in ("BASE_RPC_URL", "BASE_RPC_URLS"):
        raw = os.getenv(key, "")
        if raw:
            env_values.extend(x.strip() for x in raw.split(","))
    return _unique_nonempty([*env_values, *BASE_RPC_FALLBACK_URLS])


def _supported_listing_chain_ids(cfg: BotConfig) -> set[int]:
    return {cfg.chain_id, BASE_CHAIN_ID}


def _listing_network_filter_chain_ids(cfg: BotConfig, network_mode_raw: str) -> set[int]:
    if network_mode_raw == "1":
        return {cfg.chain_id}
    if network_mode_raw == "2":
        return {BASE_CHAIN_ID}
    return _supported_listing_chain_ids(cfg)


def _listing_network_label(cfg: BotConfig, network_mode_raw: str) -> str:
    if network_mode_raw == "1":
        return f"Doma/eip155:{cfg.chain_id}"
    if network_mode_raw == "2":
        return f"Base/eip155:{BASE_CHAIN_ID}"
    return "any supported"


def _listing_network_chain_ids(cfg: BotConfig, network_mode_raw: str) -> List[int]:
    if network_mode_raw == "1":
        return [cfg.chain_id]
    if network_mode_raw == "2":
        return [BASE_CHAIN_ID]
    return sorted(_supported_listing_chain_ids(cfg))


def _listing_chain_context_from_network(cfg: BotConfig, network_id: str) -> Tuple[int, str, List[str]]:
    chain_id = _chain_id_from_network_id(network_id, cfg.chain_id)
    if chain_id == BASE_CHAIN_ID:
        rpc_urls = _base_rpc_candidates()
    else:
        rpc_urls = _doma_rpc_candidates(cfg)
    if not rpc_urls:
        raise ValueError(f"No RPC URL configured for chain_id={chain_id}")
    return chain_id, rpc_urls[0], rpc_urls


def _listing_chain_context(cfg: BotConfig, domain: OwnedDomain) -> Tuple[int, str, List[str]]:
    return _listing_chain_context_from_network(cfg, domain.network_id)


def _fetch_wallet_domain_listings_for_network_mode(
    doma_api: DomaApiClient,
    wallet: str,
    cfg: BotConfig,
    network_mode_raw: str,
) -> List[DomainListing]:
    out: List[DomainListing] = []
    seen: set[str] = set()
    for chain_id in _listing_network_chain_ids(cfg, network_mode_raw):
        for listing in doma_api.fetch_wallet_domain_listings(wallet, chain_id=chain_id):
            if listing.order_id in seen:
                continue
            seen.add(listing.order_id)
            out.append(listing)
    return out


def _listing_price_decimal(listing: DomainListing) -> Decimal:
    return raw_to_decimal(int(listing.price_raw or "0"), listing.currency_decimals or 0)


def _fetch_cheapest_domain_listings_for_network_mode(
    doma_api: DomaApiClient,
    cfg: BotConfig,
    network_mode_raw: str,
    max_price_usdc: Decimal,
    take: int = 100,
    max_pages: int = 5,
) -> List[DomainListing]:
    out: List[DomainListing] = []
    seen: set[str] = set()
    for chain_id in _listing_network_chain_ids(cfg, network_mode_raw):
        for listing in doma_api.fetch_cheapest_domain_listings(
            chain_id=chain_id,
            take=take,
            max_pages=max_pages,
            max_price_usd=max_price_usdc,
        ):
            if listing.order_id in seen:
                continue
            if "usdc" not in (listing.currency_symbol or "").lower():
                continue
            if _listing_price_decimal(listing) > max_price_usdc:
                continue
            seen.add(listing.order_id)
            out.append(listing)
    out.sort(key=_listing_price_decimal)
    return out


def _wait_for_domain_claim(
    doma_api: DomaApiClient,
    logger: logging.Logger,
    *,
    wallet: str,
    domain_name: str,
    timeout_sec: int = DOMAIN_PURCHASE_CLAIM_WAIT_TIMEOUT_SEC,
    interval_sec: int = DOMAIN_PURCHASE_CLAIM_WAIT_INTERVAL_SEC,
) -> Tuple[bool, str]:
    expected_suffix = f":{wallet.lower()}"
    deadline = time.time() + max(0, timeout_sec)
    last_claimed_by = ""
    while True:
        try:
            last_claimed_by = doma_api.fetch_domain_claimed_by(domain_name)
        except Exception as exc:
            logger.warning("[BUY_DOMAIN] wallet=%s domain=%s claim status check failed: %s", wallet, domain_name, exc)
            last_claimed_by = ""
        if last_claimed_by.lower().endswith(expected_suffix):
            logger.info("[BUY_DOMAIN] wallet=%s domain=%s claimed", wallet, domain_name)
            return True, last_claimed_by
        if time.time() >= deadline:
            logger.warning(
                "[BUY_DOMAIN] wallet=%s domain=%s claim not confirmed before timeout | claimed_by=%s",
                wallet,
                domain_name,
                last_claimed_by or "unknown",
            )
            return False, last_claimed_by
        logger.info(
            "[BUY_DOMAIN] wallet=%s domain=%s waiting registrar claim | claimed_by=%s | next_check=%ss",
            wallet,
            domain_name,
            last_claimed_by or "unknown",
            interval_sec,
        )
        time.sleep(max(1, interval_sec))


def _run_domain_listing_helper(
    cfg: BotConfig,
    logger: logging.Logger,
    wallet: str,
    private_key: str,
    domain: OwnedDomain,
    price: Decimal,
    duration_days: Decimal,
    proxy: Optional[str],
) -> Tuple[bool, str, str]:
    helper_path = _domain_listing_helper_path()
    if not helper_path.exists():
        raise FileNotFoundError(f"Listing helper not found: {helper_path}")
    loader_path = _domain_listing_loader_path()
    if not loader_path.exists():
        raise FileNotFoundError(f"Node ESM loader not found: {loader_path}")
    price_raw = decimal_to_raw(price, 6)
    if price_raw <= 0:
        raise ValueError(f"Listing price is too small after USDC.E conversion: {price}")
    duration_ms = int(duration_days * Decimal("86400000"))
    chain_id, rpc_url, rpc_urls = _listing_chain_context(cfg, domain)
    payload = {
        "chainId": chain_id,
        "rpcUrl": rpc_url,
        "rpcUrls": rpc_urls,
        "privateKey": private_key,
        "contract": domain.token_address,
        "tokenId": domain.token_id,
        "priceRaw": str(price_raw),
        "currencyContractAddress": _listing_currency_address(cfg),
        "durationMs": duration_ms,
        "source": DOMAIN_LISTING_SOURCE,
        "orderbookBaseUrl": _doma_orderbook_base_url(cfg),
        "apiKey": _first_doma_api_key(cfg),
        "proxy": proxy or "",
        "timeoutMs": 120000,
    }
    env = dict(os.environ)
    if proxy:
        env["HTTP_PROXY"] = proxy
        env["HTTPS_PROXY"] = proxy
    env["NODE_NO_WARNINGS"] = "1"
    try:
        result = subprocess.run(
            ["node", "--loader", loader_path.as_uri(), str(helper_path)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            cwd=str(helper_path.parent),
            env=env,
            timeout=150,
        )
    except subprocess.TimeoutExpired as exc:
        partial = "\n".join(str(x or "").strip() for x in (exc.stderr, exc.stdout) if x)
        return False, "", f"listing helper timed out after 150 seconds; output={partial[:1000]}"
    for line in result.stderr.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except Exception:
            logger.info("[LIST] wallet=%s domain=%s helper: %s", wallet, domain.name, line)
            continue
        if event.get("type") == "progress":
            tx_hashes = event.get("tx_hashes") or ""
            if tx_hashes:
                logger.info(
                    "[LIST] wallet=%s domain=%s progress | action=%s state=%s tx=%s",
                    wallet,
                    domain.name,
                    event.get("action") or "",
                    event.get("state") or event.get("status") or "",
                    tx_hashes,
                )
        elif event.get("type") == "rpc_retry":
            logger.info(
                "[LIST] wallet=%s domain=%s rpc retry | url=%s | error=%s",
                wallet,
                domain.name,
                event.get("rpc_url") or "",
                event.get("error") or "",
            )
        elif not event.get("ok", True):
            logger.warning("[LIST] wallet=%s domain=%s helper error: %s", wallet, domain.name, event.get("error"))
    if result.returncode != 0:
        return False, "", (result.stderr or result.stdout or f"node exited with {result.returncode}").strip()
    data = None
    for line in reversed(result.stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
            break
        except Exception:
            continue
    if data is None:
        return False, "", f"failed to parse helper output; stdout={result.stdout.strip()}"
    if not data.get("ok"):
        return False, "", str(data.get("error") or "unknown helper error")
    orders = (data.get("result") or {}).get("orders") or []
    order_id = str((orders[0] or {}).get("orderId") or "") if orders else ""
    return True, order_id, ""


def _run_domain_cancel_listing_helper(
    cfg: BotConfig,
    logger: logging.Logger,
    wallet: str,
    private_key: str,
    listing: DomainListing,
    cancellation_type: str,
    proxy: Optional[str],
) -> Tuple[bool, str, str]:
    helper_path = _domain_cancel_listing_helper_path()
    if not helper_path.exists():
        raise FileNotFoundError(f"Cancel listing helper not found: {helper_path}")
    loader_path = _domain_listing_loader_path()
    if not loader_path.exists():
        raise FileNotFoundError(f"Node ESM loader not found: {loader_path}")
    chain_id, rpc_url, _ = _listing_chain_context_from_network(cfg, listing.network_id)
    payload = {
        "chainId": chain_id,
        "rpcUrl": rpc_url,
        "privateKey": private_key,
        "orderId": listing.order_id,
        "cancellationType": cancellation_type,
        "source": DOMAIN_LISTING_SOURCE,
        "orderbookBaseUrl": _doma_orderbook_base_url(cfg),
        "apiKey": _first_doma_api_key(cfg),
        "proxy": proxy or "",
        "timeoutMs": 120000,
    }
    env = dict(os.environ)
    if proxy:
        env["HTTP_PROXY"] = proxy
        env["HTTPS_PROXY"] = proxy
    env["NODE_NO_WARNINGS"] = "1"
    try:
        result = subprocess.run(
            ["node", "--loader", loader_path.as_uri(), str(helper_path)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            cwd=str(helper_path.parent),
            env=env,
            timeout=150,
        )
    except subprocess.TimeoutExpired as exc:
        partial = "\n".join(str(x or "").strip() for x in (exc.stderr, exc.stdout) if x)
        return False, "", f"cancel listing helper timed out after 150 seconds; output={partial[:1000]}"
    for line in result.stderr.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except Exception:
            logger.info("[DELIST] wallet=%s domain=%s helper: %s", wallet, listing.name, line)
            continue
        if event.get("type") == "progress":
            tx_hashes = event.get("tx_hashes") or ""
            logger.info(
                "[DELIST] wallet=%s domain=%s progress | action=%s state=%s tx=%s",
                wallet,
                listing.name,
                event.get("action") or "",
                event.get("state") or event.get("status") or "",
                tx_hashes,
            )
        elif not event.get("ok", True):
            logger.warning("[DELIST] wallet=%s domain=%s helper error: %s", wallet, listing.name, event.get("error"))
    if result.returncode != 0:
        return False, "", (result.stderr or result.stdout or f"node exited with {result.returncode}").strip()
    data = None
    for line in reversed(result.stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
            break
        except Exception:
            continue
    if data is None:
        return False, "", f"failed to parse helper output; stdout={result.stdout.strip()}"
    if not data.get("ok"):
        return False, "", str(data.get("error") or "unknown helper error")
    tx_hash = str(((data.get("result") or {}).get("transactionHash")) or "")
    return True, tx_hash, ""


def _run_domain_place_offer_helper(
    cfg: BotConfig,
    logger: logging.Logger,
    wallet: str,
    private_key: str,
    domain: DomainOfferCandidate,
    offer_amount: Decimal,
    duration_days: Decimal,
    proxy: Optional[str],
) -> Tuple[bool, str, str]:
    helper_path = _domain_place_offer_helper_path()
    if not helper_path.exists():
        raise FileNotFoundError(f"Place offer helper not found: {helper_path}")
    loader_path = _domain_listing_loader_path()
    if not loader_path.exists():
        raise FileNotFoundError(f"Node ESM loader not found: {loader_path}")
    price_raw = int((offer_amount * (Decimal(10) ** 6)).to_integral_value(rounding=ROUND_CEILING))
    if price_raw <= 0:
        raise ValueError(f"Offer amount is too small after USDC.E conversion: {offer_amount}")
    duration_ms = int(duration_days * Decimal("86400000"))
    payload = {
        "chainId": cfg.chain_id,
        "rpcUrl": cfg.rpc_url,
        "rpcUrls": _doma_rpc_candidates(cfg),
        "privateKey": private_key,
        "contract": domain.token_address,
        "tokenId": domain.token_id,
        "priceRaw": str(price_raw),
        "currencyContractAddress": _listing_currency_address(cfg),
        "durationMs": duration_ms,
        "source": DOMAIN_LISTING_SOURCE,
        "orderbookBaseUrl": _doma_orderbook_base_url(cfg),
        "apiKey": _first_doma_api_key(cfg),
        "proxy": proxy or "",
        "timeoutMs": 120000,
    }
    env = dict(os.environ)
    if proxy:
        env["HTTP_PROXY"] = proxy
        env["HTTPS_PROXY"] = proxy
    env["NODE_NO_WARNINGS"] = "1"
    try:
        result = subprocess.run(
            ["node", "--loader", loader_path.as_uri(), str(helper_path)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            cwd=str(helper_path.parent),
            env=env,
            timeout=150,
        )
    except subprocess.TimeoutExpired as exc:
        partial = "\n".join(str(x or "").strip() for x in (exc.stderr, exc.stdout) if x)
        return False, "", f"place offer helper timed out after 150 seconds; output={partial[:1000]}"
    for line in result.stderr.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except Exception:
            logger.info("[OFFER] wallet=%s domain=%s helper: %s", wallet, domain.name, line)
            continue
        if event.get("type") == "progress":
            tx_hashes = event.get("tx_hashes") or ""
            if tx_hashes:
                logger.info(
                    "[OFFER] wallet=%s domain=%s progress | action=%s state=%s tx=%s",
                    wallet,
                    domain.name,
                    event.get("action") or "",
                    event.get("state") or event.get("status") or "",
                    tx_hashes,
                )
        elif event.get("type") == "rpc_retry":
            logger.info(
                "[OFFER] wallet=%s domain=%s rpc retry | url=%s | error=%s",
                wallet,
                domain.name,
                event.get("rpc_url") or "",
                event.get("error") or "",
            )
        elif not event.get("ok", True):
            logger.warning("[OFFER] wallet=%s domain=%s helper error: %s", wallet, domain.name, event.get("error"))
    if result.returncode != 0:
        return False, "", (result.stderr or result.stdout or f"node exited with {result.returncode}").strip()
    data = None
    for line in reversed(result.stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
            break
        except Exception:
            continue
    if data is None:
        return False, "", f"failed to parse helper output; stdout={result.stdout.strip()}"
    if not data.get("ok"):
        return False, "", str(data.get("error") or "unknown helper error")
    orders = (data.get("result") or {}).get("orders") or []
    order_id = str((orders[0] or {}).get("orderId") or "") if orders else ""
    return True, order_id, ""


def _run_domain_accept_offer_helper(
    cfg: BotConfig,
    logger: logging.Logger,
    wallet: str,
    private_key: str,
    offer: DomainReceivedOffer,
    proxy: Optional[str],
) -> Tuple[bool, str, str]:
    helper_path = _domain_accept_offer_helper_path()
    if not helper_path.exists():
        raise FileNotFoundError(f"Accept offer helper not found: {helper_path}")
    loader_path = _domain_listing_loader_path()
    if not loader_path.exists():
        raise FileNotFoundError(f"Node ESM loader not found: {loader_path}")
    payload = {
        "chainId": cfg.chain_id,
        "rpcUrl": cfg.rpc_url,
        "rpcUrls": _doma_rpc_candidates(cfg),
        "privateKey": private_key,
        "orderId": offer.order_id,
        "source": DOMAIN_LISTING_SOURCE,
        "orderbookBaseUrl": _doma_orderbook_base_url(cfg),
        "apiKey": _first_doma_api_key(cfg),
        "proxy": proxy or "",
    }
    env = dict(os.environ)
    if proxy:
        env["HTTP_PROXY"] = proxy
        env["HTTPS_PROXY"] = proxy
    env["NODE_NO_WARNINGS"] = "1"
    result = subprocess.run(
        ["node", "--loader", loader_path.as_uri(), str(helper_path)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=str(helper_path.parent),
        env=env,
        timeout=600,
    )
    for line in result.stderr.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except Exception:
            logger.info("[ACCEPT_OFFER] wallet=%s domain=%s helper: %s", wallet, offer.name, line)
            continue
        if event.get("type") == "progress":
            tx_hashes = event.get("tx_hashes") or ""
            if tx_hashes:
                logger.info(
                    "[ACCEPT_OFFER] wallet=%s domain=%s progress | action=%s state=%s tx=%s",
                    wallet,
                    offer.name,
                    event.get("action") or "",
                    event.get("state") or event.get("status") or "",
                    tx_hashes,
                )
        elif event.get("type") == "rpc_retry":
            logger.info(
                "[ACCEPT_OFFER] wallet=%s domain=%s rpc retry | url=%s | error=%s",
                wallet,
                offer.name,
                event.get("rpc_url") or "",
                event.get("error") or "",
            )
        elif not event.get("ok", True):
            logger.warning("[ACCEPT_OFFER] wallet=%s domain=%s helper error: %s", wallet, offer.name, event.get("error"))
    if result.returncode != 0:
        return False, "", (result.stderr or result.stdout or f"node exited with {result.returncode}").strip()
    try:
        data = json.loads(result.stdout.splitlines()[-1])
    except Exception as exc:
        return False, "", f"failed to parse helper output: {exc}; stdout={result.stdout.strip()}"
    if not data.get("ok"):
        return False, "", str(data.get("error") or "unknown helper error")
    tx_hash = str(((data.get("result") or {}).get("transactionHash")) or "")
    status = str(((data.get("result") or {}).get("status")) or "")
    if status and status != "success":
        return False, tx_hash, f"transaction status={status}"
    return True, tx_hash, ""


def _run_domain_buy_helper(
    cfg: BotConfig,
    logger: logging.Logger,
    wallet: str,
    private_key: str,
    listing: DomainListing,
    proxy: Optional[str],
) -> Tuple[bool, str, str]:
    helper_path = _domain_buy_helper_path()
    if not helper_path.exists():
        raise FileNotFoundError(f"Buy domain helper not found: {helper_path}")
    loader_path = _domain_listing_loader_path()
    if not loader_path.exists():
        raise FileNotFoundError(f"Node ESM loader not found: {loader_path}")
    chain_id, rpc_url, rpc_urls = _listing_chain_context_from_network(cfg, listing.network_id)
    payload = {
        "chainId": chain_id,
        "rpcUrl": rpc_url,
        "rpcUrls": rpc_urls,
        "privateKey": private_key,
        "orderId": listing.order_id,
        "source": DOMAIN_LISTING_SOURCE,
        "orderbookBaseUrl": _doma_orderbook_base_url(cfg),
        "apiKey": _first_doma_api_key(cfg),
        "proxy": proxy or "",
        "timeoutMs": 120000,
    }
    env = dict(os.environ)
    if proxy:
        env["HTTP_PROXY"] = proxy
        env["HTTPS_PROXY"] = proxy
    env["NODE_NO_WARNINGS"] = "1"
    try:
        result = subprocess.run(
            ["node", "--loader", loader_path.as_uri(), str(helper_path)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            cwd=str(helper_path.parent),
            env=env,
            timeout=180,
        )
    except subprocess.TimeoutExpired as exc:
        partial = "\n".join(str(x or "").strip() for x in (exc.stderr, exc.stdout) if x)
        return False, "", f"buy helper timed out after 180 seconds; output={partial[:1000]}"
    for line in result.stderr.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except Exception:
            logger.info("[BUY_DOMAIN] wallet=%s domain=%s helper: %s", wallet, listing.name, line)
            continue
        if event.get("type") == "progress":
            tx_hashes = event.get("tx_hashes") or ""
            if tx_hashes:
                logger.info(
                    "[BUY_DOMAIN] wallet=%s domain=%s progress | action=%s state=%s tx=%s",
                    wallet,
                    listing.name,
                    event.get("action") or "",
                    event.get("state") or event.get("status") or "",
                    tx_hashes,
                )
        elif event.get("type") == "rpc_retry":
            logger.info(
                "[BUY_DOMAIN] wallet=%s domain=%s rpc retry | url=%s | error=%s",
                wallet,
                listing.name,
                event.get("rpc_url") or "",
                event.get("error") or "",
            )
        elif not event.get("ok", True):
            logger.warning("[BUY_DOMAIN] wallet=%s domain=%s helper error: %s", wallet, listing.name, event.get("error"))
    if result.returncode != 0:
        return False, "", (result.stderr or result.stdout or f"node exited with {result.returncode}").strip()
    data = None
    for line in reversed(result.stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
            break
        except Exception:
            continue
    if data is None:
        return False, "", f"failed to parse helper output; stdout={result.stdout.strip()}"
    if not data.get("ok"):
        return False, "", str(data.get("error") or "unknown helper error")
    tx_hash = str(((data.get("result") or {}).get("transactionHash")) or "")
    status = str(((data.get("result") or {}).get("status")) or "")
    if status and status != "success":
        return False, tx_hash, f"transaction status={status}"
    return True, tx_hash, ""


def _eligible_unlisted_domains(all_domains: List[OwnedDomain], listed_domains: List[OwnedDomain], chain_id: int) -> List[OwnedDomain]:
    out, _ = _eligible_unlisted_domains_with_reasons(all_domains, listed_domains, chain_id)
    return out


def _eligible_unlisted_domains_with_reasons(
    all_domains: List[OwnedDomain],
    listed_domains: List[OwnedDomain],
    chain_id: int,
    supported_chain_ids: Optional[set[int]] = None,
) -> Tuple[List[OwnedDomain], Dict[str, Any]]:
    listed_names = {d.name.lower() for d in listed_domains}
    allowed_chain_ids = supported_chain_ids or {chain_id}
    out: List[OwnedDomain] = []
    seen: set[str] = set()
    skipped = {
        "listed": 0,
        "duplicate": 0,
        "wrong_network": 0,
        "orderbook_disabled": 0,
        "examples": [],
    }
    def add_example(domain: OwnedDomain, reason: str) -> None:
        examples = skipped["examples"]
        if isinstance(examples, list) and len(examples) < 5:
            examples.append(f"{domain.name}:{reason}")

    for domain in all_domains:
        if domain.name.lower() in listed_names:
            skipped["listed"] += 1
            add_example(domain, "listed")
            continue
        if domain.name.lower() in seen:
            skipped["duplicate"] += 1
            add_example(domain, "duplicate")
            continue
        seen.add(domain.name.lower())
        if domain.network_id and _chain_id_from_network_id(domain.network_id, chain_id) not in allowed_chain_ids:
            skipped["wrong_network"] += 1
            add_example(domain, f"wrong_network:{domain.network_id}")
            continue
        if domain.orderbook_disabled:
            skipped["orderbook_disabled"] += 1
            add_example(domain, "orderbook_disabled")
            continue
        out.append(domain)
    return out, skipped


def _eligible_bridge_domains(
    all_domains: List[OwnedDomain],
    listed_domains: List[OwnedDomain],
    chain_id: int,
    selection: str,
) -> List[OwnedDomain]:
    target_network = f"eip155:{chain_id}"
    listed_names = {d.name.lower() for d in listed_domains}
    source_domains = listed_domains if selection == "listed" else all_domains
    out: List[OwnedDomain] = []
    seen: set[str] = set()
    for domain in source_domains:
        name_key = domain.name.lower()
        if name_key in seen:
            continue
        seen.add(name_key)
        if domain.network_id and domain.network_id != target_network:
            continue
        if selection == "unlisted" and name_key in listed_names:
            continue
        out.append(domain)
    return out


def run_domain_listing_once(cfg: BotConfig, logger: logging.Logger, state: BotState) -> None:
    _ = state
    picked = get_domain_listing_menu_input()
    if not picked:
        logger.info("[LIST] canceled by user.")
        return
    (
        min_raw,
        max_raw,
        duration_raw,
        network_mode_raw,
        count_mode_raw,
        count_min_raw,
        count_max_raw,
        delay_min_raw,
        delay_max_raw,
    ) = picked
    min_price = _parse_decimal_input(min_raw)
    max_price = _parse_decimal_input(max_raw)
    duration_days = _parse_decimal_input(duration_raw)
    count_min = int(_parse_decimal_input(count_min_raw)) if count_min_raw else 0
    count_max = int(_parse_decimal_input(count_max_raw)) if count_max_raw else 0
    listing_delay_min = float(_parse_decimal_input(delay_min_raw))
    listing_delay_max = float(_parse_decimal_input(delay_max_raw))

    wallet_key_records = _build_wallet_key_records(cfg, logger, "LIST")
    if not wallet_key_records:
        raise ValueError(
            "No wallet/private-key pairs available for domain listing "
            "(fill wallets.txt + keys.txt line-by-line or set valid PRIVATE_KEY in .env)"
        )
    wallet_key_records, wallet_start_offset, total_loaded_wallets = _apply_wallet_start_selection(wallet_key_records)

    listing_csv = cfg.trades_csv_file.parent / DOMAIN_LISTING_CSV.name
    ensure_csv(
        listing_csv,
        [
            "timestamp_utc",
            "status",
            "wallet",
            "domain",
            "price_usdce",
            "duration_days",
            "token_address",
            "token_id",
            "order_id",
            "reason",
        ],
        delimiter=cfg.csv_delimiter,
    )

    logger.info(
        "[LIST] mode started | wallets=%s | start_wallet=%s | network=%s | price=%s-%s USDC.E | duration=%s days | count=%s | delay=%s-%s sec | currency=USDC.E",
        len(wallet_key_records),
        wallet_start_offset + 1,
        _listing_network_label(cfg, network_mode_raw),
        _format_decimal_plain(min_price),
        _format_decimal_plain(max_price),
        _format_decimal_plain(duration_days),
        "all" if count_mode_raw == "1" else f"{count_min}-{count_max}",
        _format_decimal_plain(_parse_decimal_input(delay_min_raw)),
        _format_decimal_plain(_parse_decimal_input(delay_max_raw)),
    )
    success_wallets = 0
    failed_wallets = 0
    skipped_wallets = 0
    failed_wallet_addresses: List[str] = []
    skipped_wallet_details: List[str] = []

    def _print_list_summary() -> None:
        _print_mode_summary(
            "LIST",
            len(wallet_key_records),
            success_wallets,
            failed_wallets,
            skipped_wallets,
            failed_wallet_addresses,
        )
        if skipped_wallet_details:
            print(_color("[LIST] skipped wallets:", ANSI_YELLOW))
            logger.info("[LIST] skipped wallets:")
            for detail in skipped_wallet_details:
                line = f"[LIST] skipped | {detail}"
                print(_color(line, ANSI_YELLOW))
                logger.info(line)

    interrupted = False
    for idx, (line_idx, wallet, private_key) in enumerate(wallet_key_records):
        wallet_number = line_idx + 1
        try:
            proxies, skip_wallet = _proxy_for_line(cfg, line_idx, logger, "LIST")
            proxy = (proxies or {}).get("https") or (proxies or {}).get("http") or ""
            if skip_wallet:
                skipped_wallets += 1
                skipped_wallet_details.append(
                    f"wallet#{wallet_number}/{total_loaded_wallets} | wallet={wallet} | reason=no proxy on matching line"
                )
                continue
            logger.info(
                "[LIST] wallet %s",
                _wallet_record_progress_label(idx, len(wallet_key_records), line_idx, total_loaded_wallets, wallet),
            )
            doma_api = DomaApiClient(
                cfg.doma_api_url,
                api_key=cfg.doma_api_key,
                api_keys=cfg.doma_api_keys,
                proxies=proxies,
            )
            all_domains = doma_api.fetch_owned_domains(wallet, chain_id=cfg.chain_id, listed=None)
            listed_domains = doma_api.fetch_owned_domains(wallet, chain_id=cfg.chain_id, listed=True)
            unlisted_domains, unlisted_skip_reasons = _eligible_unlisted_domains_with_reasons(
                all_domains,
                listed_domains,
                cfg.chain_id,
                supported_chain_ids=_listing_network_filter_chain_ids(cfg, network_mode_raw),
            )
            if not unlisted_domains:
                skipped_wallets += 1
                skipped_wallet_details.append(
                    f"wallet {wallet_number}/{total_loaded_wallets} | wallet={wallet} | reason=no eligible unlisted domains | owned={len(all_domains)} listed={len(listed_domains)} | skipped={unlisted_skip_reasons}"
                )
                logger.info(
                    "[LIST] wallet=%s no eligible unlisted domains | owned=%s listed=%s | skipped=%s",
                    wallet,
                    len(all_domains),
                    len(listed_domains),
                    unlisted_skip_reasons,
                )
                continue
            selected_domains = _select_domains_for_listing(unlisted_domains, count_mode_raw, count_min, count_max)
            logger.info(
                "[LIST] wallet=%s selected=%s/%s eligible unlisted domains | owned=%s listed=%s | proxy=%s",
                wallet,
                len(selected_domains),
                len(unlisted_domains),
                len(all_domains),
                len(listed_domains),
                "yes" if proxy else "no",
            )
            wallet_success = 0
            wallet_failed = 0
            for domain_idx, domain in enumerate(selected_domains, start=1):
                price = _random_listing_price(min_price, max_price)
                logger.info(
                    "[LIST] wallet=%s domain %s/%s %s | network=%s | price=%s USDC.E",
                    wallet,
                    domain_idx,
                    len(selected_domains),
                    domain.name,
                    domain.network_id or f"eip155:{cfg.chain_id}",
                    _format_decimal_plain(price),
                )
                ok, order_id, reason = _run_domain_listing_helper(
                    cfg=cfg,
                    logger=logger,
                    wallet=wallet,
                    private_key=private_key,
                    domain=domain,
                    price=price,
                    duration_days=duration_days,
                    proxy=proxy,
                )
                append_csv(
                    listing_csv,
                    [
                        datetime.now(timezone.utc).isoformat(),
                        "ok" if ok else "failed",
                        wallet,
                        domain.name,
                        _format_decimal_plain(price),
                        _format_decimal_plain(duration_days),
                        domain.token_address,
                        domain.token_id,
                        order_id,
                        reason,
                    ],
                    delimiter=cfg.csv_delimiter,
                )
                if ok:
                    wallet_success += 1
                    logger.info("[LIST] wallet=%s domain=%s listed | order_id=%s", wallet, domain.name, order_id)
                else:
                    wallet_failed += 1
                    logger.warning("[LIST] wallet=%s domain=%s list failed: %s", wallet, domain.name, reason)
                if domain_idx < len(selected_domains):
                    delay_sec = random.uniform(listing_delay_min, listing_delay_max)
                    logger.info("[LIST] delay before next domain: %.2f sec", delay_sec)
                    time.sleep(delay_sec)
            if wallet_success > 0:
                success_wallets += 1
            if wallet_failed > 0:
                failed_wallets += 1
                failed_wallet_addresses.append(wallet)
        except Exception as exc:
            failed_wallets += 1
            failed_wallet_addresses.append(wallet)
            logger.warning("[LIST] wallet=%s failed: %s", wallet, exc)
        except KeyboardInterrupt:
            interrupted = True
            logger.warning("[LIST] interrupted by user at wallet %s/%s | wallet=%s", wallet_number, total_loaded_wallets, wallet)
            break
        if idx < len(wallet_key_records) - 1 and cfg.wallet_delay_max_sec > 0:
            delay_sec = random.uniform(cfg.wallet_delay_min_sec, cfg.wallet_delay_max_sec)
            logger.info("[LIST] delay before next wallet: %.2f sec", delay_sec)
            try:
                time.sleep(delay_sec)
            except KeyboardInterrupt:
                interrupted = True
                logger.warning("[LIST] interrupted by user during delay after wallet %s/%s | wallet=%s", wallet_number, total_loaded_wallets, wallet)
                break

    if interrupted:
        print(_color("[LIST] stopped by user; partial summary:", ANSI_YELLOW))
    _print_list_summary()


def run_domain_delisting_once(cfg: BotConfig, logger: logging.Logger, state: BotState) -> None:
    _ = state
    picked = get_domain_delisting_menu_input()
    if not picked:
        logger.info("[DELIST] canceled by user.")
        return
    cancellation_type, network_mode_raw, delay_min_raw, delay_max_raw = picked
    delisting_delay_min = float(_parse_decimal_input(delay_min_raw))
    delisting_delay_max = float(_parse_decimal_input(delay_max_raw))

    wallet_key_records = _build_wallet_key_records(cfg, logger, "DELIST")
    if not wallet_key_records:
        raise ValueError(
            "No wallet/private-key pairs available for domain delisting "
            "(fill wallets.txt + keys.txt line-by-line or set valid PRIVATE_KEY in .env)"
        )
    wallet_key_records, wallet_start_offset, total_loaded_wallets = _apply_wallet_start_selection(wallet_key_records)

    delisting_csv = cfg.trades_csv_file.parent / DOMAIN_DELISTING_CSV.name
    ensure_csv(
        delisting_csv,
        [
            "timestamp_utc",
            "status",
            "wallet",
            "domain",
            "order_id",
            "cancellation_type",
            "tx_hash",
            "reason",
        ],
        delimiter=cfg.csv_delimiter,
    )

    logger.info(
        "[DELIST] mode started | wallets=%s | start_wallet=%s | network=%s | cancellation=%s | delay=%s-%s sec",
        len(wallet_key_records),
        wallet_start_offset + 1,
        _listing_network_label(cfg, network_mode_raw),
        cancellation_type,
        delay_min_raw,
        delay_max_raw,
    )

    success_wallets = 0
    failed_wallets = 0
    skipped_wallets = 0
    canceled_count = 0
    failed_wallet_addresses: List[str] = []

    for idx, (line_idx, wallet, private_key) in enumerate(wallet_key_records):
        proxies, skip_wallet = _proxy_for_line(cfg, line_idx, logger, "DELIST")
        proxy = (proxies or {}).get("https") or (proxies or {}).get("http") or ""
        if skip_wallet:
            skipped_wallets += 1
            continue
        logger.info(
            "[DELIST] wallet %s",
            _wallet_record_progress_label(idx, len(wallet_key_records), line_idx, total_loaded_wallets, wallet),
        )
        try:
            doma_api = DomaApiClient(
                cfg.doma_api_url,
                api_key=cfg.doma_api_key,
                api_keys=cfg.doma_api_keys,
                proxies=proxies,
            )
            listings = _fetch_wallet_domain_listings_for_network_mode(doma_api, wallet, cfg, network_mode_raw)
            if not listings:
                logger.info(
                    "[DELIST] wallet=%s no active listings | network=%s | proxy=%s",
                    wallet,
                    _listing_network_label(cfg, network_mode_raw),
                    "yes" if proxies else "no",
                )
                continue
            logger.info(
                "[DELIST] wallet=%s active listings=%s | network=%s | proxy=%s",
                wallet,
                len(listings),
                _listing_network_label(cfg, network_mode_raw),
                "yes" if proxies else "no",
            )
            wallet_success = 0
            wallet_failed = 0
            for listing_idx, listing in enumerate(listings, start=1):
                logger.info(
                    "[DELIST] wallet=%s domain %s/%s %s | network=%s | order_id=%s",
                    wallet,
                    listing_idx,
                    len(listings),
                    listing.name,
                    listing.network_id,
                    listing.order_id,
                )
                ok, tx_hash, reason = _run_domain_cancel_listing_helper(
                    cfg=cfg,
                    logger=logger,
                    wallet=wallet,
                    private_key=private_key,
                    listing=listing,
                    cancellation_type=cancellation_type,
                    proxy=proxy,
                )
                append_csv(
                    delisting_csv,
                    [
                        datetime.now(timezone.utc).isoformat(),
                        "ok" if ok else "failed",
                        wallet,
                        listing.name,
                        listing.order_id,
                        cancellation_type,
                        tx_hash,
                        reason,
                    ],
                    delimiter=cfg.csv_delimiter,
                )
                if ok:
                    wallet_success += 1
                    canceled_count += 1
                    logger.info(
                        "[DELIST] wallet=%s domain=%s canceled | order_id=%s tx=%s",
                        wallet,
                        listing.name,
                        listing.order_id,
                        tx_hash,
                    )
                else:
                    wallet_failed += 1
                    logger.warning("[DELIST] wallet=%s domain=%s cancel failed: %s", wallet, listing.name, reason)
                    if _is_proxy_connectivity_text(reason):
                        logger.warning(
                            "[DELIST] wallet=%s proxy failed during cancel, skipping remaining listings for this wallet",
                            wallet,
                        )
                        break
                if listing_idx < len(listings):
                    delay_sec = random.uniform(delisting_delay_min, delisting_delay_max)
                    logger.info("[DELIST] delay before next domain: %.2f sec", delay_sec)
                    time.sleep(delay_sec)
            if wallet_success > 0:
                success_wallets += 1
            if wallet_failed > 0:
                failed_wallets += 1
                failed_wallet_addresses.append(wallet)
        except Exception as exc:
            failed_wallets += 1
            failed_wallet_addresses.append(wallet)
            logger.warning("[DELIST] wallet=%s failed: %s", wallet, exc)
        if idx < len(wallet_key_records) - 1 and cfg.wallet_delay_max_sec > 0:
            delay_sec = random.uniform(cfg.wallet_delay_min_sec, cfg.wallet_delay_max_sec)
            logger.info("[DELIST] delay before next wallet: %.2f sec", delay_sec)
            time.sleep(delay_sec)

    logger.info("[DELIST] canceled listings total=%s", canceled_count)
    _print_mode_summary(
        "DELIST",
        len(wallet_key_records),
        success_wallets,
        failed_wallets,
        skipped_wallets,
        failed_wallet_addresses,
    )


def run_domain_purchase_once(cfg: BotConfig, logger: logging.Logger, state: BotState) -> None:
    picked = get_domain_purchase_menu_input()
    if not picked:
        logger.info("[BUY_DOMAIN] canceled by user.")
        return
    action_raw, max_price_raw, network_mode_raw, min_count_raw, max_count_raw, delay_min_raw, delay_max_raw = picked
    relist_enabled = action_raw == "2"
    max_price = _parse_decimal_input(max_price_raw)
    count_min = int(_parse_decimal_input(min_count_raw))
    count_max = int(_parse_decimal_input(max_count_raw))
    buy_delay_min = float(_parse_decimal_input(delay_min_raw))
    buy_delay_max = float(_parse_decimal_input(delay_max_raw))

    wallet_key_records = _build_wallet_key_records(cfg, logger, "BUY_DOMAIN")
    if not wallet_key_records:
        raise ValueError("No wallet/private-key pairs available for domain purchases")
    wallet_key_records, wallet_start_offset, total_loaded_wallets = _apply_wallet_start_selection(wallet_key_records)
    quote_token = _usdce_token_from_config(cfg)
    weth_token = _token_from_config_override(cfg, "WETH", 18)
    purchase_csv = cfg.trades_csv_file.parent / DOMAIN_PURCHASES_CSV.name
    ensure_csv(
        purchase_csv,
        [
            "timestamp_utc",
            "status",
            "wallet",
            "domain",
            "price_usdce",
            "network_id",
            "token_address",
            "token_id",
            "order_id",
            "tx_hash",
            "relist_price_usdce",
            "relist_order_id",
            "relist_reason",
            "reason",
        ],
        delimiter=cfg.csv_delimiter,
    )

    metadata_proxies: Optional[Dict[str, str]] = None
    for metadata_line_idx, _, _ in wallet_key_records:
        candidate_proxies, skip_metadata_proxy = _proxy_for_line(cfg, metadata_line_idx, None, "BUY_DOMAIN_METADATA")
        if not skip_metadata_proxy:
            metadata_proxies = candidate_proxies
            break
    metadata_api = DomaApiClient(
        cfg.doma_api_url,
        api_keys=[cfg.doma_api_key, *cfg.doma_api_keys, *cfg.file_api_keys],
        proxies=metadata_proxies,
    )
    listings = _fetch_cheapest_domain_listings_for_network_mode(
        metadata_api,
        cfg,
        network_mode_raw,
        max_price_usdc=max_price,
        take=100,
        max_pages=10,
    )
    if not listings:
        raise RuntimeError(f"No active domain listings found below {max_price} USDC.E")

    logger.info(
        "[BUY_DOMAIN] mode started | wallets=%s | start_wallet=%s | network=%s | max_price=%s USDC.E | buy_per_wallet=%s-%s | action=%s | relist_markup=%s | candidates=%s | delay=%s-%s sec",
        total_loaded_wallets,
        wallet_start_offset + 1,
        _listing_network_label(cfg, network_mode_raw),
        _format_decimal_plain(max_price),
        count_min,
        count_max,
        "buy+list" if relist_enabled else "buy-only",
        (
            f"{_format_decimal_plain(DOMAIN_PURCHASE_RELIST_MARKUP_MIN)}-{_format_decimal_plain(DOMAIN_PURCHASE_RELIST_MARKUP_MAX)} USDC.E"
            if relist_enabled
            else "off"
        ),
        len(listings),
        delay_min_raw,
        delay_max_raw,
    )

    used_order_ids: set[str] = set()
    success_wallets = 0
    failed_wallets = 0
    skipped_wallets = 0
    bought_total = 0
    failed_wallet_addresses: List[str] = []

    for idx, (line_idx, wallet, private_key) in enumerate(wallet_key_records, start=1):
        proxies, skip_wallet = _proxy_for_line(cfg, line_idx, logger, "BUY_DOMAIN")
        proxy = (proxies or {}).get("https") or (proxies or {}).get("http") or ""
        wallet_number = line_idx + 1
        logger.info("[BUY_DOMAIN] wallet %s", _wallet_record_progress_label(idx - 1, len(wallet_key_records), line_idx, total_loaded_wallets, wallet))
        if skip_wallet:
            skipped_wallets += 1
            continue
        try:
            doma_api = DomaApiClient(
                cfg.doma_api_url,
                api_keys=[cfg.doma_api_key, *cfg.doma_api_keys, *cfg.file_api_keys],
                proxies=proxies,
            )
            eth_price = _fetch_eth_price_via_doma_quote(cfg, doma_api, quote_token)
            exec_client = _build_exec_client_with_rpc_fallback(
                cfg=cfg,
                logger=logger,
                wallet=wallet,
                private_key=private_key,
                proxies=proxies,
                log_prefix="[BUY_DOMAIN]",
            )

            owner_prefixes = {
                f"eip155:{chain_id}:{wallet.lower()}"
                for chain_id in _listing_network_chain_ids(cfg, network_mode_raw)
            }
            eligible = [
                listing
                for listing in listings
                if listing.order_id not in used_order_ids and listing.offerer_address.lower() not in owner_prefixes
            ]
            buy_count = min(random.randint(count_min, count_max), len(eligible))
            if buy_count <= 0:
                skipped_wallets += 1
                logger.info("[BUY_DOMAIN] wallet=%s no eligible cheapest listings left", wallet)
                continue
            selected = eligible[:buy_count]
            required_usdc = sum((_listing_price_decimal(listing) for listing in selected), Decimal("0")) * Decimal("1.03")
            usdc_balance = exec_client.get_erc20_balance(quote_token.address, quote_token.decimals)
            if usdc_balance < required_usdc:
                topup_ok = _top_up_usdce_from_eth_for_offer(
                    cfg=cfg,
                    logger=logger,
                    state=state,
                    doma_api=doma_api,
                    exec_client=exec_client,
                    quote_token=quote_token,
                    weth_token=weth_token,
                    wallet=wallet,
                    eth_price=eth_price,
                    required_usdc=required_usdc,
                )
                if topup_ok:
                    usdc_balance = exec_client.get_erc20_balance(quote_token.address, quote_token.decimals)
            if usdc_balance < required_usdc:
                skipped_wallets += 1
                logger.warning(
                    "[BUY_DOMAIN] wallet=%s skipped | USDC.E balance below required buy amount (%s < %s)",
                    wallet,
                    _format_decimal_plain(usdc_balance),
                    _format_decimal_plain(required_usdc),
                )
                continue

            wallet_success = 0
            wallet_failed = 0
            for buy_idx, listing in enumerate(selected, start=1):
                price = _listing_price_decimal(listing)
                logger.info(
                    "[BUY_DOMAIN] wallet=%s domain %s/%s %s | price=%s %s | network=%s | order_id=%s",
                    wallet,
                    buy_idx,
                    len(selected),
                    listing.name,
                    _format_decimal_plain(price),
                    listing.currency_symbol or "USDC.E",
                    listing.network_id,
                    listing.order_id,
                )
                ok, tx_hash, reason = _run_domain_buy_helper(
                    cfg=cfg,
                    logger=logger,
                    wallet=wallet,
                    private_key=private_key,
                    listing=listing,
                    proxy=proxy,
                )
                relist_price = Decimal("0")
                relist_order_id = ""
                relist_reason = ""
                claim_ok = False
                claim_status = ""
                if ok and relist_enabled:
                    claim_ok, claim_status = _wait_for_domain_claim(
                        doma_api,
                        logger,
                        wallet=wallet,
                        domain_name=listing.name,
                    )
                elif ok:
                    claim_ok = True
                if ok and relist_enabled and claim_ok:
                    markup = Decimal(random.randint(
                        int(DOMAIN_PURCHASE_RELIST_MARKUP_MIN * Decimal("100")),
                        int(DOMAIN_PURCHASE_RELIST_MARKUP_MAX * Decimal("100")),
                    )) / Decimal("100")
                    relist_price = price + markup
                    owned_domain = OwnedDomain(
                        name=listing.name,
                        token_id=listing.token_id,
                        token_address=listing.token_address,
                        network_id=listing.network_id,
                        owner_address=f"{listing.network_id}:{wallet.lower()}",
                        token_type="OWNERSHIP_TOKEN",
                        orderbook_disabled=False,
                    )
                    logger.info(
                        "[BUY_DOMAIN] wallet=%s domain=%s relist | buy=%s USDC.E | markup=%s | list_price=%s USDC.E",
                        wallet,
                        listing.name,
                        _format_decimal_plain(price),
                        _format_decimal_plain(markup),
                        _format_decimal_plain(relist_price),
                    )
                    relist_ok, relist_order_id, relist_reason = _run_domain_listing_helper(
                        cfg=cfg,
                        logger=logger,
                        wallet=wallet,
                        private_key=private_key,
                        domain=owned_domain,
                        price=relist_price,
                        duration_days=Decimal(str(DOMAIN_LISTING_DEFAULT_DURATION_DAYS)),
                        proxy=proxy,
                    )
                    if relist_ok:
                        logger.info(
                            "[BUY_DOMAIN] wallet=%s domain=%s relisted | price=%s USDC.E | order_id=%s",
                            wallet,
                            listing.name,
                            _format_decimal_plain(relist_price),
                            relist_order_id,
                        )
                    else:
                        logger.warning("[BUY_DOMAIN] wallet=%s domain=%s relist failed: %s", wallet, listing.name, relist_reason)
                elif ok and relist_enabled and not claim_ok:
                    relist_reason = f"domain claim not confirmed: {claim_status or 'unknown'}"
                    logger.warning("[BUY_DOMAIN] wallet=%s domain=%s relist skipped: %s", wallet, listing.name, relist_reason)
                append_csv(
                    purchase_csv,
                    [
                        datetime.now(timezone.utc).isoformat(),
                        (
                            "ok"
                            if ok and claim_ok and (not relist_enabled or relist_order_id)
                            else ("bought_claim_pending" if ok and not claim_ok else ("bought_relist_failed" if ok else "failed"))
                        ),
                        wallet,
                        listing.name,
                        _format_decimal_plain(price),
                        listing.network_id,
                        listing.token_address,
                        listing.token_id,
                        listing.order_id,
                        tx_hash,
                        _format_decimal_plain(relist_price) if relist_price > 0 else "",
                        relist_order_id,
                        relist_reason,
                        reason,
                    ],
                    delimiter=cfg.csv_delimiter,
                )
                used_order_ids.add(listing.order_id)
                if ok:
                    wallet_success += 1
                    bought_total += 1
                    logger.info("[BUY_DOMAIN] wallet=%s domain=%s bought | tx=%s", wallet, listing.name, tx_hash)
                    if relist_enabled and not relist_order_id:
                        wallet_failed += 1
                else:
                    wallet_failed += 1
                    logger.warning("[BUY_DOMAIN] wallet=%s domain=%s buy failed: %s", wallet, listing.name, reason)
                if buy_idx < len(selected):
                    delay_sec = random.uniform(buy_delay_min, buy_delay_max)
                    logger.info("[BUY_DOMAIN] delay before next buy: %.2f sec", delay_sec)
                    time.sleep(delay_sec)
            if wallet_success > 0:
                success_wallets += 1
            if wallet_failed > 0:
                failed_wallets += 1
                failed_wallet_addresses.append(wallet)
        except Exception as exc:
            failed_wallets += 1
            failed_wallet_addresses.append(wallet)
            logger.warning("[BUY_DOMAIN] wallet=%s failed: %s", wallet, exc)
        if idx < len(wallet_key_records):
            delay_sec = random.uniform(buy_delay_min, buy_delay_max)
            logger.info("[BUY_DOMAIN] delay before next wallet: %.2f sec", delay_sec)
            time.sleep(delay_sec)

    logger.info("[BUY_DOMAIN] bought domains total=%s", bought_total)
    _print_mode_summary("BUY_DOMAIN", len(wallet_key_records), success_wallets, failed_wallets, skipped_wallets, failed_wallet_addresses)


def run_domain_place_offer_once(cfg: BotConfig, logger: logging.Logger, state: BotState) -> None:
    picked = get_domain_offer_menu_input()
    if not picked:
        logger.info("[OFFER] canceled by user.")
        return
    buffer_raw, max_offer_raw, duration_days_raw, offers_min_raw, offers_max_raw, delay_min_raw, delay_max_raw = picked
    buffer_amount = _parse_decimal_input(buffer_raw)
    max_offer_amount = _parse_decimal_input(max_offer_raw)
    duration_days = _parse_decimal_input(duration_days_raw)
    offers_min = int(_parse_decimal_input(offers_min_raw))
    offers_max = int(_parse_decimal_input(offers_max_raw))
    offer_delay_min = float(_parse_decimal_input(delay_min_raw))
    offer_delay_max = float(_parse_decimal_input(delay_max_raw))

    wallet_key_records = _build_wallet_key_records(cfg, logger, "OFFER")
    if not wallet_key_records:
        raise ValueError("No wallet/private-key pairs available for domain offers")
    wallet_key_records, wallet_start_offset, total_loaded_wallets = _apply_wallet_start_selection(wallet_key_records)
    quote_token = _usdce_token_from_config(cfg)
    weth_token = _token_from_config_override(cfg, "WETH", 18)
    offer_csv = cfg.trades_csv_file.parent / DOMAIN_OFFERS_CSV.name
    ensure_csv(
        offer_csv,
        [
            "timestamp_utc",
            "status",
            "wallet",
            "domain",
            "offer_usdce",
            "duration_days",
            "token_address",
            "token_id",
            "highest_offer_usdce",
            "order_id",
            "reason",
        ],
        delimiter=cfg.csv_delimiter,
    )

    logger.info(
        "[OFFER] mode started | wallets=%s | start_wallet=%s | offer=%s-%s USDC.E | duration=%s days | offers_per_wallet=%s-%s | delay=%s-%s sec",
        total_loaded_wallets,
        wallet_start_offset + 1,
        _format_decimal_plain(buffer_amount),
        _format_decimal_plain(max_offer_amount),
        _format_decimal_plain(duration_days),
        offers_min,
        offers_max,
        delay_min_raw,
        delay_max_raw,
    )

    success_wallets = 0
    failed_wallets = 0
    skipped_wallets = 0
    placed_total = 0
    failed_wallet_addresses: List[str] = []
    skipped_wallet_details: List[Tuple[int, str, str]] = []

    for idx, (line_idx, wallet, private_key) in enumerate(wallet_key_records, start=1):
        proxies, skip_wallet = _proxy_for_line(cfg, line_idx, logger, "OFFER")
        proxy = (proxies or {}).get("https") or (proxies or {}).get("http") or ""
        wallet_number = line_idx + 1
        logger.info("[OFFER] wallet %s", _wallet_record_progress_label(idx - 1, len(wallet_key_records), line_idx, total_loaded_wallets, wallet))
        if skip_wallet or not proxies:
            skipped_wallets += 1
            skipped_wallet_details.append((wallet_number, wallet, "proxy is required"))
            logger.warning("[OFFER] wallet=%s skipped: proxy is required for domain offers", wallet)
            continue
        try:
            doma_api = DomaApiClient(
                cfg.doma_api_url,
                api_keys=[cfg.doma_api_key, *cfg.doma_api_keys, *cfg.file_api_keys],
                proxies=proxies,
            )
            eth_price = _fetch_eth_price_via_doma_quote(cfg, doma_api, quote_token)

            candidates = doma_api.fetch_domain_offer_candidates(chain_id=cfg.chain_id, take=100, max_pages=5)
            owner_caip = f"eip155:{cfg.chain_id}:{wallet.lower()}"
            eligible = [
                c
                for c in candidates
                if c.owner_address.lower() != owner_caip and c.token_address and c.token_id
            ]
            random.shuffle(eligible)
            offers_to_place = min(random.randint(offers_min, offers_max), len(eligible))
            if offers_to_place <= 0:
                skipped_wallets += 1
                skipped_wallet_details.append((wallet_number, wallet, "no eligible offer candidates"))
                logger.info("[OFFER] wallet=%s no eligible offer candidates | fetched=%s", wallet, len(candidates))
                continue

            offer_amounts = [
                Decimal(int((_random_decimal_between(buffer_amount, max_offer_amount, buffer_raw, max_offer_raw) * Decimal("1000000")).to_integral_value(rounding=ROUND_CEILING))) / Decimal("1000000")
                for _ in range(offers_to_place)
            ]

            exec_client = _build_exec_client_with_rpc_fallback(
                cfg,
                logger,
                wallet,
                private_key,
                proxies=proxies,
                log_prefix="[OFFER] ",
            )
            required_usdc = sum(offer_amounts, Decimal("0"))
            usdc_balance = exec_client.get_erc20_balance(quote_token.address, quote_token.decimals)
            if usdc_balance < required_usdc:
                topup_ok = _top_up_usdce_from_eth_for_offer(
                    cfg=cfg,
                    logger=logger,
                    state=state,
                    doma_api=doma_api,
                    exec_client=exec_client,
                    quote_token=quote_token,
                    weth_token=weth_token,
                    wallet=wallet,
                    eth_price=eth_price,
                    required_usdc=required_usdc,
                )
                if topup_ok:
                    usdc_balance = exec_client.get_erc20_balance(quote_token.address, quote_token.decimals)
            if usdc_balance < required_usdc:
                skipped_wallets += 1
                skipped_wallet_details.append((wallet_number, wallet, "USDC.E balance below required offers amount"))
                logger.warning(
                    "[OFFER] wallet=%s skipped | USDC.E balance below required offers amount (%s < %s)",
                    wallet,
                    _format_decimal_plain(usdc_balance),
                    _format_decimal_plain(required_usdc),
                )
                continue

            wallet_success = 0
            wallet_failed = 0
            selected_domains = eligible[:offers_to_place]
            logger.info(
                "[OFFER] wallet=%s selected=%s/%s candidates | offer_range=%s-%s USDC.E | required=%s USDC.E | proxy=yes",
                wallet,
                len(selected_domains),
                len(eligible),
                _format_decimal_plain(min(offer_amounts) if offer_amounts else Decimal("0")),
                _format_decimal_plain(max(offer_amounts) if offer_amounts else Decimal("0")),
                _format_decimal_plain(required_usdc),
            )
            for offer_idx, domain in enumerate(selected_domains, start=1):
                offer_amount = offer_amounts[offer_idx - 1]
                highest_offer = raw_to_decimal(int(domain.highest_offer_raw or "0"), domain.highest_offer_decimals or 6)
                logger.info(
                    "[OFFER] wallet=%s domain %s/%s %s | offer=%s USDC.E | current_top=%s %s | active_offers=%s",
                    wallet,
                    offer_idx,
                    len(selected_domains),
                    domain.name,
                    _format_decimal_plain(offer_amount),
                    _format_decimal_plain(highest_offer),
                    domain.highest_offer_symbol or "USDC.E",
                    domain.active_offers_count,
                )
                ok, order_id, reason = _run_domain_place_offer_helper(
                    cfg=cfg,
                    logger=logger,
                    wallet=wallet,
                    private_key=private_key,
                    domain=domain,
                    offer_amount=offer_amount,
                    duration_days=duration_days,
                    proxy=proxy,
                )
                append_csv(
                    offer_csv,
                    [
                        datetime.now(timezone.utc).isoformat(),
                        "ok" if ok else "failed",
                        wallet,
                        domain.name,
                        _format_decimal_plain(offer_amount),
                        _format_decimal_plain(duration_days),
                        domain.token_address,
                        domain.token_id,
                        _format_decimal_plain(highest_offer),
                        order_id,
                        reason,
                    ],
                    delimiter=cfg.csv_delimiter,
                )
                if ok:
                    wallet_success += 1
                    placed_total += 1
                    logger.info("[OFFER] wallet=%s domain=%s offer placed | order_id=%s", wallet, domain.name, order_id)
                else:
                    wallet_failed += 1
                    logger.warning("[OFFER] wallet=%s domain=%s offer failed: %s", wallet, domain.name, reason)
                if offer_idx < len(selected_domains):
                    delay_sec = random.uniform(offer_delay_min, offer_delay_max)
                    logger.info("[OFFER] delay before next offer: %.2f sec", delay_sec)
                    time.sleep(delay_sec)
            if wallet_success > 0:
                success_wallets += 1
            if wallet_failed > 0:
                failed_wallets += 1
                failed_wallet_addresses.append(wallet)
        except Exception as exc:
            failed_wallets += 1
            failed_wallet_addresses.append(wallet)
            logger.warning("[OFFER] wallet=%s failed: %s", wallet, exc)
        if idx < len(wallet_key_records):
            delay_sec = random.uniform(offer_delay_min, offer_delay_max)
            logger.info("[OFFER] delay before next wallet: %.2f sec", delay_sec)
            time.sleep(delay_sec)
    logger.info("[OFFER] placed offers total=%s", placed_total)
    if skipped_wallet_details:
        logger.warning("[OFFER] skipped wallets:")
        print("\n[OFFER] skipped wallets:")
        for wallet_number, wallet, reason in skipped_wallet_details:
            line = f"  #{wallet_number} {wallet} | {reason}"
            logger.warning("[OFFER] skipped | #%s %s | %s", wallet_number, wallet, reason)
            print(line)
    _print_mode_summary("OFFER", len(wallet_key_records), success_wallets, failed_wallets, skipped_wallets, failed_wallet_addresses)


def run_domain_accept_offer_once(cfg: BotConfig, logger: logging.Logger, state: BotState) -> None:
    picked = get_domain_accept_offer_menu_input()
    if not picked:
        logger.info("[ACCEPT_OFFER] canceled by user.")
        return
    delay_min_raw, delay_max_raw = picked
    accept_delay_min = float(_parse_decimal_input(delay_min_raw))
    accept_delay_max = float(_parse_decimal_input(delay_max_raw))

    wallet_key_records = _build_wallet_key_records(cfg, logger, "ACCEPT_OFFER")
    if not wallet_key_records:
        raise ValueError("No wallet/private-key pairs available for accepting domain offers")
    wallet_key_records, wallet_start_offset, total_loaded_wallets = _apply_wallet_start_selection(wallet_key_records)
    accept_csv = cfg.trades_csv_file.parent / DOMAIN_ACCEPTED_OFFERS_CSV.name
    ensure_csv(
        accept_csv,
        [
            "timestamp_utc",
            "status",
            "wallet",
            "domain",
            "accepted_usdce",
            "token_address",
            "token_id",
            "offerer_address",
            "active_offers_count",
            "order_id",
            "tx_hash",
            "reason",
        ],
        delimiter=cfg.csv_delimiter,
    )

    logger.info(
        "[ACCEPT_OFFER] mode started | wallets=%s | start_wallet=%s | accepting highest received top offer per wallet | delay=%s-%s sec",
        total_loaded_wallets,
        wallet_start_offset + 1,
        delay_min_raw,
        delay_max_raw,
    )

    success_wallets = 0
    failed_wallets = 0
    skipped_wallets = 0
    accepted_total = 0
    failed_wallet_addresses: List[str] = []
    skipped_wallet_details: List[Tuple[int, str, str]] = []

    for idx, (line_idx, wallet, private_key) in enumerate(wallet_key_records, start=1):
        proxies, skip_wallet = _proxy_for_line(cfg, line_idx, logger, "ACCEPT_OFFER")
        proxy = (proxies or {}).get("https") or (proxies or {}).get("http") or ""
        wallet_number = line_idx + 1
        logger.info("[ACCEPT_OFFER] wallet %s", _wallet_record_progress_label(idx - 1, len(wallet_key_records), line_idx, total_loaded_wallets, wallet))
        if skip_wallet or not proxies:
            skipped_wallets += 1
            skipped_wallet_details.append((wallet_number, wallet, "proxy is required"))
            logger.warning("[ACCEPT_OFFER] wallet=%s skipped: proxy is required for accepting offers", wallet)
            continue
        try:
            doma_api = DomaApiClient(
                cfg.doma_api_url,
                api_keys=[cfg.doma_api_key, *cfg.doma_api_keys, *cfg.file_api_keys],
                proxies=proxies,
            )
            offers = doma_api.fetch_wallet_received_top_offers(wallet, chain_id=cfg.chain_id, take=100, max_pages=10)
            owner_caip = f"eip155:{cfg.chain_id}:{wallet.lower()}"
            eligible: List[Tuple[DomainReceivedOffer, Decimal]] = []
            for offer in offers:
                if offer.owner_address.lower() != owner_caip:
                    continue
                if offer.offerer_address.lower() == owner_caip:
                    continue
                amount = raw_to_decimal(int(offer.price_raw or "0"), offer.currency_decimals or 6)
                if (offer.currency_symbol or "").lower() not in {"usdc.e", "usdce"}:
                    continue
                eligible.append((offer, amount))
            eligible.sort(key=lambda item: item[1], reverse=True)
            if not eligible:
                skipped_wallets += 1
                skipped_wallet_details.append((wallet_number, wallet, "no received top offers"))
                logger.info(
                    "[ACCEPT_OFFER] wallet=%s no eligible received offers | fetched=%s",
                    wallet,
                    len(offers),
                )
                continue

            wallet_success = 0
            wallet_failed = 0
            selected_offers = eligible[:1]
            logger.info(
                "[ACCEPT_OFFER] wallet=%s selected highest=%s/%s received top offers | proxy=yes",
                wallet,
                len(selected_offers),
                len(eligible),
            )
            for offer_idx, (offer, amount) in enumerate(selected_offers, start=1):
                logger.info(
                    "[ACCEPT_OFFER] wallet=%s domain %s/%s %s | accept=%s %s | active_offers=%s | order_id=%s",
                    wallet,
                    offer_idx,
                    len(selected_offers),
                    offer.name,
                    _format_decimal_plain(amount),
                    offer.currency_symbol or "USDC.E",
                    offer.active_offers_count,
                    offer.order_id,
                )
                ok, tx_hash, reason = _run_domain_accept_offer_helper(
                    cfg=cfg,
                    logger=logger,
                    wallet=wallet,
                    private_key=private_key,
                    offer=offer,
                    proxy=proxy,
                )
                append_csv(
                    accept_csv,
                    [
                        datetime.now(timezone.utc).isoformat(),
                        "ok" if ok else "failed",
                        wallet,
                        offer.name,
                        _format_decimal_plain(amount),
                        offer.token_address,
                        offer.token_id,
                        offer.offerer_address,
                        str(offer.active_offers_count),
                        offer.order_id,
                        tx_hash,
                        reason,
                    ],
                    delimiter=cfg.csv_delimiter,
                )
                if ok:
                    wallet_success += 1
                    accepted_total += 1
                    logger.info("[ACCEPT_OFFER] wallet=%s domain=%s offer accepted | tx=%s", wallet, offer.name, tx_hash)
                else:
                    wallet_failed += 1
                    logger.warning("[ACCEPT_OFFER] wallet=%s domain=%s accept failed: %s", wallet, offer.name, reason)
                if offer_idx < len(selected_offers):
                    delay_sec = random.uniform(accept_delay_min, accept_delay_max)
                    logger.info("[ACCEPT_OFFER] delay before next accept: %.2f sec", delay_sec)
                    time.sleep(delay_sec)
            if wallet_success > 0:
                success_wallets += 1
            if wallet_failed > 0:
                failed_wallets += 1
                failed_wallet_addresses.append(wallet)
        except Exception as exc:
            failed_wallets += 1
            failed_wallet_addresses.append(wallet)
            logger.warning("[ACCEPT_OFFER] wallet=%s failed: %s", wallet, exc)
        if idx < len(wallet_key_records):
            delay_sec = random.uniform(accept_delay_min, accept_delay_max)
            logger.info("[ACCEPT_OFFER] delay before next wallet: %.2f sec", delay_sec)
            time.sleep(delay_sec)
    logger.info("[ACCEPT_OFFER] accepted offers total=%s", accepted_total)
    if skipped_wallet_details:
        logger.warning("[ACCEPT_OFFER] skipped wallets:")
        print("\n[ACCEPT_OFFER] skipped wallets:")
        for wallet_number, wallet, reason in skipped_wallet_details:
            line = f"  #{wallet_number} {wallet} | {reason}"
            logger.warning("[ACCEPT_OFFER] skipped | #%s %s | %s", wallet_number, wallet, reason)
            print(line)
    _print_mode_summary("ACCEPT_OFFER", len(wallet_key_records), success_wallets, failed_wallets, skipped_wallets, failed_wallet_addresses)


def _token_amount_for_usd(token: Token, usd_amount: Decimal, eth_price: Decimal) -> Decimal:
    price = pick_token_usd_price(token, eth_price)
    if price <= 0:
        raise RuntimeError(f"Unknown USD price for {token.symbol}")
    return usd_amount / price


def _is_weth_usdce_pool(pool: Pool, quote_token: Token, weth_token: Token) -> bool:
    addrs = {pool.token0.address.lower(), pool.token1.address.lower()}
    return quote_token.address.lower() in addrs and weth_token.address.lower() in addrs


def _liquidity_available_usd(
    exec_client: EvmExecutionClient,
    pool: Pool,
    quote_token: Token,
    weth_token: Token,
    eth_price: Decimal,
) -> Decimal:
    native_spendable = _spendable_native_eth(exec_client, eth_price)
    total_usd = native_spendable * eth_price
    tokens_by_address: Dict[str, Token] = {}
    for token in (quote_token, weth_token, pool.token0, pool.token1):
        address = token.address.strip().lower()
        if address:
            tokens_by_address[address] = token
    for token in tokens_by_address.values():
        token_price = pick_token_usd_price(token, eth_price)
        if token_price <= 0:
            continue
        balance = exec_client.get_erc20_balance(token.address, token.decimals)
        total_usd += balance * token_price
    return total_usd


def _top_up_liquidity_token(
    cfg: BotConfig,
    logger: logging.Logger,
    state: BotState,
    doma_api: DomaApiClient,
    exec_client: EvmExecutionClient,
    wallet: str,
    token: Token,
    quote_token: Token,
    weth_token: Token,
    eth_price: Decimal,
    target_usd: Decimal,
    label: str,
    reserve_quote_usd: Decimal = Decimal("0"),
    preserve_weth_balance: bool = False,
) -> bool:
    token_price = pick_token_usd_price(token, eth_price)
    if token_price <= 0:
        logger.warning("[%s] cannot top up %s: unknown token price", label, token.symbol)
        return False
    balance = exec_client.get_erc20_balance(token.address, token.decimals)
    balance_usd = balance * token_price
    missing_usd = target_usd - balance_usd
    if missing_usd <= Decimal("0.05"):
        return True

    if token.address.lower() == quote_token.address.lower():
        buy_usd = missing_usd.quantize(Decimal("0.000001"))
    else:
        buy_usd = (missing_usd * DOMAIN_LIQUIDITY_SWAP_BUFFER).quantize(Decimal("0.000001"))
    if token.address.lower() == weth_token.address.lower():
        target_weth = target_usd / eth_price
        missing_weth = target_weth - balance
        native_spendable = _spendable_native_eth(exec_client, eth_price)
        wrap_amount = min(missing_weth, native_spendable)
        if wrap_amount > Decimal("0"):
            required_after_wrap_raw = decimal_to_raw(balance + wrap_amount, weth_token.decimals)
            try:
                wrap_tx = exec_client.ensure_weth_balance(weth_token.address, required_after_wrap_raw)
                if wrap_tx:
                    state.last_tx_hash = wrap_tx
                    logger.info("[%s] WETH wrap tx sent: %s", label, wrap_tx)
                    if not _wait_tx_receipt(exec_client, wrap_tx, timeout_sec=180):
                        logger.warning("[%s] WETH wrap did not confirm", label)
                        return False
                balance = exec_client.get_erc20_balance(token.address, token.decimals)
                balance_usd = balance * token_price
                missing_usd = target_usd - balance_usd
                if missing_usd <= Decimal("0.05"):
                    return True
                buy_usd = (missing_usd * DOMAIN_LIQUIDITY_SWAP_BUFFER).quantize(Decimal("0.000001"))
            except Exception as exc:
                logger.info("[%s] WETH partial wrap unavailable, trying swap route: %s", label, exc)

    usdc_balance = exec_client.get_erc20_balance(quote_token.address, quote_token.decimals)
    quote_price = pick_token_usd_price(quote_token, eth_price)
    quote_balance_usd = usdc_balance * quote_price if quote_price > 0 else usdc_balance
    spendable_usdc_usd = quote_balance_usd
    if token.address.lower() != quote_token.address.lower() and reserve_quote_usd > 0:
        spendable_usdc_usd = max(Decimal("0"), quote_balance_usd - reserve_quote_usd)
    if token.address.lower() != quote_token.address.lower() and spendable_usdc_usd >= MIN_EXECUTABLE_TRADE_USD:
        usdc_topup_usd = min(buy_usd, spendable_usdc_usd).quantize(Decimal("0.000001"))
        logger.info(
            "[%s] combined balance topup | using=%s USDC.E | remaining source=ETH",
            label,
            _format_decimal_plain(usdc_topup_usd),
        )
        ok_usdc_topup = _execute_trade_via_doma_ui_route(
            cfg=cfg,
            logger=logger,
            state=state,
            doma_api=doma_api,
            exec_client=exec_client,
            token_in=quote_token,
            token_out=token,
            display_in_symbol="USDC.E",
            display_out_symbol=token.symbol,
            trade_amount_expr=f"${_format_decimal_plain(usdc_topup_usd)}",
            eth_price=eth_price,
            label=f"{label} USDC.E>{token.symbol} TOPUP",
            wait_for_pre_tx=True,
        )
        if not ok_usdc_topup or not state.last_tx_hash:
            return False
        if not _wait_tx_receipt(exec_client, state.last_tx_hash, timeout_sec=180):
            logger.warning("[%s] USDC.E topup did not confirm", label)
            return False
        balance = exec_client.get_erc20_balance(token.address, token.decimals)
        balance_usd = balance * token_price
        missing_usd = target_usd - balance_usd
        if missing_usd <= Decimal("0.05"):
            return True
        buy_usd = (
            missing_usd
            if token.address.lower() == quote_token.address.lower()
            else missing_usd * DOMAIN_LIQUIDITY_SWAP_BUFFER
        ).quantize(Decimal("0.000001"))

    native_balance = exec_client.get_native_balance()
    native_spendable = _spendable_native_eth(exec_client, eth_price)
    if native_spendable * eth_price < buy_usd:
        logger.warning(
            "[%s] insufficient source balance for %s topup | missing=%s USD | native=%s ETH | USDC.E=%s",
            label,
            token.symbol,
            _format_decimal_plain(missing_usd),
            _format_decimal_plain(native_balance),
            _format_decimal_plain(usdc_balance),
        )
        return False
    return _execute_trade_via_doma_ui_route(
        cfg=cfg,
        logger=logger,
        state=state,
        doma_api=doma_api,
        exec_client=exec_client,
        token_in=weth_token,
        token_out=token,
        display_in_symbol="ETH",
        display_out_symbol=token.symbol,
        trade_amount_expr=f"${_format_decimal_plain(buy_usd)}",
        eth_price=eth_price,
        label=f"{label} ETH>{token.symbol} TOPUP",
        is_eth_source=True,
        wait_for_pre_tx=True,
        cleanup_weth_before_eth_source=not preserve_weth_balance,
    )


def run_domain_liquidity_once(cfg: BotConfig, logger: logging.Logger, state: BotState) -> None:
    picked = get_domain_liquidity_menu_input()
    if not picked:
        logger.info("[LIQUIDITY] canceled by user.")
        return
    pool_mode_raw, min_usd_raw, max_usd_raw, delay_min_raw, delay_max_raw = picked
    min_usd = _parse_decimal_input(min_usd_raw)
    max_usd = _parse_decimal_input(max_usd_raw)
    delay_min = float(_parse_decimal_input(delay_min_raw))
    delay_max = float(_parse_decimal_input(delay_max_raw))

    if not cfg.position_manager_address or cfg.position_manager_address == "0x0000000000000000000000000000000000000000":
        raise ValueError("Set position_manager_address in contracts.json")

    wallet_key_records = _build_wallet_key_records(cfg, logger, "LIQUIDITY")
    if not wallet_key_records:
        raise ValueError("No wallet/private-key pairs available for liquidity mode")
    wallet_key_records, wallet_start_offset, total_loaded_wallets = _apply_wallet_start_selection(wallet_key_records)

    liquidity_csv = cfg.trades_csv_file.parent / DOMAIN_LIQUIDITY_CSV.name
    ensure_csv(
        liquidity_csv,
        [
            "timestamp_utc",
            "status",
            "wallet",
            "pool",
            "token0",
            "token1",
            "fee_tier",
            "target_usd",
            "amount0",
            "amount1",
            "tx_hash",
            "reason",
        ],
        delimiter=cfg.csv_delimiter,
    )

    metadata_proxies: Optional[Dict[str, str]] = None
    for metadata_line_idx, _, _ in wallet_key_records:
        candidate_proxies, skip_metadata_proxy = _proxy_for_line(cfg, metadata_line_idx, None, "LIQUIDITY_METADATA")
        if not skip_metadata_proxy:
            metadata_proxies = candidate_proxies
            break

    doma_api_shared = DomaApiClient(
        cfg.doma_api_url,
        api_keys=[cfg.doma_api_key, *cfg.doma_api_keys, *cfg.file_api_keys],
        proxies=metadata_proxies,
    )
    quote_token = _usdce_token_from_config(cfg)
    weth_token = _token_from_config_override(cfg, "WETH", 18)
    eth_price = _fetch_eth_price_via_doma_quote(cfg, doma_api_shared, quote_token)
    if eth_price <= 0:
        raise RuntimeError("Failed to resolve ETH price")
    pool_source_label = "top10-random"
    if pool_mode_raw == "2":
        pool_source_label = "WETH/USDC.E"
        try:
            candidate_pools = doma_api_shared.fetch_top_pools_by_tvl(limit=100, eth_price_usd=eth_price)
        except Exception as exc:
            logger.warning("[LIQUIDITY] Doma API pools failed, falling back to subgraph: %s", exc)
            subgraph = DomaSubgraphClient(cfg.subgraph_url, proxies=metadata_proxies)
            candidate_pools = subgraph.fetch_top_pools(limit=200)
        top_pools = [p for p in candidate_pools if _is_weth_usdce_pool(p, quote_token, weth_token)]
        top_pools.sort(key=lambda p: p.tvl_usd, reverse=True)
        top_pools = top_pools[:1]
    else:
        try:
            top_pools = doma_api_shared.fetch_top_pools_by_tvl(limit=10, eth_price_usd=eth_price)
        except Exception as exc:
            logger.warning("[LIQUIDITY] Doma API pools failed, falling back to subgraph: %s", exc)
            subgraph = DomaSubgraphClient(cfg.subgraph_url, proxies=metadata_proxies)
            pools = subgraph.fetch_top_pools(limit=10)
            top_pools = [p for p in pools if p.token0.address and p.token1.address][:10]
    if not top_pools:
        raise RuntimeError(f"No liquidity pools available for selection: {pool_source_label}")

    logger.info(
        "[LIQUIDITY] mode started | wallets=%s | start_wallet=%s | pool_mode=%s | pools=%s | target=%s-%s USD | mint_buffer=%sx | delay=%s-%s sec",
        total_loaded_wallets,
        wallet_start_offset + 1,
        pool_source_label,
        len(top_pools),
        _format_decimal_plain(min_usd),
        _format_decimal_plain(max_usd),
        _format_decimal_plain(DOMAIN_LIQUIDITY_MINT_BUFFER),
        delay_min_raw,
        delay_max_raw,
    )

    success_wallets = 0
    failed_wallets = 0
    skipped_wallets = 0
    failed_wallet_addresses: List[str] = []
    skipped_wallet_details: List[Tuple[int, str, str]] = []

    for idx, (line_idx, wallet, private_key) in enumerate(wallet_key_records, start=1):
        wallet_number = line_idx + 1
        proxies, skip_wallet = _proxy_for_line(cfg, line_idx, logger, "LIQUIDITY")
        logger.info("[LIQUIDITY] wallet %s", _wallet_record_progress_label(idx - 1, len(wallet_key_records), line_idx, total_loaded_wallets, wallet))
        if skip_wallet:
            skipped_wallets += 1
            skipped_wallet_details.append((wallet_number, wallet, "proxy is required"))
            continue

        target_usd = _random_decimal_between(min_usd, max_usd, min_usd_raw, max_usd_raw)
        pool = random.choice(top_pools)
        mint_target_usd = max(
            target_usd * DOMAIN_LIQUIDITY_MINT_BUFFER,
            target_usd + DOMAIN_LIQUIDITY_MIN_EXTRA_USD,
        ).quantize(Decimal("0.000001"))
        half_usd = target_usd / Decimal("2")
        half_mint_usd = mint_target_usd / Decimal("2")
        token0_price = pick_token_usd_price(pool.token0, eth_price)
        token1_price = pick_token_usd_price(pool.token1, eth_price)
        if token0_price <= 0 or token1_price <= 0:
            skipped_wallets += 1
            skipped_wallet_details.append((wallet_number, wallet, f"unknown token price for pool {pool.address}"))
            continue

        label = f"LIQUIDITY {wallet} {pool.token0.symbol}/{pool.token1.symbol}"
        try:
            doma_api = DomaApiClient(
                cfg.doma_api_url,
                api_keys=[cfg.doma_api_key, *cfg.doma_api_keys, *cfg.file_api_keys],
                proxies=proxies,
            )
            exec_client = _build_exec_client_with_rpc_fallback(
                cfg,
                logger,
                wallet,
                private_key,
                proxies=proxies,
                log_prefix="[LIQUIDITY] ",
            )
            position_client = _build_position_manager_client_with_rpc_fallback(
                cfg,
                logger,
                wallet,
                private_key,
                proxies=proxies,
                log_prefix="[LIQUIDITY] ",
            )

            logger.info(
                "[LIQUIDITY] wallet=%s pool=%s %s/%s fee=%s target=%s USD split=%s/%s USD | mint_budget=%s USD split=%s/%s USD | tvl=%s",
                wallet,
                pool.address,
                pool.token0.symbol,
                pool.token1.symbol,
                pool.fee_tier,
                _format_decimal_plain(target_usd),
                _format_decimal_plain(half_usd),
                _format_decimal_plain(half_usd),
                _format_decimal_plain(mint_target_usd),
                _format_decimal_plain(half_mint_usd),
                _format_decimal_plain(half_mint_usd),
                _format_decimal_plain(pool.tvl_usd),
            )

            available_usd = _liquidity_available_usd(
                exec_client,
                pool,
                quote_token,
                weth_token,
                eth_price,
            )
            if available_usd < mint_target_usd:
                reason = (
                    "insufficient Doma balance before swaps "
                    f"(available=${_format_decimal_plain(available_usd)}, "
                    f"required=${_format_decimal_plain(mint_target_usd)})"
                )
                skipped_wallets += 1
                skipped_wallet_details.append((wallet_number, wallet, reason))
                logger.warning("[LIQUIDITY] wallet=%s skipped | %s", wallet, reason)
                append_csv(
                    liquidity_csv,
                    [
                        datetime.now(timezone.utc).isoformat(),
                        "skipped",
                        wallet,
                        pool.address,
                        pool.token0.symbol,
                        pool.token1.symbol,
                        str(pool.fee_tier),
                        _format_decimal_plain(target_usd),
                        "",
                        "",
                        "",
                        reason,
                    ],
                    delimiter=cfg.csv_delimiter,
                )
                continue

            quote_addr = quote_token.address.lower()
            token0_addr = pool.token0.address.lower()
            token1_addr = pool.token1.address.lower()
            token0_reserve_quote = half_mint_usd if token0_addr != quote_addr and token1_addr == quote_addr else Decimal("0")
            token1_reserve_quote = half_mint_usd if token1_addr != quote_addr and token0_addr == quote_addr else Decimal("0")
            weth_addr = weth_token.address.lower()
            token0_preserve_weth = token0_addr != weth_addr and token1_addr == weth_addr
            token1_preserve_weth = token1_addr != weth_addr and token0_addr == weth_addr

            prev_tx_hash = state.last_tx_hash
            ok0 = _top_up_liquidity_token(
                cfg,
                logger,
                state,
                doma_api,
                exec_client,
                wallet,
                pool.token0,
                quote_token,
                weth_token,
                eth_price,
                half_mint_usd,
                label,
                reserve_quote_usd=token0_reserve_quote,
                preserve_weth_balance=token0_preserve_weth,
            )
            if ok0 and state.last_tx_hash and state.last_tx_hash != prev_tx_hash:
                _wait_tx_receipt(exec_client, state.last_tx_hash, timeout_sec=180)
            prev_tx_hash = state.last_tx_hash
            ok1 = _top_up_liquidity_token(
                cfg,
                logger,
                state,
                doma_api,
                exec_client,
                wallet,
                pool.token1,
                quote_token,
                weth_token,
                eth_price,
                half_mint_usd,
                label,
                reserve_quote_usd=token1_reserve_quote,
                preserve_weth_balance=token1_preserve_weth,
            )
            if ok1 and state.last_tx_hash and state.last_tx_hash != prev_tx_hash:
                _wait_tx_receipt(exec_client, state.last_tx_hash, timeout_sec=180)
            if not ok0 or not ok1:
                raise RuntimeError("failed to prepare token balances for liquidity")

            desired0 = _token_amount_for_usd(pool.token0, half_mint_usd, eth_price)
            desired1 = _token_amount_for_usd(pool.token1, half_mint_usd, eth_price)
            bal0 = exec_client.get_erc20_balance(pool.token0.address, pool.token0.decimals)
            bal1 = exec_client.get_erc20_balance(pool.token1.address, pool.token1.decimals)
            if bal0 < desired0 * DOMAIN_LIQUIDITY_MIN_BALANCE_RATIO or bal1 < desired1 * DOMAIN_LIQUIDITY_MIN_BALANCE_RATIO:
                raise RuntimeError(
                    "prepared token balances below liquidity target "
                    f"({pool.token0.symbol}={_format_decimal_plain(bal0)}/{_format_decimal_plain(desired0)}, "
                    f"{pool.token1.symbol}={_format_decimal_plain(bal1)}/{_format_decimal_plain(desired1)})"
                )
            amount0 = min(bal0, desired0)
            amount1 = min(bal1, desired1)
            amount0_raw = decimal_to_raw(amount0, pool.token0.decimals)
            amount1_raw = decimal_to_raw(amount1, pool.token1.decimals)
            if amount0_raw <= 0 or amount1_raw <= 0:
                raise RuntimeError(f"prepared token amount is zero ({pool.token0.symbol}={amount0}, {pool.token1.symbol}={amount1})")

            if cfg.paper_mode or cfg.dry_run or not cfg.enable_execution:
                tx_hash = ""
                logger.info("[LIQUIDITY] PAPER/DRY mode: wallet=%s no mint tx sent", wallet)
            else:
                approve0 = position_client.ensure_allowance(pool.token0.address, amount0_raw, approve_max=True)
                if approve0:
                    logger.info("[LIQUIDITY] wallet=%s approve %s tx=%s", wallet, pool.token0.symbol, approve0)
                    _wait_tx_receipt(exec_client, approve0, timeout_sec=180)
                approve1 = position_client.ensure_allowance(pool.token1.address, amount1_raw, approve_max=True)
                if approve1:
                    logger.info("[LIQUIDITY] wallet=%s approve %s tx=%s", wallet, pool.token1.symbol, approve1)
                    _wait_tx_receipt(exec_client, approve1, timeout_sec=180)
                tx_hash = position_client.mint_full_range(
                    token0=pool.token0.address,
                    token1=pool.token1.address,
                    fee_tier=pool.fee_tier,
                    amount0_desired=amount0_raw,
                    amount1_desired=amount1_raw,
                    recipient=wallet,
                    deadline_sec=600,
                )
                state.last_tx_hash = tx_hash
                logger.info("[LIQUIDITY] wallet=%s full-range mint tx=%s", wallet, tx_hash)
                if not _wait_tx_receipt(exec_client, tx_hash, timeout_sec=240):
                    raise RuntimeError("mint tx failed or timed out")

            append_csv(
                liquidity_csv,
                [
                    datetime.now(timezone.utc).isoformat(),
                    "ok",
                    wallet,
                    pool.address,
                    pool.token0.symbol,
                    pool.token1.symbol,
                    str(pool.fee_tier),
                    _format_decimal_plain(target_usd),
                    _format_decimal_plain(amount0),
                    _format_decimal_plain(amount1),
                    tx_hash,
                    "",
                ],
                delimiter=cfg.csv_delimiter,
            )
            success_wallets += 1
        except Exception as exc:
            failed_wallets += 1
            failed_wallet_addresses.append(wallet)
            logger.warning("[LIQUIDITY] wallet=%s failed: %s", wallet, exc)
            append_csv(
                liquidity_csv,
                [
                    datetime.now(timezone.utc).isoformat(),
                    "failed",
                    wallet,
                    pool.address,
                    pool.token0.symbol,
                    pool.token1.symbol,
                    str(pool.fee_tier),
                    _format_decimal_plain(target_usd),
                    "",
                    "",
                    "",
                    str(exc),
                ],
                delimiter=cfg.csv_delimiter,
            )
        if idx < len(wallet_key_records):
            delay_sec = random.uniform(delay_min, delay_max)
            logger.info("[LIQUIDITY] delay before next wallet: %.2f sec", delay_sec)
            time.sleep(delay_sec)

    if skipped_wallet_details:
        logger.warning("[LIQUIDITY] skipped wallets:")
        print("\n[LIQUIDITY] skipped wallets:")
        for wallet_number, wallet, reason in skipped_wallet_details:
            line = f"  #{wallet_number} {wallet} | {reason}"
            logger.warning("[LIQUIDITY] skipped | #%s %s | %s", wallet_number, wallet, reason)
            print(line)
    _print_mode_summary("LIQUIDITY", len(wallet_key_records), success_wallets, failed_wallets, skipped_wallets, failed_wallet_addresses)



def _random_subdomain_label(length: int = 32) -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyz"
    return "".join(random.choice(alphabet) for _ in range(max(1, length)))


def _pick_available_subdomain_label(exec_client: EvmExecutionClient, token_address: str, min_length: int = 32) -> str:
    for _ in range(20):
        label = _random_subdomain_label(min_length)
        if exec_client.is_subdomain_claim_available(token_address, label):
            return label
    raise RuntimeError("Unable to find available subdomain label")


def _subdomain_staking_amount_for_length(prices_raw: List[int], token_decimals: int, label_length: int) -> Decimal:
    if not prices_raw:
        return Decimal("0")
    label_price_index = 0 if len(prices_raw) == 1 else min(max(0, label_length - 1), len(prices_raw) - 1)
    return raw_to_decimal(int(prices_raw[label_price_index]), token_decimals)


def _pick_affordable_subdomain_label(
    exec_client: EvmExecutionClient,
    token_address: str,
    prices_raw: List[int],
    token_decimals: int,
    token_balance: Decimal,
    min_length: int = 20,
    max_length: int = 40,
) -> Tuple[Optional[str], Decimal]:
    affordable_lengths = [
        length
        for length in range(min_length, max_length + 1)
        if _subdomain_staking_amount_for_length(prices_raw, token_decimals, length) <= token_balance
    ]
    random.shuffle(affordable_lengths)
    for length in affordable_lengths:
        try:
            label = _pick_available_subdomain_label(exec_client, token_address, length)
            required_amount = _subdomain_staking_amount_for_length(prices_raw, token_decimals, len(label))
            return label, required_amount
        except Exception:
            continue
    return None, Decimal("0")


def _top_up_domain_token_for_subdomain(
    cfg: BotConfig,
    logger: logging.Logger,
    state: BotState,
    doma_api: DomaApiClient,
    exec_client: EvmExecutionClient,
    wallet: str,
    domain_token: Token,
    quote_token: Token,
    weth_token: Token,
    eth_price: Decimal,
    required_amount: Decimal,
) -> bool:
    current_balance = exec_client.get_erc20_balance(domain_token.address, domain_token.decimals)
    if current_balance >= required_amount:
        return True
    token_price = pick_token_usd_price(domain_token, eth_price)
    if token_price <= 0:
        logger.warning("[CHEAP_BUY] wallet=%s token=%s cannot top up: unknown token price", wallet, domain_token.symbol)
        return False

    missing_tokens = required_amount - current_balance
    buy_usd = (missing_tokens * token_price * Decimal("1.10")).quantize(Decimal("0.000001"))
    if buy_usd < Decimal("0.01"):
        buy_usd = Decimal("0.01")

    usdc_balance = exec_client.get_erc20_balance(quote_token.address, quote_token.decimals)
    if usdc_balance < buy_usd:
        topup_ok, topup_reason, _, _ = _top_up_usdce_from_eth_for_cheap_buy(
            cfg,
            logger,
            state,
            doma_api,
            exec_client,
            quote_token,
            weth_token,
            wallet,
            eth_price,
            buy_usd,
        )
        if not topup_ok:
            logger.warning(
                "[CHEAP_BUY] wallet=%s token=%s cannot top up for subdomain | need=%s USDC.E | reason=%s",
                wallet,
                domain_token.symbol,
                _format_decimal_plain(buy_usd),
                topup_reason,
            )
            return False

    logger.info(
        "[CHEAP_BUY] wallet=%s token=%s topup before subdomain | balance=%s required=%s buy=%s USDC.E",
        wallet,
        domain_token.symbol,
        _format_decimal_plain(current_balance),
        _format_decimal_plain(required_amount),
        _format_decimal_plain(buy_usd),
    )
    ok = _execute_trade_via_doma_ui_route(
        cfg=cfg,
        logger=logger,
        state=state,
        doma_api=doma_api,
        exec_client=exec_client,
        token_in=quote_token,
        token_out=domain_token,
        display_in_symbol="USDC.E",
        display_out_symbol=domain_token.symbol,
        trade_amount_expr=f"${_format_decimal_plain(buy_usd)}",
        eth_price=eth_price,
        label=f"CHEAP_BUY {wallet} USDC.E>{domain_token.symbol} SUBDOMAIN-TOPUP",
        wait_for_pre_tx=True,
    )
    if not ok:
        return False
    refreshed_balance = current_balance
    for attempt in range(1, 11):
        refreshed_balance = exec_client.get_erc20_balance(domain_token.address, domain_token.decimals)
        if refreshed_balance >= required_amount:
            logger.info(
                "[CHEAP_BUY] wallet=%s token=%s topup balance confirmed | balance=%s required=%s | check=%s/10",
                wallet,
                domain_token.symbol,
                _format_decimal_plain(refreshed_balance),
                _format_decimal_plain(required_amount),
                attempt,
            )
            return True
        if attempt < 10:
            time.sleep(3)
    logger.warning(
        "[CHEAP_BUY] wallet=%s token=%s topup balance not updated after 30 sec | balance=%s required=%s",
        wallet,
        domain_token.symbol,
        _format_decimal_plain(refreshed_balance),
        _format_decimal_plain(required_amount),
    )
    return False


def _claim_subdomains_for_domain_token(
    cfg: BotConfig,
    logger: logging.Logger,
    state: BotState,
    doma_api: DomaApiClient,
    exec_client: EvmExecutionClient,
    chain_id: int,
    wallet: str,
    domain_token: Token,
    quote_token: Token,
    weth_token: Token,
    eth_price: Decimal,
    domain_name: str,
    subdomains_min_per_token: int,
    subdomains_max_per_token: int,
    delay_min: Decimal,
    delay_max: Decimal,
    max_claims: Optional[int] = None,
) -> Tuple[int, int]:
    prices_raw = exec_client.get_subdomain_staking_prices(domain_token.address)
    subdomains_to_claim = random.randint(subdomains_min_per_token, subdomains_max_per_token)
    if max_claims is not None:
        subdomains_to_claim = min(subdomains_to_claim, max(0, max_claims))
    if subdomains_to_claim <= 0:
        return 0, 0

    success_count = 0
    failed_count = 0
    for sub_idx in range(1, subdomains_to_claim + 1):
        token_balance = exec_client.get_erc20_balance(domain_token.address, domain_token.decimals)
        label_length = random.randint(20, 40)
        label = _pick_available_subdomain_label(exec_client, domain_token.address, label_length)
        required_amount = _subdomain_staking_amount_for_length(prices_raw, domain_token.decimals, len(label))
        if token_balance < required_amount:
            if not _top_up_domain_token_for_subdomain(
                cfg,
                logger,
                state,
                doma_api,
                exec_client,
                wallet,
                domain_token,
                quote_token,
                weth_token,
                eth_price,
                required_amount,
            ):
                failed_count += 1
                logger.warning(
                    "[CHEAP_BUY] wallet=%s token=%s subdomain skipped | topup failed | balance=%s required=%s",
                    wallet,
                    domain_token.symbol,
                    _format_decimal_plain(token_balance),
                    _format_decimal_plain(required_amount),
                )
                continue
            token_balance = exec_client.get_erc20_balance(domain_token.address, domain_token.decimals)
        logger.info(
            "[CHEAP_BUY] wallet=%s subdomain %s/%s claim | %s.%s | required=%s %s | balance=%s",
            wallet,
            sub_idx,
            subdomains_to_claim,
            label,
            domain_name,
            _format_decimal_plain(required_amount),
            domain_token.symbol,
            _format_decimal_plain(token_balance),
        )
        try:
            voucher_contract, staking_voucher, staking_signature = doma_api.sign_fractional_staking_subdomain_voucher(
                domain_token.address,
                label,
                wallet,
                chain_id=chain_id,
            )
            approve_hash, stake_hash, subdomain_id, dns_hash = exec_client.claim_subdomain_and_set_dns(
                domain_token.address,
                label,
                wallet,
                voucher_contract_address=voucher_contract,
                staking_voucher=staking_voucher,
                staking_signature=staking_signature,
            )
            if approve_hash:
                logger.info("[CHEAP_BUY] wallet=%s subdomain approve tx sent: %s", wallet, approve_hash)
            logger.info("[CHEAP_BUY] wallet=%s subdomain stake tx sent: %s | subdomain_id=%s", wallet, stake_hash, subdomain_id if subdomain_id is not None else "unknown")
            if dns_hash:
                logger.info("[CHEAP_BUY] wallet=%s subdomain DNS save tx sent: %s", wallet, dns_hash)
            success_count += 1
        except Exception as exc:
            failed_count += 1
            logger.warning("[CHEAP_BUY] wallet=%s token=%s subdomain claim/save failed: %s", wallet, domain_token.symbol, exc)
        if sub_idx < subdomains_to_claim:
            delay_sec = random.uniform(float(delay_min), float(delay_max))
            logger.info("[CHEAP_BUY] delay before next subdomain: %.2f sec", delay_sec)
            time.sleep(delay_sec)
    return success_count, failed_count


def _eligible_cheap_tokens(catalog: List[LaunchpadTokenInfo], quote_token: Token, max_price_usd: Decimal) -> List[LaunchpadTokenInfo]:
    quote_address = quote_token.address.lower()
    eligible: List[LaunchpadTokenInfo] = []
    seen: set[str] = set()
    for token in catalog:
        address = (token.address or "").strip().lower()
        if not address or address in seen:
            continue
        if token.price_usd <= 0 or token.price_usd >= max_price_usd:
            continue
        if (token.quote_token_address or "").strip().lower() != quote_address:
            continue
        if not token.pool_address:
            continue
        seen.add(address)
        eligible.append(token)
    random.shuffle(eligible)
    return eligible


def _supports_20_40_subdomain_claim(exec_client: EvmExecutionClient, token_address: str) -> bool:
    prices_raw = exec_client.get_subdomain_staking_prices(token_address)
    if not prices_raw:
        return False
    lengths = list(range(20, 41))
    random.shuffle(lengths)
    for length in lengths[:5]:
        label = _random_subdomain_label(length)
        if exec_client.is_subdomain_claim_available(token_address, label):
            return True
    return False


def _supports_subdomain_staking_api(
    doma_api: DomaApiClient,
    exec_client: EvmExecutionClient,
    token_address: str,
    owner_address: str,
    chain_id: int,
) -> bool:
    prices_raw = exec_client.get_subdomain_staking_prices(token_address)
    if not prices_raw:
        return False
    lengths = list(range(20, 41))
    random.shuffle(lengths)
    for length in lengths[:5]:
        label = _random_subdomain_label(length)
        if not exec_client.is_subdomain_claim_available(token_address, label):
            continue
        doma_api.sign_fractional_staking_subdomain_voucher(
            fractional_token_address=token_address,
            label=label,
            owner_address=owner_address,
            chain_id=chain_id,
        )
        return True
    return False


def _select_cheap_tokens_with_subdomains(
    logger: logging.Logger,
    doma_api: DomaApiClient,
    exec_client: EvmExecutionClient,
    wallet: str,
    catalog: List[LaunchpadTokenInfo],
    quote_token: Token,
    max_price_usd: Decimal,
    existing_token_addresses: set[str],
    count: int,
    chain_id: int,
) -> List[LaunchpadTokenInfo]:
    if count <= 0:
        return []
    selected: List[LaunchpadTokenInfo] = []
    checked = 0
    skipped_no_subdomain = 0
    skipped_api_inactive = 0
    skipped_blocklisted = 0
    for info in _eligible_cheap_tokens(catalog, quote_token, max_price_usd):
        address = (info.address or "").strip().lower()
        if not address or address in existing_token_addresses:
            continue
        token_names = {
            str(info.name or "").strip().lower(),
            str(info.symbol or "").strip().lower(),
        }
        if token_names & CHEAP_BUY_TOKEN_BLOCKLIST:
            skipped_blocklisted += 1
            continue
        checked += 1
        try:
            if not _supports_20_40_subdomain_claim(exec_client, info.address):
                skipped_no_subdomain += 1
                continue
        except Exception:
            skipped_no_subdomain += 1
            continue
        try:
            if not _supports_subdomain_staking_api(
                doma_api=doma_api,
                exec_client=exec_client,
                token_address=info.address,
                owner_address=wallet,
                chain_id=chain_id,
            ):
                skipped_api_inactive += 1
                continue
        except Exception as exc:
            skipped_api_inactive += 1
            logger.info(
                "[CHEAP_BUY] wallet=%s token=%s skipped before buy | staking API preflight failed: %s",
                wallet,
                info.symbol or info.name,
                exc,
            )
            continue
        selected.append(info)
        if len(selected) >= count:
            break
    logger.info(
        "[CHEAP_BUY] wallet=%s subdomain-capable token filter | selected=%s | checked=%s | skipped_no_subdomain=%s | skipped_api_inactive=%s | skipped_blocklisted=%s",
        wallet,
        len(selected),
        checked,
        skipped_no_subdomain,
        skipped_api_inactive,
        skipped_blocklisted,
    )
    return selected


def _top_tvl_com_tokens(catalog: List[LaunchpadTokenInfo], quote_token: Token, limit: int) -> List[LaunchpadTokenInfo]:
    quote_address = quote_token.address.lower()
    eligible: List[LaunchpadTokenInfo] = []
    seen_names: set[str] = set()
    seen_addresses: set[str] = set()
    for token in catalog:
        name = str(token.name or token.symbol or "").strip().lower()
        address = (token.address or "").strip().lower()
        if not name.endswith(".com") or not address:
            continue
        if name in seen_names or address in seen_addresses:
            continue
        if (token.quote_token_address or "").strip().lower() != quote_address:
            continue
        if not token.pool_address:
            continue
        if token.price_usd <= 0:
            continue
        seen_names.add(name)
        seen_addresses.add(address)
        eligible.append(token)
    eligible.sort(key=lambda item: item.tvl_usd, reverse=True)
    return eligible[:limit]


def _read_today_com_daily_success_domains(csv_path: Path, wallet: str, delimiter: str) -> set[str]:
    if not csv_path.exists():
        return set()
    today = datetime.now(timezone.utc).date()
    wallet_lc = wallet.lower()
    out: set[str] = set()

    def _read_with(delim: str) -> List[dict]:
        try:
            with csv_path.open("r", newline="", encoding="utf-8") as f:
                return list(csv.DictReader(f, delimiter=delim))
        except Exception:
            return []

    rows = _read_with(delimiter)
    if rows and "wallet" not in rows[0]:
        rows = _read_with("," if delimiter != "," else ";")
    for row in rows:
        if str(row.get("status") or "").strip().lower() != "ok":
            continue
        if str(row.get("wallet") or "").strip().lower() != wallet_lc:
            continue
        domain = str(row.get("domain") or "").strip().lower()
        if not domain.endswith(".com"):
            continue
        ts_raw = str(row.get("timestamp_utc") or "").strip()
        if ts_raw:
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                if ts.astimezone(timezone.utc).date() != today:
                    continue
            except Exception:
                continue
        out.add(domain)
    return out


def _is_com_daily_quest_completed(api: DomaApiClient, wallet: str, chain_id: int) -> Optional[bool]:
    quests = api.fetch_quests(wallet, chain_id)
    for quest in quests:
        description = quest.description.lower()
        reset_period = quest.reset_period.upper()
        if reset_period == "DAILY" and ".com" in description and "swap" in description:
            return bool(quest.completed)
    return None


def _is_daily_subdomain_stake_quest_completed(api: DomaApiClient, wallet: str, chain_id: int) -> Optional[bool]:
    quests = api.fetch_quests(wallet, chain_id)
    for quest in quests:
        description = quest.description.lower()
        reset_period = quest.reset_period.upper()
        if reset_period == "DAILY" and "subdomain" in description and ("stake" in description or "staking" in description):
            return bool(quest.completed)
    return None


def _wait_daily_subdomain_stake_quest_completed(
    api: DomaApiClient,
    wallet: str,
    chain_id: int,
    timeout_sec: int = 45,
    poll_sec: int = 5,
) -> Optional[bool]:
    deadline = time.time() + max(0, timeout_sec)
    last_status: Optional[bool] = None
    while True:
        last_status = _is_daily_subdomain_stake_quest_completed(api, wallet, chain_id)
        if last_status is True:
            return True
        if time.time() >= deadline:
            return last_status
        time.sleep(max(1, poll_sec))


def _top_up_usdce_from_eth_for_cheap_buy(
    cfg: BotConfig,
    logger: logging.Logger,
    state: BotState,
    doma_api: DomaApiClient,
    exec_client: EvmExecutionClient,
    quote_token: Token,
    weth_token: Token,
    wallet: str,
    eth_price: Decimal,
    required_usdc: Decimal,
    log_prefix: str = "CHEAP_BUY",
) -> Tuple[bool, str, Decimal, Decimal]:
    current_usdc = exec_client.get_erc20_balance(quote_token.address, quote_token.decimals)
    if current_usdc >= required_usdc:
        return True, "", current_usdc, required_usdc
    if eth_price <= 0:
        logger.warning("[%s] wallet=%s cannot top up USDC.E from ETH: ETH price is unknown", log_prefix, wallet)
        return False, "eth_price_unknown", current_usdc, required_usdc

    reserve_eth = _native_gas_reserve_eth(eth_price)
    native_eth = exec_client.get_native_balance()
    spendable_eth = native_eth - reserve_eth
    if spendable_eth <= 0:
        logger.warning("[%s] wallet=%s skipped | no USDC.E and no spendable ETH", log_prefix, wallet)
        return False, "no_spendable_eth", native_eth, reserve_eth

    missing_usdc = required_usdc - current_usdc
    target_usdc = missing_usdc * Decimal("1.05")
    bootstrap_eth = target_usdc / eth_price
    max_bootstrap_eth = spendable_eth
    if bootstrap_eth > max_bootstrap_eth:
        bootstrap_eth = max_bootstrap_eth
    bootstrap_usd = bootstrap_eth * eth_price
    if bootstrap_eth <= 0:
        logger.warning(
            "[%s] wallet=%s skipped | ETH->USDC.E bootstrap has no spendable amount (%s USD)",
            log_prefix,
            wallet,
            _format_decimal_plain(bootstrap_usd),
        )
        return False, "eth_bootstrap_no_spendable_amount", bootstrap_usd, missing_usdc

    logger.info(
        "[%s] wallet=%s bootstrap | ETH->USDC.E amount=%s ETH | target_missing=%s USDC.E",
        log_prefix,
        wallet,
        _format_decimal_plain(bootstrap_eth),
        _format_decimal_plain(missing_usdc),
    )
    ok = _execute_trade_via_doma_ui_route(
        cfg=cfg,
        logger=logger,
        state=state,
        doma_api=doma_api,
        exec_client=exec_client,
        token_in=weth_token,
        token_out=quote_token,
        display_in_symbol="ETH",
        display_out_symbol="USDC.E",
        trade_amount_expr=_format_decimal_plain(bootstrap_eth),
        eth_price=eth_price,
        label=f"{log_prefix} {wallet} ETH>USDC.E BOOTSTRAP",
        is_eth_source=True,
        unwrap_to_native=False,
        wait_for_pre_tx=True,
    )
    if not ok or not state.last_tx_hash or not _wait_tx_receipt(exec_client, state.last_tx_hash, timeout_sec=180):
        logger.warning("[%s] wallet=%s ETH->USDC.E bootstrap failed", log_prefix, wallet)
        return False, "eth_to_usdc_bootstrap_failed", current_usdc, required_usdc

    refreshed_usdc = exec_client.get_erc20_balance(quote_token.address, quote_token.decimals)
    logger.info("[%s] wallet=%s USDC.E after bootstrap=%s", log_prefix, wallet, _format_decimal_plain(refreshed_usdc))
    if refreshed_usdc >= required_usdc:
        return True, "", refreshed_usdc, required_usdc
    return False, "usdc_after_bootstrap_below_required", refreshed_usdc, required_usdc


def _prepare_all_usdce_for_bonding_daily(
    cfg: BotConfig,
    logger: logging.Logger,
    state: BotState,
    doma_api: DomaApiClient,
    exec_client: EvmExecutionClient,
    quote_token: Token,
    weth_token: Token,
    wallet: str,
    eth_price: Decimal,
    log_prefix: str = "BONDING_DAILY",
) -> Tuple[bool, str, Decimal]:
    current_usdc = exec_client.get_erc20_balance(quote_token.address, quote_token.decimals)
    if current_usdc >= BONDING_DAILY_INITIAL_MIN_USDCE:
        logger.info(
            "[%s] wallet=%s using all available USDC.E | amount=%s USDC.E | ETH not touched",
            log_prefix,
            wallet,
            _format_decimal_plain(current_usdc),
        )
        return True, "", current_usdc
    if eth_price <= 0:
        return False, "ETH price is unknown", Decimal("0")

    native_eth = exec_client.get_native_balance()
    protected_usd = BONDING_DAILY_GAS_RESERVE_USD + BONDING_DAILY_BOOTSTRAP_GAS_BUFFER_USD
    reserve_eth = protected_usd / eth_price
    spendable_eth = max(Decimal("0"), native_eth - reserve_eth)
    if spendable_eth <= 0:
        return (
            False,
            "no spendable ETH after "
            f"${_format_decimal_plain(BONDING_DAILY_GAS_RESERVE_USD)} minimum gas reserve "
            f"and ${_format_decimal_plain(BONDING_DAILY_BOOTSTRAP_GAS_BUFFER_USD)} bootstrap buffer",
            Decimal("0"),
        )

    logger.info(
        "[%s] wallet=%s USDC.E below executable minimum (%s < %s) | converting spendable ETH=%s (~$%s) | protected_ETH=%s (~$%s: $%s minimum plus $%s bootstrap gas buffer)",
        log_prefix,
        wallet,
        _format_decimal_plain(current_usdc),
        _format_decimal_plain(BONDING_DAILY_INITIAL_MIN_USDCE),
        _format_decimal_plain(spendable_eth),
        _format_decimal_plain(spendable_eth * eth_price),
        _format_decimal_plain(reserve_eth),
        _format_decimal_plain(protected_usd),
        _format_decimal_plain(BONDING_DAILY_GAS_RESERVE_USD),
        _format_decimal_plain(BONDING_DAILY_BOOTSTRAP_GAS_BUFFER_USD),
    )
    swap_ok = _execute_trade_via_doma_ui_route(
        cfg=cfg,
        logger=logger,
        state=state,
        doma_api=doma_api,
        exec_client=exec_client,
        token_in=weth_token,
        token_out=quote_token,
        display_in_symbol="ETH",
        display_out_symbol="USDC.E",
        trade_amount_expr=_format_decimal_plain(spendable_eth),
        eth_price=eth_price,
        label=f"{log_prefix} {wallet} ETH>USDC.E BOOTSTRAP",
        is_eth_source=True,
        unwrap_to_native=False,
        wait_for_pre_tx=True,
    )
    tx_hash = state.last_tx_hash if swap_ok else ""
    if not swap_ok or not tx_hash or not _wait_tx_receipt(exec_client, tx_hash, timeout_sec=180):
        return False, "ETH->USDC.E bootstrap failed or timed out", Decimal("0")

    refreshed_usdc = exec_client.get_erc20_balance(quote_token.address, quote_token.decimals)
    if refreshed_usdc < BONDING_DAILY_INITIAL_MIN_USDCE:
        return (
            False,
            "combined USDC.E and spendable ETH are below the $1 initial minimum "
            f"(USDC.E={_format_decimal_plain(refreshed_usdc)})",
            refreshed_usdc,
        )
    logger.info(
        "[%s] wallet=%s bootstrap complete | available=%s USDC.E | ETH gas reserve kept at about $%s",
        log_prefix,
        wallet,
        _format_decimal_plain(refreshed_usdc),
        _format_decimal_plain(BONDING_DAILY_GAS_RESERVE_USD),
    )
    return True, "", refreshed_usdc


def _can_fully_fund_usdce_topup(
    exec_client: EvmExecutionClient,
    quote_token: Token,
    eth_price: Decimal,
    required_usdc: Decimal,
    conversion_buffer: Decimal = Decimal("1.05"),
) -> Tuple[bool, Decimal, Decimal, Decimal]:
    current_usdc = exec_client.get_erc20_balance(quote_token.address, quote_token.decimals)
    if current_usdc >= required_usdc:
        return True, current_usdc, Decimal("0"), current_usdc
    if eth_price <= 0:
        return False, current_usdc, Decimal("0"), current_usdc

    reserve_eth = _native_gas_reserve_eth(eth_price)
    native_eth = exec_client.get_native_balance()
    spendable_eth = max(Decimal("0"), native_eth - reserve_eth)
    spendable_eth_usd = spendable_eth * eth_price
    missing_usdc = required_usdc - current_usdc
    required_eth_usd = missing_usdc * conversion_buffer
    total_spendable_usd = current_usdc + spendable_eth_usd
    return spendable_eth_usd >= required_eth_usd, current_usdc, spendable_eth_usd, total_spendable_usd


def _top_up_usdce_from_eth_for_offer(
    cfg: BotConfig,
    logger: logging.Logger,
    state: BotState,
    doma_api: DomaApiClient,
    exec_client: EvmExecutionClient,
    quote_token: Token,
    weth_token: Token,
    wallet: str,
    eth_price: Decimal,
    required_usdc: Decimal,
) -> bool:
    current_usdc = exec_client.get_erc20_balance(quote_token.address, quote_token.decimals)
    if current_usdc >= required_usdc:
        return True
    if eth_price <= 0:
        logger.warning("[OFFER] wallet=%s cannot top up USDC.E from ETH: ETH price is unknown", wallet)
        return False

    reserve_eth = _native_gas_reserve_eth(eth_price)
    native_eth = exec_client.get_native_balance()
    spendable_eth = native_eth - reserve_eth
    if spendable_eth <= 0:
        logger.warning("[OFFER] wallet=%s skipped | no USDC.E and no spendable ETH", wallet)
        return False

    missing_usdc = required_usdc - current_usdc
    target_usdc = max(missing_usdc * Decimal("1.03"), MIN_EXECUTABLE_TRADE_USD)
    bootstrap_eth = min(target_usdc / eth_price, spendable_eth)
    bootstrap_usd = bootstrap_eth * eth_price
    if bootstrap_eth <= 0 or bootstrap_usd < MIN_EXECUTABLE_TRADE_USD:
        logger.warning(
            "[OFFER] wallet=%s skipped | ETH->USDC.E bootstrap below minimum executable amount (%s USD)",
            wallet,
            _format_decimal_plain(bootstrap_usd),
        )
        return False

    logger.info(
        "[OFFER] wallet=%s bootstrap | ETH->USDC.E amount=%s ETH | target_missing=%s USDC.E",
        wallet,
        _format_decimal_plain(bootstrap_eth),
        _format_decimal_plain(missing_usdc),
    )
    ok = _execute_trade_via_doma_ui_route(
        cfg=cfg,
        logger=logger,
        state=state,
        doma_api=doma_api,
        exec_client=exec_client,
        token_in=weth_token,
        token_out=quote_token,
        display_in_symbol="ETH",
        display_out_symbol="USDC.E",
        trade_amount_expr=_format_decimal_plain(bootstrap_eth),
        eth_price=eth_price,
        label=f"OFFER {wallet} ETH>USDC.E BOOTSTRAP",
        is_eth_source=True,
        unwrap_to_native=False,
        wait_for_pre_tx=True,
    )
    if not ok or not state.last_tx_hash or not _wait_tx_receipt(exec_client, state.last_tx_hash, timeout_sec=180):
        logger.warning("[OFFER] wallet=%s ETH->USDC.E bootstrap failed", wallet)
        return False

    refreshed_usdc = exec_client.get_erc20_balance(quote_token.address, quote_token.decimals)
    logger.info("[OFFER] wallet=%s USDC.E after bootstrap=%s", wallet, _format_decimal_plain(refreshed_usdc))
    return refreshed_usdc >= required_usdc


def _has_spendable_eth_for_cheap_buy(exec_client: EvmExecutionClient, eth_price: Decimal, required_usdc: Decimal = Decimal("0")) -> bool:
    if eth_price <= 0:
        return False
    reserve_eth = _native_gas_reserve_eth(eth_price)
    spendable_eth = exec_client.get_native_balance() - reserve_eth
    required_usd = max(required_usdc, Decimal("0.000001"))
    return spendable_eth > 0 and (spendable_eth * eth_price) >= required_usd


def run_cheap_token_buy_once(cfg: BotConfig, logger: logging.Logger, state: BotState) -> None:
    picked = get_cheap_token_buy_menu_input()
    if not picked:
        logger.info("[CHEAP_BUY] mode canceled by user.")
        return
    max_price_raw, buy_amount_min_raw, buy_amount_max_raw, tokens_min_raw, tokens_max_raw, subdomains_min_raw, subdomains_max_raw, delay_min_raw, delay_max_raw = picked
    max_price_usd = _parse_decimal_input(max_price_raw)
    buy_amount_min_usdc = _parse_decimal_input(buy_amount_min_raw)
    buy_amount_max_usdc = _parse_decimal_input(buy_amount_max_raw)
    tokens_min_per_wallet = int(_parse_decimal_input(tokens_min_raw).to_integral_value(rounding=ROUND_FLOOR))
    tokens_max_per_wallet = int(_parse_decimal_input(tokens_max_raw).to_integral_value(rounding=ROUND_CEILING))
    subdomains_min_per_token = int(_parse_decimal_input(subdomains_min_raw).to_integral_value(rounding=ROUND_FLOOR))
    subdomains_max_per_token = int(_parse_decimal_input(subdomains_max_raw).to_integral_value(rounding=ROUND_CEILING))
    delay_min = _parse_decimal_input(delay_min_raw)
    delay_max = _parse_decimal_input(delay_max_raw)

    wallet_key_records = _build_wallet_key_records(cfg, logger, "CHEAP_BUY")
    if not wallet_key_records:
        raise RuntimeError("No wallet/private-key pairs available for cheap token buy")
    wallet_key_records, wallet_start_offset, total_loaded_wallets = _apply_wallet_start_selection(wallet_key_records)
    quote_token = _usdce_token_from_config(cfg)
    weth_token = _token_from_config_override(cfg, "WETH", 18)
    logger.info(
        "[CHEAP_BUY] mode started | wallets=%s | start_wallet=%s | max_price<$%s | buy_amount=%s-%s USDC.E | tokens_per_wallet=%s-%s | subdomains_per_token=%s-%s | delay=%s-%s sec",
        total_loaded_wallets,
        wallet_start_offset + 1,
        _format_decimal_plain(max_price_usd),
        _format_decimal_plain(buy_amount_min_usdc),
        _format_decimal_plain(buy_amount_max_usdc),
        tokens_min_per_wallet,
        tokens_max_per_wallet,
        subdomains_min_per_token,
        subdomains_max_per_token,
        _format_decimal_plain(delay_min),
        _format_decimal_plain(delay_max),
    )

    metadata_proxies: Optional[Dict[str, str]] = None
    for metadata_line_idx, _, _ in wallet_key_records:
        candidate_proxies, skip_metadata_proxy = _proxy_for_line(cfg, metadata_line_idx, None, "CHEAP_BUY_METADATA")
        if not skip_metadata_proxy:
            metadata_proxies = candidate_proxies
            break
    logger.info("[CHEAP_BUY] loading token catalog once | max_pages=10")
    shared_doma_api = DomaApiClient(
        cfg.doma_api_url,
        api_keys=[cfg.doma_api_key, *cfg.doma_api_keys, *cfg.file_api_keys],
        proxies=metadata_proxies,
    )
    catalog = shared_doma_api.fetch_fractional_tokens(take=100, max_pages=10)
    logger.info("[CHEAP_BUY] token catalog loaded | tokens=%s", len(catalog))

    success_wallets = failed_wallets = skipped_wallets = 0
    buy_success_count = buy_failed_count = 0
    subdomain_success_count = subdomain_failed_count = 0
    failed_wallet_addresses: List[str] = []
    failed_wallet_seen: set[str] = set()
    subdomain_failed_wallets: List[str] = []
    subdomain_failed_seen: set[str] = set()
    insufficient_balance_wallets: List[str] = []
    insufficient_balance_seen: set[str] = set()

    def _remember_failed_wallet(wallet_number: int, wallet_address: str, reason: str) -> None:
        _ = wallet_address
        key = str(wallet_number)
        if key in failed_wallet_seen:
            return
        failed_wallet_seen.add(key)
        failed_wallet_addresses.append(f"wallet#{wallet_number} | {reason}")

    def _remember_subdomain_failed(wallet_number: int, wallet_address: str, reason: str) -> None:
        _ = wallet_address
        key = str(wallet_number)
        if key in subdomain_failed_seen:
            return
        subdomain_failed_seen.add(key)
        subdomain_failed_wallets.append(f"wallet#{wallet_number} | {reason}")

    def _remember_insufficient(wallet_number: int, wallet_address: str) -> None:
        _ = wallet_address
        key = str(wallet_number)
        if key in insufficient_balance_seen:
            return
        insufficient_balance_seen.add(key)
        insufficient_balance_wallets.append(f"wallet#{wallet_number} | insufficient balance")

    for idx, (line_idx, wallet, private_key) in enumerate(wallet_key_records, start=1):
        proxies, skip_wallet = _proxy_for_line(cfg, line_idx, logger, "CHEAP_BUY")
        wallet_number = line_idx + 1
        logger.info("[CHEAP_BUY] wallet %s/%s | wallet#%s - %s", idx, len(wallet_key_records), wallet_number, wallet)
        if skip_wallet or not proxies:
            skipped_wallets += 1
            logger.warning("[CHEAP_BUY] wallet=%s skipped: proxy is required for cheap token buy", wallet)
            continue
        try:
            logger.info("[CHEAP_BUY] wallet=%s loading wallet context", wallet)
            doma_api = DomaApiClient(cfg.doma_api_url, api_keys=[cfg.doma_api_key, *cfg.doma_api_keys, *cfg.file_api_keys], proxies=proxies)
            tokens_per_wallet = random.randint(tokens_min_per_wallet, tokens_max_per_wallet)
            exec_client = _build_exec_client_with_rpc_fallback(cfg, logger, wallet, private_key, proxies=proxies, log_prefix="[CHEAP_BUY]")
            logger.info("[CHEAP_BUY] wallet=%s RPC ready", wallet)
            eth_price = _fetch_eth_price_via_doma_quote(cfg, doma_api, quote_token)
            logger.info("[CHEAP_BUY] wallet=%s ETH price ready | skipping full existing-token scan", wallet)
            wallet_success = wallet_failed = 0
            existing_token_addresses: set[str] = set()
            existing_subdomains_claimed = 0

            tokens_to_buy = max(0, tokens_per_wallet - existing_subdomains_claimed)
            selected_tokens = _select_cheap_tokens_with_subdomains(
                logger,
                doma_api,
                exec_client,
                wallet,
                catalog,
                quote_token,
                max_price_usd,
                existing_token_addresses,
                tokens_to_buy,
                cfg.chain_id,
            )
            if tokens_to_buy <= 0:
                success_wallets += 1
                logger.info("[CHEAP_BUY] wallet=%s target satisfied from existing token balances | claimed_subdomains=%s", wallet, existing_subdomains_claimed)
                continue
            if not selected_tokens:
                if wallet_success > 0:
                    success_wallets += 1
                else:
                    skipped_wallets += 1
                logger.info("[CHEAP_BUY] wallet=%s no eligible tokens to buy | catalog=%s | max_price<$%s | proxy=yes", wallet, len(catalog), _format_decimal_plain(max_price_usd))
                continue
            required_usdc_for_wallet = buy_amount_max_usdc * Decimal(tokens_to_buy)
            topup_ok, topup_reason, _, _ = _top_up_usdce_from_eth_for_cheap_buy(
                cfg,
                logger,
                state,
                doma_api,
                exec_client,
                quote_token,
                weth_token,
                wallet,
                eth_price,
                required_usdc_for_wallet,
            )
            if not topup_ok:
                usdc_balance = exec_client.get_erc20_balance(quote_token.address, quote_token.decimals)
                if usdc_balance >= buy_amount_min_usdc:
                    logger.warning(
                        "[CHEAP_BUY] wallet=%s partial USDC.E available after failed/full bootstrap (%s), continuing with current balance",
                        wallet,
                        _format_decimal_plain(usdc_balance),
                    )
                else:
                    skipped_wallets += 1
                    if not _has_spendable_eth_for_cheap_buy(exec_client, eth_price, buy_amount_min_usdc):
                        _remember_insufficient(wallet_number, wallet)
                        logger.warning("[CHEAP_BUY] wallet=%s skipped | insufficient USDC.E and spendable ETH", wallet)
                    else:
                        logger.warning(
                            "[CHEAP_BUY] wallet=%s skipped | bootstrap failed (%s), but ETH balance exists; not marking as insufficient balance",
                            wallet,
                            topup_reason,
                        )
                    continue
            usdc_balance = exec_client.get_erc20_balance(quote_token.address, quote_token.decimals)
            if usdc_balance < buy_amount_min_usdc:
                skipped_wallets += 1
                if not _has_spendable_eth_for_cheap_buy(exec_client, eth_price, buy_amount_min_usdc):
                    _remember_insufficient(wallet_number, wallet)
                    logger.warning("[CHEAP_BUY] wallet=%s skipped | insufficient USDC.E and spendable ETH", wallet)
                else:
                    logger.warning(
                        "[CHEAP_BUY] wallet=%s skipped | USDC.E below minimum, but ETH balance exists; not marking as insufficient balance",
                        wallet,
                    )
                continue
            for token_idx, info in enumerate(selected_tokens, start=1):
                domain_token = _token_from_launchpad_price(info, eth_price)
                buy_amount_usdc = _random_decimal_between(buy_amount_min_usdc, buy_amount_max_usdc, buy_amount_min_raw, buy_amount_max_raw)
                current_usdc_balance = exec_client.get_erc20_balance(quote_token.address, quote_token.decimals)
                if current_usdc_balance < buy_amount_usdc:
                    wallet_failed += 1
                    buy_failed_count += 1
                    if not _has_spendable_eth_for_cheap_buy(exec_client, eth_price, buy_amount_usdc):
                        _remember_insufficient(wallet_number, wallet)
                    logger.warning("[CHEAP_BUY] wallet=%s token=%s skipped | USDC.E balance below selected buy amount (%s < %s)", wallet, domain_token.symbol, _format_decimal_plain(current_usdc_balance), _format_decimal_plain(buy_amount_usdc))
                    break
                expected_tokens = (buy_amount_usdc / info.price_usd) if info.price_usd > 0 else Decimal("0")
                logger.info("[CHEAP_BUY] wallet=%s token %s/%s %s | token_price=$%s | buy=%s USDC.E | expected~%s %s", wallet, token_idx, len(selected_tokens), domain_token.symbol, _format_decimal_plain(info.price_usd), _format_decimal_plain(buy_amount_usdc), _format_decimal_plain(expected_tokens), domain_token.symbol)
                ok = _execute_trade_via_doma_ui_route(cfg=cfg, logger=logger, state=state, doma_api=doma_api, exec_client=exec_client, token_in=quote_token, token_out=domain_token, display_in_symbol="USDC.E", display_out_symbol=domain_token.symbol, trade_amount_expr=f"${_format_decimal_plain(buy_amount_usdc)}", eth_price=eth_price, label=f"CHEAP_BUY {wallet} USDC.E>{domain_token.symbol}", wait_for_pre_tx=True)
                if ok:
                    buy_success_count += 1
                    claimed_ok, claimed_failed = _claim_subdomains_for_domain_token(
                        cfg=cfg,
                        logger=logger,
                        state=state,
                        doma_api=doma_api,
                        exec_client=exec_client,
                        chain_id=cfg.chain_id,
                        wallet=wallet,
                        domain_token=domain_token,
                        quote_token=quote_token,
                        weth_token=weth_token,
                        eth_price=eth_price,
                        domain_name=info.name,
                        subdomains_min_per_token=subdomains_min_per_token,
                        subdomains_max_per_token=subdomains_max_per_token,
                        delay_min=delay_min,
                        delay_max=delay_max,
                    )
                    subdomain_success_count += claimed_ok
                    subdomain_failed_count += claimed_failed
                    if claimed_ok > 0:
                        try:
                            daily_done = _wait_daily_subdomain_stake_quest_completed(doma_api, wallet, cfg.chain_id)
                        except Exception as exc:
                            daily_done = None
                            logger.warning("[CHEAP_BUY] wallet=%s daily subdomain staking quest verify failed: %s", wallet, exc)
                        if daily_done is True:
                            wallet_success += 1
                            logger.info("[CHEAP_BUY] wallet=%s daily subdomain staking quest satisfied | staked=%s", wallet, claimed_ok)
                            break
                        if daily_done is False:
                            wallet_failed += 1
                            reason = f"daily subdomain quest still not completed after stake token={domain_token.symbol}"
                            logger.warning("[CHEAP_BUY] wallet=%s %s", wallet, reason)
                            _remember_subdomain_failed(wallet_number, wallet, reason)
                            _remember_failed_wallet(wallet_number, wallet, reason)
                        else:
                            wallet_success += 1
                            logger.info("[CHEAP_BUY] wallet=%s subdomain stake sent but daily quest status is unknown | staked=%s", wallet, claimed_ok)
                            break
                    if claimed_failed > 0:
                        wallet_failed += 1
                        reason = f"subdomain creation failed={claimed_failed} token={domain_token.symbol}"
                        _remember_subdomain_failed(wallet_number, wallet, reason)
                        _remember_failed_wallet(wallet_number, wallet, reason)
                else:
                    buy_failed_count += 1
                    wallet_failed += 1
                    _remember_failed_wallet(wallet_number, wallet, f"buy failed token={domain_token.symbol}")
                if token_idx < len(selected_tokens):
                    delay_sec = random.uniform(float(delay_min), float(delay_max))
                    logger.info("[CHEAP_BUY] delay before next token: %.2f sec", delay_sec)
                    time.sleep(delay_sec)
            if wallet_success > 0:
                success_wallets += 1
            elif wallet_failed > 0:
                failed_wallets += 1
                _remember_failed_wallet(wallet_number, wallet, "wallet finished with errors")
        except Exception as exc:
            failed_wallets += 1
            _remember_failed_wallet(wallet_number, wallet, f"exception: {exc}")
            logger.warning("[CHEAP_BUY] wallet=%s failed: %s", wallet, exc)
        if idx < len(wallet_key_records):
            delay_sec = random.uniform(float(delay_min), float(delay_max))
            logger.info("[CHEAP_BUY] delay before next wallet: %.2f sec", delay_sec)
            time.sleep(delay_sec)
    logger.info("[CHEAP_BUY] buys total | success=%s failed=%s | subdomains success=%s failed=%s", buy_success_count, buy_failed_count, subdomain_success_count, subdomain_failed_count)
    if insufficient_balance_wallets:
        print("\n[CHEAP_BUY] wallets with insufficient balance:")
        for entry in insufficient_balance_wallets:
            print(_redact_wallet_addresses(f"  {entry}"))
    if subdomain_failed_wallets:
        print("\n[CHEAP_BUY] wallets with subdomain errors:")
        for entry in subdomain_failed_wallets:
            print(_redact_wallet_addresses(f"  {entry}"))
    _print_mode_summary(
        "CHEAP_BUY",
        total=len(wallet_key_records),
        success=success_wallets,
        failed=failed_wallets,
        skipped=skipped_wallets,
        failed_wallets=failed_wallet_addresses,
    )


def _is_currently_bonding_token(info: LaunchpadTokenInfo, quote_token: Token) -> bool:
    return bool(
        info.name.strip()
        and info.address
        and info.launchpad_address
        and not info.pool_address
        and info.status.strip().upper() == "FRACTIONALIZED"
        and info.price_usd > 0
        and info.quote_token_address == quote_token.address.lower()
        and info.name.strip().lower() not in CHEAP_BUY_TOKEN_BLOCKLIST
    )


def _is_launchpad_sellable_token(info: LaunchpadTokenInfo, quote_token: Token) -> bool:
    return bool(
        info.name.strip()
        and info.address
        and info.launchpad_address
        and info.quote_token_address == quote_token.address.lower()
        and info.name.strip().lower() not in CHEAP_BUY_TOKEN_BLOCKLIST
    )


def _prompt_bonding_token_by_fdv(candidates: List[LaunchpadTokenInfo]) -> LaunchpadTokenInfo:
    top_candidates = candidates[:5]
    if not top_candidates:
        raise RuntimeError("No active bonding tokens available for FDV selection")
    print("\nActive bonding tokens by FDV:")
    for idx, info in enumerate(top_candidates, start=1):
        print(
            f"{idx}) {info.name.lower()} | price=${_format_decimal_plain(info.price_usd)} "
            f"| status={info.status}"
        )
    while True:
        raw = input(f"Select token [1-{len(top_candidates)}, default 1]: ").strip() or "1"
        try:
            selected = int(raw)
        except ValueError:
            print(f"Enter a number from 1 to {len(top_candidates)}.")
            continue
        if 1 <= selected <= len(top_candidates):
            return top_candidates[selected - 1]
        print(f"Enter a number from 1 to {len(top_candidates)}.")


def _select_bonding_token_by_tvl(candidates: List[LaunchpadTokenInfo]) -> LaunchpadTokenInfo:
    if not candidates:
        raise RuntimeError("No active bonding tokens available for TVL selection")
    return max(
        candidates,
        key=lambda info: (info.tvl_usd, info.volume_usd, info.price_usd),
    )


def run_bonding_token_buy_once(
    cfg: BotConfig,
    logger: logging.Logger,
    state: BotState,
    preset: Optional[Tuple[str, str, str, str, str, str, str, str, str, str, str, str, str, str]] = None,
) -> None:
    (
        selection,
        specific_domain,
        poll_interval_raw,
        max_wait_minutes_raw,
        action,
        bonding_amount_mode,
        buy_amount_min_raw,
        buy_amount_max_raw,
        delay_min_raw,
        delay_max_raw,
        preapprove_raw,
        fast_broadcast_raw,
        launch_time_utc_raw,
        bonding_target_percent_raw,
    ) = preset or get_bonding_token_buy_menu_input()
    buy_amount_min = _parse_decimal_input(buy_amount_min_raw)
    buy_amount_max = _parse_decimal_input(buy_amount_max_raw)
    poll_interval = _parse_decimal_input(poll_interval_raw)
    max_wait_minutes = _parse_decimal_input(max_wait_minutes_raw)
    bonding_target_percent = _parse_decimal_input(bonding_target_percent_raw)
    delay_min = _parse_decimal_input(delay_min_raw)
    delay_max = _parse_decimal_input(delay_max_raw)
    daily_target_volume = max_wait_minutes if selection == "daily_quest" else Decimal("0")
    daily_swap_delay_min = poll_interval if selection == "daily_quest" else Decimal("0")
    daily_swap_delay_max = bonding_target_percent if selection == "daily_quest" else Decimal("0")
    if selection == "daily_quest":
        if daily_target_volume <= 0:
            raise ValueError("Daily bonding volume target must be greater than zero")
        if daily_swap_delay_min < 0 or daily_swap_delay_max < daily_swap_delay_min:
            raise ValueError("Invalid daily bonding swap delay range")
    mode_tag = (
        "BONDING_DAILY"
        if selection == "daily_quest"
        else "BONDING_SELL" if action == "sell" else "BONDING_BUY"
    )
    mode_prefix = f"[{mode_tag}]"

    all_records = _build_wallet_key_records(cfg, logger, mode_tag)
    if not all_records:
        raise RuntimeError("No wallet/private-key pairs available for bonding token mode")
    total_wallets = len(all_records)
    start_number = _prompt_start_wallet_number(total_wallets)
    end_number = _prompt_end_wallet_number(total_wallets, start_number)
    order = _prompt_wallet_order(default_random=True)
    wallet_records = _apply_wallet_order(all_records[start_number - 1:end_number], order)
    quote_token = _usdce_token_from_config(cfg)
    weth_token = _token_from_config_override(cfg, "WETH", 18)
    preapprove_before_launch = selection == "specific" and preapprove_raw != "2"
    fast_broadcast_after_launch = selection == "specific" and action == "buy" and fast_broadcast_raw != "2"
    launch_time_utc = None

    metadata_proxies: Optional[Dict[str, str]] = None
    for line_idx, _, _ in wallet_records:
        candidate_proxies, skip_proxy = _proxy_for_line(cfg, line_idx, None, f"{mode_tag}_METADATA")
        if not skip_proxy:
            metadata_proxies = candidate_proxies
            break
    shared_api = DomaApiClient(
        cfg.doma_api_url,
        api_keys=[cfg.doma_api_key, *cfg.doma_api_keys, *cfg.file_api_keys],
        proxies=metadata_proxies,
    )
    candidates: List[LaunchpadTokenInfo] = []
    launch_wait_deadline: Optional[float] = None
    preapproved_wallet_numbers: set[int] = set()
    prepared_fast_wallets: Dict[int, Tuple[EvmExecutionClient, Decimal, dict]] = {}
    if selection == "specific":
        selected_candidate = None
        attempt = 0
        preapproved_launchpad = ""
        armed_candidate: Optional[LaunchpadTokenInfo] = None
        next_metadata_refresh_at = 0.0
        last_metadata_status = "not found"
        while selected_candidate is None:
            attempt += 1
            if armed_candidate is not None and prepared_fast_wallets:
                logger.info(
                    "%s wallets armed | starting immediate repeated broadcasts | token=%s | wallets=%s | interval=%ss",
                    mode_prefix,
                    specific_domain,
                    len(prepared_fast_wallets),
                    _format_decimal_plain(poll_interval),
                )
                selected_candidate = armed_candidate
                candidates = [armed_candidate]
                break
            current: Optional[LaunchpadTokenInfo] = None
            if time.time() >= next_metadata_refresh_at:
                try:
                    current = shared_api.fetch_fractional_token_by_name(specific_domain)
                except Exception as exc:
                    logger.warning(
                        "%s metadata refresh failed while waiting for %s; continuing RPC wait: %s",
                        mode_prefix,
                        specific_domain,
                        exc,
                    )
                    next_metadata_refresh_at = time.time() + 30.0
                    time.sleep(float(poll_interval))
                    continue
                next_metadata_refresh_at = time.time() + 10.0
            if (
                preapprove_before_launch
                and current
                and current.launchpad_address
                and current.quote_token_address.lower() == quote_token.address.lower()
                and current.launchpad_address.lower() != preapproved_launchpad
            ):
                prepared_fast_wallets.clear()
                preapproved_wallet_numbers.clear()
                armed_candidate = None
                logger.info(
                    "%s parallel pre-approve stage for %s | launchpad=%s | wallets=%s",
                    mode_prefix,
                    specific_domain,
                    current.launchpad_address,
                    len(wallet_records),
                )

                def _prepare_launch_wallet(
                    record: Tuple[int, str, str],
                ) -> Tuple[int, EvmExecutionClient, Decimal, Decimal, Optional[dict]]:
                    pre_line_idx, pre_wallet, pre_private_key = record
                    pre_wallet_number = pre_line_idx + 1
                    pre_proxies, pre_skip_wallet = _proxy_for_line(cfg, pre_line_idx, logger, f"{mode_tag}_PREAPPROVE")
                    if pre_skip_wallet or not pre_proxies:
                        raise RuntimeError("proxy is required")
                    pre_exec = _build_exec_client_with_rpc_fallback(
                        cfg,
                        logger,
                        pre_wallet,
                        pre_private_key,
                        proxies=pre_proxies,
                        log_prefix=mode_prefix,
                    )
                    available_usdc = pre_exec.get_erc20_balance(quote_token.address, quote_token.decimals)
                    if bonding_amount_mode == "all_usdc":
                        if available_usdc < BONDING_DAILY_INITIAL_MIN_USDCE:
                            pre_api = DomaApiClient(
                                cfg.doma_api_url,
                                api_keys=[cfg.doma_api_key, *cfg.doma_api_keys, *cfg.file_api_keys],
                                proxies=pre_proxies,
                            )
                            pre_eth_price = _fetch_eth_price_via_doma_quote(cfg, pre_api, quote_token)
                            prepared_ok, prepared_reason, available_usdc = _prepare_all_usdce_for_bonding_daily(
                                cfg,
                                logger,
                                BotState.create_default(),
                                pre_api,
                                pre_exec,
                                quote_token,
                                weth_token,
                                f"wallet#{pre_wallet_number}",
                                pre_eth_price,
                                log_prefix="BONDING_BUY",
                            )
                            if not prepared_ok:
                                raise RuntimeError(prepared_reason)
                        prepared_amount = available_usdc
                        approve_amount = available_usdc
                    else:
                        prepared_amount = _random_decimal_between(
                            buy_amount_min,
                            buy_amount_max,
                            buy_amount_min_raw,
                            buy_amount_max_raw,
                        )
                        approve_amount = buy_amount_max
                    approve_raw = decimal_to_raw(approve_amount, quote_token.decimals)
                    if approve_raw <= 0:
                        raise RuntimeError("no USDC.E amount")
                    approve_hash = pre_exec.ensure_allowance(
                        quote_token.address,
                        approve_raw,
                        spender_address=current.launchpad_address,
                    )
                    if approve_hash:
                        logger.info("%s pre-approve wallet#%s tx sent: %s", mode_prefix, pre_wallet_number, approve_hash)
                        if not _wait_tx_receipt(pre_exec, approve_hash, timeout_sec=180):
                            raise RuntimeError("approve transaction failed or timed out")
                    prepared_tx: Optional[dict] = None
                    if available_usdc >= prepared_amount > 0:
                        amount_raw = decimal_to_raw(prepared_amount, quote_token.decimals)
                        expected_out = (
                            prepared_amount / current.price_usd
                            if current.price_usd > 0
                            else Decimal("0")
                        )
                        min_out_raw = (
                            max(1, decimal_to_raw(expected_out * Decimal("0.7"), current.decimals))
                            if expected_out > 0
                            else 1
                        )
                        prepared_tx = pre_exec.prepare_launchpad_buy_transaction(
                            launchpad_address=current.launchpad_address,
                            amount_in_raw=amount_raw,
                            min_amount_out_raw=min_out_raw,
                        )
                    return pre_wallet_number, pre_exec, prepared_amount, available_usdc, prepared_tx

                prepare_workers = min(16, len(wallet_records))
                with ThreadPoolExecutor(max_workers=prepare_workers, thread_name_prefix="launch-arm") as executor:
                    futures = {
                        executor.submit(_prepare_launch_wallet, record): record[0] + 1
                        for record in wallet_records
                    }
                    for future in as_completed(futures):
                        pre_wallet_number = futures[future]
                        try:
                            _, pre_exec, prepared_amount, available_usdc, prepared_tx = future.result()
                            preapproved_wallet_numbers.add(pre_wallet_number)
                            if prepared_tx is not None:
                                prepared_fast_wallets[pre_wallet_number] = (pre_exec, prepared_amount, prepared_tx)
                                logger.info(
                                    "%s wallet#%s launch buy armed | amount=%s USDC.E | nonce=%s",
                                    mode_prefix,
                                    pre_wallet_number,
                                    _format_decimal_plain(prepared_amount),
                                    prepared_tx.get("nonce"),
                                )
                            else:
                                logger.warning(
                                    "%s wallet#%s not armed | USDC.E=%s required=%s; will use fallback after fast broadcasts",
                                    mode_prefix,
                                    pre_wallet_number,
                                    _format_decimal_plain(available_usdc),
                                    _format_decimal_plain(prepared_amount),
                                )
                        except Exception as exc:
                            logger.warning("%s pre-approve wallet#%s failed: %s", mode_prefix, pre_wallet_number, exc)
                preapproved_launchpad = current.launchpad_address.lower()
                if prepared_fast_wallets:
                    armed_candidate = current
                    # Start broadcasts on the next loop iteration without an
                    # additional metadata polling delay.
                    continue
                else:
                    # A temporary RPC/proxy failure must not permanently disable
                    # preparation for this launchpad on the next metadata pass.
                    preapproved_launchpad = ""
                    next_metadata_refresh_at = time.time() + float(poll_interval)
            # The API can report FRACTIONALIZED before the scheduled launch time.
            # In fast pre-approved mode only the on-chain buy probe may arm the broadcast.
            if (
                current
                and _is_currently_bonding_token(current, quote_token)
                and not (preapprove_before_launch and prepared_fast_wallets)
            ):
                selected_candidate = current
                candidates = [current]
                break
            status = "not found"
            if current:
                if current.pool_address:
                    status = f"already has pool={current.pool_address}"
                elif not current.launchpad_address:
                    status = "launchpad missing"
                else:
                    status = f"status={current.status or 'unknown'}"
                last_metadata_status = status
            else:
                status = last_metadata_status
            if launch_wait_deadline is not None and time.time() >= launch_wait_deadline:
                raise RuntimeError(f"{specific_domain} did not enter active bonding state before timeout; last status: {status}")
            logger.info(
                "%s waiting for %s active bonding | attempt=%s | last_status=%s | next_check=%ss",
                mode_prefix,
                specific_domain,
                attempt,
                status,
                _format_decimal_plain(poll_interval),
            )
            time.sleep(float(poll_interval))
    elif selection in {"sell_specific", "sell_at_curve"}:
        selected_candidate = shared_api.fetch_fractional_token_by_name(specific_domain)
        if selected_candidate is None:
            raise RuntimeError(f"{specific_domain} launchpad token not found")
        if not _is_launchpad_sellable_token(selected_candidate, quote_token):
            raise RuntimeError(f"{specific_domain} is not sellable through launchpad")
        candidates = [selected_candidate]
    else:
        catalog = _fetch_fractional_tokens_with_same_proxy_retry(
            shared_api,
            logger,
            mode_tag,
            take=100,
            max_pages=10,
        )
        candidates = [info for info in catalog if _is_currently_bonding_token(info, quote_token)]
        if not candidates:
            raise RuntimeError("No active bonding tokens found (status=FRACTIONALIZED, launchpad present, pool absent)")
        if selection == "daily_quest":
            selected_candidate = _select_bonding_token_by_tvl(candidates)
            logger.info(
                "%s automatically selected highest-TVL active token | token=%s | tvl=$%s | volume=$%s",
                mode_prefix,
                selected_candidate.name.lower(),
                _format_decimal_plain(selected_candidate.tvl_usd),
                _format_decimal_plain(selected_candidate.volume_usd),
            )
        else:
            selected_candidate = _prompt_bonding_token_by_fdv(candidates)

    if selection == "sell_at_curve":
        monitor_line_idx, monitor_wallet, monitor_private_key = wallet_records[0]
        monitor_proxies, monitor_skip = _proxy_for_line(cfg, monitor_line_idx, logger, f"{mode_tag}_CURVE")
        if monitor_skip or not monitor_proxies:
            raise RuntimeError("proxy is required for bonding curve monitoring")
        monitor_exec = _build_exec_client_with_rpc_fallback(
            cfg,
            logger,
            monitor_wallet,
            monitor_private_key,
            proxies=monitor_proxies,
            log_prefix=mode_prefix,
        )
        check_attempt = 0
        while True:
            check_attempt += 1
            try:
                _, _, curve_percent = monitor_exec.get_launchpad_bonding_progress(
                    selected_candidate.launchpad_address,
                )
                if curve_percent >= bonding_target_percent:
                    logger.info(
                        "%s bonding target reached | token=%s | current=%s%% | target=%s%% | selling starts now",
                        mode_prefix,
                        selected_candidate.name.lower(),
                        _format_decimal_plain(curve_percent),
                        _format_decimal_plain(bonding_target_percent),
                    )
                    break
                logger.info(
                    "%s waiting for bonding curve | token=%s | current=%s%% | target=%s%% | attempt=%s | next_check=%ss",
                    mode_prefix,
                    selected_candidate.name.lower(),
                    _format_decimal_plain(curve_percent),
                    _format_decimal_plain(bonding_target_percent),
                    check_attempt,
                    _format_decimal_plain(poll_interval),
                )
            except Exception as exc:
                logger.warning(
                    "%s bonding curve check failed | token=%s | attempt=%s | error=%s | retrying in %ss",
                    mode_prefix,
                    selected_candidate.name.lower(),
                    check_attempt,
                    exc,
                    _format_decimal_plain(poll_interval),
                )
            time.sleep(float(poll_interval))

    logger.info(
        "%s mode started | wallets=%s | start_wallet=%s | end_wallet=%s | order=%s | selection=%s | action=%s | token=%s | candidate_rank=%s | tvl=$%s | amount=%s | active_tokens=%s | delay=%s-%s sec | launch_detection=%s",
        mode_prefix,
        len(wallet_records),
        start_number,
        end_number,
        order,
        selection,
        "sell-only" if action == "sell" else ("buy-only" if action == "buy" else "buy+sell"),
        selected_candidate.name.lower(),
        candidates.index(selected_candidate) + 1 if candidates else "n/a",
        _format_decimal_plain(selected_candidate.tvl_usd),
        (
            f"{_format_decimal_plain(buy_amount_min)}-{_format_decimal_plain(buy_amount_max)}%"
            if bonding_amount_mode == "sell_percent"
            else (
                f"{_format_decimal_plain(buy_amount_min)}-{_format_decimal_plain(buy_amount_max)} tokens"
                if bonding_amount_mode == "sell_number"
                else ("all available USDC.E" if bonding_amount_mode in {"all_usdc", "daily_all_usdc"} else f"{_format_decimal_plain(buy_amount_min)}-{_format_decimal_plain(buy_amount_max)} USDC.E")
            )
        ),
        len(candidates),
        _format_decimal_plain(delay_min),
        _format_decimal_plain(delay_max),
        (
            f"exact_utc:{launch_time_utc.isoformat()}"
            if launch_time_utc is not None
            else "immediate_rebroadcast"
        ),
    )
    if selection == "sell_at_curve":
        logger.info(
            "%s curve-triggered sell enabled | target=%s%% | check_interval=%ss",
            mode_prefix,
            _format_decimal_plain(bonding_target_percent),
            _format_decimal_plain(poll_interval),
        )
    if selection == "daily_quest":
        logger.info(
            "%s volume loop enabled | target=%s USDC.E | initial_min=%s USDC.E | swap_amount=all available USDC.E | ETH bootstrap below initial minimum | gas_reserve=$%s | swap_delay=%s-%s sec | return_asset=USDC.E",
            mode_prefix,
            _format_decimal_plain(daily_target_volume),
            _format_decimal_plain(BONDING_DAILY_INITIAL_MIN_USDCE),
            _format_decimal_plain(BONDING_DAILY_GAS_RESERVE_USD),
            _format_decimal_plain(daily_swap_delay_min),
            _format_decimal_plain(daily_swap_delay_max),
        )
    if preapprove_before_launch:
        logger.info(
            "%s fast specific launch mode enabled | preapprove=yes | poll=%ss",
            mode_prefix,
            _format_decimal_plain(poll_interval),
        )
    if fast_broadcast_after_launch:
        logger.info(
            "%s fast broadcast enabled | buy txs will be sent first, receipts checked after broadcast",
            mode_prefix,
        )

    success_wallets = failed_wallets = skipped_wallets = 0
    failed_entries: List[str] = []
    pending_fast_buys: List[Tuple[int, str, EvmExecutionClient, str, bytes, str, LaunchpadTokenInfo, Decimal]] = []
    fast_broadcasted_wallet_numbers: set[int] = set()

    if (
        fast_broadcast_after_launch
        and prepared_fast_wallets
        and cfg.enable_execution
        and not cfg.paper_mode
        and not cfg.dry_run
    ):
        wallet_by_number = {
            line_idx + 1: wallet
            for line_idx, wallet, _ in wallet_records
        }
        logger.info(
            "%s launch detected | parallel broadcast starting | armed_wallets=%s",
            mode_prefix,
            len(prepared_fast_wallets),
        )

        def _broadcast_prepared_buy(wallet_number: int) -> Tuple[int, str, EvmExecutionClient, Decimal, str, bytes]:
            exec_client, prepared_amount, prepared_tx = prepared_fast_wallets[wallet_number]
            raw_transaction = exec_client.sign_prepared_transaction(prepared_tx)
            attempt_number = 0
            while True:
                attempt_number += 1
                try:
                    tx_hash = exec_client.broadcast_signed_transaction(raw_transaction)
                    return wallet_number, wallet_by_number[wallet_number], exec_client, prepared_amount, tx_hash, raw_transaction
                except Exception as exc:
                    logger.warning(
                        "%s wallet#%s initial broadcast attempt=%s failed: %s | retrying in %ss",
                        mode_prefix,
                        wallet_number,
                        attempt_number,
                        exc,
                        _format_decimal_plain(poll_interval),
                    )
                    time.sleep(float(poll_interval))

        workers = min(64, len(prepared_fast_wallets))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="launch-buy") as executor:
            futures = {
                executor.submit(_broadcast_prepared_buy, wallet_number): wallet_number
                for wallet_number in prepared_fast_wallets
                if wallet_number in wallet_by_number
            }
            for future in as_completed(futures):
                wallet_number = futures[future]
                wallet = wallet_by_number[wallet_number]
                try:
                    _, _, exec_client, amount, tx_hash, raw_transaction = future.result()
                    fast_broadcasted_wallet_numbers.add(wallet_number)
                    pending_fast_buys.append(
                        (
                            wallet_number,
                            wallet,
                            exec_client,
                            tx_hash,
                            raw_transaction,
                            selected_candidate.name.lower(),
                            selected_candidate,
                            amount,
                        )
                    )
                    logger.info(
                        "%s wallet#%s launch buy broadcasted | amount=%s USDC.E | tx=%s",
                        mode_prefix,
                        wallet_number,
                        _format_decimal_plain(amount),
                        tx_hash,
                    )
                except Exception as exc:
                    logger.warning(
                        "%s wallet#%s fast broadcast failed: %s | using fallback",
                        mode_prefix,
                        wallet_number,
                        exc,
                    )

    for position, (line_idx, wallet, private_key) in enumerate(wallet_records, start=1):
        wallet_number = line_idx + 1
        if wallet_number in fast_broadcasted_wallet_numbers:
            continue
        logger.info("%s wallet %s/%s | wallet#%s - %s", mode_prefix, position, len(wallet_records), wallet_number, wallet)
        proxies, skip_wallet = _proxy_for_line(cfg, line_idx, logger, mode_tag)
        if skip_wallet or not proxies:
            skipped_wallets += 1
            reason = "proxy is required"
            failed_entries.append(f"wallet#{wallet_number} | skipped: {reason}")
            logger.warning("%s wallet=%s skipped | %s", mode_prefix, wallet, reason)
            continue

        amount = Decimal("0")
        token_name = ""
        buy_tx_hash = ""
        sell_tx_hash = ""
        try:
            doma_api = DomaApiClient(
                cfg.doma_api_url,
                api_keys=[cfg.doma_api_key, *cfg.doma_api_keys, *cfg.file_api_keys],
                proxies=proxies,
            )
            current = doma_api.fetch_fractional_token_by_name(selected_candidate.name)
            token_ok = _is_launchpad_sellable_token(current, quote_token) if (current and action == "sell") else (current is not None and _is_currently_bonding_token(current, quote_token))
            if current is None or not token_ok:
                skipped_wallets += 1
                reason = (
                    f"selected token {selected_candidate.name.lower()} is not sellable through launchpad"
                    if action == "sell"
                    else f"selected token {selected_candidate.name.lower()} is no longer in active bonding state"
                )
                failed_entries.append(f"wallet#{wallet_number} | skipped: {reason}")
                logger.warning("%s wallet=%s skipped | %s", mode_prefix, wallet, reason)
                continue
            token_name = current.name.strip().lower()
            exec_client = _build_exec_client_with_rpc_fallback(
                cfg,
                logger,
                wallet,
                private_key,
                proxies=proxies,
                log_prefix=mode_prefix,
            )
            eth_price = _fetch_eth_price_via_doma_quote(cfg, doma_api, quote_token)
            if action == "sell":
                sell_amount_expr = _pick_random_amount_expr(
                    "percent" if bonding_amount_mode == "sell_percent" else "number",
                    buy_amount_min,
                    buy_amount_max,
                    state,
                    min_raw=buy_amount_min_raw,
                    max_raw=buy_amount_max_raw,
                )
                launchpad_token = _token_from_launchpad(current)
                token_balance = exec_client.get_erc20_balance(launchpad_token.address, launchpad_token.decimals)
                if token_balance <= 0:
                    skipped_wallets += 1
                    reason = f"no {launchpad_token.symbol} balance"
                    failed_entries.append(f"wallet#{wallet_number} | skipped: {reason}")
                    logger.warning("%s wallet=%s skipped | %s", mode_prefix, wallet, reason)
                    continue
                logger.info(
                    "%s wallet=%s sell-only | token=%s | balance=%s | amount=%s",
                    mode_prefix,
                    wallet,
                    launchpad_token.symbol,
                    _format_decimal_plain(token_balance),
                    sell_amount_expr,
                )
                sell_ok = _execute_bonding_sell_with_route_fallback(
                    cfg=cfg,
                    logger=logger,
                    state=state,
                    doma_api=doma_api,
                    exec_client=exec_client,
                    launchpad=current,
                    quote_token=quote_token,
                    trade_amount_expr=sell_amount_expr,
                    eth_price=eth_price,
                    label=f"{mode_tag} {wallet} {canonical_symbol(current.symbol or current.name)}>USDC.E SELL_ONLY",
                    wait_for_pre_tx=True,
                    post_approve_delay_range=(float(delay_min), float(delay_max)),
                    refresh_launchpad=lambda: doma_api.fetch_fractional_token_by_name(selected_candidate.name),
                )
                sell_tx_hash = state.last_tx_hash if sell_ok else ""
                if not sell_ok or not sell_tx_hash or not _wait_tx_receipt(exec_client, sell_tx_hash, timeout_sec=180):
                    raise RuntimeError("bonding sell transaction failed or timed out")
                logger.info("%s wallet=%s token=%s sold | amount=%s | tx=%s", mode_prefix, wallet, token_name, sell_amount_expr, sell_tx_hash)
                success_wallets += 1
                append_csv(
                    cfg.points_csv_file.parent / DOMAIN_BONDING_BUYS_CSV.name,
                    [datetime.now(timezone.utc).isoformat(), "ok", wallet, wallet_number, token_name, current.address, current.launchpad_address, current.status, sell_amount_expr, current.price_usd, f"sell={sell_tx_hash}", "action=sell"],
                    delimiter=cfg.csv_delimiter,
                )
                if position < len(wallet_records):
                    delay_sec = random.uniform(float(delay_min), float(delay_max))
                    logger.info("%s delay before next wallet: %.2f sec", mode_prefix, delay_sec)
                    time.sleep(delay_sec)
                continue
            if selection == "daily_quest":
                daily_recovered_volume = Decimal("0")
                daily_recovery_hash = ""
                recovery_current = doma_api.fetch_fractional_token_by_name(selected_candidate.name)
                if recovery_current is None:
                    raise RuntimeError(f"{selected_candidate.name} launchpad token not found")
                recovery_token = _token_from_launchpad(recovery_current)
                recovery_balance = exec_client.get_erc20_balance(recovery_token.address, recovery_token.decimals)
                if decimal_to_raw(recovery_balance, recovery_token.decimals) > 0:
                    logger.warning(
                        "%s wallet=%s found leftover %s=%s from an interrupted run | selling before new buys",
                        mode_prefix,
                        wallet,
                        recovery_token.symbol,
                        _format_decimal_plain(recovery_balance),
                    )
                    recovery_usdc_before = exec_client.get_erc20_balance(quote_token.address, quote_token.decimals)
                    recovery_ok = _execute_bonding_sell_with_route_fallback(
                        cfg=cfg,
                        logger=logger,
                        state=state,
                        doma_api=doma_api,
                        exec_client=exec_client,
                        launchpad=recovery_current,
                        quote_token=quote_token,
                        trade_amount_expr=_format_decimal_plain(recovery_balance),
                        eth_price=eth_price,
                        label=f"{mode_tag} {wallet} {recovery_token.symbol}>USDC.E RECOVERY",
                        wait_for_pre_tx=True,
                        post_approve_delay_range=(float(daily_swap_delay_min), float(daily_swap_delay_max)),
                        refresh_launchpad=lambda: doma_api.fetch_fractional_token_by_name(selected_candidate.name),
                    )
                    daily_recovery_hash = state.last_tx_hash if recovery_ok else ""
                    if not recovery_ok or not daily_recovery_hash or not _wait_tx_receipt(exec_client, daily_recovery_hash, timeout_sec=180):
                        raise RuntimeError("daily bonding recovery sell failed or timed out; no new buy was made")
                    recovery_usdc_after = exec_client.get_erc20_balance(quote_token.address, quote_token.decimals)
                    daily_recovered_volume = max(Decimal("0"), recovery_usdc_after - recovery_usdc_before)
                    logger.info(
                        "%s wallet=%s recovery complete | sell_volume=%s USDC.E",
                        mode_prefix,
                        wallet,
                        _format_decimal_plain(daily_recovered_volume),
                    )
                prepared_ok, prepared_reason, amount = _prepare_all_usdce_for_bonding_daily(
                    cfg,
                    logger,
                    state,
                    doma_api,
                    exec_client,
                    quote_token,
                    weth_token,
                    wallet,
                    eth_price,
                )
                if not prepared_ok:
                    skipped_wallets += 1
                    failed_entries.append(f"wallet#{wallet_number} | skipped: {prepared_reason}")
                    logger.warning("%s wallet=%s skipped | %s", mode_prefix, wallet, prepared_reason)
                    continue
            elif bonding_amount_mode == "all_usdc":
                prepared_ok, prepared_reason, amount = _prepare_all_usdce_for_bonding_daily(
                    cfg,
                    logger,
                    state,
                    doma_api,
                    exec_client,
                    quote_token,
                    weth_token,
                    wallet,
                    eth_price,
                    log_prefix=mode_tag,
                )
                if not prepared_ok:
                    skipped_wallets += 1
                    reason = prepared_reason
                    failed_entries.append(f"wallet#{wallet_number} | skipped: {reason}")
                    logger.warning("%s wallet=%s skipped | %s", mode_prefix, wallet, reason)
                    continue
            else:
                prepared = prepared_fast_wallets.get(wallet_number)
                amount = (
                    prepared[1]
                    if prepared is not None
                    else _random_decimal_between(buy_amount_min, buy_amount_max, buy_amount_min_raw, buy_amount_max_raw)
                )
                can_fund, current_usdc, spendable_eth_usd, total_spendable_usd = _can_fully_fund_usdce_topup(
                    exec_client,
                    quote_token,
                    eth_price,
                    amount,
                )
                if not can_fund:
                    skipped_wallets += 1
                    reason = (
                        "insufficient combined balance before swaps: "
                        f"USDC.E={_format_decimal_plain(current_usdc)}, "
                        f"spendable_ETH=${_format_decimal_plain(spendable_eth_usd)}, "
                        f"total=${_format_decimal_plain(total_spendable_usd)}, "
                        f"required={_format_decimal_plain(amount)} USDC.E plus conversion buffer"
                    )
                    failed_entries.append(f"wallet#{wallet_number} | skipped: {reason}")
                    logger.warning("%s wallet=%s skipped before swaps | %s", mode_prefix, wallet, reason)
                    continue
                logger.info(
                    "%s wallet=%s preflight balance ok | USDC.E=%s | spendable_ETH=$%s | total_spendable=$%s | target=%s USDC.E",
                    mode_prefix,
                    wallet,
                    _format_decimal_plain(current_usdc),
                    _format_decimal_plain(spendable_eth_usd),
                    _format_decimal_plain(total_spendable_usd),
                    _format_decimal_plain(amount),
                )
                topup_ok, topup_reason, available_usdc, _ = _top_up_usdce_from_eth_for_cheap_buy(
                    cfg,
                    logger,
                    state,
                    doma_api,
                    exec_client,
                    quote_token,
                    weth_token,
                    wallet,
                    eth_price,
                    amount,
                    log_prefix=mode_tag,
                )
                if not topup_ok:
                    skipped_wallets += 1
                    reason = f"insufficient balance after top-up: {topup_reason} (USDC.E={_format_decimal_plain(available_usdc)})"
                    failed_entries.append(f"wallet#{wallet_number} | skipped: {reason}")
                    logger.warning("%s wallet=%s skipped | %s", mode_prefix, wallet, reason)
                    continue

            if selection == "daily_quest":
                accumulated_volume = daily_recovered_volume
                cycle_number = 0
                cycle_tx_hashes: List[str] = [daily_recovery_hash] if daily_recovery_hash else []
                current = recovery_current
                token_name = current.name.strip().lower()

                while accumulated_volume < daily_target_volume:
                    cycle_number += 1
                    available_usdc = exec_client.get_erc20_balance(quote_token.address, quote_token.decimals)
                    cycle_amount = available_usdc
                    if decimal_to_raw(cycle_amount, quote_token.decimals) <= 0:
                        raise RuntimeError(
                            "daily bonding volume stopped: no USDC.E remains for the next swap "
                            f"(progress={_format_decimal_plain(accumulated_volume)}/"
                            f"{_format_decimal_plain(daily_target_volume)})"
                        )

                    current = doma_api.fetch_fractional_token_by_name(selected_candidate.name)
                    if current is None or not _is_currently_bonding_token(current, quote_token):
                        raise RuntimeError(
                            f"daily bonding volume stopped at {_format_decimal_plain(accumulated_volume)} USDC.E: "
                            f"{selected_candidate.name.lower()} is no longer in active bonding state"
                        )
                    token_name = current.name.strip().lower()
                    token_balance_before = exec_client.get_erc20_balance(current.address, current.decimals)
                    logger.info(
                        "%s wallet=%s cycle=%s | progress=%s/%s USDC.E | buy=%s USDC.E | token=%s",
                        mode_prefix,
                        wallet,
                        cycle_number,
                        _format_decimal_plain(accumulated_volume),
                        _format_decimal_plain(daily_target_volume),
                        _format_decimal_plain(cycle_amount),
                        token_name,
                    )
                    buy_ok = _execute_launchpad_buy(
                        cfg=cfg,
                        logger=logger,
                        state=state,
                        exec_client=exec_client,
                        launchpad=current,
                        quote_token=quote_token,
                        trade_amount_expr=f"${_format_decimal_plain(cycle_amount)}",
                        eth_price=eth_price,
                        label=f"{mode_tag} {wallet} USDC.E>{canonical_symbol(current.symbol or current.name)} CYCLE_{cycle_number}",
                        wait_for_pre_tx=True,
                        post_approve_delay_range=(float(daily_swap_delay_min), float(daily_swap_delay_max)),
                    )
                    cycle_buy_tx = state.last_tx_hash if buy_ok else ""
                    if not buy_ok or not cycle_buy_tx or not _wait_tx_receipt(exec_client, cycle_buy_tx, timeout_sec=180):
                        raise RuntimeError(f"daily bonding buy failed or timed out on cycle {cycle_number}")
                    accumulated_volume += cycle_amount

                    token_balance_after = exec_client.get_erc20_balance(current.address, current.decimals)
                    bought_token_amount = token_balance_after - token_balance_before
                    if bought_token_amount <= 0:
                        raise RuntimeError(
                            f"daily bonding buy confirmed on cycle {cycle_number}, but token balance did not increase"
                        )

                    delay_before_sell = random.uniform(
                        float(daily_swap_delay_min),
                        float(daily_swap_delay_max),
                    )
                    if delay_before_sell > 0:
                        logger.info(
                            "%s wallet=%s cycle=%s | delay before sell: %.2f sec",
                            mode_prefix,
                            wallet,
                            cycle_number,
                            delay_before_sell,
                        )
                        time.sleep(delay_before_sell)

                    usdc_before_sell = exec_client.get_erc20_balance(quote_token.address, quote_token.decimals)
                    sell_ok = _execute_bonding_sell_with_route_fallback(
                        cfg=cfg,
                        logger=logger,
                        state=state,
                        doma_api=doma_api,
                        exec_client=exec_client,
                        launchpad=current,
                        quote_token=quote_token,
                        trade_amount_expr=_format_decimal_plain(bought_token_amount),
                        eth_price=eth_price,
                        label=f"{mode_tag} {wallet} {canonical_symbol(current.symbol or current.name)}>USDC.E CYCLE_{cycle_number}",
                        wait_for_pre_tx=True,
                        post_approve_delay_range=(float(daily_swap_delay_min), float(daily_swap_delay_max)),
                        refresh_launchpad=lambda: doma_api.fetch_fractional_token_by_name(selected_candidate.name),
                    )
                    cycle_sell_tx = state.last_tx_hash if sell_ok else ""
                    if not sell_ok or not cycle_sell_tx or not _wait_tx_receipt(exec_client, cycle_sell_tx, timeout_sec=180):
                        raise RuntimeError(
                            f"daily bonding sell failed or timed out on cycle {cycle_number}; purchased tokens remain in wallet"
                        )
                    usdc_after_sell = exec_client.get_erc20_balance(quote_token.address, quote_token.decimals)
                    sell_volume = max(Decimal("0"), usdc_after_sell - usdc_before_sell)
                    accumulated_volume += sell_volume
                    cycle_tx_hashes.extend((cycle_buy_tx, cycle_sell_tx))
                    logger.info(
                        "%s wallet=%s cycle=%s complete | buy_volume=%s | sell_volume=%s | total_volume=%s/%s USDC.E",
                        mode_prefix,
                        wallet,
                        cycle_number,
                        _format_decimal_plain(cycle_amount),
                        _format_decimal_plain(sell_volume),
                        _format_decimal_plain(accumulated_volume),
                        _format_decimal_plain(daily_target_volume),
                    )

                    if accumulated_volume < daily_target_volume:
                        delay_before_next_cycle = random.uniform(
                            float(daily_swap_delay_min),
                            float(daily_swap_delay_max),
                        )
                        if delay_before_next_cycle > 0:
                            logger.info(
                                "%s wallet=%s delay before next cycle: %.2f sec",
                                mode_prefix,
                                wallet,
                                delay_before_next_cycle,
                            )
                            time.sleep(delay_before_next_cycle)

                success_wallets += 1
                logger.info(
                    "%s wallet=%s target reached | cycles=%s | actual_volume=%s/%s USDC.E | final_asset=USDC.E",
                    mode_prefix,
                    wallet,
                    cycle_number,
                    _format_decimal_plain(accumulated_volume),
                    _format_decimal_plain(daily_target_volume),
                )
                append_csv(
                    cfg.points_csv_file.parent / DOMAIN_BONDING_BUYS_CSV.name,
                    [
                        datetime.now(timezone.utc).isoformat(),
                        "ok",
                        wallet,
                        wallet_number,
                        token_name,
                        current.address,
                        current.launchpad_address,
                        current.status,
                        accumulated_volume,
                        current.price_usd,
                        ";".join(cycle_tx_hashes),
                        f"action=daily_volume;cycles={cycle_number};target={_format_decimal_plain(daily_target_volume)}",
                    ],
                    delimiter=cfg.csv_delimiter,
                )
                if position < len(wallet_records):
                    delay_sec = random.uniform(float(delay_min), float(delay_max))
                    logger.info("%s delay before next wallet: %.2f sec", mode_prefix, delay_sec)
                    time.sleep(delay_sec)
                continue

            logger.info(
                "%s wallet=%s token=%s | status=%s | price=$%s | buy=%s USDC.E | launchpad=%s",
                mode_prefix,
                wallet,
                token_name,
                current.status,
                _format_decimal_plain(current.price_usd),
                _format_decimal_plain(amount),
                current.launchpad_address,
            )
            token_balance_before = exec_client.get_erc20_balance(current.address, current.decimals)
            while True:
                ok = _execute_launchpad_buy(
                    cfg=cfg,
                    logger=logger,
                    state=state,
                    exec_client=exec_client,
                    launchpad=current,
                    quote_token=quote_token,
                    trade_amount_expr=f"${_format_decimal_plain(amount)}",
                    eth_price=eth_price,
                    label=f"{mode_tag} {wallet} USDC.E>{canonical_symbol(current.symbol or current.name)}",
                    wait_for_pre_tx=True,
                    post_approve_delay_range=(float(delay_min), float(delay_max)),
                    skip_allowance_check=wallet_number in preapproved_wallet_numbers,
                    probe_before_send=selection == "specific",
                    quiet_probe_fail=True,
                )
                if ok:
                    break
                last_error = getattr(state, "last_error", "")
                not_ready = "0xa1fa02b3" in last_error
                can_wait_more = launch_wait_deadline is None or time.time() < launch_wait_deadline
                if selection == "specific" and not_ready and can_wait_more:
                    logger.warning(
                        "%s wallet=%s launchpad not ready yet for %s | retry_in=%ss | error=%s",
                        mode_prefix,
                        wallet,
                        token_name,
                        _format_decimal_plain(poll_interval),
                        last_error,
                    )
                    time.sleep(float(poll_interval))
                    refreshed = doma_api.fetch_fractional_token_by_name(selected_candidate.name)
                    if refreshed and _is_currently_bonding_token(refreshed, quote_token):
                        current = refreshed
                    continue
                break
            buy_tx_hash = state.last_tx_hash if ok else ""
            if not ok or not buy_tx_hash:
                raise RuntimeError("bonding buy transaction was not sent")
            if fast_broadcast_after_launch:
                pending_fast_buys.append((wallet_number, wallet, exec_client, buy_tx_hash, b"", token_name, current, amount))
                logger.info(
                    "%s wallet=%s token=%s buy broadcasted | amount=%s USDC.E | tx=%s",
                    mode_prefix,
                    wallet,
                    token_name,
                    _format_decimal_plain(amount),
                    buy_tx_hash,
                )
                continue
            if not _wait_tx_receipt(exec_client, buy_tx_hash, timeout_sec=180):
                raise RuntimeError("bonding buy transaction failed or timed out")
            logger.info("%s wallet=%s token=%s bought | amount=%s USDC.E | tx=%s", mode_prefix, wallet, token_name, _format_decimal_plain(amount), buy_tx_hash)

            if action == "buy_sell":
                token_balance_after = exec_client.get_erc20_balance(current.address, current.decimals)
                bought_token_amount = token_balance_after - token_balance_before
                if bought_token_amount <= 0:
                    raise RuntimeError("buy confirmed but purchased token balance did not increase")
                delay_before_sell = _random_swap_delay_sec()
                logger.info(
                    "%s wallet=%s delay before sell: %.2f sec | token_amount=%s %s",
                    mode_prefix,
                    wallet,
                    delay_before_sell,
                    _format_decimal_plain(bought_token_amount),
                    canonical_symbol(current.symbol or current.name),
                )
                time.sleep(delay_before_sell)
                sell_ok = _execute_bonding_sell_with_route_fallback(
                    cfg=cfg,
                    logger=logger,
                    state=state,
                    doma_api=doma_api,
                    exec_client=exec_client,
                    launchpad=current,
                    quote_token=quote_token,
                    trade_amount_expr=_format_decimal_plain(bought_token_amount),
                    eth_price=eth_price,
                    label=f"{mode_tag} {wallet} {canonical_symbol(current.symbol or current.name)}>USDC.E SELL",
                    wait_for_pre_tx=True,
                    refresh_launchpad=lambda: doma_api.fetch_fractional_token_by_name(selected_candidate.name),
                )
                sell_tx_hash = state.last_tx_hash if sell_ok else ""
                if not sell_ok or not sell_tx_hash or not _wait_tx_receipt(exec_client, sell_tx_hash, timeout_sec=180):
                    raise RuntimeError("buy succeeded but bonding sell transaction failed or timed out; purchased tokens remain in wallet")
                logger.info(
                    "%s wallet=%s token=%s sold | token_amount=%s | tx=%s",
                    mode_prefix,
                    wallet,
                    token_name,
                    _format_decimal_plain(bought_token_amount),
                    sell_tx_hash,
                )

            success_wallets += 1
            tx_summary = buy_tx_hash if action == "buy" else f"buy={buy_tx_hash};sell={sell_tx_hash}"
            append_csv(
                cfg.points_csv_file.parent / DOMAIN_BONDING_BUYS_CSV.name,
                [datetime.now(timezone.utc).isoformat(), "ok", wallet, wallet_number, token_name, current.address, current.launchpad_address, current.status, amount, current.price_usd, tx_summary, f"action={action}"],
                delimiter=cfg.csv_delimiter,
            )
        except Exception as exc:
            failed_wallets += 1
            reason = str(exc)
            failed_entries.append(f"wallet#{wallet_number} | {token_name or 'no token'} | {reason}")
            logger.warning("%s wallet=%s failed: %s", mode_prefix, wallet, exc)
            append_csv(
                cfg.points_csv_file.parent / DOMAIN_BONDING_BUYS_CSV.name,
                [datetime.now(timezone.utc).isoformat(), "failed", wallet, wallet_number, token_name, "", "", "", amount, "", f"buy={buy_tx_hash};sell={sell_tx_hash}", reason],
                delimiter=cfg.csv_delimiter,
            )
        if position < len(wallet_records):
            delay_sec = random.uniform(float(delay_min), float(delay_max))
            logger.info("%s delay before next wallet: %.2f sec", mode_prefix, delay_sec)
            time.sleep(delay_sec)

    if pending_fast_buys:
        logger.info("%s waiting fast broadcast receipts | txs=%s", mode_prefix, len(pending_fast_buys))
        def _confirm_or_retry_fast_buy(
            pending: Tuple[int, str, EvmExecutionClient, str, bytes, str, LaunchpadTokenInfo, Decimal],
        ) -> Tuple[int, str, str, str, LaunchpadTokenInfo, Decimal, int]:
            wallet_number, wallet, exec_client, buy_tx_hash, raw_transaction, token_name, current, amount = pending
            retry_number = 0
            rebroadcast_number = 0
            while True:
                try:
                    receipt = exec_client.web3.eth.get_transaction_receipt(buy_tx_hash)
                except Exception:
                    receipt = None
                if receipt is not None:
                    if int(getattr(receipt, "status", 0)) == 1:
                        return wallet_number, wallet, buy_tx_hash, token_name, current, amount, retry_number
                else:
                    rebroadcast_number += 1
                    if raw_transaction:
                        try:
                            exec_client.broadcast_signed_transaction(raw_transaction)
                        except Exception as exc:
                            if rebroadcast_number == 1 or rebroadcast_number % 25 == 0:
                                logger.warning(
                                    "%s wallet=%s rebroadcast=%s failed: %s",
                                    mode_prefix,
                                    wallet,
                                    rebroadcast_number,
                                    exc,
                                )
                    time.sleep(max(0.05, float(poll_interval)))
                    continue

                retry_number += 1
                logger.warning(
                    "%s wallet=%s launch buy reverted | tx=%s | sending retry=%s",
                    mode_prefix,
                    wallet,
                    buy_tx_hash,
                    retry_number,
                )
                while True:
                    try:
                        expected_out = amount / current.price_usd if current.price_usd > 0 else Decimal("0")
                        min_out_raw = (
                            max(1, decimal_to_raw(expected_out * Decimal("0.7"), current.decimals))
                            if expected_out > 0
                            else 1
                        )
                        retry_tx = exec_client.prepare_launchpad_buy_transaction(
                            launchpad_address=current.launchpad_address,
                            amount_in_raw=decimal_to_raw(amount, quote_token.decimals),
                            min_amount_out_raw=min_out_raw,
                        )
                        raw_transaction = exec_client.sign_prepared_transaction(retry_tx)
                        buy_tx_hash = exec_client.broadcast_signed_transaction(raw_transaction)
                        logger.info(
                            "%s wallet=%s retry=%s broadcasted | amount=%s USDC.E | nonce=%s | tx=%s",
                            mode_prefix,
                            wallet,
                            retry_number,
                            _format_decimal_plain(amount),
                            retry_tx.get("nonce"),
                            buy_tx_hash,
                        )
                        break
                    except Exception as exc:
                        logger.warning(
                            "%s wallet=%s retry=%s send failed: %s | retrying in %ss",
                            mode_prefix,
                            wallet,
                            retry_number,
                            exc,
                            _format_decimal_plain(poll_interval),
                        )
                        time.sleep(float(poll_interval))

        receipt_workers = min(64, len(pending_fast_buys))
        with ThreadPoolExecutor(max_workers=receipt_workers, thread_name_prefix="launch-retry") as executor:
            futures = {
                executor.submit(_confirm_or_retry_fast_buy, pending): pending[0]
                for pending in pending_fast_buys
            }
            for future in as_completed(futures):
                wallet_number, wallet, buy_tx_hash, token_name, current, amount, retry_number = future.result()
                success_wallets += 1
                logger.info(
                    "%s wallet=%s token=%s bought confirmed | amount=%s USDC.E | tx=%s | retries=%s",
                    mode_prefix,
                    wallet,
                    token_name,
                    _format_decimal_plain(amount),
                    buy_tx_hash,
                    retry_number,
                )
                append_csv(
                    cfg.points_csv_file.parent / DOMAIN_BONDING_BUYS_CSV.name,
                    [datetime.now(timezone.utc).isoformat(), "ok", wallet, wallet_number, token_name, current.address, current.launchpad_address, current.status, amount, current.price_usd, buy_tx_hash, f"action=buy;retries={retry_number}"],
                    delimiter=cfg.csv_delimiter,
                )

    _print_mode_summary(
        mode_tag,
        total=len(wallet_records),
        success=success_wallets,
        failed=failed_wallets,
        skipped=skipped_wallets,
        failed_wallets=failed_entries,
    )


def _closeable_subdomains(items: List[StakedSubdomain]) -> Tuple[List[StakedSubdomain], List[Tuple[str, str]]]:
    closeable: List[StakedSubdomain] = []
    skipped: List[Tuple[str, str]] = []
    seen_ids: set[str] = set()
    for item in items:
        token_id = str(item.token_id or "").strip()
        if not token_id or token_id in seen_ids:
            continue
        seen_ids.add(token_id)
        if item.listed:
            skipped.append((item.name, "listed on marketplace"))
            continue
        if not item.fractional_token_address:
            skipped.append((item.name, "missing fractional token"))
            continue
        closeable.append(item)
    return closeable, skipped


def run_close_subdomains_once(cfg: BotConfig, logger: logging.Logger, state: BotState) -> None:
    picked = get_close_subdomains_menu_input()
    if not picked:
        logger.info("[SUBDOMAIN_CLOSE] mode canceled by user.")
        return
    max_to_close_raw, delay_min_raw, delay_max_raw = picked
    max_to_close = int(max_to_close_raw)
    delay_min = _parse_decimal_input(delay_min_raw)
    delay_max = _parse_decimal_input(delay_max_raw)

    wallet_key_records = _build_wallet_key_records(cfg, logger, "SUBDOMAIN_CLOSE")
    if not wallet_key_records:
        raise RuntimeError("No wallet/private-key pairs available for subdomain close")
    wallet_key_records, wallet_start_offset, total_loaded_wallets = _apply_wallet_start_selection(wallet_key_records)
    logger.info(
        "[SUBDOMAIN_CLOSE] mode started | wallets=%s | start_wallet=%s | max_per_wallet=%s | delay=%s-%s sec",
        total_loaded_wallets,
        wallet_start_offset + 1,
        "all" if max_to_close <= 0 else max_to_close,
        _format_decimal_plain(delay_min),
        _format_decimal_plain(delay_max),
    )

    success_wallets = failed_wallets = skipped_wallets = 0
    closed_total = failed_total = 0
    skipped_details: List[Tuple[int, str, str]] = []
    failed_wallet_addresses: List[str] = []

    for idx, (line_idx, wallet, private_key) in enumerate(wallet_key_records, start=1):
        wallet_number = line_idx + 1
        logger.info("[SUBDOMAIN_CLOSE] wallet %s", _wallet_record_progress_label(idx - 1, len(wallet_key_records), line_idx, total_loaded_wallets, wallet))
        proxies, skip_wallet = _proxy_for_line(cfg, line_idx, logger, "SUBDOMAIN_CLOSE")
        if skip_wallet or not proxies:
            skipped_wallets += 1
            reason = "no proxy"
            skipped_details.append((wallet_number, wallet, reason))
            logger.warning("[SUBDOMAIN_CLOSE] wallet=%s skipped | %s", wallet, reason)
            continue
        try:
            doma_api = DomaApiClient(cfg.doma_api_url, api_keys=[cfg.doma_api_key, *cfg.doma_api_keys, *cfg.file_api_keys], proxies=proxies)
            subdomains = doma_api.fetch_wallet_staked_subdomains(wallet, chain_id=cfg.chain_id, take=100, max_pages=20)
            closeable, per_item_skipped = _closeable_subdomains(subdomains)
            if max_to_close > 0:
                closeable = closeable[:max_to_close]
            if not closeable:
                skipped_wallets += 1
                reason = "no closeable staked subdomains"
                if per_item_skipped:
                    reason += " | " + "; ".join(f"{name}: {why}" for name, why in per_item_skipped[:3])
                skipped_details.append((wallet_number, wallet, reason))
                logger.info("[SUBDOMAIN_CLOSE] wallet=%s skipped | %s | found=%s", wallet, reason, len(subdomains))
                continue

            exec_client = _build_exec_client_with_rpc_fallback(cfg, logger, wallet, private_key, proxies=proxies, log_prefix="[SUBDOMAIN_CLOSE]")
            wallet_closed = wallet_failed = 0
            logger.info("[SUBDOMAIN_CLOSE] wallet=%s closeable=%s/%s", wallet, len(closeable), len(subdomains))
            for sub_idx, subdomain in enumerate(closeable, start=1):
                logger.info(
                    "[SUBDOMAIN_CLOSE] wallet=%s subdomain %s/%s close | %s | tokenId=%s",
                    wallet,
                    sub_idx,
                    len(closeable),
                    subdomain.name,
                    subdomain.token_id,
                )
                try:
                    tx_hash = exec_client.unstake_subdomain(subdomain.token_id)
                    wallet_closed += 1
                    closed_total += 1
                    logger.info("[SUBDOMAIN_CLOSE] wallet=%s subdomain=%s closed | tx=%s", wallet, subdomain.name, tx_hash)
                except Exception as exc:
                    wallet_failed += 1
                    failed_total += 1
                    logger.warning("[SUBDOMAIN_CLOSE] wallet=%s subdomain=%s close failed: %s", wallet, subdomain.name, exc)
                if sub_idx < len(closeable):
                    delay_sec = random.uniform(float(delay_min), float(delay_max))
                    logger.info("[SUBDOMAIN_CLOSE] delay before next subdomain: %.2f sec", delay_sec)
                    time.sleep(delay_sec)

            if wallet_closed > 0:
                success_wallets += 1
            elif wallet_failed > 0:
                failed_wallets += 1
                failed_wallet_addresses.append(wallet)
        except Exception as exc:
            failed_wallets += 1
            failed_wallet_addresses.append(wallet)
            logger.warning("[SUBDOMAIN_CLOSE] wallet=%s failed: %s", wallet, exc)
        if idx < len(wallet_key_records):
            delay_sec = random.uniform(float(delay_min), float(delay_max))
            logger.info("[SUBDOMAIN_CLOSE] delay before next wallet: %.2f sec", delay_sec)
            time.sleep(delay_sec)

    logger.info("[SUBDOMAIN_CLOSE] subdomains total | closed=%s failed=%s", closed_total, failed_total)
    if skipped_details:
        print("\n[SUBDOMAIN_CLOSE] skipped wallets:")
        for wallet_number, wallet, reason in skipped_details:
            line = f"  #{wallet_number} {wallet} | {reason}"
            logger.warning("[SUBDOMAIN_CLOSE] skipped | #%s %s | %s", wallet_number, wallet, reason)
            print(line)
    _print_mode_summary("SUBDOMAIN_CLOSE", total=len(wallet_key_records), success=success_wallets, failed=failed_wallets, skipped=skipped_wallets, failed_wallets=failed_wallet_addresses)


def run_com_daily_swap_once(cfg: BotConfig, logger: logging.Logger, state: BotState) -> None:
    picked = get_com_daily_swap_menu_input()
    if not picked:
        logger.info("[COM_DAILY] mode canceled by user.")
        return
    swap_min_raw, swap_max_raw, domains_min_raw, domains_max_raw, delay_min_raw, delay_max_raw = picked
    swap_min_usdc = _parse_decimal_input(swap_min_raw)
    swap_max_usdc = _parse_decimal_input(swap_max_raw)
    domains_min = int(_parse_decimal_input(domains_min_raw).to_integral_value(rounding=ROUND_FLOOR))
    domains_max = int(_parse_decimal_input(domains_max_raw).to_integral_value(rounding=ROUND_CEILING))
    delay_min = _parse_decimal_input(delay_min_raw)
    delay_max = _parse_decimal_input(delay_max_raw)

    wallet_key_records = _build_wallet_key_records(cfg, logger, "COM_DAILY")
    if not wallet_key_records:
        raise RuntimeError("No wallet/private-key pairs available for .com daily swaps")
    wallet_key_records, wallet_start_offset, total_loaded_wallets = _apply_wallet_start_selection(wallet_key_records)
    quote_token = _usdce_token_from_config(cfg)
    weth_token = _token_from_config_override(cfg, "WETH", 18)

    shared_doma_api, catalog, metadata_proxies = _fetch_fractional_tokens_with_wallet_proxy_fallback(
        cfg,
        logger,
        wallet_key_records,
        "COM_DAILY",
        take=100,
        max_pages=10,
    )
    top_com_tokens = _top_tvl_com_tokens(catalog, quote_token, max(10, domains_max + 5))
    if len(top_com_tokens) < domains_min:
        raise RuntimeError(f"Not enough eligible .com tokens by TVL: found {len(top_com_tokens)}, need at least {domains_min}")

    logger.info(
        "[COM_DAILY] mode started | wallets=%s | start_wallet=%s | swap=%s-%s USDC.E | domains=%s-%s | selected_top_by_tvl=%s | delay=%s-%s sec",
        total_loaded_wallets,
        wallet_start_offset + 1,
        _format_decimal_plain(swap_min_usdc),
        _format_decimal_plain(swap_max_usdc),
        domains_min,
        domains_max,
        ", ".join(f"{token.name}(${_format_decimal_plain(token.tvl_usd)})" for token in top_com_tokens),
        _format_decimal_plain(delay_min),
        _format_decimal_plain(delay_max),
    )

    swap_csv = cfg.trades_csv_file.parent / DOMAIN_COM_DAILY_CSV.name
    ensure_csv(
        swap_csv,
        [
            "timestamp_utc",
            "status",
            "wallet",
            "domain",
            "token_address",
            "pool_address",
            "tvl_usd",
            "swap_usdc",
            "tx_hash",
            "reason",
        ],
        delimiter=cfg.csv_delimiter,
    )

    success_wallets = failed_wallets = skipped_wallets = 0
    total_success_swaps = total_failed_swaps = 0
    failed_wallet_details: List[str] = []
    skipped_wallet_details: List[str] = []

    def _mark_com_daily_skipped(wallet_number: int, reason: str) -> None:
        nonlocal skipped_wallets
        skipped_wallets += 1
        skipped_wallet_details.append(_redact_wallet_addresses(f"wallet#{wallet_number}: {reason}"))

    for idx, (line_idx, wallet, private_key) in enumerate(wallet_key_records, start=1):
        proxies, skip_wallet = _proxy_for_line(cfg, line_idx, logger, "COM_DAILY")
        wallet_number = line_idx + 1
        wallet_log_prefix = f"[COM_DAILY wallet {wallet_number}/{total_loaded_wallets}]"
        logger.info("%s wallet=%s", wallet_log_prefix, wallet)
        if skip_wallet:
            _mark_com_daily_skipped(wallet_number, "proxy skipped/unavailable")
            continue

        wallet_success = wallet_failed = 0
        wallet_failure_reasons: List[str] = []
        try:
            doma_api = DomaApiClient(
                cfg.doma_api_url,
                api_key=cfg.doma_api_key,
                api_keys=cfg.doma_api_keys,
                proxies=proxies,
            )
            exec_client = _build_exec_client_with_rpc_fallback(
                cfg=cfg,
                logger=logger,
                wallet=wallet,
                private_key=private_key,
                proxies=proxies,
                log_prefix=wallet_log_prefix,
            )
            eth_price = _fetch_eth_price_via_doma_quote(cfg, doma_api, quote_token)

            quest_completed: Optional[bool] = None
            try:
                quest_completed = _is_com_daily_quest_completed(doma_api, wallet, cfg.chain_id)
            except Exception as exc:
                logger.warning("%s wallet=%s quest status fetch failed, using local CSV checker: %s", wallet_log_prefix, wallet, exc)
            if quest_completed is True:
                reason = "daily .com quest already completed by Doma API"
                _mark_com_daily_skipped(wallet_number, reason)
                logger.info("%s wallet=%s %s | skipping", wallet_log_prefix, wallet, reason)
                continue

            already_domains = _read_today_com_daily_success_domains(swap_csv, wallet, cfg.csv_delimiter)
            target_domains = random.randint(domains_min, domains_max)
            missing_domains = target_domains - len(already_domains)
            if missing_domains <= 0:
                # If Doma API still says incomplete, do one extra distinct .com swap to help indexing.
                missing_domains = 1 if quest_completed is False else 0
            if missing_domains <= 0:
                reason = f"local checker shows complete | done={len(already_domains)}/{target_domains}"
                _mark_com_daily_skipped(wallet_number, reason)
                logger.info(
                    "%s wallet=%s local checker shows complete | done=%s/%s | skipping",
                    wallet_log_prefix,
                    wallet,
                    len(already_domains),
                    target_domains,
                )
                continue

            selected_tokens = [
                token
                for token in top_com_tokens
                if str(token.name or token.symbol or "").strip().lower() not in already_domains
            ]
            if len(selected_tokens) < len(top_com_tokens):
                selected_tokens = selected_tokens + [
                    token
                    for token in top_com_tokens
                    if token not in selected_tokens
                ]
            if not selected_tokens:
                reason = f"no .com tokens left to complete quest | local_done={len(already_domains)} target={target_domains}"
                _mark_com_daily_skipped(wallet_number, reason)
                logger.warning(
                    "%s wallet=%s no .com tokens left to complete quest | local_done=%s target=%s",
                    wallet_log_prefix,
                    wallet,
                    len(already_domains),
                    target_domains,
                )
                continue
            required_new_domains = min(missing_domains, len(selected_tokens))
            logger.info(
                "%s wallet=%s checker | doma_completed=%s | local_done_today=%s/%s | will_do=%s | already=%s",
                wallet_log_prefix,
                wallet,
                quest_completed,
                len(already_domains),
                target_domains,
                required_new_domains,
                ", ".join(sorted(already_domains)) or "none",
            )

            def _topup_usdc_to(required_usdc: Decimal, reason_label: str) -> Tuple[bool, Decimal, str]:
                current = exec_client.get_erc20_balance(quote_token.address, quote_token.decimals)
                if current >= required_usdc:
                    return True, current, ""
                if eth_price <= 0:
                    return False, current, "eth_price_unknown"
                # Route output can be slightly below the quote. Fund a small surplus so a
                # nominal $1 quest swap is not rejected after ETH/WETH conversion.
                funding_buffer_usdc = Decimal("0.05")
                funding_target_usdc = required_usdc + funding_buffer_usdc
                missing_usdc = funding_target_usdc - current

                weth_balance = exec_client.get_erc20_balance(weth_token.address, weth_token.decimals)
                weth_balance_usd = weth_balance * eth_price
                reserve_eth = _native_gas_reserve_eth(eth_price)
                native_eth = exec_client.get_native_balance()
                spendable_eth = max(Decimal("0"), native_eth - reserve_eth)
                spendable_eth_usd = spendable_eth * eth_price
                total_usable_usd = current + weth_balance_usd + spendable_eth_usd
                if total_usable_usd < required_usdc:
                    return (
                        False,
                        current,
                        "combined_doma_balance_below_min:"
                        f"usdc=${_format_decimal_plain(current)},"
                        f"weth=${_format_decimal_plain(weth_balance_usd)},"
                        f"native_eth=${_format_decimal_plain(native_eth * eth_price)},"
                        f"gas_reserve=${_format_decimal_plain(NATIVE_GAS_RESERVE_USD)},"
                        f"usable_total=${_format_decimal_plain(total_usable_usd)},"
                        f"required=${_format_decimal_plain(required_usdc)}",
                    )
                if weth_balance_usd >= MIN_EXECUTABLE_TRADE_USD:
                    weth_topup_usd = min(max(missing_usdc, MIN_EXECUTABLE_TRADE_USD), weth_balance_usd)
                    logger.info(
                        "%s wallet=%s bootstrap | WETH->USDC.E amount=$%s | target_missing=%s USDC.E | WETH_balance=$%s | %s",
                        wallet_log_prefix,
                        wallet,
                        _format_decimal_plain(weth_topup_usd),
                        _format_decimal_plain(missing_usdc),
                        _format_decimal_plain(weth_balance_usd),
                        reason_label,
                    )
                    topup_ok = _execute_trade_via_doma_ui_route(
                        cfg=cfg,
                        logger=logger,
                        state=state,
                        doma_api=doma_api,
                        exec_client=exec_client,
                        token_in=weth_token,
                        token_out=quote_token,
                        display_in_symbol="WETH",
                        display_out_symbol="USDC.E",
                        trade_amount_expr=f"${_format_decimal_plain(weth_topup_usd)}",
                        eth_price=eth_price,
                        label=f"COM_DAILY wallet {wallet_number}/{total_loaded_wallets} {wallet} WETH>USDC.E TOPUP",
                        is_eth_source=False,
                        unwrap_to_native=False,
                        wait_for_pre_tx=True,
                    )
                    if topup_ok and state.last_tx_hash:
                        topup_ok = _wait_tx_receipt(exec_client, state.last_tx_hash, timeout_sec=180)
                    current = exec_client.get_erc20_balance(quote_token.address, quote_token.decimals)
                    if current >= required_usdc:
                        return True, current, ""
                    if not topup_ok:
                        logger.warning(
                            "%s wallet=%s WETH->USDC.E bootstrap failed, trying native ETH fallback",
                            wallet_log_prefix,
                            wallet,
                        )

                missing_usdc = funding_target_usdc - current
                native_eth = exec_client.get_native_balance()
                spendable_eth = max(Decimal("0"), native_eth - reserve_eth)
                bootstrap_eth = min(max(missing_usdc, MIN_EXECUTABLE_TRADE_USD) / eth_price, spendable_eth)
                bootstrap_usd = bootstrap_eth * eth_price
                if bootstrap_eth <= 0 or bootstrap_usd < MIN_EXECUTABLE_TRADE_USD:
                    native_eth_usd = native_eth * eth_price
                    spendable_eth_usd = spendable_eth * eth_price
                    return (
                        False,
                        current,
                        "eth_bootstrap_below_min:"
                        f"native_eth={_format_decimal_plain(native_eth)}(~${_format_decimal_plain(native_eth_usd)}),"
                        f"reserve=${_format_decimal_plain(NATIVE_GAS_RESERVE_USD)},"
                        f"spendable=${_format_decimal_plain(spendable_eth_usd)},"
                        f"weth=${_format_decimal_plain(weth_balance_usd)},"
                        f"missing_usdc={_format_decimal_plain(missing_usdc)},"
                        f"min_swap=${_format_decimal_plain(swap_min_usdc)}",
                    )
                logger.info(
                    "%s wallet=%s bootstrap | ETH->USDC.E amount=%s ETH | target_missing=%s USDC.E | %s",
                    wallet_log_prefix,
                    wallet,
                    _format_decimal_plain(bootstrap_eth),
                    _format_decimal_plain(missing_usdc),
                    reason_label,
                )
                topup_ok = _execute_trade_via_doma_ui_route(
                    cfg=cfg,
                    logger=logger,
                    state=state,
                    doma_api=doma_api,
                    exec_client=exec_client,
                    token_in=weth_token,
                    token_out=quote_token,
                    display_in_symbol="ETH",
                    display_out_symbol="USDC.E",
                    trade_amount_expr=_format_decimal_plain(bootstrap_eth),
                    eth_price=eth_price,
                    label=f"COM_DAILY wallet {wallet_number}/{total_loaded_wallets} {wallet} ETH>USDC.E TOPUP",
                    is_eth_source=True,
                    unwrap_to_native=False,
                    wait_for_pre_tx=True,
                )
                if topup_ok and state.last_tx_hash:
                    topup_ok = _wait_tx_receipt(exec_client, state.last_tx_hash, timeout_sec=180)
                current = exec_client.get_erc20_balance(quote_token.address, quote_token.decimals)
                if current >= required_usdc:
                    return True, current, ""
                if topup_ok:
                    return False, current, (
                        "eth_to_usdc_topup_below_target:"
                        f"available={_format_decimal_plain(current)},"
                        f"required={_format_decimal_plain(required_usdc)}"
                    )
                return False, current, "eth_to_usdc_topup_failed"

            required_usdc = swap_max_usdc
            _, current_usdc, topup_reason = _topup_usdc_to(required_usdc, "initial")
            if current_usdc < swap_min_usdc:
                reason = f"insufficient USDC.E after bootstrap ({_format_decimal_plain(current_usdc)}), reason={topup_reason}"
                _mark_com_daily_skipped(wallet_number, reason)
                logger.warning(
                    "%s wallet=%s skipped | insufficient USDC.E after bootstrap (%s), reason=%s",
                    wallet_log_prefix,
                    wallet,
                    _format_decimal_plain(current_usdc),
                    topup_reason,
                )
                continue

            for token_idx, info in enumerate(selected_tokens, start=1):
                if wallet_success >= required_new_domains:
                    break
                domain_token = _token_from_launchpad_price(info, eth_price)
                swap_usdc = _random_decimal_between(swap_min_usdc, swap_max_usdc, swap_min_raw, swap_max_raw)
                current_usdc = exec_client.get_erc20_balance(quote_token.address, quote_token.decimals)
                if current_usdc < swap_usdc:
                    if current_usdc < swap_min_usdc:
                        _, current_usdc, topup_reason = _topup_usdc_to(swap_usdc, f"domain={info.name}")
                    if current_usdc >= swap_min_usdc:
                        adjusted_swap_usdc = current_usdc.quantize(
                            Decimal("0.000001"),
                            rounding=ROUND_FLOOR,
                        )
                        if adjusted_swap_usdc >= swap_min_usdc:
                            logger.info(
                                "%s wallet=%s domain=%s swap amount adjusted to available USDC.E | selected=%s available=%s adjusted=%s",
                                wallet_log_prefix,
                                wallet,
                                info.name,
                                _format_decimal_plain(swap_usdc),
                                _format_decimal_plain(current_usdc),
                                _format_decimal_plain(adjusted_swap_usdc),
                            )
                            swap_usdc = adjusted_swap_usdc
                        else:
                            reason = f"USDC.E balance below minimum swap amount after rounding ({_format_decimal_plain(current_usdc)} < {_format_decimal_plain(swap_min_usdc)})"
                            logger.warning("%s wallet=%s domain=%s skipped | %s", wallet_log_prefix, wallet, info.name, reason)
                            wallet_failed += 1
                            wallet_failure_reasons.append(f"{info.name}: {reason}")
                            total_failed_swaps += 1
                            append_csv(
                                swap_csv,
                                [
                                    datetime.now(timezone.utc).isoformat(),
                                    "failed",
                                    wallet,
                                    info.name,
                                    info.address,
                                    info.pool_address or "",
                                    _format_decimal_plain(info.tvl_usd),
                                    _format_decimal_plain(swap_usdc),
                                    "",
                                    reason,
                                ],
                                delimiter=cfg.csv_delimiter,
                            )
                            break
                    else:
                        reason = f"USDC.E balance below minimum swap amount ({_format_decimal_plain(current_usdc)} < {_format_decimal_plain(swap_min_usdc)}), topup_reason={topup_reason}"
                        logger.warning("%s wallet=%s domain=%s skipped | %s", wallet_log_prefix, wallet, info.name, reason)
                        wallet_failed += 1
                        wallet_failure_reasons.append(f"{info.name}: {reason}")
                        total_failed_swaps += 1
                        append_csv(
                            swap_csv,
                            [
                                datetime.now(timezone.utc).isoformat(),
                                "failed",
                                wallet,
                                info.name,
                                info.address,
                                info.pool_address or "",
                                _format_decimal_plain(info.tvl_usd),
                                _format_decimal_plain(swap_usdc),
                                "",
                                reason,
                            ],
                            delimiter=cfg.csv_delimiter,
                        )
                        break
                logger.info(
                    "%s wallet=%s domain candidate %s/%s %s | success=%s/%s | tvl=$%s | round_trip_swap=%s USDC.E",
                    wallet_log_prefix,
                    wallet,
                    token_idx,
                    len(selected_tokens),
                    info.name,
                    wallet_success,
                    required_new_domains,
                    _format_decimal_plain(info.tvl_usd),
                    _format_decimal_plain(swap_usdc),
                )
                before_domain_balance = exec_client.get_erc20_balance(domain_token.address, domain_token.decimals)
                ok_forward = _execute_trade_via_doma_ui_route(
                    cfg=cfg,
                    logger=logger,
                    state=state,
                    doma_api=doma_api,
                    exec_client=exec_client,
                    token_in=quote_token,
                    token_out=domain_token,
                    display_in_symbol="USDC.E",
                    display_out_symbol=domain_token.symbol,
                    trade_amount_expr=f"${_format_decimal_plain(swap_usdc)}",
                    eth_price=eth_price,
                    label=f"COM_DAILY wallet {wallet_number}/{total_loaded_wallets} {wallet} USDC.E>{domain_token.symbol}",
                    wait_for_pre_tx=True,
                )
                forward_tx_hash = state.last_tx_hash if ok_forward else ""
                if ok_forward and forward_tx_hash:
                    ok_forward = _wait_tx_receipt(exec_client, forward_tx_hash, timeout_sec=180)
                reverse_tx_hash = ""
                ok_reverse = False
                reason = ""
                if ok_forward:
                    after_domain_balance = exec_client.get_erc20_balance(domain_token.address, domain_token.decimals)
                    received_domain = after_domain_balance - before_domain_balance
                    if received_domain <= 0:
                        reason = "no domain token received for reverse swap"
                        logger.warning("%s wallet=%s domain=%s reverse skipped | %s", wallet_log_prefix, wallet, info.name, reason)
                    else:
                        if token_idx < len(selected_tokens):
                            delay_sec = random.uniform(float(delay_min), float(delay_max))
                            logger.info("%s delay before reverse swap: %.2f sec", wallet_log_prefix, delay_sec)
                            time.sleep(delay_sec)
                        ok_reverse = _execute_trade_via_doma_ui_route(
                            cfg=cfg,
                            logger=logger,
                            state=state,
                            doma_api=doma_api,
                            exec_client=exec_client,
                            token_in=domain_token,
                            token_out=quote_token,
                            display_in_symbol=domain_token.symbol,
                            display_out_symbol="USDC.E",
                            trade_amount_expr=_format_decimal_plain(received_domain),
                            eth_price=eth_price,
                            label=f"COM_DAILY wallet {wallet_number}/{total_loaded_wallets} {wallet} {domain_token.symbol}>USDC.E",
                            wait_for_pre_tx=True,
                        )
                        reverse_tx_hash = state.last_tx_hash if ok_reverse else ""
                        if ok_reverse and reverse_tx_hash:
                            ok_reverse = _wait_tx_receipt(exec_client, reverse_tx_hash, timeout_sec=180)
                        if not ok_reverse:
                            reason = "reverse swap failed"
                else:
                    reason = "forward swap failed"
                ok = ok_forward
                if ok_forward:
                    wallet_success += 1
                    total_success_swaps += 1
                    if ok_reverse:
                        logger.info(
                            "%s wallet=%s domain=%s round trip complete | forward=%s reverse=%s",
                            wallet_log_prefix,
                            wallet,
                            info.name,
                            forward_tx_hash,
                            reverse_tx_hash,
                        )
                    else:
                        logger.warning(
                            "%s wallet=%s domain=%s forward swap counted for quest, reverse cleanup failed | forward=%s | %s",
                            wallet_log_prefix,
                            wallet,
                            info.name,
                            forward_tx_hash,
                            reason,
                        )
                else:
                    wallet_failed += 1
                    wallet_failure_reasons.append(f"{info.name}: {reason or 'forward swap failed'}")
                    total_failed_swaps += 1
                    logger.warning("%s wallet=%s domain=%s round trip failed | %s", wallet_log_prefix, wallet, info.name, reason)
                append_csv(
                    swap_csv,
                    [
                        datetime.now(timezone.utc).isoformat(),
                        "ok" if ok else "failed",
                        wallet,
                        info.name,
                        info.address,
                        info.pool_address or "",
                        _format_decimal_plain(info.tvl_usd),
                        _format_decimal_plain(swap_usdc),
                        "|".join([tx for tx in [forward_tx_hash, reverse_tx_hash] if tx]),
                        "" if ok_reverse else reason,
                    ],
                    delimiter=cfg.csv_delimiter,
                )
                if wallet_success >= required_new_domains:
                    break
                if token_idx < len(selected_tokens):
                    delay_sec = random.uniform(float(delay_min), float(delay_max))
                    logger.info("%s delay before next .com domain: %.2f sec", wallet_log_prefix, delay_sec)
                    time.sleep(delay_sec)
            if wallet_success >= required_new_domains:
                success_wallets += 1
            else:
                failed_wallets += 1
                detail = "; ".join(wallet_failure_reasons)
                if not detail:
                    detail = f"выполнено свапов {wallet_success}/{required_new_domains}"
                failed_wallet_details.append(
                    _redact_wallet_addresses(f"wallet#{wallet_number}: {detail}")
                )
        except Exception as exc:
            failed_wallets += 1
            failed_wallet_details.append(
                _redact_wallet_addresses(f"wallet#{wallet_number}: {exc}")
            )
            logger.warning("%s wallet=%s failed: %s", wallet_log_prefix, wallet, exc)
        if idx < len(wallet_key_records):
            delay_sec = random.uniform(float(delay_min), float(delay_max))
            logger.info("%s delay before next wallet: %.2f sec", wallet_log_prefix, delay_sec)
            time.sleep(delay_sec)

    logger.info("[COM_DAILY] swaps total | success=%s failed=%s", total_success_swaps, total_failed_swaps)
    _print_mode_summary(
        "COM_DAILY",
        total=len(wallet_key_records),
        success=success_wallets,
        failed=failed_wallets,
        skipped=skipped_wallets,
        failed_wallets=failed_wallet_details,
    )
    if skipped_wallet_details:
        print(_color(f"[COM_DAILY] пропущенные кошельки: {'; '.join(skipped_wallet_details)}", ANSI_YELLOW))


def run_domain_bridge_to_base_once(cfg: BotConfig, logger: logging.Logger, state: BotState) -> None:
    picked = get_domain_bridge_to_base_menu_input()
    if not picked:
        logger.info("[DOMAIN_BRIDGE] canceled by user.")
        return
    selection, domains_per_wallet_raw, delay_min_raw, delay_max_raw = picked
    domains_per_wallet = int(_parse_decimal_input(domains_per_wallet_raw))
    bridge_delay_min = float(_parse_decimal_input(delay_min_raw))
    bridge_delay_max = float(_parse_decimal_input(delay_max_raw))

    wallet_key_records = _build_wallet_key_records(cfg, logger, "DOMAIN_BRIDGE")
    if not wallet_key_records:
        raise ValueError(
            "No wallet/private-key pairs available for domain bridge "
            "(fill wallets.txt + keys.txt line-by-line or set valid PRIVATE_KEY in .env)"
        )
    wallet_key_records, wallet_start_offset, total_loaded_wallets = _apply_wallet_start_selection(wallet_key_records)

    bridge_csv = cfg.trades_csv_file.parent / DOMAIN_BRIDGE_CSV.name
    ensure_csv(
        bridge_csv,
        [
            "timestamp_utc",
            "status",
            "wallet",
            "domain",
            "token_address",
            "token_id",
            "source_chain",
            "target_chain",
            "target_owner",
            "selection",
            "fee_native_raw",
            "tx_hash",
            "reason",
        ],
        delimiter=cfg.csv_delimiter,
    )

    logger.info(
        "[DOMAIN_BRIDGE] mode started | wallets=%s | start_wallet=%s | target=Base(%s) | selection=%s | domains_per_wallet=%s | delay=%s-%s sec",
        len(wallet_key_records),
        wallet_start_offset + 1,
        BASE_CHAIN_CAIP2,
        selection,
        domains_per_wallet,
        delay_min_raw,
        delay_max_raw,
    )

    success_wallets = 0
    failed_wallets = 0
    skipped_wallets = 0
    bridged_count = 0
    failed_wallet_addresses: List[str] = []

    for idx, (line_idx, wallet, private_key) in enumerate(wallet_key_records):
        proxies, skip_wallet = _proxy_for_line(cfg, line_idx, logger, "DOMAIN_BRIDGE")
        if skip_wallet:
            skipped_wallets += 1
            continue
        if not proxies:
            skipped_wallets += 1
            logger.warning("[DOMAIN_BRIDGE] wallet=%s skipped: proxy is required for domain bridge", wallet)
            continue
        logger.info(
            "[DOMAIN_BRIDGE] wallet %s",
            _wallet_record_progress_label(idx, len(wallet_key_records), line_idx, total_loaded_wallets, wallet),
        )
        try:
            doma_api = DomaApiClient(
                cfg.doma_api_url,
                api_key=cfg.doma_api_key,
                api_keys=cfg.doma_api_keys,
                proxies=proxies,
            )
            all_domains = doma_api.fetch_owned_domains(wallet, chain_id=cfg.chain_id, listed=None)
            listed_domains = doma_api.fetch_owned_domains(wallet, chain_id=cfg.chain_id, listed=True)
            eligible_domains = _eligible_bridge_domains(all_domains, listed_domains, cfg.chain_id, selection)
            if not eligible_domains:
                skipped_wallets += 1
                logger.info(
                    "[DOMAIN_BRIDGE] wallet=%s no eligible %s domains | owned=%s listed=%s | proxy=%s",
                    wallet,
                    selection,
                    len(all_domains),
                    len(listed_domains),
                    "yes" if proxies else "no",
                )
                continue
            selected_domains = eligible_domains[:domains_per_wallet]
            exec_client = _build_exec_client_with_rpc_fallback(
                cfg,
                logger,
                wallet,
                private_key,
                proxies,
                "[DOMAIN_BRIDGE] ",
            )
            wallet_success = 0
            wallet_failed = 0
            for domain_idx, domain in enumerate(selected_domains, start=1):
                target_owner = Web3.to_checksum_address(wallet)
                logger.info(
                    "[DOMAIN_BRIDGE] wallet=%s domain %s/%s %s | token=%s | token_id=%s | target=%s",
                    wallet,
                    domain_idx,
                    len(selected_domains),
                    domain.name,
                    domain.token_address,
                    domain.token_id,
                    target_owner,
                )
                tx_hash = ""
                fee_raw = 0
                reason = ""
                ok = False
                try:
                    if cfg.paper_mode or cfg.dry_run or not cfg.enable_execution:
                        reason = "PAPER/DRY mode active. No transaction sent."
                        ok = True
                        logger.info("[DOMAIN_BRIDGE] wallet=%s domain=%s %s", wallet, domain.name, reason)
                    else:
                        tx_hash, fee_raw = exec_client.execute_domain_bridge(
                            proxy_doma_record_address=PROXY_DOMA_RECORD_ADDRESS,
                            token_id=domain.token_id,
                            target_chain_id=BASE_CHAIN_CAIP2,
                            target_owner_address=target_owner,
                        )
                        logger.info(
                            "[DOMAIN_BRIDGE] wallet=%s domain=%s bridge tx sent: %s | fee_native_raw=%s",
                            wallet,
                            domain.name,
                            tx_hash,
                            fee_raw,
                        )
                        ok = _wait_tx_receipt(exec_client, tx_hash, timeout_sec=240)
                        if not ok:
                            reason = "bridge tx failed or timed out"
                except Exception as exc:
                    reason = str(exc)
                    logger.warning("[DOMAIN_BRIDGE] wallet=%s domain=%s bridge failed: %s", wallet, domain.name, exc)
                append_csv(
                    bridge_csv,
                    [
                        datetime.now(timezone.utc).isoformat(),
                        "ok" if ok else "failed",
                        wallet,
                        domain.name,
                        domain.token_address,
                        domain.token_id,
                        f"eip155:{cfg.chain_id}",
                        BASE_CHAIN_CAIP2,
                        target_owner,
                        selection,
                        str(fee_raw),
                        tx_hash,
                        reason,
                    ],
                    delimiter=cfg.csv_delimiter,
                )
                if ok:
                    wallet_success += 1
                    bridged_count += 1
                    logger.info("[DOMAIN_BRIDGE] wallet=%s domain=%s bridged to Base", wallet, domain.name)
                else:
                    wallet_failed += 1
                if domain_idx < len(selected_domains):
                    delay_sec = random.uniform(bridge_delay_min, bridge_delay_max)
                    logger.info("[DOMAIN_BRIDGE] delay before next domain: %.2f sec", delay_sec)
                    time.sleep(delay_sec)
            if wallet_success > 0:
                success_wallets += 1
            if wallet_failed > 0:
                failed_wallets += 1
                failed_wallet_addresses.append(wallet)
        except Exception as exc:
            failed_wallets += 1
            failed_wallet_addresses.append(wallet)
            logger.warning("[DOMAIN_BRIDGE] wallet=%s failed: %s", wallet, exc)
        if idx < len(wallet_key_records) - 1 and cfg.wallet_delay_max_sec > 0:
            delay_sec = random.uniform(cfg.wallet_delay_min_sec, cfg.wallet_delay_max_sec)
            logger.info("[DOMAIN_BRIDGE] delay before next wallet: %.2f sec", delay_sec)
            time.sleep(delay_sec)

    logger.info("[DOMAIN_BRIDGE] bridged domains total=%s", bridged_count)
    _print_mode_summary(
        "DOMAIN_BRIDGE",
        len(wallet_key_records),
        success_wallets,
        failed_wallets,
        skipped_wallets,
        failed_wallet_addresses,
    )


def run_domain_swap_once(cfg: BotConfig, logger: logging.Logger, state: BotState) -> None:
    success_wallets = 0
    failed_wallets = 0
    skipped_wallets = 0
    failed_wallet_addresses: List[str] = []

    def _fail_wallet() -> None:
        nonlocal failed_wallets
        failed_wallets += 1
        wallet_label = f"wallet#{line_idx + 1}"
        if wallet_label not in failed_wallet_addresses:
            failed_wallet_addresses.append(wallet_label)
    domain_mode = get_domain_mode_menu_choice()
    if not domain_mode:
        logger.info("Domain swap canceled by user.")
        return
    if domain_mode == "quest":
        run_domain_quest_volume_once(cfg, logger, state)
        return
    is_one_swap = domain_mode == "one_swap"
    picked = get_domain_single_swap_menu_input(state) if is_one_swap else get_domain_swap_menu_input(state)
    if not picked:
        logger.info("Domain swap canceled by user.")
        return
    src_symbol, dst_symbol, amount_mode, range_raw, swap_delay_min_raw, swap_delay_max_raw = picked
    min_raw, max_raw = [x.strip() for x in range_raw.split("|", 1)]
    swap_delay_min_sec = float(_parse_decimal_input(swap_delay_min_raw))
    swap_delay_max_sec = float(_parse_decimal_input(swap_delay_max_raw))

    wallet_key_records = _build_wallet_key_records(cfg, logger, "DOMAIN")
    if not wallet_key_records:
        raise ValueError(
            "No wallet/private-key pairs available for domain swap "
            "(fill wallets.txt + keys.txt line-by-line or set valid PRIVATE_KEY in .env)"
        )
    wallet_key_records, wallet_start_offset, total_loaded_wallets = _apply_wallet_start_selection(wallet_key_records)

    logger.info(
        "[DOMAIN] mode started | source=%s target=%s wallets=%s | start_wallet=%s | mode=%s | swap_delay=%s-%s sec",
        src_symbol,
        dst_symbol,
        len(wallet_key_records),
        wallet_start_offset + 1,
        "one_swap" if is_one_swap else "round_trip",
        swap_delay_min_raw,
        swap_delay_max_raw,
    )

    def _sleep_between_domain_swaps() -> None:
        delay_sec = random.uniform(swap_delay_min_sec, swap_delay_max_sec)
        logger.info("[DOMAIN] delay between swaps: %.2f sec", delay_sec)
        time.sleep(delay_sec)

    domain_pre_tx_delay_range = (swap_delay_min_sec, swap_delay_max_sec)

    def _resolve_single_swap_token(
        *,
        cfg: BotConfig,
        doma_api: DomaApiClient,
        pools: List[Pool],
        eth_price: Decimal,
        symbol: str,
    ) -> Tuple[Token, str, bool, bool]:
        normalized = _normalize_domain_swap_asset(symbol)
        if normalized == "ETH":
            return _token_from_config_override(cfg, "WETH", 18), "ETH", True, False
        if normalized == "WETH":
            return _token_from_config_override(cfg, "WETH", 18), "WETH", False, False
        if normalized == "USDC.E":
            return _usdce_token_from_config(cfg), "USDC.E", False, False

        launchpad = doma_api.fetch_fractional_token_by_name(normalized.lower())
        if launchpad:
            if launchpad.pool_address:
                return _token_from_launchpad_price(launchpad, eth_price), canonical_symbol(launchpad.symbol or normalized), False, False
            return _token_from_launchpad(launchpad), canonical_symbol(launchpad.symbol or normalized), False, False

        target = canonical_symbol(normalized)
        for pool in pools:
            if canonical_symbol(pool.token0.symbol) == target:
                return pool.token0, pool.token0.symbol, False, False
            if canonical_symbol(pool.token1.symbol) == target:
                return pool.token1, pool.token1.symbol, False, False
        raise RuntimeError(f"Token {normalized} not found")

    use_relay_swap = not cfg.router_address or cfg.router_address == "0x0000000000000000000000000000000000000000"
    pooled_launchpad_info: Optional[LaunchpadTokenInfo] = None
    pooled_quote_token: Optional[Token] = None
    pooled_domain_token: Optional[Token] = None
    pooled_weth_token: Optional[Token] = None
    pooled_eth_price = Decimal("0")
    try:
        shared_doma_api = DomaApiClient(
            cfg.doma_api_url,
            api_key=cfg.doma_api_key,
            api_keys=cfg.doma_api_keys,
        )
        candidate_launchpad = shared_doma_api.fetch_fractional_token_by_name(dst_symbol.lower())
        if candidate_launchpad and candidate_launchpad.pool_address:
            pooled_launchpad_info = candidate_launchpad
            pooled_quote_token = Token(
                address=pooled_launchpad_info.quote_token_address,
                symbol="USDC.E",
                decimals=6,
                derived_eth=Decimal("0"),
            )
            pooled_eth_price = _fetch_eth_price_via_doma_quote(cfg, shared_doma_api, pooled_quote_token)
            pooled_domain_token = _token_from_launchpad_price(pooled_launchpad_info, pooled_eth_price)
            pooled_weth_token = _token_from_config_override(cfg, "WETH", 18)
            logger.info(
                "[DOMAIN] metadata loaded | symbol=%s token=%s pool=%s token_price=%s eth_price=%s",
                pooled_domain_token.symbol,
                pooled_domain_token.address,
                pooled_launchpad_info.pool_address,
                _format_decimal_plain(pooled_launchpad_info.price_usd),
                _format_decimal_plain(pooled_eth_price),
            )
    except Exception as exc:
        logger.warning("[DOMAIN] metadata prefetch failed for %s: %s", dst_symbol, exc)

    for idx, (line_idx, wallet, private_key) in enumerate(wallet_key_records):
        proxies, skip_wallet = _proxy_for_line(cfg, line_idx, logger, "DOMAIN")
        if skip_wallet:
            skipped_wallets += 1
            continue
        logger.info("[DOMAIN] wallet %s", _wallet_record_progress_label(idx, len(wallet_key_records), line_idx, total_loaded_wallets, wallet))
        before_points_snapshot: Optional[PointsSnapshot] = None
        try:
            if is_one_swap:
                try:
                    doma_api = DomaApiClient(
                        cfg.doma_api_url,
                        api_key=cfg.doma_api_key,
                        api_keys=cfg.doma_api_keys,
                        proxies=proxies,
                    )
                    pools: List[Pool] = []
                    eth_price = _fetch_eth_price_via_doma_quote(cfg, doma_api, _usdce_token_from_config(cfg))
                    if eth_price <= 0:
                        raise RuntimeError("Failed to resolve ETH/USD")

                    def _resolve_single_swap_token_with_fallback(symbol: str) -> Tuple[Token, str, bool, bool]:
                        nonlocal pools, eth_price
                        try:
                            return _resolve_single_swap_token(
                                cfg=cfg,
                                doma_api=doma_api,
                                pools=pools,
                                eth_price=eth_price,
                                symbol=symbol,
                            )
                        except Exception as first_exc:
                            if pools:
                                raise
                            try:
                                subgraph = DomaSubgraphClient(cfg.subgraph_url, proxies=proxies)
                                pools = subgraph.fetch_top_pools(limit=1000)
                            except Exception as exc:
                                logger.warning("[DOMAIN] wallet=%s token lookup subgraph fallback failed: %s", wallet, exc)
                                raise first_exc
                            return _resolve_single_swap_token(
                                cfg=cfg,
                                doma_api=doma_api,
                                pools=pools,
                                eth_price=eth_price,
                                symbol=symbol,
                            )

                    exec_client = _build_exec_client_with_rpc_fallback(
                        cfg=cfg,
                        logger=logger,
                        wallet=wallet,
                        private_key=private_key,
                        proxies=proxies,
                        log_prefix="[DOMAIN]",
                    )
                    before_points_snapshot = _fetch_wallet_points_snapshot(cfg, wallet, proxies, logger, "DOMAIN")
                    amount_expr = _pick_random_amount_expr(
                        amount_mode,
                        _parse_decimal_input(min_raw),
                        _parse_decimal_input(max_raw),
                        state,
                        min_raw=min_raw,
                        max_raw=max_raw,
                    )
                    token_in, display_in, is_eth_source, _ = _resolve_single_swap_token_with_fallback(src_symbol)
                    token_out, display_out, _, _ = _resolve_single_swap_token_with_fallback(dst_symbol)
                    unwrap_to_native = display_out == "ETH"
                    logger.info("[DOMAIN] wallet=%s one swap | %s->%s amount=%s %s", wallet, display_in, display_out, amount_expr, display_in)
                    ok = _execute_trade_via_doma_ui_route(
                        cfg=cfg,
                        logger=logger,
                        state=state,
                        doma_api=doma_api,
                        exec_client=exec_client,
                        token_in=token_in,
                        token_out=token_out,
                        display_in_symbol=display_in,
                        display_out_symbol=display_out,
                        trade_amount_expr=amount_expr,
                        eth_price=eth_price,
                        label=f"DOMAIN {wallet} {display_in}>{display_out}",
                        is_eth_source=is_eth_source,
                        unwrap_to_native=unwrap_to_native,
                        wait_for_pre_tx=True,
                        post_approve_delay_range=domain_pre_tx_delay_range,
                    )
                    if ok:
                        success_wallets += 1
                    else:
                        _fail_wallet()
                except Exception as exc:
                    _fail_wallet()
                    logger.warning("[DOMAIN] wallet=%s one swap failed: %s", wallet, exc)
                if idx < len(wallet_key_records) - 1 and cfg.wallet_delay_max_sec > 0:
                    delay_sec = random.uniform(cfg.wallet_delay_min_sec, cfg.wallet_delay_max_sec)
                    logger.info("[DOMAIN] delay before next wallet: %.2f sec", delay_sec)
                    time.sleep(delay_sec)
                continue

            if pooled_launchpad_info and pooled_quote_token and pooled_domain_token and pooled_weth_token and pooled_eth_price > 0:
                try:
                    doma_api = DomaApiClient(
                        cfg.doma_api_url,
                        api_key=cfg.doma_api_key,
                        api_keys=cfg.doma_api_keys,
                        proxies=proxies,
                    )
                    exec_client = _build_exec_client_with_rpc_fallback(
                        cfg=cfg,
                        logger=logger,
                        wallet=wallet,
                        private_key=private_key,
                        proxies=proxies,
                        log_prefix="[DOMAIN]",
                    )
                    before_points_snapshot = _fetch_wallet_points_snapshot(cfg, wallet, proxies, logger, "DOMAIN")
                    amount_expr = _pick_random_amount_expr(
                        amount_mode,
                        _parse_decimal_input(min_raw),
                        _parse_decimal_input(max_raw),
                        state,
                        min_raw=min_raw,
                        max_raw=max_raw,
                    )
                    logger.info("[DOMAIN] wallet=%s amount=%s %s", wallet, amount_expr, src_symbol)
                    if src_symbol == "ETH":
                        forward_token_in = pooled_weth_token
                        forward_display_in = "ETH"
                        forward_is_eth_source = True
                        reverse_token_out = pooled_weth_token
                        reverse_display_out = "ETH"
                        reverse_unwrap_to_native = True
                    elif src_symbol == "USDC.E":
                        forward_token_in = pooled_quote_token
                        forward_display_in = "USDC.E"
                        forward_is_eth_source = False
                        reverse_token_out = pooled_quote_token
                        reverse_display_out = "USDC.E"
                        reverse_unwrap_to_native = False
                    else:
                        logger.warning("[DOMAIN] wallet=%s unsupported source %s for %s", wallet, src_symbol, dst_symbol)
                        _fail_wallet()
                        continue

                    before_dst = exec_client.get_erc20_balance(pooled_domain_token.address, pooled_domain_token.decimals)
                    ok_fw = _execute_trade_via_doma_ui_route(
                        cfg=cfg,
                        logger=logger,
                        state=state,
                        doma_api=doma_api,
                        exec_client=exec_client,
                        token_in=forward_token_in,
                        token_out=pooled_domain_token,
                        display_in_symbol=forward_display_in,
                        display_out_symbol=pooled_domain_token.symbol,
                        trade_amount_expr=amount_expr,
                        eth_price=pooled_eth_price,
                        label=f"DOMAIN {wallet} {src_symbol}>{pooled_domain_token.symbol}",
                        is_eth_source=forward_is_eth_source,
                        wait_for_pre_tx=True,
                        post_approve_delay_range=domain_pre_tx_delay_range,
                    )
                    if not ok_fw:
                        _fail_wallet()
                        continue
                    if state.last_tx_hash:
                        _wait_tx_receipt(exec_client, state.last_tx_hash, timeout_sec=180)
                        _sleep_between_domain_swaps()

                    after_dst = exec_client.get_erc20_balance(pooled_domain_token.address, pooled_domain_token.decimals)
                    delta_dst = after_dst - before_dst
                    if delta_dst <= 0:
                        logger.warning("[DOMAIN] wallet=%s no received %s for reverse swap", wallet, pooled_domain_token.symbol)
                        _fail_wallet()
                        continue

                    ok_bw = _execute_trade_via_doma_ui_route(
                        cfg=cfg,
                        logger=logger,
                        state=state,
                        doma_api=doma_api,
                        exec_client=exec_client,
                        token_in=pooled_domain_token,
                        token_out=reverse_token_out,
                        display_in_symbol=pooled_domain_token.symbol,
                        display_out_symbol=reverse_display_out,
                        trade_amount_expr=_format_decimal_plain(delta_dst),
                        eth_price=pooled_eth_price,
                        label=f"DOMAIN {wallet} {pooled_domain_token.symbol}>{src_symbol}",
                        is_eth_source=False,
                        unwrap_to_native=reverse_unwrap_to_native,
                        wait_for_pre_tx=True,
                        post_approve_delay_range=domain_pre_tx_delay_range,
                    )
                    if ok_bw:
                        success_wallets += 1
                    else:
                        _fail_wallet()
                    if idx < len(wallet_key_records) - 1 and cfg.wallet_delay_max_sec > 0:
                        delay_sec = random.uniform(cfg.wallet_delay_min_sec, cfg.wallet_delay_max_sec)
                        logger.info("[DOMAIN] delay before next wallet: %.2f sec", delay_sec)
                        time.sleep(delay_sec)
                    continue
                except Exception as exc:
                    _fail_wallet()
                    logger.warning("[DOMAIN] wallet=%s Doma UI route failed: %s", wallet, exc)
                    continue

            try:
                subgraph = DomaSubgraphClient(cfg.subgraph_url, proxies=proxies)
                doma_api = DomaApiClient(
                    cfg.doma_api_url,
                    api_key=cfg.doma_api_key,
                    api_keys=cfg.doma_api_keys,
                    proxies=proxies,
                )
                pools = subgraph.fetch_top_pools(limit=1000)
                eth_price = subgraph.fetch_eth_price_usd()
                if eth_price <= 0:
                    raise RuntimeError("Failed to resolve ETH/USD")
                launchpad_info = doma_api.fetch_fractional_token_by_name(dst_symbol.lower())
                has_target = any(p.token0.symbol == dst_symbol or p.token1.symbol == dst_symbol for p in pools)
                if not has_target and not launchpad_info:
                    raise RuntimeError(f"Token {dst_symbol} not found in top pools")
            except Exception as exc:
                _fail_wallet()
                logger.warning("[DOMAIN] wallet=%s init failed: %s", wallet, exc)
                continue

            def _pick_pool(a: str, b: str) -> Optional[Pool]:
                return _find_best_pool_for_symbols(cfg, pools, a, b, ignore_limits=True)

            def _find_token_by_symbol(sym: str) -> Optional[Token]:
                target = canonical_symbol(sym)
                for p in pools:
                    if p.token0.symbol == target:
                        return p.token0
                    if p.token1.symbol == target:
                        return p.token1
                return None

            amount_expr = _pick_random_amount_expr(
                amount_mode,
                _parse_decimal_input(min_raw),
                _parse_decimal_input(max_raw),
                state,
                min_raw=min_raw,
                max_raw=max_raw,
            )
            logger.info("[DOMAIN] wallet=%s amount=%s %s", wallet, amount_expr, src_symbol)
            exec_client = _build_exec_client_with_rpc_fallback(
                cfg=cfg,
                logger=logger,
                wallet=wallet,
                private_key=private_key,
                proxies=proxies,
                log_prefix="[DOMAIN]",
            )
            before_points_snapshot = _fetch_wallet_points_snapshot(cfg, wallet, proxies, logger, "DOMAIN")

            if use_relay_swap:
                src_token = _find_token_by_symbol(src_symbol) if src_symbol != "ETH" else None
                dst_token = _find_token_by_symbol(dst_symbol)
                if not dst_token:
                    logger.warning("[DOMAIN] wallet=%s token %s not found for relay swap", wallet, dst_symbol)
                    _fail_wallet()
                    continue

                if src_symbol == "ETH":
                    src_balance_dec = exec_client.get_native_balance()
                    src_decimals = 18
                    src_usd = eth_price
                    src_currency_for_relay = NATIVE_ETH
                    src_currency_for_balance = NATIVE_ETH
                else:
                    if not src_token:
                        logger.warning("[DOMAIN] wallet=%s source token %s not found", wallet, src_symbol)
                        _fail_wallet()
                        continue
                    src_balance_dec = exec_client.get_erc20_balance(src_token.address, src_token.decimals)
                    src_decimals = src_token.decimals
                    src_usd = pick_token_usd_price(src_token, eth_price)
                    src_currency_for_relay = src_token.address
                    src_currency_for_balance = src_token.address

                try:
                    amount_in_dec, _ = resolve_trade_amount(amount_expr, src_balance_dec, src_usd)
                except Exception as exc:
                    logger.warning("[DOMAIN] wallet=%s amount resolve failed: %s", wallet, exc)
                    _fail_wallet()
                    continue
                amount_in_raw = decimal_to_raw(amount_in_dec, src_decimals)
                if amount_in_raw <= 0:
                    logger.warning("[DOMAIN] wallet=%s amount too small", wallet)
                    _fail_wallet()
                    continue

                before_dst = exec_client.get_erc20_balance(dst_token.address, dst_token.decimals)
                try:
                    execute_relay_swap(
                        logger=logger,
                        wallet=wallet,
                        private_key=private_key,
                        chain_id=cfg.chain_id,
                        origin_currency=src_currency_for_relay,
                        destination_currency=dst_token.address,
                        amount_raw=amount_in_raw,
                        proxies=proxies,
                    )
                except Exception as exc:
                    logger.warning("[DOMAIN] wallet=%s relay swap failed %s->%s: %s", wallet, src_symbol, dst_symbol, exc)
                    _fail_wallet()
                    continue

                _sleep_between_domain_swaps()
                after_dst = exec_client.get_erc20_balance(dst_token.address, dst_token.decimals)
                delta_dst = after_dst - before_dst
                if delta_dst <= 0:
                    logger.warning("[DOMAIN] wallet=%s no received %s for reverse swap", wallet, dst_symbol)
                    _fail_wallet()
                    continue

                amount_back_raw = decimal_to_raw(delta_dst, dst_token.decimals)
                dest_back_currency = src_currency_for_balance
                try:
                    execute_relay_swap(
                        logger=logger,
                        wallet=wallet,
                        private_key=private_key,
                        chain_id=cfg.chain_id,
                        origin_currency=dst_token.address,
                        destination_currency=dest_back_currency,
                        amount_raw=amount_back_raw,
                        proxies=proxies,
                    )
                except Exception as exc:
                    logger.warning("[DOMAIN] wallet=%s relay reverse swap failed %s->%s: %s", wallet, dst_symbol, src_symbol, exc)
                    _fail_wallet()
                    continue
                success_wallets += 1
                continue

            if launchpad_info and not launchpad_info.pool_address:
                quote_token = _find_token_by_address(pools, launchpad_info.quote_token_address)
                if not quote_token:
                    quote_token = Token(
                        address=launchpad_info.quote_token_address,
                        symbol="USDC.E",
                        decimals=6,
                        derived_eth=Decimal("0"),
                    )
                launchpad_token = _token_from_launchpad(launchpad_info)

                if src_symbol == "USDC.E":
                    if canonical_symbol(quote_token.symbol) != "USDC.E":
                        logger.warning(
                            "[DOMAIN] wallet=%s unsupported launchpad quote token %s for source %s",
                            wallet,
                            quote_token.symbol,
                            src_symbol,
                        )
                        _fail_wallet()
                        continue
                    before_dst = exec_client.get_erc20_balance(launchpad_token.address, launchpad_token.decimals)
                    ok_fw = _execute_launchpad_buy(
                        cfg=cfg,
                        logger=logger,
                        state=state,
                        exec_client=exec_client,
                        launchpad=launchpad_info,
                        quote_token=quote_token,
                        trade_amount_expr=amount_expr,
                        eth_price=eth_price,
                        label=f"DOMAIN {wallet} USDC.E>{dst_symbol}",
                        wait_for_pre_tx=True,
                        post_approve_delay_range=domain_pre_tx_delay_range,
                    )
                    if not ok_fw:
                        _fail_wallet()
                        continue
                    if state.last_tx_hash:
                        _wait_tx_receipt(exec_client, state.last_tx_hash, timeout_sec=180)
                        _sleep_between_domain_swaps()
                    after_dst = exec_client.get_erc20_balance(launchpad_token.address, launchpad_token.decimals)
                    delta_dst = after_dst - before_dst
                    if delta_dst <= 0:
                        logger.warning("[DOMAIN] wallet=%s no received %s for reverse swap", wallet, dst_symbol)
                        _fail_wallet()
                        continue
                    ok_bw = _execute_launchpad_sell(
                        cfg=cfg,
                        logger=logger,
                        state=state,
                        exec_client=exec_client,
                        launchpad=launchpad_info,
                        quote_token=quote_token,
                        trade_amount_expr=_format_decimal_plain(delta_dst),
                        eth_price=eth_price,
                        label=f"DOMAIN {wallet} {dst_symbol}>USDC.E",
                        wait_for_pre_tx=True,
                        post_approve_delay_range=domain_pre_tx_delay_range,
                    )
                    if ok_bw:
                        success_wallets += 1
                    else:
                        _fail_wallet()
                    if idx < len(wallet_key_records) - 1 and cfg.wallet_delay_max_sec > 0:
                        delay_sec = random.uniform(cfg.wallet_delay_min_sec, cfg.wallet_delay_max_sec)
                        logger.info("[DOMAIN] delay before next wallet: %.2f sec", delay_sec)
                        time.sleep(delay_sec)
                    continue

                pool_eth_usdc = _pick_pool("ETH", "USDC.E")
                pool_usdc_eth = _pick_pool("USDC.E", "ETH")
                if not pool_eth_usdc or not pool_usdc_eth:
                    logger.warning("[DOMAIN] wallet=%s no route ETH<->USDC.E for launchpad token %s", wallet, dst_symbol)
                    _fail_wallet()
                    continue
                usdc_pair = _find_tokens_for_direction(pool_eth_usdc, "ETH", "USDC.E")
                if not usdc_pair:
                    logger.warning("[DOMAIN] wallet=%s invalid ETH>USDC.E pool for launchpad token %s", wallet, dst_symbol)
                    _fail_wallet()
                    continue

                before_usdc = exec_client.get_erc20_balance(quote_token.address, quote_token.decimals)
                ok_eth_to_usdc = _execute_trade_for_pair(
                    cfg=cfg,
                    logger=logger,
                    state=state,
                    exec_client=exec_client,
                    pool=pool_eth_usdc,
                    symbol_in="ETH",
                    symbol_out="USDC.E",
                    trade_amount_expr=amount_expr,
                    eth_price=eth_price,
                    label=f"DOMAIN {wallet} ETH>USDC.E",
                    bypass_risk_checks=True,
                    allow_no_quoter_execution=True,
                    wait_for_pre_tx=True,
                    post_approve_delay_range=domain_pre_tx_delay_range,
                )
                if not ok_eth_to_usdc:
                    logger.warning("[DOMAIN] wallet=%s first leg failed ETH>USDC.E", wallet)
                    _fail_wallet()
                    continue
                if state.last_tx_hash:
                    _wait_tx_receipt(exec_client, state.last_tx_hash, timeout_sec=180)
                    _sleep_between_domain_swaps()
                after_usdc = exec_client.get_erc20_balance(quote_token.address, quote_token.decimals)
                delta_usdc = after_usdc - before_usdc
                if delta_usdc <= 0:
                    logger.warning("[DOMAIN] wallet=%s no received USDC.E for launchpad buy", wallet)
                    _fail_wallet()
                    continue

                before_dst = exec_client.get_erc20_balance(launchpad_token.address, launchpad_token.decimals)
                ok_buy = _execute_launchpad_buy(
                    cfg=cfg,
                    logger=logger,
                    state=state,
                    exec_client=exec_client,
                    launchpad=launchpad_info,
                    quote_token=quote_token,
                    trade_amount_expr=_format_decimal_plain(delta_usdc),
                    eth_price=eth_price,
                    label=f"DOMAIN {wallet} USDC.E>{dst_symbol}",
                    wait_for_pre_tx=True,
                    post_approve_delay_range=domain_pre_tx_delay_range,
                )
                if not ok_buy:
                    _fail_wallet()
                    continue
                if state.last_tx_hash:
                    _wait_tx_receipt(exec_client, state.last_tx_hash, timeout_sec=180)
                    _sleep_between_domain_swaps()
                after_dst = exec_client.get_erc20_balance(launchpad_token.address, launchpad_token.decimals)
                delta_dst = after_dst - before_dst
                if delta_dst <= 0:
                    logger.warning("[DOMAIN] wallet=%s no received %s for reverse swap", wallet, dst_symbol)
                    _fail_wallet()
                    continue

                before_usdc_back = exec_client.get_erc20_balance(quote_token.address, quote_token.decimals)
                ok_sell = _execute_launchpad_sell(
                    cfg=cfg,
                    logger=logger,
                    state=state,
                    exec_client=exec_client,
                    launchpad=launchpad_info,
                    quote_token=quote_token,
                    trade_amount_expr=_format_decimal_plain(delta_dst),
                    eth_price=eth_price,
                    label=f"DOMAIN {wallet} {dst_symbol}>USDC.E",
                    wait_for_pre_tx=True,
                    post_approve_delay_range=domain_pre_tx_delay_range,
                )
                if not ok_sell:
                    _fail_wallet()
                    continue
                if state.last_tx_hash:
                    _wait_tx_receipt(exec_client, state.last_tx_hash, timeout_sec=180)
                    _sleep_between_domain_swaps()
                after_usdc_back = exec_client.get_erc20_balance(quote_token.address, quote_token.decimals)
                delta_usdc_back = after_usdc_back - before_usdc_back
                if delta_usdc_back <= 0:
                    logger.warning("[DOMAIN] wallet=%s no received USDC.E for final swap back to ETH", wallet)
                    _fail_wallet()
                    continue

                ok_usdc_to_eth = _execute_trade_for_pair(
                    cfg=cfg,
                    logger=logger,
                    state=state,
                    exec_client=exec_client,
                    pool=pool_usdc_eth,
                    symbol_in="USDC.E",
                    symbol_out="ETH",
                    trade_amount_expr=_format_decimal_plain(delta_usdc_back),
                    eth_price=eth_price,
                    label=f"DOMAIN {wallet} USDC.E>ETH",
                    bypass_risk_checks=True,
                    allow_no_quoter_execution=True,
                    wait_for_pre_tx=True,
                    post_approve_delay_range=domain_pre_tx_delay_range,
                )
                if ok_usdc_to_eth:
                    success_wallets += 1
                else:
                    _fail_wallet()
                if idx < len(wallet_key_records) - 1 and cfg.wallet_delay_max_sec > 0:
                    delay_sec = random.uniform(cfg.wallet_delay_min_sec, cfg.wallet_delay_max_sec)
                    logger.info("[DOMAIN] delay before next wallet: %.2f sec", delay_sec)
                    time.sleep(delay_sec)
                continue

            if src_symbol == "USDC.E":
                pool_fw = _pick_pool("USDC.E", dst_symbol)
                pool_bw = _pick_pool(dst_symbol, "USDC.E")
                if not pool_fw or not pool_bw:
                    logger.warning("[DOMAIN] wallet=%s no route USDC.E<->%s", wallet, dst_symbol)
                    _fail_wallet()
                else:
                    pair_fw = _find_tokens_for_direction(pool_fw, "USDC.E", dst_symbol)
                    if not pair_fw:
                        logger.warning("[DOMAIN] wallet=%s invalid pool direction USDC.E>%s", wallet, dst_symbol)
                        _fail_wallet()
                    else:
                        dst_token = pair_fw[1]
                        before_dst = exec_client.get_erc20_balance(dst_token.address, dst_token.decimals)
                        ok_fw = _execute_trade_for_pair(
                            cfg=cfg,
                            logger=logger,
                            state=state,
                            exec_client=exec_client,
                            pool=pool_fw,
                            symbol_in="USDC.E",
                            symbol_out=dst_symbol,
                            trade_amount_expr=amount_expr,
                            eth_price=eth_price,
                            label=f"DOMAIN {wallet} USDC.E>{dst_symbol}",
                            bypass_risk_checks=True,
                            allow_no_quoter_execution=True,
                            wait_for_pre_tx=True,
                            post_approve_delay_range=domain_pre_tx_delay_range,
                        )
                        if not ok_fw:
                            _fail_wallet()
                            continue
                        if ok_fw and state.last_tx_hash:
                            _wait_tx_receipt(exec_client, state.last_tx_hash, timeout_sec=180)
                            _sleep_between_domain_swaps()
                        after_dst = exec_client.get_erc20_balance(dst_token.address, dst_token.decimals)
                        delta_dst = after_dst - before_dst
                        if delta_dst <= 0:
                            logger.warning("[DOMAIN] wallet=%s no received %s for reverse swap", wallet, dst_symbol)
                            _fail_wallet()
                        else:
                            back_expr = _format_decimal_plain(delta_dst)
                            ok_bw = _execute_trade_for_pair(
                                cfg=cfg,
                                logger=logger,
                                state=state,
                                exec_client=exec_client,
                                pool=pool_bw,
                                symbol_in=dst_symbol,
                                symbol_out="USDC.E",
                                trade_amount_expr=back_expr,
                                eth_price=eth_price,
                                label=f"DOMAIN {wallet} {dst_symbol}>USDC.E",
                                bypass_risk_checks=True,
                                allow_no_quoter_execution=True,
                                wait_for_pre_tx=True,
                                post_approve_delay_range=domain_pre_tx_delay_range,
                            )
                            if ok_bw:
                                success_wallets += 1
                            else:
                                _fail_wallet()
            else:
                pool_direct_fw = _pick_pool("ETH", dst_symbol)
                pool_direct_bw = _pick_pool(dst_symbol, "ETH")
                if pool_direct_fw and pool_direct_bw:
                    pair_fw = _find_tokens_for_direction(pool_direct_fw, "ETH", dst_symbol)
                    if not pair_fw:
                        logger.warning("[DOMAIN] wallet=%s invalid direct pool direction ETH>%s", wallet, dst_symbol)
                        _fail_wallet()
                    else:
                        dst_token = pair_fw[1]
                        before_dst = exec_client.get_erc20_balance(dst_token.address, dst_token.decimals)
                        ok_fw = _execute_trade_for_pair(
                            cfg=cfg,
                            logger=logger,
                            state=state,
                            exec_client=exec_client,
                            pool=pool_direct_fw,
                            symbol_in="ETH",
                            symbol_out=dst_symbol,
                            trade_amount_expr=amount_expr,
                            eth_price=eth_price,
                            label=f"DOMAIN {wallet} ETH>{dst_symbol}",
                            bypass_risk_checks=True,
                            allow_no_quoter_execution=True,
                            wait_for_pre_tx=True,
                            post_approve_delay_range=domain_pre_tx_delay_range,
                        )
                        if not ok_fw:
                            _fail_wallet()
                            continue
                        if ok_fw and state.last_tx_hash:
                            _wait_tx_receipt(exec_client, state.last_tx_hash, timeout_sec=180)
                            _sleep_between_domain_swaps()
                        after_dst = exec_client.get_erc20_balance(dst_token.address, dst_token.decimals)
                        delta_dst = after_dst - before_dst
                        if delta_dst <= 0:
                            logger.warning("[DOMAIN] wallet=%s no received %s for reverse swap", wallet, dst_symbol)
                            _fail_wallet()
                        else:
                            back_expr = _format_decimal_plain(delta_dst)
                            ok_bw = _execute_trade_for_pair(
                                cfg=cfg,
                                logger=logger,
                                state=state,
                                exec_client=exec_client,
                                pool=pool_direct_bw,
                                symbol_in=dst_symbol,
                                symbol_out="ETH",
                                trade_amount_expr=back_expr,
                                eth_price=eth_price,
                                label=f"DOMAIN {wallet} {dst_symbol}>ETH",
                                bypass_risk_checks=True,
                                allow_no_quoter_execution=True,
                                wait_for_pre_tx=True,
                                post_approve_delay_range=domain_pre_tx_delay_range,
                            )
                            if ok_bw:
                                success_wallets += 1
                            else:
                                _fail_wallet()
                else:
                    pool_eth_usdc = _pick_pool("ETH", "USDC.E")
                    pool_usdc_dst = _pick_pool("USDC.E", dst_symbol)
                    pool_dst_usdc = _pick_pool(dst_symbol, "USDC.E")
                    pool_usdc_eth = _pick_pool("USDC.E", "ETH")
                    if not pool_eth_usdc or not pool_usdc_dst or not pool_dst_usdc or not pool_usdc_eth:
                        logger.warning("[DOMAIN] wallet=%s no route ETH>USDC.E>%s and back", wallet, dst_symbol)
                        _fail_wallet()
                    else:
                        usdc_from_eth_pair = _find_tokens_for_direction(pool_eth_usdc, "ETH", "USDC.E")
                        dst_from_usdc_pair = _find_tokens_for_direction(pool_usdc_dst, "USDC.E", dst_symbol)
                        usdc_from_dst_pair = _find_tokens_for_direction(pool_dst_usdc, dst_symbol, "USDC.E")
                        eth_from_usdc_pair = _find_tokens_for_direction(pool_usdc_eth, "USDC.E", "ETH")
                        if not usdc_from_eth_pair or not dst_from_usdc_pair or not usdc_from_dst_pair or not eth_from_usdc_pair:
                            logger.warning("[DOMAIN] wallet=%s invalid multi-hop pool direction for %s", wallet, dst_symbol)
                            _fail_wallet()
                        else:
                            weth_token = usdc_from_eth_pair[0]
                            usdc_token = usdc_from_eth_pair[1]
                            dst_token = dst_from_usdc_pair[1]

                            # Forward in one logical swap: ETH -> DOMAIN_TOKEN (via USDC.E path).
                            before_dst = exec_client.get_erc20_balance(dst_token.address, dst_token.decimals)
                            ok_fw = _execute_trade_for_path(
                                cfg=cfg,
                                logger=logger,
                                state=state,
                                exec_client=exec_client,
                                token_in=weth_token,
                                token_out=dst_token,
                                path_tokens=[weth_token, usdc_token, dst_token],
                                path_token_addresses=[weth_token.address, usdc_token.address, dst_token.address],
                                path_fee_tiers=[pool_eth_usdc.fee_tier, pool_usdc_dst.fee_tier],
                                trade_amount_expr=amount_expr,
                                eth_price=eth_price,
                                label=f"DOMAIN {wallet} ETH>{dst_symbol}",
                                is_eth_source=True,
                                wait_for_pre_tx=True,
                                post_approve_delay_range=domain_pre_tx_delay_range,
                            )
                            if not ok_fw:
                                logger.warning("[DOMAIN] wallet=%s first leg failed ETH>%s", wallet, dst_symbol)
                                _fail_wallet()
                            else:
                                if state.last_tx_hash:
                                    _wait_tx_receipt(exec_client, state.last_tx_hash, timeout_sec=180)
                                    _sleep_between_domain_swaps()
                                after_dst = exec_client.get_erc20_balance(dst_token.address, dst_token.decimals)
                                delta_dst = after_dst - before_dst
                                if delta_dst <= 0:
                                    logger.warning("[DOMAIN] wallet=%s no received %s for reverse route", wallet, dst_symbol)
                                    _fail_wallet()
                                else:
                                    ok_bw = _execute_trade_for_path(
                                        cfg=cfg,
                                        logger=logger,
                                        state=state,
                                        exec_client=exec_client,
                                        token_in=dst_token,
                                        token_out=eth_from_usdc_pair[1],
                                        path_tokens=[dst_token, usdc_token, eth_from_usdc_pair[1]],
                                        path_token_addresses=[dst_token.address, usdc_token.address, eth_from_usdc_pair[1].address],
                                        path_fee_tiers=[pool_dst_usdc.fee_tier, pool_usdc_eth.fee_tier],
                                        trade_amount_expr=_format_decimal_plain(delta_dst),
                                        eth_price=eth_price,
                                        label=f"DOMAIN {wallet} {dst_symbol}>ETH",
                                        is_eth_source=False,
                                        wait_for_pre_tx=True,
                                        post_approve_delay_range=domain_pre_tx_delay_range,
                                    )
                                    if ok_bw:
                                        success_wallets += 1
                                    else:
                                        _fail_wallet()

        finally:
            if before_points_snapshot is not None:
                after_points_snapshot = _fetch_wallet_points_snapshot(cfg, wallet, proxies, logger, "DOMAIN")
                _log_wallet_points_delta(logger, "DOMAIN", wallet, before_points_snapshot, after_points_snapshot)

        if idx < len(wallet_key_records) - 1 and cfg.wallet_delay_max_sec > 0:
            delay_sec = random.uniform(cfg.wallet_delay_min_sec, cfg.wallet_delay_max_sec)
            logger.info("[DOMAIN] delay before next wallet: %.2f sec", delay_sec)
            time.sleep(delay_sec)
    _print_mode_summary(
        "DOMAIN",
        len(wallet_key_records),
        success_wallets,
        failed_wallets,
        skipped_wallets,
        failed_wallet_addresses,
    )


def get_sweep_menu_input(state: BotState) -> Optional[Tuple[str, str]]:
    _ = state
    print("\nSweep target:")
    print("1) Collect to USDC.E")
    print("2) Collect to ETH")
    print("3) Back")
    print("Token exceptions: sweep_token_exclusions.txt (one name, symbol, or address per line)")
    target_raw = input("Select [1-3]: ").strip()
    if target_raw == "3":
        return None
    if target_raw not in {"1", "2"}:
        raise ValueError("Invalid sweep target selection")
    target_asset = "USDC.E" if target_raw == "1" else "ETH"
    eth_percent_raw = "0"
    if target_asset == "USDC.E":
        eth_percent_raw = input("Percent of native ETH to swap to USDC.E [100]: ").strip() or "100"
        eth_percent = _parse_decimal_input(eth_percent_raw)
        if eth_percent < 0 or eth_percent > 100:
            raise ValueError("ETH percent must be between 0 and 100")
    return target_asset, eth_percent_raw


def run_sweep_tokens_to_usdce_once(cfg: BotConfig, logger: logging.Logger, state: BotState) -> None:
    success_wallets = 0
    failed_wallets = 0
    skipped_wallets = 0
    failed_wallet_addresses: List[str] = []

    def _fail_wallet() -> None:
        nonlocal failed_wallets
        failed_wallets += 1
        if wallet not in failed_wallet_addresses:
            failed_wallet_addresses.append(wallet)

    picked = get_sweep_menu_input(state)
    if not picked:
        logger.info("Sweep mode canceled by user.")
        return
    target_asset, eth_percent_raw = picked
    eth_percent = _parse_decimal_input(eth_percent_raw)
    sweep_exclusions = _load_sweep_token_exclusions(cfg.sweep_token_exclusions_file)

    wallet_key_records = _build_wallet_key_records(cfg, logger, "SWEEP")
    if not wallet_key_records:
        raise ValueError(
            "No wallet/private-key pairs available for sweep "
            "(fill wallets.txt + keys.txt line-by-line or set valid PRIVATE_KEY in .env)"
        )
    wallet_key_records, wallet_start_offset, total_loaded_wallets = _apply_wallet_start_selection(wallet_key_records)

    logger.info(
        "[SWEEP] mode started | target=%s | wallets=%s | start_wallet=%s | exclusions=%s%s",
        target_asset,
        len(wallet_key_records),
        wallet_start_offset + 1,
        len(sweep_exclusions),
        f" | native_eth_to_usdc={_format_decimal_plain(eth_percent)}%" if target_asset == "USDC.E" else "",
    )

    def _sleep_between_actions() -> None:
        delay_sec = _random_swap_delay_sec()
        logger.info("[SWEEP] delay between token sales: %.2f sec", delay_sec)
        time.sleep(delay_sec)

    logger.info("[SWEEP] loading shared metadata...")
    shared_doma_api = DomaApiClient(
        cfg.doma_api_url,
        api_key=cfg.doma_api_key,
        api_keys=cfg.doma_api_keys,
        proxies=None,
    )
    usdc_token = _usdce_token_from_config(cfg)
    weth_token = _token_from_config_override(cfg, "WETH", 18)
    eth_price = _fetch_eth_price_via_doma_quote(cfg, shared_doma_api, usdc_token)
    if eth_price <= 0:
        raise RuntimeError("Failed to resolve ETH/USD")
    token_catalog = shared_doma_api.fetch_fractional_tokens(take=100, max_pages=10)
    if not token_catalog:
        raise RuntimeError("Doma fractional token catalog is empty")
    try:
        pools = shared_doma_api.fetch_top_pools_by_tvl(limit=100, eth_price_usd=eth_price)
    except Exception as exc:
        logger.warning("[SWEEP] Doma API pools failed, direct pool fallback disabled: %s", exc)
        pools = []
    logger.info(
        "[SWEEP] shared metadata loaded | pools=%s | tokens=%s | eth_price=%s | route_source=doma_ui",
        len(pools),
        len(token_catalog),
        _format_decimal_plain(eth_price),
    )

    for idx, (line_idx, wallet, private_key) in enumerate(wallet_key_records):
        proxies, skip_wallet = _proxy_for_line(cfg, line_idx, logger, "SWEEP")
        if skip_wallet:
            skipped_wallets += 1
            continue
        logger.info("[SWEEP] wallet %s", _wallet_record_progress_label(idx, len(wallet_key_records), line_idx, total_loaded_wallets, wallet))

        exec_client = _build_exec_client_with_rpc_fallback(
            cfg=cfg,
            logger=logger,
            wallet=wallet,
            private_key=private_key,
            proxies=proxies,
            log_prefix="[SWEEP]",
        )

        held_launchpad_tokens: List[Tuple[LaunchpadTokenInfo, Decimal]] = []
        seen_token_addresses: set[str] = set()
        had_errors = False
        sold_any = False

        for info in token_catalog:
            token_symbol = canonical_symbol(info.symbol or info.name)
            if token_symbol in {"ETH", "USDC", "USDC.E"}:
                continue
            token_address = (info.address or "").lower()
            if not token_address or token_address in seen_token_addresses:
                continue
            try:
                balance_dec = exec_client.get_erc20_balance(info.address, info.decimals)
            except Exception:
                continue
            if balance_dec <= 0:
                continue
            seen_token_addresses.add(token_address)
            if _is_sweep_token_excluded(info, sweep_exclusions):
                logger.info(
                    "[SWEEP] wallet=%s token=%s excluded | balance=%s",
                    wallet,
                    token_symbol,
                    _format_decimal_plain(balance_dec),
                )
                continue
            token_value_usd = balance_dec * info.price_usd if info.price_usd > 0 else Decimal("0")
            if info.price_usd <= 0:
                logger.info(
                    "[SWEEP] wallet=%s token=%s skipped | token price unavailable",
                    wallet,
                    token_symbol,
                )
                continue
            if token_value_usd < SWEEP_MIN_TOKEN_VALUE_USD:
                logger.info(
                    "[SWEEP] wallet=%s token=%s dust skipped | value=$%s < $%s",
                    wallet,
                    token_symbol,
                    _format_decimal_plain(token_value_usd),
                    _format_decimal_plain(SWEEP_MIN_TOKEN_VALUE_USD),
                )
                continue
            held_launchpad_tokens.append((info, balance_dec))

        held_weth_balance = Decimal("0")
        try:
            held_weth_balance = exec_client.get_erc20_balance(weth_token.address, weth_token.decimals)
        except Exception:
            held_weth_balance = Decimal("0")
        held_weth_value_usd = held_weth_balance * eth_price
        if held_weth_balance > 0 and held_weth_value_usd < SWEEP_MIN_TOKEN_VALUE_USD:
            logger.info(
                "[SWEEP] wallet=%s token=WETH dust skipped | value=$%s < $%s",
                wallet,
                _format_decimal_plain(held_weth_value_usd),
                _format_decimal_plain(SWEEP_MIN_TOKEN_VALUE_USD),
            )
            held_weth_balance = Decimal("0")
        native_eth_balance = exec_client.get_native_balance()
        reserve_eth = _native_gas_reserve_eth(eth_price)
        spendable_eth = native_eth_balance - reserve_eth
        spendable_eth = spendable_eth if spendable_eth > 0 else Decimal("0")

        has_any_assets = bool(held_launchpad_tokens) or held_weth_balance > 0
        if target_asset == "ETH":
            has_any_assets = has_any_assets or exec_client.get_erc20_balance(usdc_token.address, usdc_token.decimals) > 0
        else:
            has_any_assets = has_any_assets or (spendable_eth > 0 and eth_percent > 0)

        if not has_any_assets:
            logger.info("[SWEEP] wallet=%s nothing to collect for target=%s", wallet, target_asset)
            skipped_wallets += 1
            if idx < len(wallet_key_records) - 1 and cfg.wallet_delay_max_sec > 0:
                delay_sec = random.uniform(cfg.wallet_delay_min_sec, cfg.wallet_delay_max_sec)
                logger.info("[SWEEP] delay before next wallet: %.2f sec", delay_sec)
                time.sleep(delay_sec)
            continue

        logger.info(
            "[SWEEP] wallet=%s tokens_to_sell=%s%s%s%s",
            wallet,
            len(held_launchpad_tokens),
            f" | WETH={_format_decimal_plain(held_weth_balance)}" if held_weth_balance > 0 else "",
            f" | ETH={_format_decimal_plain(spendable_eth)}" if spendable_eth > 0 else "",
            f" | target={target_asset}",
        )

        for info, balance_dec in held_launchpad_tokens:
            token_symbol = canonical_symbol(info.symbol or info.name)
            logger.info(
                "[SWEEP] wallet=%s token=%s balance=%s",
                wallet,
                token_symbol,
                _format_decimal_plain(balance_dec),
            )
            ok = False
            token_meta = _token_from_launchpad_price(info, eth_price)
            if info.address and info.price_usd > 0:
                ok = _execute_trade_via_doma_ui_route(
                    cfg=cfg,
                    logger=logger,
                    state=state,
                    doma_api=shared_doma_api,
                    exec_client=exec_client,
                    token_in=token_meta,
                    token_out=usdc_token,
                    display_in_symbol=token_symbol,
                    display_out_symbol="USDC.E",
                    trade_amount_expr="100%",
                    eth_price=eth_price,
                    label=f"SWEEP {wallet} {token_symbol}>USDC.E",
                    is_eth_source=False,
                    unwrap_to_native=False,
                    wait_for_pre_tx=True,
                )
            if not ok and info.pool_address:
                pool = _find_pool_by_address(pools, info.pool_address)
                if pool is None:
                    pool = _find_best_pool_for_symbols(cfg, pools, token_symbol, "USDC.E", ignore_limits=True)
                if pool is not None:
                    ok = _execute_trade_for_pair(
                        cfg=cfg,
                        logger=logger,
                        state=state,
                        exec_client=exec_client,
                        pool=pool,
                        symbol_in=token_symbol,
                        symbol_out="USDC.E",
                        trade_amount_expr="100%",
                        eth_price=eth_price,
                        label=f"SWEEP {wallet} {token_symbol}>USDC.E",
                        bypass_risk_checks=True,
                        allow_no_quoter_execution=True,
                        wait_for_pre_tx=True,
                    )
                else:
                    logger.warning("[SWEEP] wallet=%s token=%s no pool route to USDC.E", wallet, token_symbol)
            if not ok and info.launchpad_address and info.quote_token_address == usdc_token.address:
                ok = _execute_launchpad_sell(
                    cfg=cfg,
                    logger=logger,
                    state=state,
                    exec_client=exec_client,
                    launchpad=info,
                    quote_token=usdc_token,
                    trade_amount_expr="100%",
                    eth_price=eth_price,
                    label=f"SWEEP {wallet} {token_symbol}>USDC.E",
                    wait_for_pre_tx=True,
                )
            if not ok and not info.pool_address and not info.launchpad_address:
                logger.warning(
                    "[SWEEP] wallet=%s token=%s unsupported route to USDC.E (pool=%s launchpad=%s quote=%s)",
                    wallet,
                    token_symbol,
                    bool(info.pool_address),
                    bool(info.launchpad_address),
                    info.quote_token_address or "n/a",
                    )

            if ok:
                sold_any = True
                _sleep_between_actions()
            else:
                had_errors = True

        if held_weth_balance > 0:
            logger.info(
                "[SWEEP] wallet=%s token=%s balance=%s",
                wallet,
                weth_token.symbol,
                _format_decimal_plain(held_weth_balance),
            )
            if target_asset == "ETH":
                ok = _cleanup_weth_balance(
                    logger=logger,
                    exec_client=exec_client,
                    weth_token=weth_token,
                    label=f"SWEEP {wallet} WETH>ETH",
                    reason="target ETH",
                    wait_for_receipt=True,
                )
            else:
                ok = _execute_trade_via_doma_ui_route(
                    cfg=cfg,
                    logger=logger,
                    state=state,
                    doma_api=shared_doma_api,
                    exec_client=exec_client,
                    token_in=weth_token,
                    token_out=usdc_token,
                    display_in_symbol="WETH",
                    display_out_symbol="USDC.E",
                    trade_amount_expr="100%",
                    eth_price=eth_price,
                    label=f"SWEEP {wallet} WETH>USDC.E",
                    is_eth_source=False,
                    unwrap_to_native=False,
                    wait_for_pre_tx=True,
                )
            if ok:
                sold_any = True
                _sleep_between_actions()
            else:
                had_errors = True

        if target_asset == "USDC.E" and eth_percent > 0:
            native_eth_balance = exec_client.get_native_balance()
            spendable_eth = native_eth_balance - reserve_eth
            spendable_eth = spendable_eth if spendable_eth > 0 else Decimal("0")
            native_eth_expr = f"{_format_decimal_plain(eth_percent)}%"
            _, native_eth_usd = resolve_trade_amount(native_eth_expr, spendable_eth, eth_price) if spendable_eth > 0 else (Decimal("0"), Decimal("0"))
            if spendable_eth > 0 and native_eth_usd >= MIN_EXECUTABLE_TRADE_USD:
                logger.info(
                    "[SWEEP] wallet=%s native ETH->USDC.E amount=%s of spendable ETH",
                    wallet,
                    native_eth_expr,
                )
                ok = _execute_trade_via_doma_ui_route(
                    cfg=cfg,
                    logger=logger,
                    state=state,
                    doma_api=shared_doma_api,
                    exec_client=exec_client,
                    token_in=weth_token,
                    token_out=usdc_token,
                    display_in_symbol="ETH",
                    display_out_symbol="USDC.E",
                    trade_amount_expr=native_eth_expr,
                    eth_price=eth_price,
                    label=f"SWEEP {wallet} ETH>USDC.E",
                    is_eth_source=True,
                    unwrap_to_native=False,
                    wait_for_pre_tx=True,
                )
                if ok and state.last_tx_hash and _wait_tx_receipt(exec_client, state.last_tx_hash, timeout_sec=180):
                    sold_any = True
                else:
                    had_errors = True
            elif spendable_eth > 0 and eth_percent > 0:
                logger.info(
                    "[SWEEP] wallet=%s native ETH->USDC.E skipped | below $0.10 (%s)",
                    wallet,
                    _format_decimal_plain(native_eth_usd),
                )

        if target_asset == "ETH":
            usdc_balance = exec_client.get_erc20_balance(usdc_token.address, usdc_token.decimals)
            if usdc_balance >= MIN_EXECUTABLE_TRADE_USD:
                logger.info(
                    "[SWEEP] wallet=%s final settle | USDC.E->ETH amount=100%% of USDC.E",
                    wallet,
                )
                ok = _execute_trade_via_doma_ui_route(
                    cfg=cfg,
                    logger=logger,
                    state=state,
                    doma_api=shared_doma_api,
                    exec_client=exec_client,
                    token_in=usdc_token,
                    token_out=weth_token,
                    display_in_symbol="USDC.E",
                    display_out_symbol="ETH",
                    trade_amount_expr="100%",
                    eth_price=eth_price,
                    label=f"SWEEP {wallet} USDC.E>ETH",
                    is_eth_source=False,
                    unwrap_to_native=True,
                    wait_for_pre_tx=True,
                )
                if ok and state.last_tx_hash and _wait_tx_receipt(exec_client, state.last_tx_hash, timeout_sec=180):
                    sold_any = True
                else:
                    had_errors = True
            elif usdc_balance > 0:
                logger.info(
                    "[SWEEP] wallet=%s final settle skipped | USDC.E dust below $0.10 (%s)",
                    wallet,
                    _format_decimal_plain(usdc_balance),
                )

        if had_errors:
            _fail_wallet()
        elif sold_any:
            success_wallets += 1
        else:
            skipped_wallets += 1

        if idx < len(wallet_key_records) - 1 and cfg.wallet_delay_max_sec > 0:
            delay_sec = random.uniform(cfg.wallet_delay_min_sec, cfg.wallet_delay_max_sec)
            logger.info("[SWEEP] delay before next wallet: %.2f sec", delay_sec)
            time.sleep(delay_sec)

    _print_mode_summary(
        "SWEEP",
        len(wallet_key_records),
        success_wallets,
        failed_wallets,
        skipped_wallets,
        failed_wallet_addresses,
    )


def get_pair_swap_menu_input(state: BotState) -> Optional[Tuple[str, str, str, str]]:
    _ = state
    print("\nPair swap ETH / WETH / USDC.E:")
    print("1) ETH -> USDC.E")
    print("2) USDC.E -> ETH")
    print("3) WETH -> ETH")
    print("4) WETH -> USDC.E")
    print("5) Back")
    route_raw = input("Select [1-5]: ").strip()
    if route_raw == "5":
        return None
    route_map = {
        "1": ("ETH", "USDC.E"),
        "2": ("USDC.E", "ETH"),
        "3": ("WETH", "ETH"),
        "4": ("WETH", "USDC.E"),
    }
    if route_raw not in route_map:
        raise ValueError("Invalid route selection")
    src_symbol, dst_symbol = route_map[route_raw]

    print("\nAmount mode:")
    print(f"1) Number ({src_symbol})")
    print("2) Percent (%)")
    mode_raw = input("Select [1-2]: ").strip()
    if mode_raw not in {"1", "2"}:
        raise ValueError("Invalid amount mode selection")
    amount_mode = "number" if mode_raw == "1" else "percent"

    if amount_mode == "percent":
        percent_raw = input("Percent: ").strip()
        _ = _parse_decimal_input(percent_raw)
        return src_symbol, dst_symbol, amount_mode, percent_raw

    min_raw = input("Minimum: ").strip()
    max_raw = input("Maximum: ").strip()
    _ = _parse_decimal_input(min_raw)
    _ = _parse_decimal_input(max_raw)
    return src_symbol, dst_symbol, amount_mode, f"{min_raw}|{max_raw}"


def run_pair_swap_once(cfg: BotConfig, logger: logging.Logger, state: BotState) -> None:
    success_wallets = 0
    failed_wallets = 0
    skipped_wallets = 0
    failed_wallet_addresses: List[str] = []

    def _fail_wallet() -> None:
        nonlocal failed_wallets
        failed_wallets += 1
        if wallet not in failed_wallet_addresses:
            failed_wallet_addresses.append(wallet)
    picked = get_pair_swap_menu_input(state)
    if not picked:
        logger.info("Pair swap canceled by user.")
        return
    src_symbol, dst_symbol, amount_mode, amount_raw = picked

    wallet_key_records = _build_wallet_key_records(cfg, logger, "PAIR")
    if not wallet_key_records:
        raise ValueError(
            "No wallet/private-key pairs available for pair swap "
            "(fill wallets.txt + keys.txt line-by-line or set valid PRIVATE_KEY in .env)"
        )
    wallet_key_records, wallet_start_offset, total_loaded_wallets = _apply_wallet_start_selection(wallet_key_records)

    logger.info(
        "[PAIR] mode started | source=%s target=%s wallets=%s | start_wallet=%s",
        src_symbol,
        dst_symbol,
        len(wallet_key_records),
        wallet_start_offset + 1,
    )

    for idx, (line_idx, wallet, private_key) in enumerate(wallet_key_records):
        proxies, skip_wallet = _proxy_for_line(cfg, line_idx, logger, "PAIR")
        if skip_wallet:
            skipped_wallets += 1
            continue
        logger.info("[PAIR] wallet %s", _wallet_record_progress_label(idx, len(wallet_key_records), line_idx, total_loaded_wallets, wallet))
        before_points_snapshot: Optional[PointsSnapshot] = None
        try:
            try:
                subgraph = DomaSubgraphClient(cfg.subgraph_url, proxies=proxies)
                doma_api = DomaApiClient(
                    cfg.doma_api_url,
                    api_key=cfg.doma_api_key,
                    api_keys=cfg.doma_api_keys,
                    proxies=proxies,
                )
                pools = subgraph.fetch_top_pools(limit=1000)
                eth_price = subgraph.fetch_eth_price_usd()
                if eth_price <= 0:
                    raise RuntimeError("Failed to resolve ETH/USD")
                pool = _find_best_pool_for_symbols(cfg, pools, src_symbol, dst_symbol, ignore_limits=True)
                if not pool:
                    raise RuntimeError(f"No pool route found for {src_symbol}->{dst_symbol}")
            except Exception as exc:
                logger.warning("[PAIR] wallet=%s subgraph init failed, using Doma quote fallback: %s", wallet, exc)
                try:
                    doma_api = DomaApiClient(
                        cfg.doma_api_url,
                        api_key=cfg.doma_api_key,
                        api_keys=cfg.doma_api_keys,
                        proxies=proxies,
                    )
                    pool, eth_price = _fallback_eth_usdce_pool_for_ui_route(cfg, doma_api)
                except Exception as fallback_exc:
                    _fail_wallet()
                    logger.warning("[PAIR] wallet=%s init failed: %s", wallet, fallback_exc)
                    continue

            if amount_mode == "percent":
                amount_expr = f"{amount_raw}%"
            else:
                min_raw, max_raw = [x.strip() for x in amount_raw.split("|", 1)]
                amount_expr = _pick_random_amount_expr(
                    amount_mode,
                    _parse_decimal_input(min_raw),
                    _parse_decimal_input(max_raw),
                    state,
                    min_raw=min_raw,
                    max_raw=max_raw,
                )
            logger.info("[PAIR] wallet=%s amount=%s %s", wallet, amount_expr, src_symbol)
            exec_client = _build_exec_client_with_rpc_fallback(
                cfg=cfg,
                logger=logger,
                wallet=wallet,
                private_key=private_key,
                proxies=proxies,
                log_prefix="[PAIR]",
            )
            before_points_snapshot = _fetch_wallet_points_snapshot(cfg, wallet, proxies, logger, "PAIR")

            usdc_token, weth_token = _find_tokens_for_direction(pool, "USDC.E", "ETH") or (None, None)
            if not usdc_token or not weth_token:
                _fail_wallet()
                logger.warning("[PAIR] wallet=%s invalid ETH<->USDC.E pool metadata", wallet)
                continue

            if src_symbol == "WETH" and dst_symbol == "ETH":
                weth_balance = exec_client.get_erc20_balance(weth_token.address, weth_token.decimals)
                try:
                    amount_in_dec, trade_usd = resolve_trade_amount(amount_expr, weth_balance, eth_price)
                except Exception as exc:
                    logger.warning("[PAIR] wallet=%s invalid WETH unwrap amount '%s': %s", wallet, amount_expr, exc)
                    ok_swap = False
                else:
                    amount_in_raw = decimal_to_raw(amount_in_dec, weth_token.decimals)
                    if amount_in_dec > weth_balance or amount_in_raw <= 0:
                        logger.warning(
                            "[PAIR] wallet=%s insufficient WETH for unwrap: need=%s have=%s",
                            wallet,
                            _format_decimal_plain(amount_in_dec),
                            _format_decimal_plain(weth_balance),
                        )
                        ok_swap = False
                    elif cfg.paper_mode or cfg.dry_run or not cfg.enable_execution:
                        logger.info(
                            "[PAIR %s WETH>ETH] PAPER/DRY mode active. Would unwrap %s WETH (~$%s).",
                            wallet,
                            _format_decimal_plain(amount_in_dec),
                            _format_decimal_plain(trade_usd),
                        )
                        ok_swap = True
                    else:
                        tx_hash = exec_client.unwrap_weth(weth_token.address, amount_in_raw)
                        state.last_tx_hash = tx_hash
                        logger.info(
                            "[PAIR] wallet=%s WETH->ETH unwrap tx=%s | amount=%s WETH (~$%s)",
                            wallet,
                            tx_hash,
                            _format_decimal_plain(amount_in_dec),
                            _format_decimal_plain(trade_usd),
                        )
                        ok_swap = bool(tx_hash) and _wait_tx_receipt(exec_client, tx_hash, timeout_sec=180)
            else:
                ui_token_in = weth_token if src_symbol in {"ETH", "WETH"} else usdc_token
                ui_token_out = weth_token if dst_symbol == "ETH" else usdc_token
                ok_swap = _execute_trade_via_doma_ui_route(
                    cfg=cfg,
                    logger=logger,
                    state=state,
                    doma_api=doma_api,
                    exec_client=exec_client,
                    token_in=ui_token_in,
                    token_out=ui_token_out,
                    display_in_symbol=src_symbol,
                    display_out_symbol=dst_symbol,
                    trade_amount_expr=amount_expr,
                    eth_price=eth_price,
                    label=f"PAIR {wallet} {src_symbol}>{dst_symbol}",
                    is_eth_source=src_symbol == "ETH",
                    unwrap_to_native=dst_symbol == "ETH",
                    wait_for_pre_tx=True,
                )
            if ok_swap:
                success_wallets += 1
            else:
                _fail_wallet()
        finally:
            if before_points_snapshot is not None:
                after_points_snapshot = _fetch_wallet_points_snapshot(cfg, wallet, proxies, logger, "PAIR")
                _log_wallet_points_delta(logger, "PAIR", wallet, before_points_snapshot, after_points_snapshot)

        if idx < len(wallet_key_records) - 1 and cfg.wallet_delay_max_sec > 0:
            delay_sec = random.uniform(cfg.wallet_delay_min_sec, cfg.wallet_delay_max_sec)
            logger.info("[PAIR] delay before next wallet: %.2f sec", delay_sec)
            time.sleep(delay_sec)
    _print_mode_summary(
        "PAIR",
        len(wallet_key_records),
        success_wallets,
        failed_wallets,
        skipped_wallets,
        failed_wallet_addresses,
    )


def get_volume_farm_menu_input(state: BotState, default_check_weekly: bool = False) -> Optional[Tuple[str, str, str, str]]:
    _ = state
    print("\nFarm volume ETH <-> USDC.E:")
    print("\nPartial return percent range:")
    min_raw = input("Minimum percent [80]: ").strip() or "80"
    max_raw = input("Maximum percent [90]: ").strip() or "90"
    _ = _parse_decimal_input(min_raw)
    _ = _parse_decimal_input(max_raw)

    target_raw = input("Target volume in USDC.E [251]: ").strip() or "251"
    _ = _parse_decimal_input(target_raw)
    print("\nExisting ETH/USDC.E volume check:")
    print("1) Check current UTC week and only top up remaining volume")
    print("2) Ignore history and run selected wallets")
    default_raw = "1" if default_check_weekly else "2"
    history_mode_raw = input(f"Select [1-2, default {default_raw}]: ").strip() or default_raw
    if history_mode_raw not in {"1", "2"}:
        raise ValueError("Invalid ETH/USDC.E volume check selection")
    history_mode = "check_topup" if history_mode_raw == "1" else "ignore"
    return min_raw, max_raw, target_raw, history_mode


def _eth_usdce_pool_addresses_from_pools(pools: List[Pool]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for pool in pools:
        symbols = {canonical_symbol(pool.token0.symbol), canonical_symbol(pool.token1.symbol)}
        if "USDC.E" not in symbols or "WETH" not in symbols:
            continue
        addr = str(pool.address or "").strip()
        if not addr:
            continue
        addr_lc = addr.lower()
        if addr_lc in seen:
            continue
        seen.add(addr_lc)
        out.append(addr)
    for addr in KNOWN_ETH_USDCE_POOL_ADDRESSES:
        addr_lc = addr.lower()
        if addr_lc not in seen:
            seen.add(addr_lc)
            out.append(addr)
    return out


def _fetch_weekly_eth_usdce_volume(
    doma_api: DomaApiClient,
    wallet: str,
    pools: List[Pool],
    since: datetime,
) -> Decimal:
    pool_addresses = _eth_usdce_pool_addresses_from_pools(pools)
    if not pool_addresses:
        return Decimal("0")
    return doma_api.fetch_wallet_pool_volume_usd(
        wallet_address=wallet,
        pool_address=pool_addresses[0],
        pool_addresses=pool_addresses,
        tracked_token_symbol="WETH",
        quote_token_symbol="USDC.E",
        max_pages=40,
        since=since,
    )


def _fetch_weekly_total_volume(
    doma_api: DomaApiClient,
    wallet: str,
    pools: List[Pool],
    since: datetime,
) -> Decimal:
    pool_addresses = _eth_usdce_pool_addresses_from_pools(pools)
    return doma_api.fetch_wallet_total_swap_volume_usd_from_explorer(
        wallet_address=wallet,
        quote_token_symbol="USDC.E",
        pool_addresses=pool_addresses,
        max_pages=80,
        since=since,
    )


def run_volume_farm_once(
    cfg: BotConfig,
    logger: logging.Logger,
    state: BotState,
    preset: Optional[Tuple[str, ...]] = None,
    weekly_remaining: bool = False,
) -> None:
    success_wallets = 0
    failed_wallets = 0
    skipped_wallets = 0
    failed_wallet_addresses: List[str] = []

    def _fail_wallet() -> None:
        nonlocal failed_wallets
        failed_wallets += 1
        if wallet not in failed_wallet_addresses:
            failed_wallet_addresses.append(wallet)

    picked = preset or get_volume_farm_menu_input(state, default_check_weekly=weekly_remaining)
    if not picked:
        logger.info("Volume farm canceled by user.")
        return
    if len(picked) == 3:
        min_raw, max_raw, target_raw = picked
        history_mode = "check_topup" if weekly_remaining else "ignore"
    else:
        min_raw, max_raw, target_raw, history_mode = picked
    if history_mode not in {"check_topup", "ignore"}:
        raise ValueError("Invalid ETH/USDC.E history mode")
    target_volume = _parse_decimal_input(target_raw)
    check_weekly_volume = history_mode == "check_topup"
    weekly_since = _current_week_start_utc() if check_weekly_volume else None

    wallet_key_records = _build_wallet_key_records(cfg, logger, "VOLUME")
    if not wallet_key_records:
        raise ValueError(
            "No wallet/private-key pairs available for volume farm "
            "(fill wallets.txt + keys.txt line-by-line or set valid PRIVATE_KEY in .env)"
        )
    wallet_key_records, wallet_start_offset, total_loaded_wallets = _apply_wallet_start_selection(wallet_key_records)

    logger.info(
        "[VOLUME] mode started | source=AUTO pair=ETH<->USDC.E wallets=%s | start_wallet=%s | target=%s USDC.E | history_mode=%s%s | pattern=auto-100%%->%s-%s%% | final=ETH",
        len(wallet_key_records),
        wallet_start_offset + 1,
        _format_decimal_plain(target_volume),
        history_mode,
        f" since={weekly_since.isoformat()}" if weekly_since else "",
        min_raw,
        max_raw,
    )

    def _sleep_between_swaps() -> None:
        delay_sec = _random_swap_delay_sec()
        logger.info("[VOLUME] delay between swaps: %.2f sec", delay_sec)
        time.sleep(delay_sec)

    for idx, (line_idx, wallet, private_key) in enumerate(wallet_key_records):
        proxies, skip_wallet = _proxy_for_line(cfg, line_idx, logger, "VOLUME")
        if skip_wallet:
            skipped_wallets += 1
            continue
        logger.info("[VOLUME] wallet %s", _wallet_record_progress_label(idx, len(wallet_key_records), line_idx, total_loaded_wallets, wallet))
        before_points_snapshot: Optional[PointsSnapshot] = None
        try:
            pools: List[Pool] = []
            try:
                subgraph = DomaSubgraphClient(cfg.subgraph_url, proxies=proxies)
                doma_api = DomaApiClient(
                    cfg.doma_api_url,
                    api_key=cfg.doma_api_key,
                    api_keys=cfg.doma_api_keys,
                    proxies=proxies,
                )
                pools = subgraph.fetch_top_pools(limit=1000)
                eth_price = subgraph.fetch_eth_price_usd()
                if eth_price <= 0:
                    raise RuntimeError("Failed to resolve ETH/USD")
                pool = _find_best_pool_for_symbols(cfg, pools, "ETH", "USDC.E", ignore_limits=True)
                if not pool:
                    raise RuntimeError("No pool route found for ETH<->USDC.E")
            except Exception as exc:
                logger.warning(
                    "[VOLUME] wallet=%s subgraph init failed, using Doma quote fallback: %s",
                    wallet,
                    exc,
                )
                try:
                    doma_api = DomaApiClient(
                        cfg.doma_api_url,
                        api_key=cfg.doma_api_key,
                        api_keys=cfg.doma_api_keys,
                        proxies=proxies,
                    )
                    pool, eth_price = _fallback_eth_usdce_pool_for_ui_route(cfg, doma_api)
                except Exception as fallback_exc:
                    _fail_wallet()
                    logger.warning("[VOLUME] wallet=%s init failed: %s", wallet, fallback_exc)
                    continue

            exec_client = _build_exec_client_with_rpc_fallback(
                cfg=cfg,
                logger=logger,
                wallet=wallet,
                private_key=private_key,
                proxies=proxies,
                log_prefix="[VOLUME]",
            )

            usdc_token, weth_token = _find_tokens_for_direction(pool, "USDC.E", "ETH") or (None, None)
            if not usdc_token or not weth_token:
                _fail_wallet()
                logger.warning("[VOLUME] wallet=%s invalid ETH<->USDC.E pool metadata", wallet)
                continue

            before_points_snapshot = _fetch_wallet_points_snapshot(cfg, wallet, proxies, logger, "VOLUME")
            accumulated_volume = Decimal("0")
            wallet_target_volume = target_volume
            if weekly_since is not None:
                try:
                    weekly_done = _fetch_weekly_total_volume(doma_api, wallet, pools, weekly_since)
                except Exception as exc:
                    logger.warning(
                        "[VOLUME] wallet=%s weekly total volume fetch failed, assuming 0: %s",
                        wallet,
                        exc,
                    )
                    weekly_done = Decimal("0")
                weekly_remaining_volume = target_volume - weekly_done
                if weekly_remaining_volume <= 0:
                    logger.info(
                        "[VOLUME] wallet=%s weekly total volume already complete | done=%s/%s since=%s | skipping wallet",
                        wallet,
                        _format_decimal_plain(weekly_done),
                        _format_decimal_plain(target_volume),
                        weekly_since.isoformat(),
                    )
                    skipped_wallets += 1
                    continue
                wallet_target_volume = weekly_remaining_volume + WEEKLY_VOLUME_TOPUP_BUFFER_USD
                logger.info(
                    "[VOLUME] wallet=%s weekly total volume | done=%s/%s since=%s | remaining=%s | planned_topup=%s | topup_pair=ETH<->USDC.E",
                    wallet,
                    _format_decimal_plain(weekly_done),
                    _format_decimal_plain(target_volume),
                    weekly_since.isoformat(),
                    _format_decimal_plain(weekly_remaining_volume),
                    _format_decimal_plain(wallet_target_volume),
                )
            cycle = 0
            wallet_failed = False
            partial_min = _parse_decimal_input(min_raw)
            partial_max = _parse_decimal_input(max_raw)
            if partial_min <= 0 or partial_max <= 0:
                raise ValueError("Partial return percent must be > 0")
            if partial_max > 100:
                raise ValueError("Partial return percent cannot be > 100")

            while accumulated_volume < wallet_target_volume:
                cycle += 1
                logger.info(
                    "[VOLUME] wallet=%s cycle=%s | progress=%s/%s",
                    wallet,
                    cycle,
                    _format_decimal_plain(accumulated_volume),
                    _format_decimal_plain(wallet_target_volume),
                )

                _cleanup_weth_balance(
                    logger=logger,
                    exec_client=exec_client,
                    weth_token=weth_token,
                    label=f"VOLUME {wallet} CYCLE",
                    reason="cycle cleanup",
                    wait_for_receipt=True,
                )

                full_balance_usdc = exec_client.get_erc20_balance(usdc_token.address, usdc_token.decimals)
                reserve_eth = _native_gas_reserve_eth(eth_price)
                full_balance_eth = exec_client.get_native_balance() - reserve_eth
                full_balance_eth = full_balance_eth if full_balance_eth > 0 else Decimal("0")

                if full_balance_usdc >= MIN_EXECUTABLE_TRADE_USD:
                    full_in_symbol = "USDC.E"
                    full_out_symbol = "ETH"
                    partial_in_symbol = "ETH"
                    partial_out_symbol = "USDC.E"
                    full_balance = full_balance_usdc
                    full_trade_usd = full_balance_usdc
                    full_trade_expr = "100%"
                else:
                    full_in_symbol = "ETH"
                    full_out_symbol = "USDC.E"
                    partial_in_symbol = "USDC.E"
                    partial_out_symbol = "ETH"
                    full_balance = full_balance_eth
                    full_trade_usd = full_balance_eth * eth_price
                    full_trade_expr = _format_decimal_plain(full_balance_eth)

                if full_balance <= 0 or full_trade_usd <= 0:
                    logger.warning("[VOLUME] wallet=%s no usable ETH/USDC.E balance for volume cycle", wallet)
                    wallet_failed = True
                    break
                if full_trade_usd < MIN_EXECUTABLE_TRADE_USD:
                    logger.warning(
                        "[VOLUME] wallet=%s full step skipped | %s input below $0.10 (%s)",
                        wallet,
                        full_in_symbol,
                        _format_decimal_plain(full_trade_usd),
                    )
                    break

                logger.info(
                    "[VOLUME] wallet=%s full step | %s->%s amount=%s",
                    wallet,
                    full_in_symbol,
                    full_out_symbol,
                    "100% of USDC.E" if full_in_symbol == "USDC.E" else "100% spendable ETH",
                )
                full_before_usdc_balance = exec_client.get_erc20_balance(usdc_token.address, usdc_token.decimals)
                full_ui_token_in = usdc_token if full_in_symbol == "USDC.E" else weth_token
                full_ui_token_out = weth_token if full_out_symbol == "ETH" else usdc_token
                ok_full = _execute_trade_via_doma_ui_route(
                    cfg=cfg,
                    logger=logger,
                    state=state,
                    doma_api=doma_api,
                    exec_client=exec_client,
                    token_in=full_ui_token_in,
                    token_out=full_ui_token_out,
                    display_in_symbol=full_in_symbol,
                    display_out_symbol=full_out_symbol,
                    trade_amount_expr=full_trade_expr,
                    eth_price=eth_price,
                    label=f"VOLUME {wallet} {full_in_symbol}>{full_out_symbol}",
                    is_eth_source=full_in_symbol == "ETH",
                    unwrap_to_native=full_out_symbol == "ETH",
                    wait_for_pre_tx=True,
                )
                if not ok_full or not state.last_tx_hash:
                    wallet_failed = True
                    break
                if not _wait_tx_receipt(exec_client, state.last_tx_hash, timeout_sec=180):
                    logger.warning("[VOLUME] wallet=%s full step tx not confirmed before timeout | tx=%s", wallet, state.last_tx_hash)
                    wallet_failed = True
                    break
                full_after_usdc_balance = exec_client.get_erc20_balance(usdc_token.address, usdc_token.decimals)
                full_added_volume = _volume_added_from_usdc_balance_change(
                    full_before_usdc_balance,
                    full_after_usdc_balance,
                    full_in_symbol,
                )
                if full_added_volume <= 0:
                    logger.warning(
                        "[VOLUME] wallet=%s full step volume fallback used | before_usdc=%s after_usdc=%s input=%s",
                        wallet,
                        _format_decimal_plain(full_before_usdc_balance),
                        _format_decimal_plain(full_after_usdc_balance),
                        full_in_symbol,
                    )
                    full_added_volume = full_trade_usd
                accumulated_volume += full_added_volume
                logger.info(
                    "[VOLUME] wallet=%s full_added_volume=%s | total_volume=%s/%s",
                    wallet,
                    _format_decimal_plain(full_added_volume),
                    _format_decimal_plain(accumulated_volume),
                    _format_decimal_plain(wallet_target_volume),
                )
                if accumulated_volume >= wallet_target_volume:
                    break
                _sleep_between_swaps()

                partial_expr = _pick_random_amount_expr(
                    "percent",
                    partial_min,
                    partial_max,
                    state,
                    min_raw=min_raw,
                    max_raw=max_raw,
                )
                if partial_in_symbol == "USDC.E":
                    partial_balance = exec_client.get_erc20_balance(usdc_token.address, usdc_token.decimals)
                    _, partial_trade_usd = resolve_trade_amount(partial_expr, partial_balance, Decimal("1"))
                else:
                    partial_balance = exec_client.get_native_balance()
                    _, partial_trade_usd = resolve_trade_amount(partial_expr, partial_balance, eth_price)

                if partial_balance <= 0 or partial_trade_usd <= 0:
                    logger.warning("[VOLUME] wallet=%s no balance for partial %s step", wallet, partial_in_symbol)
                    wallet_failed = True
                    break
                if partial_trade_usd < MIN_EXECUTABLE_TRADE_USD:
                    logger.warning(
                        "[VOLUME] wallet=%s partial step skipped | %s input below $0.10 (%s)",
                        wallet,
                        partial_in_symbol,
                        _format_decimal_plain(partial_trade_usd),
                    )
                    break

                logger.info(
                    "[VOLUME] wallet=%s partial step | %s->%s amount=%s",
                    wallet,
                    partial_in_symbol,
                    partial_out_symbol,
                    partial_expr,
                )
                partial_before_usdc_balance = exec_client.get_erc20_balance(usdc_token.address, usdc_token.decimals)
                partial_ui_token_in = usdc_token if partial_in_symbol == "USDC.E" else weth_token
                partial_ui_token_out = weth_token if partial_out_symbol == "ETH" else usdc_token
                ok_partial = _execute_trade_via_doma_ui_route(
                    cfg=cfg,
                    logger=logger,
                    state=state,
                    doma_api=doma_api,
                    exec_client=exec_client,
                    token_in=partial_ui_token_in,
                    token_out=partial_ui_token_out,
                    display_in_symbol=partial_in_symbol,
                    display_out_symbol=partial_out_symbol,
                    trade_amount_expr=partial_expr,
                    eth_price=eth_price,
                    label=f"VOLUME {wallet} {partial_in_symbol}>{partial_out_symbol}",
                    is_eth_source=partial_in_symbol == "ETH",
                    unwrap_to_native=partial_out_symbol == "ETH",
                    wait_for_pre_tx=True,
                )
                if not ok_partial or not state.last_tx_hash:
                    wallet_failed = True
                    break
                if not _wait_tx_receipt(exec_client, state.last_tx_hash, timeout_sec=180):
                    logger.warning("[VOLUME] wallet=%s partial step tx not confirmed before timeout | tx=%s", wallet, state.last_tx_hash)
                    wallet_failed = True
                    break
                partial_after_usdc_balance = exec_client.get_erc20_balance(usdc_token.address, usdc_token.decimals)
                partial_added_volume = _volume_added_from_usdc_balance_change(
                    partial_before_usdc_balance,
                    partial_after_usdc_balance,
                    partial_in_symbol,
                )
                if partial_added_volume <= 0:
                    logger.warning(
                        "[VOLUME] wallet=%s partial step volume fallback used | before_usdc=%s after_usdc=%s input=%s",
                        wallet,
                        _format_decimal_plain(partial_before_usdc_balance),
                        _format_decimal_plain(partial_after_usdc_balance),
                        partial_in_symbol,
                    )
                    partial_added_volume = partial_trade_usd
                accumulated_volume += partial_added_volume
                logger.info(
                    "[VOLUME] wallet=%s partial_added_volume=%s | total_volume=%s/%s",
                    wallet,
                    _format_decimal_plain(partial_added_volume),
                    _format_decimal_plain(accumulated_volume),
                    _format_decimal_plain(wallet_target_volume),
                )
                if accumulated_volume < wallet_target_volume:
                    _sleep_between_swaps()

            if wallet_failed:
                _fail_wallet()
            elif accumulated_volume >= wallet_target_volume:
                final_usdc_balance = exec_client.get_erc20_balance(usdc_token.address, usdc_token.decimals)
                if final_usdc_balance >= MIN_EXECUTABLE_TRADE_USD:
                    logger.info(
                        "[VOLUME] wallet=%s final settle | USDC.E->ETH amount=100%% of USDC.E",
                        wallet,
                    )
                    final_before_usdc_balance = final_usdc_balance
                    ok_settle = _execute_trade_via_doma_ui_route(
                        cfg=cfg,
                        logger=logger,
                        state=state,
                        doma_api=doma_api,
                        exec_client=exec_client,
                        token_in=usdc_token,
                        token_out=weth_token,
                        display_in_symbol="USDC.E",
                        display_out_symbol="ETH",
                        trade_amount_expr="100%",
                        eth_price=eth_price,
                        label=f"VOLUME {wallet} USDC.E>ETH FINAL",
                        is_eth_source=False,
                        unwrap_to_native=True,
                        wait_for_pre_tx=True,
                    )
                    if not ok_settle or not state.last_tx_hash:
                        wallet_failed = True
                    elif not _wait_tx_receipt(exec_client, state.last_tx_hash, timeout_sec=180):
                        logger.warning("[VOLUME] wallet=%s final settle tx not confirmed before timeout | tx=%s", wallet, state.last_tx_hash)
                        wallet_failed = True
                    else:
                        final_after_usdc_balance = exec_client.get_erc20_balance(usdc_token.address, usdc_token.decimals)
                        final_added_volume = _volume_added_from_usdc_balance_change(
                            final_before_usdc_balance,
                            final_after_usdc_balance,
                            "USDC.E",
                        )
                        if final_added_volume <= 0:
                            logger.warning(
                                "[VOLUME] wallet=%s final settle volume fallback used | before_usdc=%s after_usdc=%s",
                                wallet,
                                _format_decimal_plain(final_before_usdc_balance),
                                _format_decimal_plain(final_after_usdc_balance),
                            )
                            final_added_volume = final_before_usdc_balance
                        accumulated_volume += final_added_volume
                        logger.info(
                            "[VOLUME] wallet=%s final_settle_added_volume=%s | total_volume=%s/%s",
                            wallet,
                            _format_decimal_plain(final_added_volume),
                            _format_decimal_plain(accumulated_volume),
                            _format_decimal_plain(wallet_target_volume),
                        )
                elif final_usdc_balance > 0:
                    logger.info(
                        "[VOLUME] wallet=%s final settle skipped | USDC.E dust below $0.10 (%s)",
                        wallet,
                        _format_decimal_plain(final_usdc_balance),
                    )

                if wallet_failed:
                    _fail_wallet()
                else:
                    _cleanup_weth_balance(
                        logger=logger,
                        exec_client=exec_client,
                        weth_token=weth_token,
                        label=f"VOLUME {wallet} FINAL",
                        reason="final cleanup",
                        wait_for_receipt=True,
                    )
                    success_wallets += 1
                    logger.info(
                        "[VOLUME] wallet=%s target reached | total_volume=%s USDC.E | final_asset=ETH",
                        wallet,
                        _format_decimal_plain(accumulated_volume),
                    )
            else:
                skipped_wallets += 1
        except Exception as exc:
            _fail_wallet()
            if _is_proxy_connectivity_error(exc):
                logger.warning("[VOLUME] wallet=%s proxy/RPC failed during run, skipping wallet: %s", wallet, exc)
            else:
                logger.warning("[VOLUME] wallet=%s runtime failed, skipping wallet: %s", wallet, exc)
        finally:
            if before_points_snapshot is not None:
                after_points_snapshot = _fetch_wallet_points_snapshot(cfg, wallet, proxies, logger, "VOLUME")
                _log_wallet_points_delta(logger, "VOLUME", wallet, before_points_snapshot, after_points_snapshot)

        if idx < len(wallet_key_records) - 1 and cfg.wallet_delay_max_sec > 0:
            delay_sec = random.uniform(cfg.wallet_delay_min_sec, cfg.wallet_delay_max_sec)
            logger.info("[VOLUME] delay before next wallet: %.2f sec", delay_sec)
            time.sleep(delay_sec)

    _print_mode_summary(
        "VOLUME",
        len(wallet_key_records),
        success_wallets,
        failed_wallets,
        skipped_wallets,
        failed_wallet_addresses,
    )


def get_doma_cost_report_menu_input(wallet_count: int) -> Optional[Tuple[Optional[datetime], int, int]]:
    print("\nDoma cost report:")
    lookback_raw = input("Lookback days [7, 0 = all available explorer history]: ").strip() or "7"
    lookback_days = int(_parse_decimal_input(lookback_raw))
    if lookback_days < 0:
        raise ValueError("Lookback days cannot be negative")
    since = None if lookback_days == 0 else datetime.now(timezone.utc) - timedelta(days=lookback_days)
    start_number = _prompt_start_wallet_number(wallet_count)
    end_number = _prompt_end_wallet_number(wallet_count, start_number)
    return since, start_number - 1, end_number


def _safe_decimal_from_raw(value: object, decimals: int) -> Decimal:
    try:
        return Decimal(int(str(value or "0"))) / (Decimal(10) ** decimals)
    except Exception:
        return Decimal("0")


def _cost_report_group_transfers(items: List[dict]) -> Dict[str, List[dict]]:
    grouped: Dict[str, List[dict]] = {}
    for item in items:
        tx_hash = str(item.get("transaction_hash") or "").strip().lower()
        if tx_hash:
            grouped.setdefault(tx_hash, []).append(item)
    return grouped


def _estimate_wallet_swap_loss_usd(
    wallet: str,
    txs: List[dict],
    token_transfers_by_tx: Dict[str, List[dict]],
    internal_by_tx: Dict[str, List[dict]],
    eth_price: Decimal,
) -> Tuple[Decimal, Decimal, Decimal, Decimal, int]:
    wallet_lc = wallet.lower()
    eth_usdc_loss = Decimal("0")
    domain_usdc_sent = Decimal("0")
    domain_usdc_received = Decimal("0")
    swap_volume_usd = Decimal("0")
    swap_tx_count = 0

    for tx in txs:
        if str(tx.get("status") or "").lower() not in {"ok", "success"}:
            continue
        tx_hash = str(tx.get("hash") or "").strip().lower()
        if not tx_hash:
            continue
        transfers = token_transfers_by_tx.get(tx_hash, [])
        internals = internal_by_tx.get(tx_hash, [])

        usdc_sent = Decimal("0")
        usdc_received = Decimal("0")
        other_wallet_token_transfer = False
        for item in transfers:
            from_hash = str(((item.get("from") or {}).get("hash")) or "").strip().lower()
            to_hash = str(((item.get("to") or {}).get("hash")) or "").strip().lower()
            if from_hash != wallet_lc and to_hash != wallet_lc:
                continue
            token_meta = item.get("token") or {}
            symbol = canonical_symbol(str(token_meta.get("symbol") or ""))
            decimals = int(token_meta.get("decimals") or (item.get("total") or {}).get("decimals") or 18)
            amount = _safe_decimal_from_raw((item.get("total") or {}).get("value"), decimals)
            if symbol == "USDC.E":
                if from_hash == wallet_lc:
                    usdc_sent += amount
                if to_hash == wallet_lc:
                    usdc_received += amount
            else:
                other_wallet_token_transfer = True

        native_sent = _safe_decimal_from_raw(tx.get("value"), 18)
        native_received = Decimal("0")
        for item in internals:
            if not bool(item.get("success", True)):
                continue
            to_hash = str(((item.get("to") or {}).get("hash")) or "").strip().lower()
            if to_hash == wallet_lc:
                native_received += _safe_decimal_from_raw(item.get("value"), 18)

        direct_eth_usdc = False
        if native_sent > 0 and usdc_received > 0:
            eth_usdc_loss += (native_sent * eth_price) - usdc_received
            swap_volume_usd += max(native_sent * eth_price, usdc_received)
            direct_eth_usdc = True
        if usdc_sent > 0 and native_received > 0:
            eth_usdc_loss += usdc_sent - (native_received * eth_price)
            swap_volume_usd += max(usdc_sent, native_received * eth_price)
            direct_eth_usdc = True

        if other_wallet_token_transfer and not direct_eth_usdc:
            domain_usdc_sent += usdc_sent
            domain_usdc_received += usdc_received
            swap_volume_usd += max(usdc_sent, usdc_received)
        elif not direct_eth_usdc and (usdc_sent > 0 or usdc_received > 0):
            swap_volume_usd += max(usdc_sent, usdc_received)

        if direct_eth_usdc or other_wallet_token_transfer:
            swap_tx_count += 1

    domain_net_loss = domain_usdc_sent - domain_usdc_received
    if eth_usdc_loss < 0:
        eth_usdc_loss = Decimal("0")
    if domain_net_loss < 0:
        domain_net_loss = Decimal("0")
    total_loss = eth_usdc_loss + domain_net_loss
    return total_loss, eth_usdc_loss, domain_net_loss, swap_volume_usd, swap_tx_count


def run_doma_cost_report_once(
    cfg: BotConfig,
    logger: logging.Logger,
    state: BotState,
    preset: Optional[Tuple[Optional[datetime], ...]] = None,
    report_label: str = "custom",
    until: Optional[datetime] = None,
    wallet_order: str = "random",
) -> None:
    wallets = [w for w in cfg.points_wallets if _is_valid_evm_address(w)]
    if not wallets:
        raise ValueError("No wallets available for Doma cost report")
    picked = preset or get_doma_cost_report_menu_input(len(wallets))
    if len(picked) == 2:
        since, start_offset = picked
        end_number = len(wallets)
    else:
        since, start_offset, end_number = picked
    quote_token = _usdce_token_from_config(cfg)

    ensure_csv(
        cfg.points_csv_file.parent / DOMA_COST_REPORT_CSV.name,
        [
            "timestamp_utc",
            "wallet",
            "line",
            "since",
            "tx_count",
            "gas_tx_count",
            "swap_tx_count",
            "gas_eth",
            "gas_usd",
            "swap_volume_usd_est",
            "swap_loss_usd_est",
            "eth_usdc_loss_usd_est",
            "domain_roundtrip_loss_usd_est",
            "total_cost_usd_est",
            "note",
        ],
        delimiter=cfg.csv_delimiter,
    )

    total_gas_eth = Decimal("0")
    total_gas_usd = Decimal("0")
    total_swap_loss = Decimal("0")
    total_cost = Decimal("0")

    logger.info(
        "[COST] mode started | period=%s | wallets=%s | start_wallet=%s | end_wallet=%s | since=%s | until=%s | slippage=estimated",
        report_label,
        len(wallets),
        start_offset + 1,
        end_number,
        since.isoformat() if since else "all",
        until.isoformat() if until else "now",
    )

    wallet_records = list(enumerate(wallets))[start_offset:end_number]
    if preset is None:
        wallet_order = _prompt_wallet_order(default_random=True)
    if wallet_order == "random":
        random.shuffle(wallet_records)
    for idx, wallet in wallet_records:
        proxies, skip_wallet = _proxy_for_line(cfg, idx, logger, "COST")
        if skip_wallet:
            continue
        logger.info("[COST] wallet %s", _wallet_progress_label(idx, len(wallets), wallet))
        try:
            api = DomaApiClient(
                cfg.doma_api_url,
                api_key=cfg.doma_api_key,
                api_keys=cfg.doma_api_keys,
                proxies=proxies,
            )
            eth_price = _fetch_eth_price_via_doma_quote(cfg, api, quote_token)
            txs = api.fetch_wallet_transactions_from_explorer(wallet, max_pages=100, since=since, until=until)
            token_transfers = api.fetch_wallet_token_transfers_from_explorer(wallet, max_pages=100, since=since, until=until)
            internals = api.fetch_wallet_internal_transactions_from_explorer(wallet, max_pages=100, since=since, until=until)
        except Exception as exc:
            logger.warning("[COST] wallet=%s fetch failed: %s", wallet, exc)
            continue

        wallet_lc = wallet.lower()
        gas_tx_count = 0
        gas_eth = Decimal("0")
        for tx in txs:
            from_hash = str(((tx.get("from") or {}).get("hash")) or "").strip().lower()
            if from_hash != wallet_lc:
                continue
            fee_obj = tx.get("fee") or {}
            fee_raw = fee_obj.get("value") if isinstance(fee_obj, dict) else tx.get("fee")
            fee_eth = _safe_decimal_from_raw(fee_raw, 18)
            if fee_eth <= 0:
                continue
            gas_tx_count += 1
            gas_eth += fee_eth

        gas_usd = gas_eth * eth_price
        token_transfers_by_tx = _cost_report_group_transfers(token_transfers)
        internal_by_tx = _cost_report_group_transfers(internals)
        swap_loss, eth_usdc_loss, domain_loss, swap_volume, swap_tx_count = _estimate_wallet_swap_loss_usd(
            wallet,
            txs,
            token_transfers_by_tx,
            internal_by_tx,
            eth_price,
        )
        wallet_total_cost = gas_usd + swap_loss

        total_gas_eth += gas_eth
        total_gas_usd += gas_usd
        total_swap_loss += swap_loss
        total_cost += wallet_total_cost

        logger.info(
            "[COST] period=%s wallet=%s tx=%s gas=%s ETH (~$%s) | swap_loss_est=$%s | total_est=$%s | swap_volume_est=$%s",
            report_label,
            wallet,
            len(txs),
            _format_decimal_plain(gas_eth),
            _format_decimal_plain(gas_usd),
            _format_decimal_plain(swap_loss),
            _format_decimal_plain(wallet_total_cost),
            _format_decimal_plain(swap_volume),
        )
        append_csv(
            cfg.points_csv_file.parent / DOMA_COST_REPORT_CSV.name,
            [
                datetime.now(timezone.utc).isoformat(),
                wallet,
                str(idx + 1),
                f"{report_label}:{since.isoformat() if since else 'all'}..{until.isoformat() if until else 'now'}",
                str(len(txs)),
                str(gas_tx_count),
                str(swap_tx_count),
                str(gas_eth),
                str(gas_usd),
                str(swap_volume),
                str(swap_loss),
                str(eth_usdc_loss),
                str(domain_loss),
                str(wallet_total_cost),
                "gas exact from explorer actual fee; swap loss estimated from wallet transfers and current ETH/USD",
            ],
            delimiter=cfg.csv_delimiter,
        )

    logger.info(
        "[COST] итог | period=%s | gas=%s ETH (~$%s) | swap_loss_est=$%s | total_cost_est=$%s | since=%s | until=%s",
        report_label,
        _format_decimal_plain(total_gas_eth),
        _format_decimal_plain(total_gas_usd),
        _format_decimal_plain(total_swap_loss),
        _format_decimal_plain(total_cost),
        since.isoformat() if since else "all",
        until.isoformat() if until else "now",
    )


def get_bridge_tasks_from_menu(state: BotState) -> Optional[List[str]]:
    _ = state
    route_groups = {
        "1": (
            "Base <-> Doma",
            [
                ("Base -> Doma | ETH -> ETH", "base", "doma", "ETH", "ETH"),
                ("Base -> Doma | ETH -> USDC.E", "base", "doma", "ETH", "USDC.E"),
                ("Doma -> Base | ETH -> ETH", "doma", "base", "ETH", "ETH"),
                ("Doma -> Base | USDC.E -> USDC", "doma", "base", "USDC.E", "USDC"),
            ],
        ),
        "2": (
            "Arbitrum <-> Doma",
            [
                ("Arbitrum -> Doma | ETH -> ETH", "arbitrum", "doma", "ETH", "ETH"),
                ("Arbitrum -> Doma | ETH -> USDC.E", "arbitrum", "doma", "ETH", "USDC.E"),
                ("Doma -> Arbitrum | ETH -> ETH", "doma", "arbitrum", "ETH", "ETH"),
                ("Doma -> Arbitrum | USDC.E -> USDC", "doma", "arbitrum", "USDC.E", "USDC"),
            ],
        ),
        "3": (
            "Optimism <-> Doma",
            [
                ("Optimism -> Doma | ETH -> ETH", "optimism", "doma", "ETH", "ETH"),
                ("Optimism -> Doma | ETH -> USDC.E", "optimism", "doma", "ETH", "USDC.E"),
                ("Doma -> Optimism | ETH -> ETH", "doma", "optimism", "ETH", "ETH"),
                ("Doma -> Optimism | USDC.E -> USDC", "doma", "optimism", "USDC.E", "USDC"),
            ],
        ),
    }
    print("\nBridge routes (Relay):")
    for key, (label, _routes) in route_groups.items():
        print(f"{key}) {label}")
    mantle_blast_choice = str(len(route_groups) + 1)
    back_choice = str(len(route_groups) + 2)
    print(f"{mantle_blast_choice}) Mantle + Blast -> Doma | all native ETH -> ETH")
    print(f"{back_choice}) Back")
    route = input(f"Select [1-{back_choice}]: ").strip()
    if route == back_choice:
        return None
    if route not in set(route_groups) | {mantle_blast_choice}:
        raise ValueError("Invalid bridge route selection")

    if route == mantle_blast_choice:
        return [
            "mantle>doma:ETH>ETH:100%",
            "blast>doma:ETH>ETH:100%",
        ]

    group_label, group_routes = route_groups[route]
    group_back_choice = str(len(group_routes) + 1)
    print(f"\n{group_label}:")
    for idx, (label, *_rest) in enumerate(group_routes, start=1):
        print(f"{idx}) {label}")
    print(f"{group_back_choice}) Back")
    route_raw = input(f"Select [1-{group_back_choice}]: ").strip()
    if route_raw == group_back_choice:
        return None
    try:
        route_idx = int(route_raw)
    except ValueError as exc:
        raise ValueError("Invalid bridge sub-route selection") from exc
    if not 1 <= route_idx <= len(group_routes):
        raise ValueError("Invalid bridge sub-route selection")

    _label, src_chain, dst_chain, src_symbol, dst_symbol = group_routes[route_idx - 1]
    print("\nAmount mode:")
    print(f"1) Number ({src_symbol})")
    print("2) Percent (%)")
    amount_mode_raw = input("Select [1-2]: ").strip()
    if amount_mode_raw not in {"1", "2"}:
        raise ValueError("Invalid amount mode selection")
    amount_mode = "number" if amount_mode_raw == "1" else "percent"

    min_raw = input("Minimum: ").strip()
    max_raw = input("Maximum: ").strip()
    _ = _parse_decimal_input(min_raw)
    _ = _parse_decimal_input(max_raw)
    expr_1 = (
        f"rand_token({min_raw}|{max_raw})"
        if amount_mode == "number"
        else f"rand_percent({min_raw}|{max_raw})"
    )

    return [f"{src_chain}>{dst_chain}:{src_symbol}>{dst_symbol}:{expr_1}"]


def run_swap_loop(cfg: BotConfig, logger: logging.Logger, state: BotState) -> None:
    stop = {"flag": False}

    def _handle_stop(_sig, _frame):
        stop["flag"] = True

    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    logger.info("Swap mode started | paper=%s dry_run=%s execution=%s chain_id=%s", cfg.paper_mode, cfg.dry_run, cfg.enable_execution, cfg.chain_id)
    logger.info("Emergency stop file: %s", cfg.stop_file.resolve())

    while not stop["flag"]:
        if should_stop(cfg):
            logger.warning("STOP file detected. Exiting safely.")
            break
        try:
            _, subgraph, _, exec_client = build_clients(cfg, state, create_exec=True)
            preflight_check(cfg, logger, subgraph, exec_client)
            pools = apply_filters(cfg, subgraph.fetch_top_pools(limit=150))
            eth_price = subgraph.fetch_eth_price_usd()
            if eth_price <= 0:
                raise RuntimeError("Failed to resolve ETH/USD")
            strategy = StrategyEngine(cfg)
            run_bootstrap_swaps(cfg, logger, state, pools, eth_price, exec_client)
            run_once(cfg, logger, state, pools, eth_price, exec_client, strategy)
        except Exception as exc:
            logger.exception("Swap iteration failed: %s", exc)
        save_state(cfg.state_file, state)
        time.sleep(cfg.loop_interval_sec)

    save_state(cfg.state_file, state)
    logger.info("Swap mode stopped.")


def run_bonding_daily_quest_once(cfg: BotConfig, logger: logging.Logger, state: BotState) -> None:
    print("\nДейлик: наторговать $25 bonding-токеном")
    target_raw = input("Целевой объем USDC.E [25]: ").strip() or "25"
    swap_delay_min_raw = input("Минимальная пауза между свапами, сек [4]: ").strip() or "4"
    swap_delay_max_raw = input("Максимальная пауза между свапами, сек [10]: ").strip() or "10"
    delay_min_raw = input(
        f"Минимальная пауза между кошельками, сек [{_format_decimal_plain(DOMAIN_LISTING_DEFAULT_DELAY_MIN_SEC)}]: "
    ).strip() or _format_decimal_plain(DOMAIN_LISTING_DEFAULT_DELAY_MIN_SEC)
    delay_max_raw = input(
        f"Максимальная пауза между кошельками, сек [{_format_decimal_plain(DOMAIN_LISTING_DEFAULT_DELAY_MAX_SEC)}]: "
    ).strip() or _format_decimal_plain(DOMAIN_LISTING_DEFAULT_DELAY_MAX_SEC)

    target = _parse_decimal_input(target_raw)
    swap_delay_min = _parse_decimal_input(swap_delay_min_raw)
    swap_delay_max = _parse_decimal_input(swap_delay_max_raw)
    delay_min = _parse_decimal_input(delay_min_raw)
    delay_max = _parse_decimal_input(delay_max_raw)
    if target <= 0:
        raise ValueError("Целевой объем должен быть больше нуля")
    if swap_delay_min < 0 or swap_delay_max < swap_delay_min:
        raise ValueError("Некорректный диапазон пауз между свапами")
    if delay_min < 0 or delay_max < delay_min:
        raise ValueError("Некорректный диапазон пауз между кошельками")

    print(
        f"Софт начнет торговлю только с балансом от {_format_decimal_plain(BONDING_DAILY_INITIAL_MIN_USDCE)} USDC.E: "
        "если USDC.E меньше, доступный ETH будет обменян с сохранением минимум "
        f"${_format_decimal_plain(BONDING_DAILY_GAS_RESERVE_USD)} в ETH на газ. После первого свапа "
        "уменьшившаяся из-за проскальзывания сумма продолжит участвовать в обороте, "
        f"пока фактический объем не достигнет {_format_decimal_plain(target)} USDC.E."
    )
    run_bonding_token_buy_once(
        cfg,
        logger,
        state,
        preset=(
            "daily_quest",
            "",
            swap_delay_min_raw,
            target_raw,
            "buy_sell",
            "daily_all_usdc",
            "0",
            "0",
            delay_min_raw,
            delay_max_raw,
            "0",
            "0",
            "",
            swap_delay_max_raw,
        ),
    )


def get_menu_choice() -> str:
    print("\nВыберите действие:")
    print("1) Перевести токены между сетями (Bridge)")
    print("2) Проверить поинты / задания / балансы")
    print("3) Закрыть все позиции ликвидности")
    print("4) Свапнуть токен домена")
    print("5) Свапнуть ETH / WETH / USDC.E")
    print("6) Собрать все токены в ETH / USDC.E")
    print("7) Набить объем в паре ETH <-> USDC.E")
    print("8) Набить объем для задания токена домена")
    print("9) Выставить домены на продажу")
    print("10) Отменить активные листинги доменов")
    print("11) Купить дешевые токены и создать субдомены")
    print("12) Выполнить bridge домена Doma -> Base")
    print("13) Разместить офферы на домены")
    print("14) Принять полученные офферы на домены")
    print("15) Добавить full-range ликвидность")
    print("16) Набить недельный объем ETH / USDC.E")
    print("17) Дейлик - свапнуть токены доменов на $1+")
    print("18) Закрыть стейкинг субдоменов")
    print("19) Вывести средства с OKX на кошельки")
    print("20) Отправить средства на биржу из L2 сетей")
    print("21) Купить самые дешевые домены")
    print("22) Забрать ежедневные 100 поинтов")
    print("23) Получить Privy-токены для ежедневных 100 поинтов")
    print("24) Купить токены на стадии бондинга")
    print("25) Galxe - клейм задания D3")
    print("26) Дейлик - наторговать $25 bonding-токеном")
    print("27) Выйти")
    return input("Выберите [1-27]: ").strip()


def get_points_menu_choice() -> str:
    print("\nПроверка аккаунтов:")
    print("1) Проверить поинты и задания")
    print("2) Проверить только балансы")
    print("3) Назад")
    return input("Выберите [1-3]: ").strip()


def get_doma_quest_menu_choice() -> str:
    print("\nDoma quests:")
    print("1) Daily: $5+ swap on any domain token")
    print("2) Weekly: list any domain on marketplace")
    print("3) Weekly: trade $100 total volume")
    print("4) Weekly: trade $250 total volume")
    print("5) Season: add $10 liquidity to a domain token")
    print("6) Season: add $50 liquidity to a domain token")
    print("7) Season: bridge a domain from Doma to Base")
    print("8) Season: mint 5 domain NFTs")
    print("9) Season: stake 3 subdomains")
    print("10) Back")
    return input("Select [1-10]: ").strip()


def _run_not_implemented_quest(logger: logging.Logger, quest_name: str, reason: str) -> None:
    print("")
    print(f"{quest_name}:")
    print("Status: not automated yet.")
    print(f"Reason: {reason}")
    logger.warning("[DOMA_QUEST] %s not automated: %s", quest_name, reason)


def run_doma_quests_menu_once(cfg: BotConfig, logger: logging.Logger, state: BotState) -> None:
    while True:
        choice = get_doma_quest_menu_choice()
        if choice == "1":
            validate_config(cfg)
            domain_name = get_domain_quest_token_choice()
            if not domain_name:
                continue
            print(f"\nDaily swap quest selected: {domain_name} target=$5")
            run_domain_quest_volume_once(cfg, logger, state, preset=(domain_name, "95", "99", "5", "ETH"))
            save_state(cfg.state_file, state)
            return
        if choice == "2":
            validate_config(cfg)
            run_domain_listing_once(cfg, logger, state)
            save_state(cfg.state_file, state)
            return
        if choice == "3":
            validate_config(cfg)
            print("\nWeekly volume quest selected: target=$100 ETH <-> USDC.E")
            run_volume_farm_once(cfg, logger, state, preset=("80", "90", "100"))
            save_state(cfg.state_file, state)
            return
        if choice == "4":
            validate_config(cfg)
            print("\nWeekly volume quest selected: target=$250 ETH <-> USDC.E")
            run_volume_farm_once(cfg, logger, state, preset=("80", "90", "250"))
            save_state(cfg.state_file, state)
            return
        if choice == "5":
            _run_not_implemented_quest(
                logger,
                "Add at least $10 in liquidity to a domain token",
                "the codebase has only close/decrease/collect position logic; it does not have Uniswap V3 mint/increase-liquidity yet.",
            )
            continue
        if choice == "6":
            _run_not_implemented_quest(
                logger,
                "Add at least $50 in liquidity to a domain token",
                "the codebase has only close/decrease/collect position logic; it does not have Uniswap V3 mint/increase-liquidity yet.",
            )
            continue
        if choice == "7":
            validate_config(cfg)
            run_domain_bridge_to_base_once(cfg, logger, state)
            save_state(cfg.state_file, state)
            return
        if choice == "8":
            _run_not_implemented_quest(
                logger,
                "Mint 5 domain NFTs",
                "registrar/checkout flow for domain NFT minting is not implemented.",
            )
            continue
        if choice == "9":
            _run_not_implemented_quest(
                logger,
                "Stake 3 subdomains",
                "subdomain staking contract/API is not implemented.",
            )
            continue
        if choice == "10":
            return
        raise ValueError("Invalid Doma quest selection")


def run_points_menu_once(cfg: BotConfig, logger: logging.Logger, state: BotState) -> None:
    while True:
        choice = get_points_menu_choice()
        if choice == "1":
            run_points_once(cfg, logger, state)
            return
        if choice == "2":
            run_balances_once(cfg, logger, state)
            return
        if choice == "3":
            return
        raise ValueError("Invalid points submenu selection")


def main() -> None:
    cfg = BotConfig()
    logger = setup_logger(cfg.log_file)
    install_wallet_log_names(logger, cfg.points_wallets)
    ensure_csv(
        cfg.trades_csv_file,
        [
            "timestamp_utc",
            "status",
            "label",
            "pool",
            "fee_tier",
            "token_in",
            "token_out",
            "amount_in",
            "amount_out_quote",
            "trade_usd",
            "expected_out_usd",
            "pnl_estimate_usd",
            "tx_hash",
            "reason",
        ],
        delimiter=cfg.csv_delimiter,
    )
    ensure_csv(
        cfg.points_csv_file,
        [
            "timestamp_utc",
            "wallet",
            "rank",
            "weekly_points",
            "season_points",
            "points_multiplier",
            "referral_count",
            "total_snapshot_entries",
            "campaign_meta",
        ],
        delimiter=cfg.csv_delimiter,
    )
    ensure_csv(
        cfg.points_csv_file.parent / DOMAIN_QUESTS_CSV.name,
        [
            "timestamp_utc",
            "wallet",
            "line",
            "reset_period",
            "quest_type",
            "description",
            "points",
            "completed",
            "completed_at",
            "available_at",
        ],
        delimiter=cfg.csv_delimiter,
    )
    ensure_csv(
        cfg.points_csv_file.parent / OKX_WITHDRAWALS_CSV.name,
        [
            "timestamp_utc",
            "status",
            "address",
            "line",
            "ccy",
            "chain",
            "amount",
            "fee",
            "withdraw_id",
            "client_id",
            "reason",
        ],
        delimiter=cfg.csv_delimiter,
    )
    ensure_csv(
        cfg.points_csv_file.parent / EXCHANGE_DEPOSITS_CSV.name,
        [
            "timestamp_utc",
            "status",
            "wallet_line",
            "deposit_address",
            "chain",
            "symbol",
            "amount",
            "tx_hash",
            "reason",
        ],
        delimiter=cfg.csv_delimiter,
    )
    ensure_csv(
        cfg.points_csv_file.parent / DOMAIN_PURCHASES_CSV.name,
        [
            "timestamp_utc",
            "status",
            "wallet",
            "domain",
            "price_usdce",
            "network_id",
            "token_address",
            "token_id",
            "order_id",
            "tx_hash",
            "relist_price_usdce",
            "relist_order_id",
            "relist_reason",
            "reason",
        ],
        delimiter=cfg.csv_delimiter,
    )
    ensure_csv(
        cfg.points_csv_file.parent / DOMA_DAILY_ROLLCALL_CSV.name,
        [
            "timestamp_utc",
            "status",
            "wallet",
            "line",
            "caip_wallet",
            "points_awarded",
            "check_in_date",
            "reason",
        ],
        delimiter=cfg.csv_delimiter,
    )
    ensure_csv(
        cfg.points_csv_file.parent / DOMAIN_BONDING_BUYS_CSV.name,
        [
            "timestamp_utc",
            "status",
            "wallet",
            "line",
            "token_name",
            "token_address",
            "launchpad_address",
            "token_status",
            "amount_usdce",
            "token_price_usd",
            "tx_hash",
            "reason",
        ],
        delimiter=cfg.csv_delimiter,
    )
    ensure_csv(
        cfg.points_csv_file.parent / GALXE_CLAIMS_CSV.name,
        [
            "timestamp_utc",
            "status",
            "wallet",
            "line",
            "campaign_id",
            "campaign_name",
            "tx_hash",
            "verify_ids",
            "nonce",
            "signature",
            "reason",
        ],
        delimiter=cfg.csv_delimiter,
    )
    state = load_state(cfg.state_file)

    if cfg.paper_replay_mode:
        run_replay_report(cfg, logger)
        return

    while True:
        choice = "1"
        if sys.stdin.isatty():
            choice = get_menu_choice()

        if choice == "1":
            validate_config(cfg)
            try:
                selected_tasks = get_bridge_tasks_from_menu(state)
                if not selected_tasks:
                    logger.info("Bridge mode canceled by user.")
                    return
                original_tasks = cfg.bridge_tasks
                cfg.bridge_tasks = selected_tasks
                try:
                    run_bridge_once(cfg, logger, state)
                finally:
                    cfg.bridge_tasks = original_tasks
                save_state(cfg.state_file, state)
            except Exception as exc:
                logger.exception("Bridge mode failed: %s", exc)
            return
        if choice == "2":
            run_points_menu_once(cfg, logger, state)
            save_state(cfg.state_file, state)
            return
        if choice == "3":
            try:
                run_close_position_once(cfg, logger, state)
                save_state(cfg.state_file, state)
            except Exception as exc:
                logger.exception("Close position failed: %s", exc)
            return
        if choice == "4":
            validate_config(cfg)
            try:
                run_domain_swap_once(cfg, logger, state)
                save_state(cfg.state_file, state)
            except Exception as exc:
                logger.exception("Domain swap failed: %s", exc)
            return
        if choice == "5":
            validate_config(cfg)
            try:
                run_pair_swap_once(cfg, logger, state)
                save_state(cfg.state_file, state)
            except Exception as exc:
                logger.exception("Pair swap failed: %s", exc)
            return
        if choice == "6":
            validate_config(cfg)
            try:
                run_sweep_tokens_to_usdce_once(cfg, logger, state)
                save_state(cfg.state_file, state)
            except Exception as exc:
                logger.exception("Sweep collect mode failed: %s", exc)
            return
        if choice == "7":
            validate_config(cfg)
            try:
                run_volume_farm_once(cfg, logger, state)
                save_state(cfg.state_file, state)
            except Exception as exc:
                logger.exception("Volume farm failed: %s", exc)
            return
        if choice == "8":
            validate_config(cfg)
            try:
                run_domain_quest_volume_once(cfg, logger, state)
                save_state(cfg.state_file, state)
            except Exception as exc:
                logger.exception("Domain quest volume failed: %s", exc)
            return
        if choice == "9":
            validate_config(cfg)
            try:
                run_domain_listing_once(cfg, logger, state)
                save_state(cfg.state_file, state)
            except Exception as exc:
                logger.exception("Domain listing failed: %s", exc)
                if sys.stdin.isatty():
                    input("\nPress Enter to return to menu...")
            return
        if choice == "10":
            validate_config(cfg)
            try:
                run_domain_delisting_once(cfg, logger, state)
                save_state(cfg.state_file, state)
            except Exception as exc:
                logger.exception("Domain delisting failed: %s", exc)
                if sys.stdin.isatty():
                    input("\nPress Enter to return to menu...")
            return
        if choice == "11":
            validate_config(cfg)
            try:
                run_cheap_token_buy_once(cfg, logger, state)
                save_state(cfg.state_file, state)
            except Exception as exc:
                logger.exception("Cheap token buy failed: %s", exc)
                if sys.stdin.isatty():
                    input("\nPress Enter to return to menu...")
            return
        if choice == "12":
            validate_config(cfg)
            try:
                run_domain_bridge_to_base_once(cfg, logger, state)
                save_state(cfg.state_file, state)
            except Exception as exc:
                logger.exception("Domain bridge to Base failed: %s", exc)
                if sys.stdin.isatty():
                    input("\nPress Enter to return to menu...")
            return
        if choice == "13":
            validate_config(cfg)
            try:
                run_domain_place_offer_once(cfg, logger, state)
                save_state(cfg.state_file, state)
            except Exception as exc:
                logger.exception("Domain offer mode failed: %s", exc)
                if sys.stdin.isatty():
                    input("\nPress Enter to return to menu...")
            return
        if choice == "14":
            validate_config(cfg)
            try:
                run_domain_accept_offer_once(cfg, logger, state)
                save_state(cfg.state_file, state)
            except Exception as exc:
                logger.exception("Domain accept-offer mode failed: %s", exc)
                if sys.stdin.isatty():
                    input("\nPress Enter to return to menu...")
            return
        if choice == "15":
            validate_config(cfg)
            try:
                run_domain_liquidity_once(cfg, logger, state)
                save_state(cfg.state_file, state)
            except Exception as exc:
                logger.exception("Domain liquidity mode failed: %s", exc)
                if sys.stdin.isatty():
                    input("\nPress Enter to return to menu...")
            return
        if choice == "16":
            validate_config(cfg)
            try:
                run_volume_farm_once(cfg, logger, state, weekly_remaining=True)
                save_state(cfg.state_file, state)
            except Exception as exc:
                logger.exception("Weekly ETH/USDC.E volume failed: %s", exc)
                if sys.stdin.isatty():
                    input("\nPress Enter to return to menu...")
            return
        if choice == "17":
            validate_config(cfg)
            try:
                run_com_daily_swap_once(cfg, logger, state)
                save_state(cfg.state_file, state)
            except Exception as exc:
                logger.exception("Daily .com top TVL swap mode failed: %s", exc)
                if sys.stdin.isatty():
                    input("\nPress Enter to return to menu...")
            return
        if choice == "18":
            validate_config(cfg)
            try:
                run_close_subdomains_once(cfg, logger, state)
                save_state(cfg.state_file, state)
            except Exception as exc:
                logger.exception("Subdomain close mode failed: %s", exc)
                if sys.stdin.isatty():
                    input("\nPress Enter to return to menu...")
            return
        if choice == "19":
            try:
                run_okx_withdrawals_once(cfg, logger, state)
            except Exception as exc:
                logger.exception("OKX withdrawal mode failed: %s", exc)
                if sys.stdin.isatty():
                    input("\nPress Enter to return to menu...")
            return
        if choice == "20":
            try:
                run_exchange_deposit_once(cfg, logger, state)
            except Exception as exc:
                logger.exception("Exchange deposit mode failed: %s", exc)
                if sys.stdin.isatty():
                    input("\nPress Enter to return to menu...")
            return
        if choice == "21":
            validate_config(cfg)
            try:
                run_domain_purchase_once(cfg, logger, state)
                save_state(cfg.state_file, state)
            except Exception as exc:
                logger.exception("Domain purchase mode failed: %s", exc)
                if sys.stdin.isatty():
                    input("\nPress Enter to return to menu...")
            return
        if choice == "22":
            try:
                run_daily_rollcall_once(cfg, logger, state)
                save_state(cfg.state_file, state)
            except Exception as exc:
                logger.exception("Daily rollcall mode failed: %s", exc)
                if sys.stdin.isatty():
                    input("\nPress Enter to return to menu...")
            return
        if choice == "23":
            try:
                run_rollcall_token_generation_once(cfg, logger, state)
                save_state(cfg.state_file, state)
            except Exception as exc:
                logger.exception("Rollcall token generation failed: %s", exc)
                if sys.stdin.isatty():
                    input("\nPress Enter to return to menu...")
            return
        if choice == "24":
            validate_config(cfg)
            try:
                run_bonding_token_buy_once(cfg, logger, state)
                save_state(cfg.state_file, state)
            except Exception as exc:
                logger.exception("Bonding token buy mode failed: %s", exc)
                if sys.stdin.isatty():
                    input("\nPress Enter to return to menu...")
            return
        if choice == "25":
            validate_config(cfg)
            try:
                run_galxe_quest_claim_once(cfg, logger, state)
                save_state(cfg.state_file, state)
            except Exception as exc:
                logger.exception("Galxe quest claim mode failed: %s", exc)
                if sys.stdin.isatty():
                    input("\nPress Enter to return to menu...")
            return
        if choice == "26":
            validate_config(cfg)
            try:
                run_bonding_daily_quest_once(cfg, logger, state)
                save_state(cfg.state_file, state)
            except Exception as exc:
                logger.exception("Bonding daily quest mode failed: %s", exc)
                if sys.stdin.isatty():
                    input("\nPress Enter to return to menu...")
            return
        if choice == "27":
            logger.info("Exit selected.")
            return
        logger.info("Exit selected.")
        return


if __name__ == "__main__":
    main()
