from __future__ import annotations

import csv
import json
import logging
import os
import random
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from web3 import Web3

from config import BotConfig
from doma_api import (
    DomaApiClient,
    DomaSubgraphClient,
    DOMA_INTERFACE_PORTION_BIPS,
    DOMA_INTERFACE_PORTION_RECIPIENT,
    DOMA_NATIVE_TOKEN_SENTINEL,
    EvmExecutionClient,
    LaunchpadTokenInfo,
    OwnedDomain,
    PointsSnapshot,
    Pool,
    Token,
    decimal_to_raw,
    pick_token_usd_price,
    raw_to_decimal,
)
from relay_bridge import NATIVE_ETH, execute_relay_swap, run_bridge_tasks
from strategy import StrategyEngine
from position_manager import PositionManagerClient


@dataclass
class BotState:
    day_utc: str
    daily_volume_usd: Decimal
    last_tx_hash: str = ""
    bootstrap_completed: List[str] = field(default_factory=list)
    last_points_check_ts: int = 0
    last_bridge_ts: int = 0
    used_bridge_amounts: List[str] = field(default_factory=list)

    @classmethod
    def create_default(cls) -> "BotState":
        return cls(day_utc=datetime.now(timezone.utc).strftime("%Y-%m-%d"), daily_volume_usd=Decimal("0"))


def setup_logger(log_path: Path) -> logging.Logger:
    logger = logging.getLogger("doma_swap_bot")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


ANSI_RESET = "\033[0m"
ANSI_GREEN = "\033[92m"
ANSI_RED = "\033[91m"
ANSI_YELLOW = "\033[93m"
ANSI_CYAN = "\033[96m"
MIN_EXECUTABLE_TRADE_USD = Decimal("0.10")
DOMAIN_QUEST_VOLUME_LOOKBACK_DAYS = 7
DOMAIN_QUEST_COMPLETION_THRESHOLD_USD = Decimal("25")
DOMAIN_QUEST_TOKENS = [
    "rides.com",
    "software.ai",
    "alert.ai",
    "swimsuits.ai",
    "trenches.ai",
    "depin.ai",
    "terabytes.ai",
    "mishka.ai",
    "playonline.ai",
    "exemption.ai",
    "bipod.ai",
    "itprojects.ai",
    "lifeadvice.ai",
    "onlineadvisor.ai",
    "continents.ai",
    "loancrypto.ai",
    "coinlogic.ai",
    "agenticconsultant.ai",
    "gobitcoin.xyz",
    "closingbells.com",
    "get.cash",
]
DOMAIN_LISTING_CSV = Path("domain_listings.csv")
DOMAIN_LISTING_DEFAULT_DURATION_DAYS = 90
DOMAIN_LISTING_DEFAULT_DELAY_MIN_SEC = Decimal("4")
DOMAIN_LISTING_DEFAULT_DELAY_MAX_SEC = Decimal("10")
DOMAIN_LISTING_SOURCE = "doma-swap-bot-public"


def _color(text: str, code: str) -> str:
    return f"{code}{text}{ANSI_RESET}"


def _wallet_progress_label(idx: int, total: int, wallet: str) -> str:
    return f"{idx + 1}/{total} - {wallet}"


def _is_proxy_connectivity_error(exc: Exception) -> bool:
    message = str(exc).lower()
    markers = [
        "proxyerror",
        "cannot connect to proxy",
        "failed to establish a new connection",
        "max retries exceeded",
        "winerror 10065",
    ]
    return any(marker in message for marker in markers)


def _random_swap_delay_sec() -> float:
    return random.uniform(4, 10)


def _doma_rpc_candidates(cfg: BotConfig) -> List[str]:
    candidates: List[str] = []
    for rpc_url in [cfg.rpc_url, "https://rpc.doma.xyz/", "https://doma.drpc.org/"]:
        normalized = (rpc_url or "").strip()
        if normalized and normalized not in candidates:
            candidates.append(normalized)
    return candidates


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
        print(_color(f"[{mode}] wallets with errors: {', '.join(failed_wallets)}", ANSI_RED))


def ensure_csv(path: Path, header: List[str], delimiter: str = ",") -> None:
    if path.exists():
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=delimiter)
        w.writerow(header)


def append_csv(path: Path, row: List[object], delimiter: str = ",") -> None:
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


