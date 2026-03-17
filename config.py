from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Set

from dotenv import load_dotenv


DOMA_POSITION_MANAGER_ADDRESS = "0xce126ca6acebbdce95d7b8a3ce637951640811e0"
DOMA_SWAP_ROUTER_ADDRESS = "0x50cdfe221f0b478b7f58319d7b42cf61e5d904a9"
DOMA_QUOTER_ADDRESS = "0x2023adf9ff50219c34989baabd76a0f5cfe432f4"


load_dotenv()


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


def _read_first_nonempty_line(path: Path) -> str:
    lines = _read_nonempty_lines(path)
    return lines[0] if lines else ""


def _parse_csv_to_set(value: str) -> Set[str]:
    return {x.strip().lower() for x in value.split(",") if x.strip()}


def _normalize_proxy(value: str) -> str:
    v = (value or "").strip()
    if not v:
        return ""
    if "://" not in v:
        return f"http://{v}"
    return v


def _is_valid_evm_address(addr: str) -> bool:
    return bool(re.fullmatch(r"0x[a-fA-F0-9]{40}", (addr or "").strip()))


@dataclass
class BotConfig:
    # Network
    rpc_url: str = os.getenv("RPC_URL", "https://rpc.doma.xyz/")
    subgraph_url: str = os.getenv(
        "SUBGRAPH_URL",
        "https://graph.doma.xyz/subgraphs/name/uniswap-v3-doma-mainnet",
    )
    chain_id: int = int(os.getenv("CHAIN_ID", "97477"))

    # Files
    keys_file: Path = Path(os.getenv("KEYS_FILE", "keys.txt"))
    api_keys_file: Path = Path(os.getenv("API_KEYS_FILE", "api_keys.txt"))
    wallets_file: Path = Path(os.getenv("WALLETS_FILE", "wallets.txt"))
    proxy_file: Path = Path(os.getenv("PROXY_FILE", "proxies.txt"))
    contracts_file: Path = Path(os.getenv("CONTRACTS_FILE", "contracts.json"))
    allowlist_pools_file: Path = Path(os.getenv("ALLOWLIST_POOLS_FILE", "allowlist_pools.txt"))
    state_file: Path = Path(os.getenv("STATE_FILE", "state.json"))
    stop_file: Path = Path(os.getenv("STOP_FILE", "STOP"))
    log_file: Path = Path(os.getenv("LOG_FILE", "bot.log"))
    trades_csv_file: Path = Path(os.getenv("TRADES_CSV_FILE", "trades.csv"))
    points_csv_file: Path = Path(os.getenv("POINTS_CSV_FILE", "points.csv"))
    replay_csv_file: Path = Path(os.getenv("REPLAY_CSV_FILE", "trades.csv"))

    # Account
    account_address: str = os.getenv("ACCOUNT_ADDRESS", "").lower()
    private_key: str = os.getenv("PRIVATE_KEY", "")

    # Proxy
    http_proxy: str = os.getenv("HTTP_PROXY", "")
    https_proxy: str = os.getenv("HTTPS_PROXY", "")
    proxy_candidates: List[str] = field(default_factory=list)

    # Execution mode
    dry_run: bool = os.getenv("DRY_RUN", "true").lower() == "true"
    enable_execution: bool = os.getenv("ENABLE_EXECUTION", "false").lower() == "true"
    require_live_confirmation: bool = os.getenv("REQUIRE_LIVE_CONFIRMATION", "true").lower() == "true"
    live_confirm_phrase: str = os.getenv("LIVE_CONFIRM_PHRASE", "I UNDERSTAND LIVE TRADING RISKS")
    paper_mode: bool = os.getenv("PAPER_MODE", "true").lower() == "true"
    paper_replay_mode: bool = os.getenv("PAPER_REPLAY_MODE", "false").lower() == "true"
    loop_interval_sec: int = int(os.getenv("LOOP_INTERVAL_SEC", "60"))

    # Router / tokens
    router_address: str = os.getenv("ROUTER_ADDRESS", "").lower()
    quoter_address: str = os.getenv("QUOTER_ADDRESS", "").lower()
    position_manager_address: str = os.getenv(
        "POSITION_MANAGER_ADDRESS",
        DOMA_POSITION_MANAGER_ADDRESS,
    ).lower()
    router_variant: str = os.getenv("ROUTER_VARIANT", "with_deadline")
    default_fee_tier: int = int(os.getenv("DEFAULT_FEE_TIER", "500"))
    token_address_overrides: Dict[str, str] = field(default_factory=dict)

    # Pair filters
    allowed_symbols: List[str] = field(
        default_factory=lambda: [
            s.strip().upper()
            for s in os.getenv("ALLOWED_SYMBOLS", "USDC.E,WETH,DOMA").split(",")
            if s.strip()
        ]
    )
    allowed_fee_tiers: Set[int] = field(
        default_factory=lambda: {int(x.strip()) for x in os.getenv("ALLOWED_FEE_TIERS", "500,3000").split(",") if x.strip()}
    )
    allowlist_pool_addresses: Set[str] = field(default_factory=set)

    # Amounts
    trade_amount: str = os.getenv("TRADE_AMOUNT", "")
    scoring_trade_usd: Decimal = Decimal(os.getenv("SCORING_TRADE_USD", "20"))
    run_bootstrap_swaps: bool = os.getenv("RUN_BOOTSTRAP_SWAPS", "true").lower() == "true"
    bootstrap_swaps: List[str] = field(
        default_factory=lambda: [
            s.strip().upper()
            for s in os.getenv("BOOTSTRAP_SWAPS", "ETH>USDC.E,USDC.E>ETH").split(",")
            if s.strip()
        ]
    )
    bootstrap_trade_amount: str = os.getenv("BOOTSTRAP_TRADE_AMOUNT", "")

    # Risk
    min_pool_tvl_usd: Decimal = Decimal(os.getenv("MIN_POOL_TVL_USD", "50000"))
    min_pool_volume_24h_usd: Decimal = Decimal(os.getenv("MIN_POOL_VOLUME_24H_USD", "10000"))
    max_fee_bps: int = int(os.getenv("MAX_FEE_BPS", "100"))
    max_slippage_bps: int = int(os.getenv("MAX_SLIPPAGE_BPS", "80"))
    max_daily_volume_usd: Decimal = Decimal(os.getenv("MAX_DAILY_VOLUME_USD", "100"))
    min_wallet_balance_usd: Decimal = Decimal(os.getenv("MIN_WALLET_BALANCE_USD", "15"))
    max_gas_gwei: Decimal = Decimal(os.getenv("MAX_GAS_GWEI", "2.5"))
    max_tx_fee_usd: Decimal = Decimal(os.getenv("MAX_TX_FEE_USD", "2"))
    skip_if_basefee_high: bool = os.getenv("SKIP_IF_BASEFEE_HIGH", "true").lower() == "true"

    # Bridge (Relay)
    bridge_enabled: bool = os.getenv("BRIDGE_ENABLED", "false").lower() == "true"
    bridge_interval_sec: int = int(os.getenv("BRIDGE_INTERVAL_SEC", "900"))
    bridge_tasks: List[str] = field(
        default_factory=lambda: [
            s.strip()
            for s in os.getenv(
                "BRIDGE_TASKS",
                "base>doma:ETH>USDC.E:5usd;base>doma:USDC>USDC.E:5usd;"
                "optimism>doma:ETH>USDC.E:5usd;arbitrum>doma:ETH>USDC.E:5usd;"
                "doma>base:USDC.E>USDC:5usd;doma>optimism:USDC.E>USDC:5usd;doma>arbitrum:USDC.E>USDC:5usd",
            ).split(";")
            if s.strip()
        ]
    )

    # Leaderboard / points
    points_check_enabled: bool = os.getenv("POINTS_CHECK_ENABLED", "true").lower() == "true"
    points_check_interval_sec: int = int(os.getenv("POINTS_CHECK_INTERVAL_SEC", "600"))
    doma_api_url: str = os.getenv("DOMA_API_URL", "https://api.doma.xyz/graphql")
    doma_api_key: str = os.getenv("DOMA_API_KEY", "")
    doma_api_keys: List[str] = field(default_factory=list)
    file_api_keys: List[str] = field(default_factory=list)
    file_proxies: List[str] = field(default_factory=list)
    points_wallets: List[str] = field(default_factory=list)
    leaderboard_rank_by: str = os.getenv("LEADERBOARD_RANK_BY", "POINTS")
    csv_delimiter: str = os.getenv("CSV_DELIMITER", ";")
    wallet_delay_min_sec: float = float(os.getenv("WALLET_DELAY_MIN_SEC", "5"))
    wallet_delay_max_sec: float = float(os.getenv("WALLET_DELAY_MAX_SEC", "20"))

    def __post_init__(self) -> None:
        if not self.private_key:
            self.private_key = _read_first_nonempty_line(self.keys_file)
        file_api_keys = _read_nonempty_lines(self.api_keys_file)
        self.file_api_keys = file_api_keys
        env_key = self.doma_api_key.strip()
        merged_keys: List[str] = []
        if env_key:
            merged_keys.append(env_key)
        for key in file_api_keys:
            k = key.strip()
            if k and k not in merged_keys:
                merged_keys.append(k)
        self.doma_api_keys = merged_keys
        if merged_keys:
            self.doma_api_key = merged_keys[0]

        wallets = [w.strip() for w in _read_nonempty_lines(self.wallets_file) if _is_valid_evm_address(w.strip())]
        env_account = (self.account_address or "").strip()
        if _is_valid_evm_address(env_account):
            selected_account = env_account
        elif wallets:
            # Primary account for swap/bridge is taken from wallets.txt first valid line.
            selected_account = wallets[0]
        else:
            selected_account = ""
        self.account_address = selected_account.lower()
        if _is_valid_evm_address(self.account_address):
            wallets = [w for w in wallets if w.lower() != self.account_address]
            wallets.insert(0, self.account_address)

        alt = os.getenv("ALT_ACCOUNT_ADDRESS", "").strip()
        if _is_valid_evm_address(alt) and alt not in wallets:
            wallets.append(alt)
        self.points_wallets = wallets if wallets else ([self.account_address] if _is_valid_evm_address(self.account_address) else [])

        # Legacy env compatibility
        if not self.trade_amount:
            legacy = os.getenv("TRADE_SIZE_USD", "").strip()
            self.trade_amount = f"{legacy}usd" if legacy else "20usd"
        if not self.bootstrap_trade_amount:
            legacy_boot = os.getenv("BOOTSTRAP_TRADE_USD", "").strip()
            self.bootstrap_trade_amount = f"{legacy_boot}usd" if legacy_boot else "5usd"

        # Load proxy candidates from file.
        file_proxies = [_normalize_proxy(x) for x in _read_nonempty_lines(self.proxy_file)]
        self.file_proxies = file_proxies
        if file_proxies:
            # Per user request: always use first line only.
            self.proxy_candidates = [file_proxies[0]]
        elif self.http_proxy or self.https_proxy:
            self.proxy_candidates = [_normalize_proxy(self.http_proxy or self.https_proxy)]

        # Default active proxy from first candidate.
        if not self.http_proxy and self.proxy_candidates:
            self.http_proxy = self.proxy_candidates[0]
        if not self.https_proxy and self.proxy_candidates:
            self.https_proxy = self.proxy_candidates[0]

        # Pool allowlist
        self.allowlist_pool_addresses = set(_read_nonempty_lines(self.allowlist_pools_file))
        env_pool_allow = _parse_csv_to_set(os.getenv("ALLOWLIST_POOLS", ""))
        self.allowlist_pool_addresses.update(env_pool_allow)

        # Optional manual contracts file (highest priority).
        if self.contracts_file.exists():
            raw = json.loads(self.contracts_file.read_text(encoding="utf-8-sig"))
            self.router_address = str(raw.get("router_address", self.router_address)).lower()
            self.quoter_address = str(raw.get("quoter_address", self.quoter_address)).lower()
            self.position_manager_address = str(
                raw.get("position_manager_address", self.position_manager_address)
            ).lower()
            self.router_variant = str(raw.get("router_variant", self.router_variant))
            self.default_fee_tier = int(raw.get("default_fee_tier", self.default_fee_tier))

            tokens = raw.get("tokens", {})
            self.token_address_overrides = {str(k).upper(): str(v).lower() for k, v in tokens.items()}

            pairs = raw.get("bootstrap_swaps", [])
            if pairs:
                self.bootstrap_swaps = [str(x).upper() for x in pairs if str(x).strip()]

            allow_pools = raw.get("allowlist_pools", [])
            if allow_pools:
                self.allowlist_pool_addresses.update({str(x).lower() for x in allow_pools})

        # Fallback for Doma mainnet if position manager is not set explicitly.
        if (
            self.chain_id == 97477
            and (
                not self.position_manager_address
                or self.position_manager_address == "0x0000000000000000000000000000000000000000"
            )
        ):
            self.position_manager_address = DOMA_POSITION_MANAGER_ADDRESS

        # Fallback contracts for Doma mainnet (used when contracts.json/.env keep zero addresses).
        if self.chain_id == 97477:
            if not self.router_address or self.router_address == "0x0000000000000000000000000000000000000000":
                self.router_address = DOMA_SWAP_ROUTER_ADDRESS
            if not self.quoter_address or self.quoter_address == "0x0000000000000000000000000000000000000000":
                self.quoter_address = DOMA_QUOTER_ADDRESS
            # Doma mainnet uses SwapRouter02-compatible exactInputSingle params (no deadline).
            if (self.router_variant or "").strip().lower() == "with_deadline":
                self.router_variant = "no_deadline"

        # Ensure delay range is valid.
        if self.wallet_delay_min_sec < 0:
            self.wallet_delay_min_sec = 0.0
        if self.wallet_delay_max_sec < self.wallet_delay_min_sec:
            self.wallet_delay_max_sec = self.wallet_delay_min_sec

