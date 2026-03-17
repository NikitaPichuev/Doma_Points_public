from __future__ import annotations

import csv
import json
import logging
import random
import re
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from web3 import Web3

from config import BotConfig
from doma_api import (
    DomaApiClient,
    DomaSubgraphClient,
    EvmExecutionClient,
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


def _color(text: str, code: str) -> str:
    return f"{code}{text}{ANSI_RESET}"


def _print_mode_summary(mode: str, total: int, success: int, failed: int, skipped: int = 0) -> None:
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
    token_in, token_out = pair

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
                delay_sec = random.uniform(5, 10)
                logger.info("[%s] delay after wrap: %.2f sec", label, delay_sec)
                time.sleep(delay_sec)

    approve_hash = exec_client.ensure_allowance(token_in.address, amount_in_raw)
    if approve_hash:
        logger.info("[%s] Approve tx sent: %s", label, approve_hash)
        if wait_for_pre_tx:
            ok = _wait_tx_receipt(exec_client, approve_hash, timeout_sec=180)
            if not ok:
                raise RuntimeError("Approve tx failed or timed out")
            delay_sec = random.uniform(5, 10)
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
            retry_delay = random.uniform(5, 10)
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
            raise
    state.daily_volume_usd += trade_usd
    state.last_tx_hash = tx_hash
    log_trade(cfg, "EXECUTED", label, pool, token_in, token_out, amount_in_dec, quote_out_dec, trade_usd, expected_out_usd, tx_hash, "")
    logger.info("[%s] Swap tx sent: %s", label, tx_hash)
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
                delay_sec = random.uniform(5, 10)
                logger.info("[%s] delay after wrap: %.2f sec", label, delay_sec)
                time.sleep(delay_sec)

    approve_hash = exec_client.ensure_allowance(token_in.address, amount_in_raw)
    if approve_hash:
        logger.info("[%s] Approve tx sent: %s", label, approve_hash)
        if wait_for_pre_tx:
            ok = _wait_tx_receipt(exec_client, approve_hash, timeout_sec=180)
            if not ok:
                raise RuntimeError("Approve tx failed or timed out")
            delay_sec = random.uniform(5, 10)
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
            retry_delay = random.uniform(5, 10)
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
            raise
    state.daily_volume_usd += trade_usd
    state.last_tx_hash = tx_hash
    logger.info("[%s] Swap tx sent: %s", label, tx_hash)
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


def run_bridge_once(cfg: BotConfig, logger: logging.Logger, state: BotState) -> None:
    bridge_tasks = cfg.bridge_tasks
    success_wallets = 0
    failed_wallets = 0
    skipped_wallets = 0

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

    logger.info("[BRIDGE] mode started | wallets=%s", len(wallet_key_records))
    for idx, (line_idx, wallet, private_key) in enumerate(wallet_key_records):
        proxies, skip_wallet = _proxy_for_line(cfg, line_idx, logger, "BRIDGE")
        if skip_wallet:
            skipped_wallets += 1
            continue
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
            failed_wallets += 1
            logger.warning("[BRIDGE] wallet %s failed: %s", wallet, exc)
            continue
        else:
            success_wallets += 1

        if idx < len(wallet_key_records) - 1 and cfg.wallet_delay_max_sec > 0:
            delay_sec = random.uniform(cfg.wallet_delay_min_sec, cfg.wallet_delay_max_sec)
            logger.info("[BRIDGE] delay before next wallet: %.2f sec", delay_sec)
            time.sleep(delay_sec)
    state.last_bridge_ts = current_ts()
    _print_mode_summary("BRIDGE", len(wallet_key_records), success_wallets, failed_wallets, skipped_wallets)


def run_close_position_once(cfg: BotConfig, logger: logging.Logger, state: BotState) -> None:
    _ = state
    success_wallets = 0
    failed_wallets = 0
    skipped_wallets = 0
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

    logger.info("[POSITION] close-all mode | wallets=%s", len(wallet_key_records))
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
            failed_wallets += 1
            continue

        try:
            active_positions = client.list_owner_positions(owner=wallet, limit=200, only_active=True)
        except Exception as exc:
            logger.warning("[POSITION] wallet %s: failed to read positions: %s", wallet, exc)
            failed_wallets += 1
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
                pos_delay_sec = random.uniform(5, 15)
                logger.info("[POSITION] delay before next position: %.2f sec", pos_delay_sec)
                time.sleep(pos_delay_sec)

        if wallet_failed:
            failed_wallets += 1
        else:
            success_wallets += 1

        if idx < len(wallet_key_records) - 1 and cfg.wallet_delay_max_sec > 0:
            delay_sec = random.uniform(cfg.wallet_delay_min_sec, cfg.wallet_delay_max_sec)
            logger.info("[POSITION] delay before next wallet: %.2f sec", delay_sec)
            time.sleep(delay_sec)
    _print_mode_summary("POSITION", len(wallet_key_records), success_wallets, failed_wallets, skipped_wallets)


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


def run_domain_swap_once(cfg: BotConfig, logger: logging.Logger, state: BotState) -> None:
    success_wallets = 0
    failed_wallets = 0
    skipped_wallets = 0
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

    logger.info(
        "[DOMAIN] mode started | source=%s target=%s wallets=%s | round_trip=true",
        src_symbol,
        dst_symbol,
        len(wallet_key_records),
    )

    def _sleep_between_domain_swaps() -> None:
        delay_sec = random.uniform(5, 10)
        logger.info("[DOMAIN] delay between swaps: %.2f sec", delay_sec)
        time.sleep(delay_sec)

    use_relay_swap = not cfg.router_address or cfg.router_address == "0x0000000000000000000000000000000000000000"

    for idx, (line_idx, wallet, private_key) in enumerate(wallet_key_records):
        proxies, skip_wallet = _proxy_for_line(cfg, line_idx, logger, "DOMAIN")
        if skip_wallet:
            skipped_wallets += 1
            continue
        try:
            subgraph = DomaSubgraphClient(cfg.subgraph_url, proxies=proxies)
            pools = subgraph.fetch_top_pools(limit=1000)
            eth_price = subgraph.fetch_eth_price_usd()
            if eth_price <= 0:
                raise RuntimeError("Failed to resolve ETH/USD")
            has_target = any(p.token0.symbol == dst_symbol or p.token1.symbol == dst_symbol for p in pools)
            if not has_target:
                raise RuntimeError(f"Token {dst_symbol} not found in top pools")
        except Exception as exc:
            failed_wallets += 1
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
        exec_client = EvmExecutionClient(
            rpc_url=cfg.rpc_url,
            chain_id=cfg.chain_id,
            account_address=wallet,
            private_key=private_key,
            router_address=cfg.router_address,
            quoter_address=cfg.quoter_address,
            router_variant=cfg.router_variant,
            request_proxies=proxies,
        )

        if use_relay_swap:
            src_token = _find_token_by_symbol(src_symbol) if src_symbol != "ETH" else None
            dst_token = _find_token_by_symbol(dst_symbol)
            if not dst_token:
                logger.warning("[DOMAIN] wallet=%s token %s not found for relay swap", wallet, dst_symbol)
                failed_wallets += 1
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
                    failed_wallets += 1
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
                failed_wallets += 1
                continue
            amount_in_raw = decimal_to_raw(amount_in_dec, src_decimals)
            if amount_in_raw <= 0:
                logger.warning("[DOMAIN] wallet=%s amount too small", wallet)
                failed_wallets += 1
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
                failed_wallets += 1
                continue

            _sleep_between_domain_swaps()
            after_dst = exec_client.get_erc20_balance(dst_token.address, dst_token.decimals)
            delta_dst = after_dst - before_dst
            if delta_dst <= 0:
                logger.warning("[DOMAIN] wallet=%s no received %s for reverse swap", wallet, dst_symbol)
                failed_wallets += 1
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
                failed_wallets += 1
                continue
            success_wallets += 1
            continue

        if src_symbol == "USDC.E":
            pool_fw = _pick_pool("USDC.E", dst_symbol)
            pool_bw = _pick_pool(dst_symbol, "USDC.E")
            if not pool_fw or not pool_bw:
                logger.warning("[DOMAIN] wallet=%s no route USDC.E<->%s", wallet, dst_symbol)
                failed_wallets += 1
            else:
                pair_fw = _find_tokens_for_direction(pool_fw, "USDC.E", dst_symbol)
                if not pair_fw:
                    logger.warning("[DOMAIN] wallet=%s invalid pool direction USDC.E>%s", wallet, dst_symbol)
                    failed_wallets += 1
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
                        failed_wallets += 1
                        continue
                    if ok_fw and state.last_tx_hash:
                        _wait_tx_receipt(exec_client, state.last_tx_hash, timeout_sec=180)
                        _sleep_between_domain_swaps()
                    after_dst = exec_client.get_erc20_balance(dst_token.address, dst_token.decimals)
                    delta_dst = after_dst - before_dst
                    if delta_dst <= 0:
                        logger.warning("[DOMAIN] wallet=%s no received %s for reverse swap", wallet, dst_symbol)
                        failed_wallets += 1
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
                            failed_wallets += 1
        else:
            pool_direct_fw = _pick_pool("ETH", dst_symbol)
            pool_direct_bw = _pick_pool(dst_symbol, "ETH")
            if pool_direct_fw and pool_direct_bw:
                pair_fw = _find_tokens_for_direction(pool_direct_fw, "ETH", dst_symbol)
                if not pair_fw:
                    logger.warning("[DOMAIN] wallet=%s invalid direct pool direction ETH>%s", wallet, dst_symbol)
                    failed_wallets += 1
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
                        failed_wallets += 1
                        continue
                    if ok_fw and state.last_tx_hash:
                        _wait_tx_receipt(exec_client, state.last_tx_hash, timeout_sec=180)
                        _sleep_between_domain_swaps()
                    after_dst = exec_client.get_erc20_balance(dst_token.address, dst_token.decimals)
                    delta_dst = after_dst - before_dst
                    if delta_dst <= 0:
                        logger.warning("[DOMAIN] wallet=%s no received %s for reverse swap", wallet, dst_symbol)
                        failed_wallets += 1
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
                            failed_wallets += 1
            else:
                pool_eth_usdc = _pick_pool("ETH", "USDC.E")
                pool_usdc_dst = _pick_pool("USDC.E", dst_symbol)
                pool_dst_usdc = _pick_pool(dst_symbol, "USDC.E")
                pool_usdc_eth = _pick_pool("USDC.E", "ETH")
                if not pool_eth_usdc or not pool_usdc_dst or not pool_dst_usdc or not pool_usdc_eth:
                    logger.warning("[DOMAIN] wallet=%s no route ETH>USDC.E>%s and back", wallet, dst_symbol)
                    failed_wallets += 1
                else:
                    usdc_from_eth_pair = _find_tokens_for_direction(pool_eth_usdc, "ETH", "USDC.E")
                    dst_from_usdc_pair = _find_tokens_for_direction(pool_usdc_dst, "USDC.E", dst_symbol)
                    usdc_from_dst_pair = _find_tokens_for_direction(pool_dst_usdc, dst_symbol, "USDC.E")
                    eth_from_usdc_pair = _find_tokens_for_direction(pool_usdc_eth, "USDC.E", "ETH")
                    if not usdc_from_eth_pair or not dst_from_usdc_pair or not usdc_from_dst_pair or not eth_from_usdc_pair:
                        logger.warning("[DOMAIN] wallet=%s invalid multi-hop pool direction for %s", wallet, dst_symbol)
                        failed_wallets += 1
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
                            failed_wallets += 1
                        else:
                            if state.last_tx_hash:
                                _wait_tx_receipt(exec_client, state.last_tx_hash, timeout_sec=180)
                                _sleep_between_domain_swaps()
                            after_dst = exec_client.get_erc20_balance(dst_token.address, dst_token.decimals)
                            delta_dst = after_dst - before_dst
                            if delta_dst <= 0:
                                logger.warning("[DOMAIN] wallet=%s no received %s for reverse route", wallet, dst_symbol)
                                failed_wallets += 1
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
                                    failed_wallets += 1

        if idx < len(wallet_key_records) - 1 and cfg.wallet_delay_max_sec > 0:
            delay_sec = random.uniform(cfg.wallet_delay_min_sec, cfg.wallet_delay_max_sec)
            logger.info("[DOMAIN] delay before next wallet: %.2f sec", delay_sec)
            time.sleep(delay_sec)
    _print_mode_summary("DOMAIN", len(wallet_key_records), success_wallets, failed_wallets, skipped_wallets)


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

    min_raw = input("Minimum: ").strip()
    max_raw = input("Maximum: ").strip()
    _ = _parse_decimal_input(min_raw)
    _ = _parse_decimal_input(max_raw)
    return src_symbol, amount_mode, f"{min_raw}|{max_raw}"


def run_pair_swap_once(cfg: BotConfig, logger: logging.Logger, state: BotState) -> None:
    success_wallets = 0
    failed_wallets = 0
    skipped_wallets = 0
    picked = get_pair_swap_menu_input(state)
    if not picked:
        logger.info("Pair swap canceled by user.")
        return
    src_symbol, amount_mode, range_raw = picked
    min_raw, max_raw = [x.strip() for x in range_raw.split("|", 1)]

    wallet_key_records = _build_wallet_key_records(cfg, logger, "PAIR")
    if not wallet_key_records:
        raise ValueError(
            "No wallet/private-key pairs available for pair swap "
            "(fill wallets.txt + keys.txt line-by-line or set valid PRIVATE_KEY in .env)"
        )

    logger.info(
        "[PAIR] mode started | source=%s target=%s wallets=%s | round_trip=true",
        src_symbol,
        "USDC.E" if src_symbol == "ETH" else "ETH",
        len(wallet_key_records),
    )

    def _sleep_between_pair_swaps() -> None:
        delay_sec = random.uniform(5, 10)
        logger.info("[PAIR] delay between swaps: %.2f sec", delay_sec)
        time.sleep(delay_sec)

    for idx, (line_idx, wallet, private_key) in enumerate(wallet_key_records):
        proxies, skip_wallet = _proxy_for_line(cfg, line_idx, logger, "PAIR")
        if skip_wallet:
            skipped_wallets += 1
            continue
        try:
            subgraph = DomaSubgraphClient(cfg.subgraph_url, proxies=proxies)
            pools = subgraph.fetch_top_pools(limit=1000)
            eth_price = subgraph.fetch_eth_price_usd()
            if eth_price <= 0:
                raise RuntimeError("Failed to resolve ETH/USD")
            pool_fw = _find_best_pool_for_symbols(cfg, pools, src_symbol, "USDC.E" if src_symbol == "ETH" else "ETH", ignore_limits=True)
            pool_bw = _find_best_pool_for_symbols(cfg, pools, "USDC.E" if src_symbol == "ETH" else "ETH", src_symbol, ignore_limits=True)
            if not pool_fw or not pool_bw:
                raise RuntimeError(f"No pool route found for {src_symbol}<->USDC.E")
        except Exception as exc:
            failed_wallets += 1
            logger.warning("[PAIR] wallet=%s init failed: %s", wallet, exc)
            continue
        amount_expr = _pick_random_amount_expr(
            amount_mode,
            _parse_decimal_input(min_raw),
            _parse_decimal_input(max_raw),
            state,
            min_raw=min_raw,
            max_raw=max_raw,
        )
        logger.info("[PAIR] wallet=%s amount=%s %s", wallet, amount_expr, src_symbol)
        exec_client = EvmExecutionClient(
            rpc_url=cfg.rpc_url,
            chain_id=cfg.chain_id,
            account_address=wallet,
            private_key=private_key,
            router_address=cfg.router_address,
            quoter_address=cfg.quoter_address,
            router_variant=cfg.router_variant,
            request_proxies=proxies,
        )

        if src_symbol == "ETH":
            pair_fw = _find_tokens_for_direction(pool_fw, "ETH", "USDC.E")
            if not pair_fw:
                logger.warning("[PAIR] wallet=%s invalid pool direction ETH>USDC.E", wallet)
                failed_wallets += 1
            else:
                usdc_token = pair_fw[1]
                before_usdc = exec_client.get_erc20_balance(usdc_token.address, usdc_token.decimals)
                ok_fw = _execute_trade_for_pair(
                    cfg=cfg,
                    logger=logger,
                    state=state,
                    exec_client=exec_client,
                    pool=pool_fw,
                    symbol_in="ETH",
                    symbol_out="USDC.E",
                    trade_amount_expr=amount_expr,
                    eth_price=eth_price,
                    label=f"PAIR {wallet} ETH>USDC.E",
                    bypass_risk_checks=True,
                    allow_no_quoter_execution=True,
                    wait_for_pre_tx=True,
                )
                if not ok_fw:
                    failed_wallets += 1
                    continue
                if ok_fw and state.last_tx_hash:
                    _wait_tx_receipt(exec_client, state.last_tx_hash, timeout_sec=180)
                    _sleep_between_pair_swaps()
                after_usdc = exec_client.get_erc20_balance(usdc_token.address, usdc_token.decimals)
                delta_usdc = after_usdc - before_usdc
                if delta_usdc <= 0:
                    logger.warning("[PAIR] wallet=%s no received USDC.E for reverse swap", wallet)
                    failed_wallets += 1
                else:
                    ok_bw = _execute_trade_for_pair(
                        cfg=cfg,
                        logger=logger,
                        state=state,
                        exec_client=exec_client,
                        pool=pool_bw,
                        symbol_in="USDC.E",
                        symbol_out="ETH",
                        trade_amount_expr=_format_decimal_plain(delta_usdc),
                        eth_price=eth_price,
                        label=f"PAIR {wallet} USDC.E>ETH",
                        bypass_risk_checks=True,
                        allow_no_quoter_execution=True,
                        wait_for_pre_tx=True,
                    )
                    if ok_bw:
                        success_wallets += 1
                    else:
                        failed_wallets += 1
        else:
            pair_fw = _find_tokens_for_direction(pool_fw, "USDC.E", "ETH")
            if not pair_fw:
                logger.warning("[PAIR] wallet=%s invalid pool direction USDC.E>ETH", wallet)
                failed_wallets += 1
            else:
                weth_token = pair_fw[1]
                before_weth = exec_client.get_erc20_balance(weth_token.address, weth_token.decimals)
                ok_fw = _execute_trade_for_pair(
                    cfg=cfg,
                    logger=logger,
                    state=state,
                    exec_client=exec_client,
                    pool=pool_fw,
                    symbol_in="USDC.E",
                    symbol_out="ETH",
                    trade_amount_expr=amount_expr,
                    eth_price=eth_price,
                    label=f"PAIR {wallet} USDC.E>ETH",
                    bypass_risk_checks=True,
                    allow_no_quoter_execution=True,
                    wait_for_pre_tx=True,
                )
                if not ok_fw:
                    failed_wallets += 1
                    continue
                if ok_fw and state.last_tx_hash:
                    _wait_tx_receipt(exec_client, state.last_tx_hash, timeout_sec=180)
                    _sleep_between_pair_swaps()
                after_weth = exec_client.get_erc20_balance(weth_token.address, weth_token.decimals)
                delta_weth = after_weth - before_weth
                if delta_weth <= 0:
                    logger.warning("[PAIR] wallet=%s no received ETH/WETH for reverse swap", wallet)
                    failed_wallets += 1
                else:
                    ok_bw = _execute_trade_for_pair(
                        cfg=cfg,
                        logger=logger,
                        state=state,
                        exec_client=exec_client,
                        pool=pool_bw,
                        symbol_in="ETH",
                        symbol_out="USDC.E",
                        trade_amount_expr=_format_decimal_plain(delta_weth),
                        eth_price=eth_price,
                        label=f"PAIR {wallet} ETH>USDC.E",
                        bypass_risk_checks=True,
                        allow_no_quoter_execution=True,
                        wait_for_pre_tx=True,
                    )
                    if ok_bw:
                        success_wallets += 1
                    else:
                        failed_wallets += 1

        if idx < len(wallet_key_records) - 1 and cfg.wallet_delay_max_sec > 0:
            delay_sec = random.uniform(cfg.wallet_delay_min_sec, cfg.wallet_delay_max_sec)
            logger.info("[PAIR] delay before next wallet: %.2f sec", delay_sec)
            time.sleep(delay_sec)
    _print_mode_summary("PAIR", len(wallet_key_records), success_wallets, failed_wallets, skipped_wallets)


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
    print("6) Exit")
    return input("Select [1-6]: ").strip()


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
    logger.info("Exit selected.")


if __name__ == "__main__":
    main()