def _apply_wallet_start_selection(records: List[Tuple[int, str, str]]) -> Tuple[List[Tuple[int, str, str]], int, int]:
    total_wallets = len(records)
    start_number = _prompt_start_wallet_number(total_wallets)
    start_offset = start_number - 1
    return records[start_offset:], start_offset, total_wallets


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
        wallet_balance_in = exec_client.get_native_balance() + exec_client.get_erc20_balance(token_in.address, token_in.decimals)
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
                delay_sec = _random_swap_delay_sec()
                logger.info("[%s] delay after wrap: %.2f sec", label, delay_sec)
                time.sleep(delay_sec)

    approve_hash = exec_client.ensure_allowance(token_in.address, amount_in_raw)
    if approve_hash:
        logger.info("[%s] Approve tx sent: %s", label, approve_hash)
        if wait_for_pre_tx:
            ok = _wait_tx_receipt(exec_client, approve_hash, timeout_sec=180)
            if not ok:
                raise RuntimeError("Approve tx failed or timed out")
            delay_sec = _random_swap_delay_sec()
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
) -> bool:
    if is_eth_source:
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
        exec_client.get_native_balance()
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

    try:
        slippage_pct = Decimal("2.5")
        quote = doma_api.fetch_universal_router_quote(
            token_in_address=quote_token_in_address,
            token_out_address=quote_token_out_address,
            amount_raw=amount_in_raw,
            chain_id=cfg.chain_id,
            trade_type="exactIn",
            slippage_tolerance_pct=slippage_pct,
            portion_bips=DOMA_INTERFACE_PORTION_BIPS,
            portion_recipient=DOMA_INTERFACE_PORTION_RECIPIENT,
        )
    except Exception as exc:
        logger.warning("[%s] Doma UI quote failed: %s", label, exc)
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

    if not is_eth_source:
        token_approve_hash = exec_client.ensure_allowance(
            token_in.address,
            amount_in_raw,
            spender_address=exec_client.permit2_address,
            approve_max=True,
        )
        if token_approve_hash:
            logger.info("[%s] Approve token->Permit2 tx sent: %s", label, token_approve_hash)
            if wait_for_pre_tx:
                ok = _wait_tx_receipt(exec_client, token_approve_hash, timeout_sec=180)
                if not ok:
                    raise RuntimeError("Approve token->Permit2 tx failed or timed out")
                delay_sec = _random_swap_delay_sec()
                logger.info("[%s] delay after approve: %.2f sec", label, delay_sec)
                time.sleep(delay_sec)

        permit2_approve_hash = exec_client.ensure_permit2_allowance(token_in.address, quote.to, amount_in_raw)
        if permit2_approve_hash:
            logger.info("[%s] Approve Permit2->UniversalRouter tx sent: %s", label, permit2_approve_hash)
            if wait_for_pre_tx:
                ok = _wait_tx_receipt(exec_client, permit2_approve_hash, timeout_sec=180)
                if not ok:
                    raise RuntimeError("Approve Permit2->UniversalRouter tx failed or timed out")
                delay_sec = _random_swap_delay_sec()
                logger.info("[%s] delay after approve: %.2f sec", label, delay_sec)
                time.sleep(delay_sec)

    try:
        tx_hash = exec_client.execute_prebuilt_transaction(
            to_address=quote.to,
            calldata=quote.calldata,
            value_raw=quote.value_raw,
        )
    except Exception as exc:
        message = str(exc)
        if "STF" in message or "AllowanceExpired" in message or "d81b2f2e" in message:
            retry_delay = _random_swap_delay_sec()
            logger.warning("[%s] Swap reverted with transferable allowance error, retrying after %.2f sec", label, retry_delay)
            time.sleep(retry_delay)
            if not is_eth_source:
                exec_client.ensure_allowance(
                    token_in.address,
                    amount_in_raw,
                    spender_address=exec_client.permit2_address,
                    approve_max=True,
                )
                exec_client.ensure_permit2_allowance(token_in.address, quote.to, amount_in_raw)
            tx_hash = exec_client.execute_prebuilt_transaction(
                to_address=quote.to,
                calldata=quote.calldata,
                value_raw=quote.value_raw,
            )
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
                delay_sec = _random_swap_delay_sec()
                logger.info("[%s] delay after wrap: %.2f sec", label, delay_sec)
                time.sleep(delay_sec)

    approve_hash = exec_client.ensure_allowance(token_in.address, amount_in_raw)
    if approve_hash:
        logger.info("[%s] Approve tx sent: %s", label, approve_hash)
        if wait_for_pre_tx:
            ok = _wait_tx_receipt(exec_client, approve_hash, timeout_sec=180)
            if not ok:
                raise RuntimeError("Approve tx failed or timed out")
            delay_sec = _random_swap_delay_sec()
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
        logger.info("Points: rank=%s points=%s volume=%s", snapshot.rank, snapshot.points, snapshot.trading_volume_usd)
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


def run_points_once(cfg: BotConfig, logger: logging.Logger, state: BotState) -> None:
    if not cfg.file_api_keys and not cfg.doma_api_key.strip():
        logger.warning("Points check skipped: DOMA_API_KEY/API_KEYS_FILE is empty")
        return
    wallets = cfg.points_wallets or ([cfg.account_address] if cfg.account_address else [])
    if not wallets:
        logger.warning("Points check skipped: no wallets configured")
        return
    for idx, wallet in enumerate(wallets):
        api_key = cfg.file_api_keys[idx].strip() if idx < len(cfg.file_api_keys) else ""
        if not api_key and cfg.file_api_keys:
            logger.warning("Points check skipped for line %s: no API key on same line", idx + 1)
            continue
        if not api_key and cfg.doma_api_key.strip():
            api_key = cfg.doma_api_key.strip()

        proxy = cfg.file_proxies[idx].strip() if idx < len(cfg.file_proxies) else ""
        if cfg.file_proxies and idx >= len(cfg.file_proxies):
            logger.warning("Points check skipped for line %s: no proxy on same line", idx + 1)
            continue

        proxies = {"http": proxy, "https": proxy} if proxy else None
        points_api = DomaApiClient(
            cfg.doma_api_url,
            api_key=api_key,
            api_keys=[api_key] if api_key else [],
            proxies=proxies,
        )
        try:
            snapshot: Optional[PointsSnapshot] = points_api.fetch_points(wallet, cfg.leaderboard_rank_by)
            if not snapshot:
                logger.info("Points: no leaderboard row for wallet %s", wallet)
                continue
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
                "Points [%s] [line=%s]: rank=%s points=%s volume=%s",
                snapshot.wallet_address,
                idx + 1,
                snapshot.rank,
                snapshot.points,
                snapshot.trading_volume_usd,
            )
        except Exception as exc:
            logger.warning("Points check failed for %s [line=%s]: %s", wallet, idx + 1, exc)
        if idx < len(wallets) - 1 and cfg.wallet_delay_max_sec > 0:
            delay_sec = random.uniform(cfg.wallet_delay_min_sec, cfg.wallet_delay_max_sec)
            logger.info("Delay before next wallet: %.2f sec", delay_sec)
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


