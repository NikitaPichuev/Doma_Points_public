from __future__ import annotations

import csv
import json
import logging
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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
from relay_bridge import run_bridge_tasks
from strategy import StrategyEngine


@dataclass
class BotState:
    day_utc: str
    daily_volume_usd: Decimal
    last_tx_hash: str = ""
    bootstrap_completed: List[str] = field(default_factory=list)
    last_points_check_ts: int = 0
    last_bridge_ts: int = 0

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
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    return BotState(
        day_utc=raw["day_utc"],
        daily_volume_usd=Decimal(raw["daily_volume_usd"]),
        last_tx_hash=raw.get("last_tx_hash", ""),
        bootstrap_completed=raw.get("bootstrap_completed", []),
        last_points_check_ts=int(raw.get("last_points_check_ts", 0)),
        last_bridge_ts=int(raw.get("last_bridge_ts", 0)),
    )


def save_state(path: Path, state: BotState) -> None:
    raw = {
        "day_utc": state.day_utc,
        "daily_volume_usd": str(state.daily_volume_usd),
        "last_tx_hash": state.last_tx_hash,
        "bootstrap_completed": state.bootstrap_completed,
        "last_points_check_ts": state.last_points_check_ts,
        "last_bridge_ts": state.last_bridge_ts,
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


def _find_best_pool_for_symbols(cfg: BotConfig, pools: List[Pool], symbol_in: str, symbol_out: str) -> Optional[Pool]:
    s_in = canonical_symbol(symbol_in)
    s_out = canonical_symbol(symbol_out)
    cands = [
        p
        for p in pools
        if {p.token0.symbol, p.token1.symbol} == {s_in, s_out}
        and p.tvl_usd >= cfg.min_pool_tvl_usd
        and p.volume_24h_usd >= cfg.min_pool_volume_24h_usd
    ]
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
) -> bool:
    pair = _find_tokens_for_direction(pool, symbol_in, symbol_out)
    if not pair:
        logger.warning("[%s] Direction %s>%s not found in selected pool.", label, symbol_in, symbol_out)
        return False
    token_in, token_out = pair

    token_in_usd = pick_token_usd_price(token_in, eth_price)
    if token_in_usd <= 0:
        logger.warning("[%s] Unknown token USD price for %s.", label, token_in.symbol)
        return False

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
    block_reason = risk_checks(cfg, state, trade_usd, impact, quote_out_dec, expected_out_usd)

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

    if quote_out_raw is None:
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

    min_out_raw = int(quote_out_raw * (10_000 - cfg.max_slippage_bps) / 10_000)
    approve_hash = exec_client.ensure_allowance(token_in.address, amount_in_raw)
    if approve_hash:
        logger.info("[%s] Approve tx sent: %s", label, approve_hash)

    tx_hash = exec_client.execute_swap_exact_input_single(
        token_in=token_in.address,
        token_out=token_out.address,
        fee_tier=pool.fee_tier,
        amount_in_raw=amount_in_raw,
        min_amount_out_raw=min_out_raw,
        recipient=cfg.account_address,
        ttl_sec=180,
    )
    state.daily_volume_usd += trade_usd
    state.last_tx_hash = tx_hash
    log_trade(cfg, "EXECUTED", label, pool, token_in, token_out, amount_in_dec, quote_out_dec, trade_usd, expected_out_usd, tx_hash, "")
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
    if not cfg.account_address:
        raise ValueError("ACCOUNT_ADDRESS is required")
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


def run_bridge_once(cfg: BotConfig, logger: logging.Logger, state: BotState) -> None:
    proxies, subgraph, _, _ = build_clients(cfg, state, create_exec=False)
    eth_price = subgraph.fetch_eth_price_usd()
    if eth_price <= 0:
        raise RuntimeError("Failed to resolve ETH/USD for bridge")
    run_bridge_tasks(
        cfg=cfg,
        logger=logger,
        wallet=cfg.account_address,
        private_key=cfg.private_key,
        tasks=cfg.bridge_tasks,
        proxies=proxies,
        eth_price_usd=eth_price,
    )
    state.last_bridge_ts = current_ts()


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
    print("1) Swap")
    print("2) Bridge")
    print("3) Check points")
    print("4) Exit")
    return input("Select [1-4]: ").strip()


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
        run_swap_loop(cfg, logger, state)
        return
    if choice == "2":
        validate_config(cfg)
        try:
            run_bridge_once(cfg, logger, state)
            save_state(cfg.state_file, state)
        except Exception as exc:
            logger.exception("Bridge mode failed: %s", exc)
        return
    if choice == "3":
        run_points_once(cfg, logger, state)
        save_state(cfg.state_file, state)
        return
    logger.info("Exit selected.")


if __name__ == "__main__":
    main()