def run_bridge_once(cfg: BotConfig, logger: logging.Logger, state: BotState) -> None:
    bridge_tasks = cfg.bridge_tasks
    success_wallets = 0
    failed_wallets = 0
    skipped_wallets = 0
    failed_wallet_addresses: List[str] = []

    def _fail_wallet() -> None:
        nonlocal failed_wallets
        failed_wallets += 1
        if wallet not in failed_wallet_addresses:
            failed_wallet_addresses.append(wallet)

    need_eth_price = False
    for raw in bridge_tasks:
        try:
            _left, _pair, amount_expr = [x.strip() for x in raw.split(":", 2)]
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
        subgraph = DomaSubgraphClient(cfg.subgraph_url, proxies=active_proxies)
        return subgraph.fetch_eth_price_usd()

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
        logger.info("[BRIDGE] wallet %s", _wallet_progress_label(wallet_start_offset + idx, total_loaded_wallets, wallet))
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

    def _fail_wallet() -> None:
        nonlocal failed_wallets
        failed_wallets += 1
        if wallet not in failed_wallet_addresses:
            failed_wallet_addresses.append(wallet)
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
        logger.info("[POSITION] wallet %s", _wallet_progress_label(wallet_start_offset + idx, total_loaded_wallets, wallet))
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
            active_positions = client.list_owner_positions(owner=wallet, limit=200, only_active=True)
        except Exception as exc:
            logger.warning("[POSITION] wallet %s: failed to read positions: %s", wallet, exc)
            _fail_wallet()
            continue
        if not active_positions:
            logger.info("[POSITION] wallet %s: no active positions", wallet)
        else:
            logger.info("[POSITION] wallet %s: active positions=%s", wallet, len(active_positions))

        for p_idx, p in enumerate(active_positions):
            token_id = int(p.token_id)
            liq_to_remove = int(p.liquidity)
            if liq_to_remove <= 0:
                continue
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
                tx1 = client.decrease_liquidity(
                    token_id=token_id,
                    liquidity_to_remove=liq_to_remove,
                    deadline_sec=600,
                )
                logger.info("[POSITION] decreaseLiquidity wallet=%s tokenId=%s tx=%s", wallet, token_id, tx1)
                tx2 = client.collect_all(token_id=token_id, recipient=wallet)
                logger.info("[POSITION] collect wallet=%s tokenId=%s tx=%s", wallet, token_id, tx2)
                try:
                    tx3 = client.burn(token_id=token_id)
                    logger.info("[POSITION] burn wallet=%s tokenId=%s tx=%s", wallet, token_id, tx3)
                except Exception as exc:
                    logger.warning("[POSITION] burn skipped/failed wallet=%s tokenId=%s: %s", wallet, token_id, exc)
            except Exception as exc:
                logger.warning("[POSITION] close failed wallet=%s tokenId=%s: %s", wallet, token_id, exc)
                wallet_failed = True

            if p_idx < len(active_positions) - 1:
                pos_delay_sec = random.uniform(10, 20)
                logger.info("[POSITION] delay before next position: %.2f sec", pos_delay_sec)
                time.sleep(pos_delay_sec)

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
    s = (raw or "").strip()
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
    return s.strip().upper()


def _wait_tx_receipt(exec_client: EvmExecutionClient, tx_hash: str, timeout_sec: int = 180) -> bool:
    try:
        receipt = exec_client.web3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout_sec, poll_latency=2)
        status = int(getattr(receipt, "status", 0))
        return status == 1
    except Exception:
        return False


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


def _fetch_eth_price_via_doma_quote(cfg: BotConfig, doma_api: DomaApiClient, quote_token: Token) -> Decimal:
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
) -> bool:
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
            delay_sec = _random_swap_delay_sec()
            logger.info("[%s] delay after approve: %.2f sec", label, delay_sec)
            time.sleep(delay_sec)
    try:
        tx_hash = exec_client.execute_launchpad_buy(
            launchpad_address=launchpad.launchpad_address,
            amount_in_raw=amount_in_raw,
            min_amount_out_raw=min_out_raw,
        )
    except Exception as exc:
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
) -> bool:
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
            delay_sec = _random_swap_delay_sec()
            logger.info("[%s] delay after approve: %.2f sec", label, delay_sec)
            time.sleep(delay_sec)
    try:
        tx_hash = exec_client.execute_launchpad_sell(
            launchpad_address=launchpad.launchpad_address,
            amount_in_raw=amount_in_raw,
            min_amount_out_raw=min_out_raw,
        )
    except Exception as exc:
        logger.warning("[%s] Launchpad sell failed: %s", label, exc)
        return False
    state.daily_volume_usd += trade_usd
    state.last_tx_hash = tx_hash
    logger.info("[%s] Sell tx sent: %s", label, tx_hash)
    return True


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


def get_domain_swap_menu_input(state: BotState) -> Optional[Tuple[str, str, str, str]]:
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
    return src_symbol, dst_symbol, amount_mode, f"{min_raw}|{max_raw}"


def get_domain_mode_menu_choice() -> Optional[str]:
    print("\nDomain token mode:")
    print("1) Single round-trip")
    print("2) Domain quest volume")
    print("3) Back")
    raw = input("Select [1-3]: ").strip()
    if raw == "3":
        return None
    if raw == "1":
        return "single"
    if raw == "2":
        return "quest"
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


def get_domain_quest_menu_input(state: BotState) -> Optional[Tuple[str, str, str, str, str]]:
    _ = state
    domain_name = get_domain_quest_token_choice()
    if not domain_name:
        return None
    print(f"\n{domain_name} quest volume:")
    print(f"Target: USDC.E <-> {domain_name}")
    print("\nPartial return percent range:")
    min_raw = input("Minimum percent [95]: ").strip() or "95"
    max_raw = input("Maximum percent [99]: ").strip() or "99"
    _ = _parse_decimal_input(min_raw)
    _ = _parse_decimal_input(max_raw)

    target_raw = input("Target volume in USDC.E [25]: ").strip() or "25"
    _ = _parse_decimal_input(target_raw)
    print("\nFinal asset:")
    print("1) USDC.E")
    print("2) ETH")
    final_raw = input("Select [1-2, default 1]: ").strip() or "1"
    if final_raw not in {"1", "2"}:
        raise ValueError("Invalid final asset selection")
    final_asset = "USDC.E" if final_raw == "1" else "ETH"
    return domain_name, min_raw, max_raw, target_raw, final_asset


def run_domain_quest_volume_once(cfg: BotConfig, logger: logging.Logger, state: BotState) -> None:
    success_wallets = 0
    failed_wallets = 0
    skipped_wallets = 0
    failed_wallet_addresses: List[str] = []

    def _fail_wallet() -> None:
        nonlocal failed_wallets
        failed_wallets += 1
        if wallet not in failed_wallet_addresses:
            failed_wallet_addresses.append(wallet)

    picked = get_domain_quest_menu_input(state)
    if not picked:
        logger.info("Domain quest volume canceled by user.")
        return
    domain_name, min_raw, max_raw, target_raw, final_asset = picked
    target_volume = _parse_decimal_input(target_raw)
    quest_target_volume = min(target_volume, DOMAIN_QUEST_COMPLETION_THRESHOLD_USD)
    execution_target_volume = quest_target_volume + max(Decimal("1"), quest_target_volume * Decimal("0.10"))
    partial_min = _parse_decimal_input(min_raw)
    partial_max = _parse_decimal_input(max_raw)
    if partial_min <= 0 or partial_max <= 0:
        raise ValueError("Partial return percent must be > 0")
    if partial_max > 100:
        raise ValueError("Partial return percent cannot be > 100")

    mode_label = f"QUEST {domain_name}"

    def _quest_log(message: str) -> str:
        return f"[{mode_label}] {message}"

    volume_since = datetime.now(timezone.utc) - timedelta(days=DOMAIN_QUEST_VOLUME_LOOKBACK_DAYS)

    wallet_key_records = _build_wallet_key_records(cfg, logger, "QUEST")
    if not wallet_key_records:
        raise ValueError(
            f"No wallet/private-key pairs available for {domain_name} quest "
            "(fill wallets.txt + keys.txt line-by-line or set valid PRIVATE_KEY in .env)"
        )
    wallet_key_records, wallet_start_offset, total_loaded_wallets = _apply_wallet_start_selection(wallet_key_records)

    shared_doma_api = DomaApiClient(
        cfg.doma_api_url,
        api_key=cfg.doma_api_key,
        api_keys=cfg.doma_api_keys,
    )
    launchpad_info = shared_doma_api.fetch_fractional_token_by_name(domain_name)
    if not launchpad_info:
        raise RuntimeError(f"{domain_name} launchpad token not found")
    quest_symbol = canonical_symbol(launchpad_info.symbol or launchpad_info.name)
    if not launchpad_info.pool_address:
        raise RuntimeError(f"{domain_name} pool route not found")
    quote_token = Token(
        address=launchpad_info.quote_token_address,
        symbol="USDC.E",
        decimals=6,
        derived_eth=Decimal("0"),
    )
    weth_token = _token_from_config_override(cfg, "WETH", 18)
    eth_price = _fetch_eth_price_via_doma_quote(cfg, shared_doma_api, quote_token)
    rides_token = _token_from_launchpad_price(launchpad_info, eth_price)
    rides_pool_addresses = [launchpad_info.pool_address]

    logger.info(
        _quest_log("mode started | source=AUTO pair=USDC.E<->%s wallets=%s | start_wallet=%s | lookback=%s days since=%s | target=%s USDC.E | quest_target=%s USDC.E | execution_target=%s USDC.E | pattern=auto-100%%->%s-%s%% | final=%s"),
        domain_name,
        len(wallet_key_records),
        wallet_start_offset + 1,
        DOMAIN_QUEST_VOLUME_LOOKBACK_DAYS,
        volume_since.isoformat(),
        _format_decimal_plain(target_volume),
        _format_decimal_plain(quest_target_volume),
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
    ) -> None:
        logger.info(
            _quest_log("wallet=%s failed before target reached | quest volume not finalized, running cleanup to %s"),
            wallet,
            final_asset,
        )
        try:
            rides_balance = exec_client.get_erc20_balance(rides_token.address, rides_token.decimals)
            rides_price_usd = pick_token_usd_price(rides_token, eth_price)
            if rides_price_usd <= 0:
                rides_price_usd = Decimal("0")
            rides_usd = rides_balance * rides_price_usd
            if rides_balance > 0 and rides_usd >= MIN_EXECUTABLE_TRADE_USD:
                logger.info(
                    _quest_log("wallet=%s failed-run cleanup | %s->USDC.E amount=100%% of %s"),
                    wallet,
                    rides_token.symbol,
                    rides_token.symbol,
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
                        trade_amount_expr="100%",
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
            elif rides_balance > 0:
                logger.info(
                    _quest_log("wallet=%s failed-run cleanup skipped | %s dust below $0.10 (%s)"),
                    wallet,
                    rides_token.symbol,
                    _format_decimal_plain(rides_usd),
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
        proxies, skip_wallet = _proxy_for_line(cfg, line_idx, logger, "QUEST")
        if skip_wallet:
            skipped_wallets += 1
            continue
        logger.info(_quest_log("wallet %s"), _wallet_progress_label(wallet_start_offset + idx, total_loaded_wallets, wallet))
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
            rides_completion_threshold = quest_target_volume
            if accumulated_volume >= rides_completion_threshold:
                logger.info(
                    _quest_log("wallet=%s already has %s volume for last %s days since %s = %s/%s | completion_threshold=%s | skipping wallet"),
                    wallet,
                    domain_name,
                    DOMAIN_QUEST_VOLUME_LOOKBACK_DAYS,
                    volume_since.isoformat(),
                    _format_decimal_plain(accumulated_volume),
                    _format_decimal_plain(target_volume),
                    _format_decimal_plain(rides_completion_threshold),
                )
                skipped_wallets += 1
                continue
            if accumulated_volume > 0:
                logger.info(
                    _quest_log("wallet=%s recent %s volume for last %s days since %s = %s/%s | remaining_to_target=%s | planned_topup_to=%s"),
                    wallet,
                    domain_name,
                    DOMAIN_QUEST_VOLUME_LOOKBACK_DAYS,
                    volume_since.isoformat(),
                    _format_decimal_plain(accumulated_volume),
                    _format_decimal_plain(target_volume),
                    _format_decimal_plain(target_volume - accumulated_volume),
                    _format_decimal_plain(execution_target_volume),
                )
            else:
                logger.info(
                    _quest_log("wallet=%s recent %s volume for last %s days since %s = 0/%s | planned_topup_to=%s"),
                    wallet,
                    domain_name,
                    DOMAIN_QUEST_VOLUME_LOOKBACK_DAYS,
                    volume_since.isoformat(),
                    _format_decimal_plain(target_volume),
                    _format_decimal_plain(execution_target_volume),
                )
            cycle = 0
            wallet_failed = False

            while accumulated_volume < execution_target_volume:
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
                full_balance_rides_usd = full_balance_rides * rides_price_usd
                has_usable_usdc = full_balance_usdc >= MIN_EXECUTABLE_TRADE_USD
                has_usable_rides = full_balance_rides_usd >= MIN_EXECUTABLE_TRADE_USD
                reserve_eth = Decimal("0.00001")
                spendable_eth = exec_client.get_native_balance() - reserve_eth
                spendable_eth = spendable_eth if spendable_eth > 0 else Decimal("0")
                spendable_eth_usd = spendable_eth * eth_price

                if not has_usable_usdc and not has_usable_rides:
                    if spendable_eth <= 0:
                        logger.warning(_quest_log("wallet=%s no usable ETH/USDC.E/%s balance for quest cycle"), wallet, rides_token.symbol)
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
                    _sleep_between_swaps()
                    continue
                elif not has_usable_rides and full_balance_usdc > 0 and spendable_eth_usd >= MIN_EXECUTABLE_TRADE_USD and spendable_eth_usd > full_balance_usdc:
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
                    _sleep_between_swaps()
                    continue
                elif full_balance_usdc > 0:
                    full_in_symbol = "USDC.E"
                    full_out_symbol = rides_token.symbol
                    partial_in_symbol = rides_token.symbol
                    partial_out_symbol = "USDC.E"
                    full_balance = full_balance_usdc
                    full_trade_usd = full_balance_usdc
                    full_trade_expr = "100%"
                else:
                    full_in_symbol = rides_token.symbol
                    full_out_symbol = "USDC.E"
                    partial_in_symbol = "USDC.E"
                    partial_out_symbol = rides_token.symbol
                    full_balance = full_balance_rides
                    full_trade_usd = full_balance * rides_price_usd
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
                if remaining_volume > 0 and remaining_volume < (full_trade_usd * Decimal("2")):
                    capped_full_usd = min(
                        full_trade_usd,
                        max(MIN_EXECUTABLE_TRADE_USD, remaining_volume / Decimal("2")),
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
                if not ok_full or not state.last_tx_hash or not _wait_tx_receipt(exec_client, state.last_tx_hash, timeout_sec=180):
                    wallet_failed = True
                    break
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
                if accumulated_volume >= execution_target_volume:
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
                    _, partial_trade_usd = resolve_trade_amount(partial_expr, partial_balance, rides_price_usd)

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
                if not ok_partial or not state.last_tx_hash or not _wait_tx_receipt(exec_client, state.last_tx_hash, timeout_sec=180):
                    wallet_failed = True
                    break
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
                if accumulated_volume < execution_target_volume:
                    _sleep_between_swaps()

            if not wallet_failed and accumulated_volume < execution_target_volume:
                logger.warning(
                    _quest_log("wallet=%s execution target not reached | local_volume=%s/%s | required_quest_target=%s"),
                    wallet,
                    _format_decimal_plain(accumulated_volume),
                    _format_decimal_plain(execution_target_volume),
                    _format_decimal_plain(quest_target_volume),
                )
                wallet_failed = True

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
                )
                _fail_wallet()
            elif accumulated_volume >= execution_target_volume:
                final_rides_balance = exec_client.get_erc20_balance(rides_token.address, rides_token.decimals)
                if final_rides_balance > 0:
                    final_rides_usd = final_rides_balance * rides_price_usd
                    if final_rides_usd < MIN_EXECUTABLE_TRADE_USD:
                        logger.info(
                            _quest_log("wallet=%s final settle skipped | %s dust below $0.10 (%s)"),
                            wallet,
                            rides_token.symbol,
                            _format_decimal_plain(final_rides_usd),
                        )
                    else:
                        logger.info(
                            _quest_log("wallet=%s final settle | %s->USDC.E amount=100%% of %s"),
                            wallet,
                            rides_token.symbol,
                            rides_token.symbol,
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
                            trade_amount_expr="100%",
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
        failed_wallet_addresses,
    )


def get_domain_listing_menu_input() -> Optional[Tuple[str, str, str, str, str]]:
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
    return min_raw, max_raw, duration_raw, delay_min_raw, delay_max_raw


def _random_listing_price(min_price: Decimal, max_price: Decimal) -> Decimal:
    if min_price == max_price:
        return min_price.quantize(Decimal("0.1"))
    spread = max_price - min_price
    price = min_price + spread * Decimal(str(random.random()))
    return price.quantize(Decimal("0.1"))


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


def _domain_listing_loader_path() -> Path:
    return Path(__file__).with_name("doma_node_esm_loader.mjs")


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
    payload = {
        "chainId": cfg.chain_id,
        "rpcUrl": cfg.rpc_url,
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
        elif not event.get("ok", True):
            logger.warning("[LIST] wallet=%s domain=%s helper error: %s", wallet, domain.name, event.get("error"))
    if result.returncode != 0:
        return False, "", (result.stderr or result.stdout or f"node exited with {result.returncode}").strip()
    try:
        data = json.loads(result.stdout.splitlines()[-1])
    except Exception as exc:
        return False, "", f"failed to parse helper output: {exc}; stdout={result.stdout.strip()}"
    if not data.get("ok"):
        return False, "", str(data.get("error") or "unknown helper error")
    orders = (data.get("result") or {}).get("orders") or []
    order_id = str((orders[0] or {}).get("orderId") or "") if orders else ""
    return True, order_id, ""


def _eligible_unlisted_domains(all_domains: List[OwnedDomain], listed_domains: List[OwnedDomain], chain_id: int) -> List[OwnedDomain]:
    listed_names = {d.name.lower() for d in listed_domains}
    target_network = f"eip155:{chain_id}"
    out: List[OwnedDomain] = []
    seen: set[str] = set()
    for domain in all_domains:
        if domain.name.lower() in listed_names:
            continue
        if domain.name.lower() in seen:
            continue
        seen.add(domain.name.lower())
        if domain.network_id and domain.network_id != target_network:
            continue
        if domain.orderbook_disabled:
            continue
        out.append(domain)
    return out


def run_domain_listing_once(cfg: BotConfig, logger: logging.Logger, state: BotState) -> None:
    _ = state
    picked = get_domain_listing_menu_input()
    if not picked:
        logger.info("[LIST] canceled by user.")
        return
    min_raw, max_raw, duration_raw, delay_min_raw, delay_max_raw = picked
    min_price = _parse_decimal_input(min_raw)
    max_price = _parse_decimal_input(max_raw)
    duration_days = _parse_decimal_input(duration_raw)
    listing_delay_min = float(_parse_decimal_input(delay_min_raw))
    listing_delay_max = float(_parse_decimal_input(delay_max_raw))

    wallet_key_records = _build_wallet_key_records(cfg, logger, "LIST")
    if not wallet_key_records:
        raise ValueError(
            "No wallet/private-key pairs available for domain listing "
            "(fill wallets.txt + keys.txt line-by-line or set valid PRIVATE_KEY in .env)"
        )
    wallet_key_records, wallet_start_offset, _ = _apply_wallet_start_selection(wallet_key_records)

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
        "[LIST] mode started | wallets=%s | start_wallet=%s | price=%s-%s USDC.E | duration=%s days | delay=%s-%s sec | currency=USDC.E",
        len(wallet_key_records),
        wallet_start_offset + 1,
        _format_decimal_plain(min_price),
        _format_decimal_plain(max_price),
        _format_decimal_plain(duration_days),
        _format_decimal_plain(_parse_decimal_input(delay_min_raw)),
        _format_decimal_plain(_parse_decimal_input(delay_max_raw)),
    )
    success_wallets = 0
    failed_wallets = 0
    skipped_wallets = 0
    failed_wallet_addresses: List[str] = []

    for idx, (line_idx, wallet, private_key) in enumerate(wallet_key_records):
        proxies, skip_wallet = _proxy_for_line(cfg, line_idx, logger, "LIST")
        proxy = (proxies or {}).get("https") or (proxies or {}).get("http") or ""
        if skip_wallet:
            skipped_wallets += 1
            continue
        logger.info(
            "[LIST] wallet %s",
            _wallet_progress_label(idx + wallet_start_offset, len(wallet_key_records) + wallet_start_offset, wallet),
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
            unlisted_domains = _eligible_unlisted_domains(all_domains, listed_domains, cfg.chain_id)
            if not unlisted_domains:
                skipped_wallets += 1
                logger.info(
                    "[LIST] wallet=%s no unlisted domains | owned=%s listed=%s",
                    wallet,
                    len(all_domains),
                    len(listed_domains),
                )
                continue
            logger.info(
                "[LIST] wallet=%s unlisted domains=%s | owned=%s listed=%s | proxy=%s",
                wallet,
                len(unlisted_domains),
                len(all_domains),
                len(listed_domains),
                "yes" if proxy else "no",
            )
            wallet_success = 0
            wallet_failed = 0
            for domain_idx, domain in enumerate(unlisted_domains, start=1):
                price = _random_listing_price(min_price, max_price)
                logger.info(
                    "[LIST] wallet=%s domain %s/%s %s | price=%s USDC.E",
                    wallet,
                    domain_idx,
                    len(unlisted_domains),
                    domain.name,
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
                if domain_idx < len(unlisted_domains):
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
        if idx < len(wallet_key_records) - 1 and cfg.wallet_delay_max_sec > 0:
            delay_sec = random.uniform(cfg.wallet_delay_min_sec, cfg.wallet_delay_max_sec)
            logger.info("[LIST] delay before next wallet: %.2f sec", delay_sec)
            time.sleep(delay_sec)

    _print_mode_summary(
        "LIST",
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
        if wallet not in failed_wallet_addresses:
            failed_wallet_addresses.append(wallet)
    domain_mode = get_domain_mode_menu_choice()
    if not domain_mode:
        logger.info("Domain swap canceled by user.")
        return
    if domain_mode == "quest":
        run_domain_quest_volume_once(cfg, logger, state)
        return
    picked = get_domain_swap_menu_input(state)
    if not picked:
        logger.info("Domain swap canceled by user.")
        return
    src_symbol, dst_symbol, amount_mode, range_raw = picked
    min_raw, max_raw = [x.strip() for x in range_raw.split("|", 1)]

    wallet_key_records = _build_wallet_key_records(cfg, logger, "DOMAIN")
    if not wallet_key_records:
        raise ValueError(
            "No wallet/private-key pairs available for domain swap "
            "(fill wallets.txt + keys.txt line-by-line or set valid PRIVATE_KEY in .env)"
        )
    wallet_key_records, wallet_start_offset, total_loaded_wallets = _apply_wallet_start_selection(wallet_key_records)

    logger.info(
        "[DOMAIN] mode started | source=%s target=%s wallets=%s | start_wallet=%s | round_trip=true",
        src_symbol,
        dst_symbol,
        len(wallet_key_records),
        wallet_start_offset + 1,
    )

    def _sleep_between_domain_swaps() -> None:
        delay_sec = _random_swap_delay_sec()
        logger.info("[DOMAIN] delay between swaps: %.2f sec", delay_sec)
        time.sleep(delay_sec)

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
        logger.info("[DOMAIN] wallet %s", _wallet_progress_label(wallet_start_offset + idx, total_loaded_wallets, wallet))
        before_points_snapshot: Optional[PointsSnapshot] = None
        try:
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

    wallet_key_records = _build_wallet_key_records(cfg, logger, "SWEEP")
    if not wallet_key_records:
        raise ValueError(
            "No wallet/private-key pairs available for sweep "
            "(fill wallets.txt + keys.txt line-by-line or set valid PRIVATE_KEY in .env)"
        )
    wallet_key_records, wallet_start_offset, total_loaded_wallets = _apply_wallet_start_selection(wallet_key_records)

    logger.info(
        "[SWEEP] mode started | target=%s | wallets=%s | start_wallet=%s%s",
        target_asset,
        len(wallet_key_records),
        wallet_start_offset + 1,
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
    shared_subgraph = DomaSubgraphClient(cfg.subgraph_url, proxies=None)
    pools = shared_subgraph.fetch_top_pools(limit=1000)
    eth_price = shared_subgraph.fetch_eth_price_usd()
    if eth_price <= 0:
        raise RuntimeError("Failed to resolve ETH/USD")
    token_catalog = shared_doma_api.fetch_fractional_tokens(take=250, max_pages=10)
    if not token_catalog:
        raise RuntimeError("Doma fractional token catalog is empty")

    def _find_token_by_symbol(sym: str) -> Optional[Token]:
        target = canonical_symbol(sym)
        for pool in pools:
            if pool.token0.symbol == target:
                return pool.token0
            if pool.token1.symbol == target:
                return pool.token1
        return None

    usdc_token = _find_token_by_symbol("USDC.E")
    if not usdc_token:
        raise RuntimeError("USDC.E token metadata not found")
    weth_token = _find_token_by_symbol("WETH")
    if not weth_token:
        raise RuntimeError("WETH token metadata not found")
    pool_weth_usdc = _find_best_pool_for_symbols(cfg, pools, "WETH", "USDC.E", ignore_limits=True)
    if not pool_weth_usdc:
        raise RuntimeError("No route WETH<->USDC.E")
    logger.info(
        "[SWEEP] shared metadata loaded | pools=%s | tokens=%s | eth_price=%s",
        len(pools),
        len(token_catalog),
        _format_decimal_plain(eth_price),
    )

    for idx, (line_idx, wallet, private_key) in enumerate(wallet_key_records):
        proxies, skip_wallet = _proxy_for_line(cfg, line_idx, logger, "SWEEP")
        if skip_wallet:
            skipped_wallets += 1
            continue
        logger.info("[SWEEP] wallet %s", _wallet_progress_label(wallet_start_offset + idx, total_loaded_wallets, wallet))

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
            if not info.address or info.address in seen_token_addresses:
                continue
            try:
                balance_dec = exec_client.get_erc20_balance(info.address, info.decimals)
            except Exception:
                continue
            if balance_dec <= 0:
                continue
            seen_token_addresses.add(info.address)
            held_launchpad_tokens.append((info, balance_dec))

        held_weth_balance = Decimal("0")
        try:
            held_weth_balance = exec_client.get_erc20_balance(weth_token.address, weth_token.decimals)
        except Exception:
            held_weth_balance = Decimal("0")
        native_eth_balance = exec_client.get_native_balance()
        reserve_eth = Decimal("0.00001")
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
            if info.pool_address:
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
            elif info.launchpad_address and info.quote_token_address == usdc_token.address:
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
            else:
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
                ok = _execute_trade_for_pair(
                    cfg=cfg,
                    logger=logger,
                    state=state,
                    exec_client=exec_client,
                    pool=pool_weth_usdc,
                    symbol_in="WETH",
                    symbol_out="USDC.E",
                    trade_amount_expr="100%",
                    eth_price=eth_price,
                    label=f"SWEEP {wallet} WETH>USDC.E",
                    bypass_risk_checks=True,
                    allow_no_quoter_execution=True,
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


def get_pair_swap_menu_input(state: BotState) -> Optional[Tuple[str, str, str]]:
    _ = state
    print("\nPair swap ETH <-> USDC.E:")
    print("1) Source ETH")
    print("2) Source USDC.E")
    print("3) Back")
    src_raw = input("Select [1-3]: ").strip()
    if src_raw == "3":
        return None
    if src_raw not in {"1", "2"}:
        raise ValueError("Invalid source selection")
    src_symbol = "ETH" if src_raw == "1" else "USDC.E"

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
        return src_symbol, amount_mode, percent_raw

    min_raw = input("Minimum: ").strip()
    max_raw = input("Maximum: ").strip()
    _ = _parse_decimal_input(min_raw)
    _ = _parse_decimal_input(max_raw)
    return src_symbol, amount_mode, f"{min_raw}|{max_raw}"


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
    src_symbol, amount_mode, amount_raw = picked
    dst_symbol = "USDC.E" if src_symbol == "ETH" else "ETH"

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
        logger.info("[PAIR] wallet %s", _wallet_progress_label(wallet_start_offset + idx, total_loaded_wallets, wallet))
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
                _fail_wallet()
                logger.warning("[PAIR] wallet=%s init failed: %s", wallet, exc)
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

            ui_token_in = weth_token if src_symbol == "ETH" else usdc_token
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


def get_volume_farm_menu_input(state: BotState) -> Optional[Tuple[str, str, str]]:
    _ = state
    print("\nFarm volume ETH <-> USDC.E:")
    print("\nPartial return percent range:")
    min_raw = input("Minimum percent [80]: ").strip() or "80"
    max_raw = input("Maximum percent [90]: ").strip() or "90"
    _ = _parse_decimal_input(min_raw)
    _ = _parse_decimal_input(max_raw)

    target_raw = input("Target volume in USDC.E [251]: ").strip() or "251"
    _ = _parse_decimal_input(target_raw)
    return min_raw, max_raw, target_raw


def run_volume_farm_once(cfg: BotConfig, logger: logging.Logger, state: BotState) -> None:
    success_wallets = 0
    failed_wallets = 0
    skipped_wallets = 0
    failed_wallet_addresses: List[str] = []

    def _fail_wallet() -> None:
        nonlocal failed_wallets
        failed_wallets += 1
        if wallet not in failed_wallet_addresses:
            failed_wallet_addresses.append(wallet)

    picked = get_volume_farm_menu_input(state)
    if not picked:
        logger.info("Volume farm canceled by user.")
        return
    min_raw, max_raw, target_raw = picked
    target_volume = _parse_decimal_input(target_raw)

    wallet_key_records = _build_wallet_key_records(cfg, logger, "VOLUME")
    if not wallet_key_records:
        raise ValueError(
            "No wallet/private-key pairs available for volume farm "
            "(fill wallets.txt + keys.txt line-by-line or set valid PRIVATE_KEY in .env)"
        )
    wallet_key_records, wallet_start_offset, total_loaded_wallets = _apply_wallet_start_selection(wallet_key_records)

    logger.info(
        "[VOLUME] mode started | source=AUTO pair=ETH<->USDC.E wallets=%s | start_wallet=%s | target=%s USDC.E | pattern=auto-100%%->%s-%s%% | final=ETH",
        len(wallet_key_records),
        wallet_start_offset + 1,
        _format_decimal_plain(target_volume),
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
        logger.info("[VOLUME] wallet %s", _wallet_progress_label(wallet_start_offset + idx, total_loaded_wallets, wallet))
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
                pool = _find_best_pool_for_symbols(cfg, pools, "ETH", "USDC.E", ignore_limits=True)
                if not pool:
                    raise RuntimeError("No pool route found for ETH<->USDC.E")
            except Exception as exc:
                _fail_wallet()
                logger.warning("[VOLUME] wallet=%s init failed: %s", wallet, exc)
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
            cycle = 0
            wallet_failed = False
            partial_min = _parse_decimal_input(min_raw)
            partial_max = _parse_decimal_input(max_raw)
            if partial_min <= 0 or partial_max <= 0:
                raise ValueError("Partial return percent must be > 0")
            if partial_max > 100:
                raise ValueError("Partial return percent cannot be > 100")

            while accumulated_volume < target_volume:
                cycle += 1
                logger.info(
                    "[VOLUME] wallet=%s cycle=%s | progress=%s/%s",
                    wallet,
                    cycle,
                    _format_decimal_plain(accumulated_volume),
                    _format_decimal_plain(target_volume),
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
                reserve_eth = Decimal("0.00001")
                full_balance_eth = exec_client.get_native_balance() - reserve_eth
                full_balance_eth = full_balance_eth if full_balance_eth > 0 else Decimal("0")

                if full_balance_usdc > 0:
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
                if not ok_full or not state.last_tx_hash or not _wait_tx_receipt(exec_client, state.last_tx_hash, timeout_sec=180):
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
                    _format_decimal_plain(target_volume),
                )
                if accumulated_volume >= target_volume:
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
                if not ok_partial or not state.last_tx_hash or not _wait_tx_receipt(exec_client, state.last_tx_hash, timeout_sec=180):
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
                    _format_decimal_plain(target_volume),
                )
                if accumulated_volume < target_volume:
                    _sleep_between_swaps()

            if wallet_failed:
                _fail_wallet()
            elif accumulated_volume >= target_volume:
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
                    if not ok_settle or not state.last_tx_hash or not _wait_tx_receipt(exec_client, state.last_tx_hash, timeout_sec=180):
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
                            _format_decimal_plain(target_volume),
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


def get_bridge_tasks_from_menu(state: BotState) -> Optional[List[str]]:
    print("\nBridge routes (Relay):")
    print("1) Base -> Doma | ETH -> ETH")
    print("2) Base -> Doma | ETH -> USDC.E")
    print("3) Back")
    route = input("Select [1-3]: ").strip()
    if route == "3":
        return None
    if route not in {"1", "2"}:
        raise ValueError("Invalid bridge route selection")

    print("\nAmount mode:")
    print("1) Number (ETH)")
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

    if route == "1":
        return [f"base>doma:ETH>ETH:{expr_1}"]
    return [f"base>doma:ETH>USDC.E:{expr_1}"]


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


def get_menu_choice() -> str:
    print("\nChoose mode:")
    print("1) Bridge")
    print("2) Check points")
    print("3) Close all positions")
    print("4) Swap domain token")
    print("5) Swap ETH <-> USDC.E")
    print("6) Collect all tokens -> ETH / USDC.E")
    print("7) Farm 250+ volume ETH <-> USDC.E")
    print("8) Domain quest volume")
    print("9) List unlisted domains for sale")
    print("10) Doma quest guide")
    print("11) Exit")
    return input("Select [1-11]: ").strip()


def print_doma_quest_guide(logger: logging.Logger) -> None:
    lines = [
        "",
        "Doma quest guide:",
        "",
        "Daily:",
        "  Make a $5 or greater swap on any domain token.",
        "  Use mode 8: Domain quest volume -> any token -> Target volume = 5.",
        "",
        "Weekly:",
        "  List any domain on the marketplace.",
        "  Use mode 9: List unlisted domains for sale.",
        "",
        "  Trade $100 in total volume this week.",
        "  Use mode 7: Farm volume ETH <-> USDC.E -> Target volume = 100.",
        "",
        "  Trade $250 in total volume this week.",
        "  Use mode 7: Farm volume ETH <-> USDC.E -> Target volume = 250.",
        "",
        "Season:",
        "  Add at least $10 / $50 in liquidity to a domain token.",
        "  Not automated yet. Needs Uniswap V3 mint/increase-liquidity implementation.",
        "",
        "  Bridge a domain from Doma to Base.",
        "  Not automated yet. Current bridge mode bridges fungible tokens, not domain NFTs.",
        "",
        "  Mint 5 domain NFTs.",
        "  Not automated yet. Needs registrar/checkout flow implementation.",
        "",
        "  Stake 3 subdomains.",
        "  Not automated yet. Needs subdomain staking contract/API implementation.",
    ]
    for line in lines:
        print(line)
    logger.info("[QUEST_GUIDE] shown")


def main() -> None:
    cfg = BotConfig()
    logger = setup_logger(cfg.log_file)
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
            "points",
            "trading_volume_usd",
            "liquid_amount_usd",
            "referral_count",
            "total_snapshot_entries",
            "snapshot_date",
        ],
        delimiter=cfg.csv_delimiter,
    )
    state = load_state(cfg.state_file)

    if cfg.paper_replay_mode:
        run_replay_report(cfg, logger)
        return

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
        run_points_once(cfg, logger, state)
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
        return
    if choice == "10":
        print_doma_quest_guide(logger)
        return
    logger.info("Exit selected.")


if __name__ == "__main__":
    main()

