from __future__ import annotations

import time
import warnings
from dataclasses import dataclass
from decimal import Decimal, getcontext
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests
from web3 import Web3


getcontext().prec = 50

PUBLIC_DOMA_API_KEY = "v1.c6e3f41019fb97237b7f192d49adb2ae464f2ba7ca6c0737fd6eab71ee01d1d4"
DOMA_INTERFACE_QUOTE_URL = "https://pqzyj9pnj7.execute-api.us-west-1.amazonaws.com/prod/quote"
DOMA_EXPLORER_API_URL = "https://explorer.doma.xyz/api/v2"
DOMA_NATIVE_TOKEN_SENTINEL = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
DOMA_INTERFACE_PORTION_BIPS = 10
DOMA_INTERFACE_PORTION_RECIPIENT = "0x582366f5f9C5ae5C658c30758dd24d57Ce4F111a"
PERMIT2_ADDRESS = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
MAX_UINT256 = (1 << 256) - 1
MAX_UINT160 = (1 << 160) - 1
MAX_UINT48 = (1 << 48) - 1
DOMA_FRACTIONALIZATION_ADDRESS = "0xd00000000004f450f1438cfA436587d8f8A55A29"
PROXY_DOMA_RECORD_ADDRESS = "0xd0000000000067CB44aE7b6aC3AB5764dE20A3E2"


ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [
            {"name": "_owner", "type": "address"},
            {"name": "_spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": False,
        "inputs": [
            {"name": "_spender", "type": "address"},
            {"name": "_value", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
]

LAUNCHPAD_ABI = [
    {
        "inputs": [
            {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
            {"internalType": "uint256", "name": "minAmountOut", "type": "uint256"},
        ],
        "name": "buy",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
            {"internalType": "uint256", "name": "minAmountOut", "type": "uint256"},
        ],
        "name": "sell",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]

PROXY_DOMA_RECORD_ABI = [
    {
        "inputs": [
            {"internalType": "uint256", "name": "tokenId", "type": "uint256"},
            {"internalType": "string", "name": "targetChainId", "type": "string"},
            {"internalType": "string", "name": "targetOwnerAddress", "type": "string"},
        ],
        "name": "bridge",
        "outputs": [],
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "bytes32", "name": "operation", "type": "bytes32"},
        ],
        "name": "getOperationFeeInNative",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "string", "name": "targetChainId", "type": "string"},
        ],
        "name": "getBridgeFeeInNative",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "string", "name": "targetChainId", "type": "string"},
        ],
        "name": "isTargetChainSupported",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "string", "name": "label", "type": "string"}],
        "name": "isLabelForbidden",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "uint256", "name": "subdomainId", "type": "uint256"},
            {"internalType": "string", "name": "host", "type": "string"},
            {"internalType": "string", "name": "recordType", "type": "string"},
            {"internalType": "uint32", "name": "ttl", "type": "uint32"},
            {"internalType": "string[]", "name": "records", "type": "string[]"},
        ],
        "name": "setDNSRRSet",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]

DOMA_FRACTIONALIZATION_SUBDOMAIN_ABI = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "internalType": "uint256", "name": "subdomainId", "type": "uint256"},
            {"indexed": True, "internalType": "address", "name": "fractionalToken", "type": "address"},
            {"indexed": True, "internalType": "address", "name": "staker", "type": "address"},
            {"indexed": False, "internalType": "string", "name": "label", "type": "string"},
            {"indexed": False, "internalType": "uint256", "name": "amount", "type": "uint256"},
        ],
        "name": "SubdomainStaked",
        "type": "event",
    },
    {
        "inputs": [{"internalType": "address", "name": "fractionalToken", "type": "address"}],
        "name": "getSubdomainStakingPrices",
        "outputs": [{"internalType": "uint256[]", "name": "prices", "type": "uint256[]"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "fractionalToken", "type": "address"}],
        "name": "isSubdomainMintingAllowed",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "fractionalToken", "type": "address"},
            {"internalType": "string", "name": "label", "type": "string"},
        ],
        "name": "isSubdomainTaken",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {
                "components": [
                    {"internalType": "address", "name": "fractionalToken", "type": "address"},
                    {"internalType": "string", "name": "label", "type": "string"},
                    {"internalType": "address", "name": "owner", "type": "address"},
                    {"internalType": "uint256", "name": "expiration", "type": "uint256"},
                ],
                "internalType": "struct FractionalStakingSubdomainVoucher",
                "name": "voucher",
                "type": "tuple",
            },
            {"internalType": "bytes", "name": "signature", "type": "bytes"},
            {"internalType": "string[]", "name": "records", "type": "string[]"},
        ],
        "name": "stakeForSubdomain",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "uint256", "name": "subdomainId", "type": "uint256"}],
        "name": "unstakeSubdomain",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]

WETH_ABI = [
    {
        "constant": False,
        "inputs": [],
        "name": "deposit",
        "outputs": [],
        "payable": True,
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "constant": False,
        "inputs": [{"name": "wad", "type": "uint256"}],
        "name": "withdraw",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]

QUOTER_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "tokenIn", "type": "address"},
            {"internalType": "address", "name": "tokenOut", "type": "address"},
            {"internalType": "uint24", "name": "fee", "type": "uint24"},
            {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
            {"internalType": "uint160", "name": "sqrtPriceLimitX96", "type": "uint160"},
        ],
        "name": "quoteExactInputSingle",
        "outputs": [{"internalType": "uint256", "name": "amountOut", "type": "uint256"}],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]

PERMIT2_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "owner", "type": "address"},
            {"internalType": "address", "name": "token", "type": "address"},
            {"internalType": "address", "name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [
            {"internalType": "uint160", "name": "amount", "type": "uint160"},
            {"internalType": "uint48", "name": "expiration", "type": "uint48"},
            {"internalType": "uint48", "name": "nonce", "type": "uint48"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "token", "type": "address"},
            {"internalType": "address", "name": "spender", "type": "address"},
            {"internalType": "uint160", "name": "amount", "type": "uint160"},
            {"internalType": "uint48", "name": "expiration", "type": "uint48"},
        ],
        "name": "approve",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]

ROUTER_ABI_WITH_DEADLINE = [
    {
        "inputs": [
            {
                "components": [
                    {"internalType": "address", "name": "tokenIn", "type": "address"},
                    {"internalType": "address", "name": "tokenOut", "type": "address"},
                    {"internalType": "uint24", "name": "fee", "type": "uint24"},
                    {"internalType": "address", "name": "recipient", "type": "address"},
                    {"internalType": "uint256", "name": "deadline", "type": "uint256"},
                    {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
                    {"internalType": "uint256", "name": "amountOutMinimum", "type": "uint256"},
                    {"internalType": "uint160", "name": "sqrtPriceLimitX96", "type": "uint160"},
                ],
                "internalType": "struct ISwapRouter.ExactInputSingleParams",
                "name": "params",
                "type": "tuple",
            }
        ],
        "name": "exactInputSingle",
        "outputs": [{"internalType": "uint256", "name": "amountOut", "type": "uint256"}],
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "inputs": [
            {
                "components": [
                    {"internalType": "bytes", "name": "path", "type": "bytes"},
                    {"internalType": "address", "name": "recipient", "type": "address"},
                    {"internalType": "uint256", "name": "deadline", "type": "uint256"},
                    {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
                    {"internalType": "uint256", "name": "amountOutMinimum", "type": "uint256"},
                ],
                "internalType": "struct ISwapRouter.ExactInputParams",
                "name": "params",
                "type": "tuple",
            }
        ],
        "name": "exactInput",
        "outputs": [{"internalType": "uint256", "name": "amountOut", "type": "uint256"}],
        "stateMutability": "payable",
        "type": "function",
    },
]

ROUTER_ABI_NO_DEADLINE = [
    {
        "inputs": [
            {
                "components": [
                    {"internalType": "address", "name": "tokenIn", "type": "address"},
                    {"internalType": "address", "name": "tokenOut", "type": "address"},
                    {"internalType": "uint24", "name": "fee", "type": "uint24"},
                    {"internalType": "address", "name": "recipient", "type": "address"},
                    {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
                    {"internalType": "uint256", "name": "amountOutMinimum", "type": "uint256"},
                    {"internalType": "uint160", "name": "sqrtPriceLimitX96", "type": "uint160"},
                ],
                "internalType": "struct IV3SwapRouter.ExactInputSingleParams",
                "name": "params",
                "type": "tuple",
            }
        ],
        "name": "exactInputSingle",
        "outputs": [{"internalType": "uint256", "name": "amountOut", "type": "uint256"}],
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "inputs": [
            {
                "components": [
                    {"internalType": "bytes", "name": "path", "type": "bytes"},
                    {"internalType": "address", "name": "recipient", "type": "address"},
                    {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
                    {"internalType": "uint256", "name": "amountOutMinimum", "type": "uint256"},
                ],
                "internalType": "struct IV3SwapRouter.ExactInputParams",
                "name": "params",
                "type": "tuple",
            }
        ],
        "name": "exactInput",
        "outputs": [{"internalType": "uint256", "name": "amountOut", "type": "uint256"}],
        "stateMutability": "payable",
        "type": "function",
    },
]


@dataclass
class Token:
    address: str
    symbol: str
    decimals: int
    derived_eth: Decimal


@dataclass
class Pool:
    address: str
    fee_tier: int
    tvl_usd: Decimal
    volume_24h_usd: Decimal
    token0: Token
    token1: Token
    token0_price: Decimal
    token1_price: Decimal


@dataclass
class PointsSnapshot:
    wallet_address: str
    rank: int
    points: Decimal
    trading_volume_usd: Decimal
    liquid_amount_usd: Decimal
    referral_count: int
    total_snapshot_entries: int
    snapshot_date: str


@dataclass
class QuestStatus:
    wallet_address: str
    quest_id: int
    quest_type: str
    description: str
    points_to_award: Decimal
    reset_period: str
    priority: int
    completed: bool
    completed_at: str
    available_at: str


@dataclass
class LaunchpadTokenInfo:
    token_id: int
    name: str
    address: str
    launchpad_address: str
    pool_address: Optional[str]
    status: str
    price_usd: Decimal
    tvl_usd: Decimal
    volume_usd: Decimal
    symbol: str
    decimals: int
    quote_token_address: str
    pool_fee_bps: int
    bonding_curve_model_impl: str
    initial_price: Decimal
    final_price: Decimal


@dataclass
class OwnedDomain:
    name: str
    token_id: str
    token_address: str
    network_id: str
    owner_address: str
    token_type: str
    orderbook_disabled: bool


@dataclass
class StakedSubdomain:
    name: str
    token_id: str
    token_address: str
    network_id: str
    owner_address: str
    fractional_token_address: str
    staked_amount_raw: str
    stake_tx_hash: str
    listed: bool


@dataclass
class DomainListing:
    name: str
    order_id: str
    token_id: str
    token_address: str
    network_id: str
    offerer_address: str
    price_raw: str
    currency_symbol: str
    currency_decimals: int
    expires_at: str


@dataclass
class DomainOfferCandidate:
    name: str
    token_id: str
    token_address: str
    network_id: str
    owner_address: str
    highest_offer_raw: str
    highest_offer_decimals: int
    highest_offer_symbol: str
    active_offers_count: int


@dataclass
class DomainReceivedOffer:
    name: str
    order_id: str
    token_id: str
    token_address: str
    network_id: str
    owner_address: str
    offerer_address: str
    price_raw: str
    currency_symbol: str
    currency_decimals: int
    expires_at: str
    active_offers_count: int


@dataclass
class UniversalRouterQuote:
    to: str
    calldata: str
    value_raw: int
    quote_raw: int
    quote_decimals: Decimal
    gas_use_estimate: int
    gas_use_estimate_usd: Decimal
    price_impact_pct: Decimal
    route_string: str
    quote_id: str


class DomaSubgraphClient:
    def __init__(self, subgraph_url: str, proxies: Optional[Dict[str, str]] = None) -> None:
        self.subgraph_url = subgraph_url
        self.proxies = proxies or None

    def _post(self, query: str, variables: Optional[dict] = None) -> dict:
        payload = {"query": query, "variables": variables or {}}
        resp = requests.post(self.subgraph_url, json=payload, timeout=20, proxies=self.proxies)
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            raise RuntimeError(f"Subgraph error: {data['errors']}")
        return data["data"]

    def fetch_top_pools(self, limit: int = 100) -> List[Pool]:
        query = """
        query TopPools($first: Int!) {
          pools(first: $first, orderBy: totalValueLockedUSD, orderDirection: desc) {
            id
            feeTier
            totalValueLockedUSD
            volumeUSD
            token0Price
            token1Price
            token0 {
              id
              symbol
              decimals
              derivedETH
            }
            token1 {
              id
              symbol
              decimals
              derivedETH
            }
            poolDayData(first: 1, orderBy: date, orderDirection: desc) {
              volumeUSD
            }
          }
        }
        """
        raw = self._post(query, {"first": limit})
        pools: List[Pool] = []
        for item in raw["pools"]:
            d0 = Decimal(item["token0"]["derivedETH"] or "0")
            d1 = Decimal(item["token1"]["derivedETH"] or "0")
            token0 = Token(
                address=item["token0"]["id"].lower(),
                symbol=item["token0"]["symbol"].upper(),
                decimals=int(item["token0"]["decimals"]),
                derived_eth=d0,
            )
            token1 = Token(
                address=item["token1"]["id"].lower(),
                symbol=item["token1"]["symbol"].upper(),
                decimals=int(item["token1"]["decimals"]),
                derived_eth=d1,
            )
            day_data = item.get("poolDayData") or []
            vol_24h = Decimal(day_data[0]["volumeUSD"]) if day_data else Decimal("0")
            pools.append(
                Pool(
                    address=item["id"].lower(),
                    fee_tier=int(item["feeTier"]),
                    tvl_usd=Decimal(item["totalValueLockedUSD"]),
                    volume_24h_usd=vol_24h,
                    token0=token0,
                    token1=token1,
                    token0_price=Decimal(item["token0Price"]),
                    token1_price=Decimal(item["token1Price"]),
                )
            )
        return pools

    def fetch_eth_price_usd(self) -> Decimal:
        # Use WETH/USDC.e as the most liquid local reference when available.
        pools = self.fetch_top_pools(limit=20)
        for p in pools:
            syms = {p.token0.symbol, p.token1.symbol}
            if "WETH" in syms and "USDC.E" in syms:
                if p.token0.symbol == "WETH":
                    return p.token1_price
                return p.token0_price
        return Decimal("0")


class DomaApiClient:
    def __init__(
        self,
        api_url: str,
        api_key: str = "",
        api_keys: Optional[List[str]] = None,
        proxies: Optional[Dict[str, str]] = None,
    ) -> None:
        self.api_url = api_url
        self.api_key = api_key.strip()
        extra = [k.strip() for k in (api_keys or []) if k and k.strip()]
        merged: List[str] = []
        if self.api_key:
            merged.append(self.api_key)
        for key in extra:
            if key not in merged:
                merged.append(key)
        if PUBLIC_DOMA_API_KEY not in merged:
            merged.append(PUBLIC_DOMA_API_KEY)
        self.api_keys = merged
        self.proxies = proxies or None
        self.explorer_api_url = DOMA_EXPLORER_API_URL.rstrip("/")

    def _headers(self, api_key: str = "") -> Dict[str, str]:
        h = {"content-type": "application/json"}
        key = (api_key or "").strip()
        if key:
            h["Api-Key"] = key
            h["x-api-key"] = key
        return h

    @staticmethod
    def _is_retryable_auth_error(errors: object) -> bool:
        text = str(errors).lower()
        return (
            "invalid api key" in text
            or "api key is missing" in text
            or "unauthenticated" in text
            or "unauthorized" in text
            or "429" in text
            or "rate limit" in text
        )

    def _post(self, query: str, variables: Optional[dict] = None) -> dict:
        payload = {"query": query, "variables": variables or {}}
        keys_to_try = self.api_keys if self.api_keys else [""]
        last_error: Optional[Exception] = None
        for i, key in enumerate(keys_to_try):
            has_next = i < len(keys_to_try) - 1
            try:
                resp = requests.post(
                    self.api_url,
                    json=payload,
                    headers=self._headers(key),
                    timeout=20,
                    proxies=self.proxies,
                )
                if resp.status_code in {401, 429} and has_next:
                    continue
                resp.raise_for_status()
                data = resp.json()
                if "errors" in data:
                    if has_next and self._is_retryable_auth_error(data["errors"]):
                        continue
                    raise RuntimeError(f"Doma API error: {data['errors']}")
                return data["data"]
            except Exception as exc:
                last_error = exc
                if has_next:
                    continue
        if last_error is not None:
            raise last_error
        raise RuntimeError("Doma API request failed")

    def _explorer_get(self, path: str, params: Optional[dict] = None) -> dict:
        resp = requests.get(
            f"{self.explorer_api_url}/{path.lstrip('/')}",
            params=params or None,
            timeout=20,
            proxies=self.proxies,
        )
        resp.raise_for_status()
        return resp.json()

    def sign_fractional_staking_subdomain_voucher(
        self,
        fractional_token_address: str,
        label: str,
        owner_address: str,
        chain_id: int = 97477,
    ) -> Tuple[str, Tuple[str, str, str, int], str]:
        query = """
        mutation SignFractionalStakingSubdomainVoucher($input: FractionalStakingSubdomainVoucherInput!) {
          signFractionalStakingSubdomainVoucher(input: $input) {
            contractAddress
            signature
            voucher {
              fractionalToken
              label
              owner
              expiration
            }
          }
        }
        """
        variables = {
            "input": {
                "fractionalToken": f"eip155:{int(chain_id)}:{Web3.to_checksum_address(fractional_token_address)}",
                "label": label.lower().strip(),
                "owner": Web3.to_checksum_address(owner_address),
            }
        }
        data = self._post(query, variables).get("signFractionalStakingSubdomainVoucher") or {}
        voucher = data.get("voucher") or {}
        return (
            Web3.to_checksum_address(str(data.get("contractAddress") or DOMA_FRACTIONALIZATION_ADDRESS)),
            (
                Web3.to_checksum_address(str(voucher.get("fractionalToken") or fractional_token_address)),
                str(voucher.get("label") or label).lower().strip(),
                Web3.to_checksum_address(str(voucher.get("owner") or owner_address)),
                int(voucher.get("expiration") or 0),
            ),
            str(data.get("signature") or ""),
        )

    def fetch_points(self, wallet_address: str, rank_by: str = "POINTS") -> Optional[PointsSnapshot]:
        caip_wallet = f"eip155:97477:{wallet_address}"
        scope = "SEASON" if str(rank_by or "").strip().upper() in {"SEASON", "SEASON_POINTS"} else "WEEKLY"
        query = """
        query Leaderboard($walletAddress: AddressCAIP10!, $scope: LeaderboardScope) {
          leaderboard(walletAddress: $walletAddress, scope: $scope) {
            walletAddress
            rank
            previousDayRank
            points
            weeklyPoints
            seasonPoints
            totalEntries
            weekNumber
            scope
            seasonNumber
            referralCount
            pointsMultiplier
          }
        }
        """
        try:
            data = self._post(query, {"walletAddress": caip_wallet, "scope": scope})
            item = data.get("leaderboard")
            if item:
                weekly_points = Decimal(str(item.get("weeklyPoints") or item.get("points") or "0"))
                season_points = Decimal(str(item.get("seasonPoints") or "0"))
                scope_value = str(item.get("scope") or scope)
                week_number = item.get("weekNumber")
                season_number = item.get("seasonNumber")
                snapshot_label = f"{scope_value}"
                if week_number is not None:
                    snapshot_label += f":week={week_number}"
                if season_number is not None:
                    snapshot_label += f":season={season_number}"
                previous_rank = item.get("previousDayRank")
                if previous_rank is not None:
                    snapshot_label += f":prev_rank={previous_rank}"
                return PointsSnapshot(
                    wallet_address=str(item.get("walletAddress") or wallet_address),
                    rank=int(item.get("rank") or 0),
                    points=weekly_points if scope_value == "WEEKLY" else Decimal(str(item.get("points") or season_points)),
                    trading_volume_usd=season_points,
                    liquid_amount_usd=Decimal(str(item.get("pointsMultiplier") or "0")),
                    referral_count=int(item.get("referralCount") or 0),
                    total_snapshot_entries=int(item.get("totalEntries") or 0),
                    snapshot_date=snapshot_label,
                )
        except Exception:
            pass

        order_by = str(rank_by or "POINTS").strip().upper()
        if order_by not in {"POINTS", "TRADING_VOLUME_USD"}:
            order_by = "POINTS"
        query = """
        query Points($walletAddress: AddressCAIP10!, $orderBy: LegacyLeaderboardOrderBy) {
          legacyLeaderboard(walletAddress: $walletAddress, orderBy: $orderBy) {
            walletAddress
            rank
            points
            tradingVolumeUsd
            referralCount
            totalEntries
          }
        }
        """
        data = self._post(query, {"walletAddress": caip_wallet, "orderBy": order_by})
        item = data.get("legacyLeaderboard")
        if not item:
            return None
        return PointsSnapshot(
            wallet_address=str(item["walletAddress"]),
            rank=int(item["rank"]),
            points=Decimal(str(item["points"])),
            trading_volume_usd=Decimal(str(item["tradingVolumeUsd"])),
            liquid_amount_usd=Decimal("0"),
            referral_count=int(item.get("referralCount") or 0),
            total_snapshot_entries=int(item.get("totalEntries") or 0),
            snapshot_date="",
        )

    def fetch_quests(self, wallet_address: str, chain_id: int = 97477) -> List[QuestStatus]:
        caip_wallet = str(wallet_address or "").strip()
        if not caip_wallet.startswith("eip155:"):
            caip_wallet = f"eip155:{chain_id}:{caip_wallet}"
        query = """
        query Quests($address: AddressCAIP10) {
          quests(address: $address) {
            id
            type
            description
            pointsToAward
            resetPeriod
            priority
            completed
            completedAt
            availableAt
          }
        }
        """
        data = self._post(query, {"address": caip_wallet})
        out: List[QuestStatus] = []
        for item in data.get("quests") or []:
            out.append(
                QuestStatus(
                    wallet_address=caip_wallet,
                    quest_id=int(item.get("id") or 0),
                    quest_type=str(item.get("type") or ""),
                    description=str(item.get("description") or ""),
                    points_to_award=Decimal(str(item.get("pointsToAward") or "0")),
                    reset_period=str(item.get("resetPeriod") or ""),
                    priority=int(item.get("priority") or 0),
                    completed=bool(item.get("completed")),
                    completed_at=str(item.get("completedAt") or ""),
                    available_at=str(item.get("availableAt") or ""),
                )
            )
        return out

    def fetch_fractional_token_by_name(self, name: str) -> Optional[LaunchpadTokenInfo]:
        query = """
        query FractionalTokens(
          $name: String
          $status: FractionalTokenStatus
          $skip: Int
          $take: Int
          $sortBy: FractionalTokensSortBy
          $sortOrder: SortOrderType
          $launchStatus: LaunchStatus
          $tlds: [String!]
        ) {
          fractionalTokens(
            name: $name
            status: $status
            skip: $skip
            take: $take
            sortBy: $sortBy
            sortOrder: $sortOrder
            launchStatus: $launchStatus
            tlds: $tlds
          ) {
            items {
              id
              address
              poolAddress
              launchpadAddress
              status
              priceUsd
              tvlUsd
              volumeUsd
              name
              params {
                quoteToken
                name
                symbol
                decimals
                poolFeeBps
                bondingCurveModelImpl
                initialPrice
                finalPrice
              }
            }
          }
        }
        """
        data = self._post(
            query,
            {
                "name": name.strip().lower(),
                "take": 5,
                "sortBy": "FDV",
                "sortOrder": "DESC",
            },
        )
        items = data.get("fractionalTokens", {}).get("items", [])
        exact_name = name.strip().lower()
        for item in items:
            if str(item.get("name") or "").strip().lower() != exact_name:
                continue
            params = item.get("params") or {}
            launchpad_address = str(item.get("launchpadAddress") or "").strip()
            if not launchpad_address:
                continue
            return LaunchpadTokenInfo(
                token_id=int(item["id"]),
                name=str(item.get("name") or ""),
                address=str(item["address"]).lower(),
                launchpad_address=launchpad_address.lower(),
                pool_address=str(item.get("poolAddress") or "").lower() or None,
                status=str(item.get("status") or ""),
                price_usd=Decimal(str(item.get("priceUsd") or "0")),
                tvl_usd=Decimal(str(item.get("tvlUsd") or "0")),
                volume_usd=Decimal(str(item.get("volumeUsd") or "0")),
                symbol=str(params.get("symbol") or ""),
                decimals=int(params.get("decimals") or 18),
                quote_token_address=str(params.get("quoteToken") or "").lower(),
                pool_fee_bps=int(params.get("poolFeeBps") or 0),
                bonding_curve_model_impl=str(params.get("bondingCurveModelImpl") or "").lower(),
                initial_price=Decimal(str(params.get("initialPrice") or "0")),
                final_price=Decimal(str(params.get("finalPrice") or "0")),
            )
        return None

    def fetch_owned_domains(
        self,
        wallet_address: str,
        chain_id: int = 97477,
        listed: Optional[bool] = None,
        take: int = 100,
        max_pages: int = 50,
    ) -> List[OwnedDomain]:
        caip_wallet = f"eip155:{chain_id}:{wallet_address.lower()}"
        query = """
        query OwnedNames(
          $ownedBy: [AddressCAIP10!]
          $listed: Boolean
          $skip: Int
          $take: Int
          $includeDetokenized: Boolean
        ) {
          names(
            ownedBy: $ownedBy
            listed: $listed
            skip: $skip
            take: $take
            includeDetokenized: $includeDetokenized
          ) {
            items {
              name
              ownershipToken {
                tokenId
                tokenAddress
                networkId
                ownerAddress
                type
                orderbookDisabled
              }
            }
            totalCount
            hasNextPage
          }
        }
        """
        out: List[OwnedDomain] = []
        skip = 0
        page_take = max(1, min(int(take), 500))
        for _ in range(max_pages):
            variables = {
                "ownedBy": [caip_wallet],
                "listed": listed,
                "skip": skip,
                "take": page_take,
                "includeDetokenized": False,
            }
            data = self._post(query, variables)
            page = data.get("names") or {}
            items = page.get("items") or []
            for item in items:
                token = item.get("ownershipToken") or {}
                name = str(item.get("name") or "").strip().lower()
                token_id = str(token.get("tokenId") or "").strip()
                token_address = str(token.get("tokenAddress") or "").strip().lower()
                network_id = str(token.get("networkId") or "").strip()
                if not name or not token_id or not token_address:
                    continue
                out.append(
                    OwnedDomain(
                        name=name,
                        token_id=token_id,
                        token_address=token_address,
                        network_id=network_id,
                        owner_address=str(token.get("ownerAddress") or "").strip().lower(),
                        token_type=str(token.get("type") or ""),
                        orderbook_disabled=bool(token.get("orderbookDisabled") or False),
                    )
                )
            if not page.get("hasNextPage"):
                break
            skip += len(items) if items else page_take
        return out

    def fetch_wallet_staked_subdomains(
        self,
        wallet_address: str,
        chain_id: int = 97477,
        take: int = 100,
        max_pages: int = 20,
    ) -> List[StakedSubdomain]:
        caip_wallet = f"eip155:{chain_id}:{wallet_address.lower()}"
        query = """
        query WalletSubdomains(
          $owner: AddressCAIP10!
          $skip: Int
          $take: Int
        ) {
          subdomains(
            owner: $owner
            skip: $skip
            take: $take
          ) {
            items {
              tokenId
              name
              networkId
              ownerAddress
              tokenAddress
              deletedAt
              listings {
                id
                externalId
              }
              fractionalizationInfo {
                fractionalTokenAddress
                stakedAmount
                stakeTxHash
              }
            }
            hasNextPage
          }
        }
        """
        out: List[StakedSubdomain] = []
        skip = 0
        page_take = max(1, min(int(take), 500))
        for _ in range(max_pages):
            data = self._post(query, {"owner": caip_wallet, "skip": skip, "take": page_take})
            page = data.get("subdomains") or {}
            items = page.get("items") or []
            for item in items:
                info = item.get("fractionalizationInfo") or {}
                if item.get("deletedAt"):
                    continue
                token_id = str(item.get("tokenId") or "").strip()
                name = str(item.get("name") or "").strip().lower()
                token_address = str(item.get("tokenAddress") or "").strip().lower()
                fractional_token_address = str(info.get("fractionalTokenAddress") or "").strip().lower()
                if not token_id or not name or not fractional_token_address:
                    continue
                out.append(
                    StakedSubdomain(
                        name=name,
                        token_id=token_id,
                        token_address=token_address,
                        network_id=str(item.get("networkId") or f"eip155:{chain_id}").strip(),
                        owner_address=str(item.get("ownerAddress") or "").strip().lower(),
                        fractional_token_address=fractional_token_address,
                        staked_amount_raw=str(info.get("stakedAmount") or "0"),
                        stake_tx_hash=str(info.get("stakeTxHash") or ""),
                        listed=bool(item.get("listings") or []),
                    )
                )
            if not page.get("hasNextPage"):
                break
            skip += len(items) if items else page_take
        return out

    def fetch_wallet_domain_listings(
        self,
        wallet_address: str,
        chain_id: int = 97477,
        take: int = 100,
        max_pages: int = 50,
    ) -> List[DomainListing]:
        listed_domains = self.fetch_owned_domains(
            wallet_address=wallet_address,
            chain_id=chain_id,
            listed=True,
            take=take,
            max_pages=max_pages,
        )
        if not listed_domains:
            return []

        query = """
        query Listings(
          $sld: String
          $tlds: [String!]
          $networkIds: [String!]
          $skip: Int
          $take: Int
        ) {
          listings(
            sld: $sld
            tlds: $tlds
            networkIds: $networkIds
            skip: $skip
            take: $take
          ) {
            items {
              id
              externalId
              name
              tokenId
              tokenAddress
              offererAddress
              price
              expiresAt
              currency {
                symbol
                decimals
              }
              chain {
                networkId
              }
            }
            hasNextPage
          }
        }
        """
        wallet_caip = f"eip155:{chain_id}:{wallet_address.lower()}"
        target_network = f"eip155:{chain_id}"
        out: List[DomainListing] = []
        seen: set[str] = set()

        for domain in listed_domains:
            parts = domain.name.split(".", 1)
            if len(parts) != 2:
                continue
            sld, tld = parts
            skip = 0
            page_take = max(1, min(int(take), 500))
            for _ in range(max_pages):
                data = self._post(
                    query,
                    {
                        "sld": sld,
                        "tlds": [tld],
                        "networkIds": [target_network],
                        "skip": skip,
                        "take": page_take,
                    },
                )
                page = data.get("listings") or {}
                items = page.get("items") or []
                for item in items:
                    name = str(item.get("name") or "").strip().lower()
                    token_id = str(item.get("tokenId") or "").strip()
                    token_address = str(item.get("tokenAddress") or "").strip().lower()
                    offerer = str(item.get("offererAddress") or "").strip().lower()
                    network_id = str(((item.get("chain") or {}).get("networkId")) or "").strip()
                    order_id = str(item.get("externalId") or item.get("id") or "").strip()
                    if name != domain.name.lower():
                        continue
                    if token_id != domain.token_id or token_address != domain.token_address.lower():
                        continue
                    if network_id != target_network or offerer != wallet_caip:
                        continue
                    if not order_id or order_id in seen:
                        continue
                    seen.add(order_id)
                    currency = item.get("currency") or {}
                    out.append(
                        DomainListing(
                            name=name,
                            order_id=order_id,
                            token_id=token_id,
                            token_address=token_address,
                            network_id=network_id,
                            offerer_address=offerer,
                            price_raw=str(item.get("price") or "0"),
                            currency_symbol=str(currency.get("symbol") or ""),
                            currency_decimals=int(currency.get("decimals") or 0),
                            expires_at=str(item.get("expiresAt") or ""),
                        )
                    )
                if not page.get("hasNextPage"):
                    break
                skip += len(items) if items else page_take
        return out

    def fetch_domain_offer_candidates(
        self,
        chain_id: int = 97477,
        take: int = 100,
        max_pages: int = 5,
        min_offer_usd: Optional[Decimal] = None,
    ) -> List[DomainOfferCandidate]:
        query = """
        query OfferCandidates(
          $skip: Int
          $take: Int
          $sortBy: NamesQuerySortBy
          $sortOrder: SortOrderType
          $networkIds: [String!]
          $statuses: [NameOrderbookStatusFilter!]
          $offerMinUsd: Float
        ) {
          names(
            skip: $skip
            take: $take
            sortBy: $sortBy
            sortOrder: $sortOrder
            networkIds: $networkIds
            statuses: $statuses
            offerMinUsd: $offerMinUsd
            includeDetokenized: false
          ) {
            items {
              name
              activeOffersCount
              highestOffer {
                price
                currency {
                  symbol
                  decimals
                }
              }
              ownershipToken {
                tokenId
                tokenAddress
                networkId
                ownerAddress
                orderbookDisabled
              }
            }
            hasNextPage
          }
        }
        """
        target_network = f"eip155:{chain_id}"
        out: List[DomainOfferCandidate] = []
        seen: set[str] = set()
        page_take = max(1, min(int(take), 500))
        for page in range(max_pages):
            data = self._post(
                query,
                {
                    "skip": page * page_take,
                    "take": page_take,
                    "sortBy": "HIGHEST_OFFER_USD",
                    "sortOrder": "ASC",
                    "networkIds": [target_network],
                    "statuses": ["OFFERS_RECEIVED"],
                    "offerMinUsd": float(min_offer_usd) if min_offer_usd is not None else None,
                },
            )
            names_page = data.get("names") or {}
            items = names_page.get("items") or []
            for item in items:
                token = item.get("ownershipToken") or {}
                if bool(token.get("orderbookDisabled") or False):
                    continue
                name = str(item.get("name") or "").strip().lower()
                token_id = str(token.get("tokenId") or "").strip()
                token_address = str(token.get("tokenAddress") or "").strip().lower()
                network_id = str(token.get("networkId") or "").strip()
                owner_address = str(token.get("ownerAddress") or "").strip().lower()
                if not name or not token_id or not token_address or network_id != target_network:
                    continue
                if name in seen:
                    continue
                seen.add(name)
                highest_offer = item.get("highestOffer") or {}
                currency = highest_offer.get("currency") or {}
                out.append(
                    DomainOfferCandidate(
                        name=name,
                        token_id=token_id,
                        token_address=token_address,
                        network_id=network_id,
                        owner_address=owner_address,
                        highest_offer_raw=str(highest_offer.get("price") or "0"),
                        highest_offer_decimals=int(currency.get("decimals") or 6),
                        highest_offer_symbol=str(currency.get("symbol") or ""),
                        active_offers_count=int(item.get("activeOffersCount") or 0),
                    )
                )
            if not names_page.get("hasNextPage"):
                break
        return out

    def fetch_top_pools_by_tvl(self, limit: int = 10, eth_price_usd: Decimal = Decimal("0")) -> List[Pool]:
        query = """
        query ApiTopPools($take: Int!, $sortBy: PoolsSortBy!, $sortOrder: SortOrderType!) {
          pools(take: $take, sortBy: $sortBy, sortOrder: $sortOrder) {
            items {
              address
              feeTier
              tvlUsd
              volume24hUsd
              token0Address
              token1Address
              token0Info {
                symbol
                decimals
                usdExchangeRate
              }
              token1Info {
                symbol
                decimals
                usdExchangeRate
              }
            }
          }
        }
        """
        data = self._post(query, {"take": int(limit), "sortBy": "TVL", "sortOrder": "DESC"})
        items = ((data.get("pools") or {}).get("items")) or []
        pools: List[Pool] = []
        for item in items:
            t0 = item.get("token0Info") or {}
            t1 = item.get("token1Info") or {}
            t0_usd = Decimal(str(t0.get("usdExchangeRate") or "0"))
            t1_usd = Decimal(str(t1.get("usdExchangeRate") or "0"))
            token0 = Token(
                address=str(item.get("token0Address") or "").lower(),
                symbol=canonical_symbol_for_api(str(t0.get("symbol") or "")),
                decimals=int(t0.get("decimals") or 18),
                derived_eth=(t0_usd / eth_price_usd) if t0_usd > 0 and eth_price_usd > 0 else Decimal("0"),
            )
            token1 = Token(
                address=str(item.get("token1Address") or "").lower(),
                symbol=canonical_symbol_for_api(str(t1.get("symbol") or "")),
                decimals=int(t1.get("decimals") or 18),
                derived_eth=(t1_usd / eth_price_usd) if t1_usd > 0 and eth_price_usd > 0 else Decimal("0"),
            )
            if not token0.address or not token1.address:
                continue
            pools.append(
                Pool(
                    address=str(item.get("address") or "").lower(),
                    fee_tier=int(item.get("feeTier") or 0),
                    tvl_usd=Decimal(str(item.get("tvlUsd") or "0")),
                    volume_24h_usd=Decimal(str(item.get("volume24hUsd") or "0")),
                    token0=token0,
                    token1=token1,
                    token0_price=Decimal("0"),
                    token1_price=Decimal("0"),
                )
            )
        return pools

    def fetch_wallet_received_top_offers(
        self,
        wallet_address: str,
        chain_id: int = 97477,
        take: int = 100,
        max_pages: int = 50,
    ) -> List[DomainReceivedOffer]:
        query = """
        query WalletReceivedOffers(
          $ownedBy: [AddressCAIP10!]
          $networkIds: [String!]
          $statuses: [NameOrderbookStatusFilter!]
          $skip: Int
          $take: Int
        ) {
          names(
            ownedBy: $ownedBy
            networkIds: $networkIds
            statuses: $statuses
            skip: $skip
            take: $take
            includeDetokenized: false
          ) {
            items {
              name
              activeOffersCount
              highestOffer {
                id
                externalId
                price
                offererAddress
                expiresAt
                currency {
                  symbol
                  decimals
                }
              }
              ownershipToken {
                tokenId
                tokenAddress
                networkId
                ownerAddress
                orderbookDisabled
              }
            }
            hasNextPage
          }
        }
        """
        wallet_caip = f"eip155:{chain_id}:{wallet_address.lower()}"
        target_network = f"eip155:{chain_id}"
        out: List[DomainReceivedOffer] = []
        seen: set[str] = set()
        page_take = max(1, min(int(take), 500))
        skip = 0
        for _ in range(max_pages):
            data = self._post(
                query,
                {
                    "ownedBy": [wallet_caip],
                    "networkIds": [target_network],
                    "statuses": ["OFFERS_RECEIVED"],
                    "skip": skip,
                    "take": page_take,
                },
            )
            names_page = data.get("names") or {}
            items = names_page.get("items") or []
            for item in items:
                token = item.get("ownershipToken") or {}
                highest_offer = item.get("highestOffer") or {}
                if bool(token.get("orderbookDisabled") or False):
                    continue
                name = str(item.get("name") or "").strip().lower()
                token_id = str(token.get("tokenId") or "").strip()
                token_address = str(token.get("tokenAddress") or "").strip().lower()
                network_id = str(token.get("networkId") or "").strip()
                owner_address = str(token.get("ownerAddress") or "").strip().lower()
                order_id = str(highest_offer.get("externalId") or highest_offer.get("id") or "").strip()
                if not name or not token_id or not token_address or network_id != target_network:
                    continue
                if not order_id or order_id in seen:
                    continue
                seen.add(order_id)
                currency = highest_offer.get("currency") or {}
                out.append(
                    DomainReceivedOffer(
                        name=name,
                        order_id=order_id,
                        token_id=token_id,
                        token_address=token_address,
                        network_id=network_id,
                        owner_address=owner_address,
                        offerer_address=str(highest_offer.get("offererAddress") or "").strip().lower(),
                        price_raw=str(highest_offer.get("price") or "0"),
                        currency_symbol=str(currency.get("symbol") or ""),
                        currency_decimals=int(currency.get("decimals") or 6),
                        expires_at=str(highest_offer.get("expiresAt") or ""),
                        active_offers_count=int(item.get("activeOffersCount") or 0),
                    )
                )
            if not names_page.get("hasNextPage"):
                break
            skip += len(items) if items else page_take
        return out

    def fetch_wallet_fractional_token_volume_usd(
        self,
        wallet_address: str,
        fractional_token_id: int,
        take: int = 100,
        max_pages: int = 20,
        since: Optional[datetime] = None,
    ) -> Decimal:
        caip_wallet = f"eip155:97477:{wallet_address}"
        query = """
        query FractionalTokenSwaps(
          $address: AddressCAIP10!
          $fractionalTokenId: Int!
          $skip: Int
          $take: Int
          $sortOrder: SortOrderType
        ) {
          fractionalTokenSwaps(
            address: $address
            fractionalTokenId: $fractionalTokenId
            skip: $skip
            take: $take
            sortOrder: $sortOrder
          ) {
            items {
              date
              quoteTokenAmount
              priceUsd
              quoteToken {
                decimals
                usdExchangeRate
              }
            }
          }
        }
        """
        total_volume_usd = Decimal("0")
        since_utc = since.astimezone(timezone.utc) if since is not None else None
        for page in range(max_pages):
            data = self._post(
                query,
                {
                    "address": caip_wallet,
                    "fractionalTokenId": int(fractional_token_id),
                    "skip": page * take,
                    "take": take,
                    "sortOrder": "DESC",
                },
            )
            items = data.get("fractionalTokenSwaps", {}).get("items", [])
            if not items:
                break
            reached_older_items = False
            for item in items:
                item_date_raw = str(item.get("date") or "").strip()
                if since_utc is not None and item_date_raw:
                    item_date = datetime.fromisoformat(item_date_raw.replace("Z", "+00:00"))
                    if item_date < since_utc:
                        reached_older_items = True
                        continue
                quote_token = item.get("quoteToken") or {}
                decimals = int(quote_token.get("decimals") or 18)
                usd_rate = Decimal(str(quote_token.get("usdExchangeRate") or "0"))
                quote_amount_raw = abs(int(str(item.get("quoteTokenAmount") or "0")))
                quote_amount = Decimal(quote_amount_raw) / (Decimal(10) ** decimals)
                if usd_rate > 0:
                    total_volume_usd += quote_amount * usd_rate
                    continue
                price_usd = Decimal(str(item.get("priceUsd") or "0"))
                if price_usd > 0:
                    total_volume_usd += quote_amount * price_usd
                else:
                    total_volume_usd += quote_amount
            if len(items) < take or reached_older_items:
                break
        return total_volume_usd

    def fetch_wallet_pool_volume_usd(
        self,
        wallet_address: str,
        pool_address: str,
        pool_addresses: Optional[List[str]] = None,
        tracked_token_symbol: str = "",
        quote_token_symbol: str = "USDC.E",
        take: int = 100,
        max_pages: int = 20,
        since: Optional[datetime] = None,
    ) -> Decimal:
        caip_wallet = f"eip155:97477:{wallet_address}"
        query = """
        query TokenSwaps(
          $buyerAddress: AddressCAIP10!
          $poolAddress: String!
          $skip: Int
          $take: Int
          $sortOrder: SortOrderType
        ) {
          tokenSwaps(
            buyerAddress: $buyerAddress
            poolAddress: $poolAddress
            skip: $skip
            take: $take
            sortOrder: $sortOrder
          ) {
            items {
              amountUsd
              date
              txHash
            }
          }
        }
        """
        total_volume_usd = Decimal("0")
        pool_address_list = [str(addr).strip().lower() for addr in (pool_addresses or [pool_address]) if str(addr).strip()]
        pool_address_list = list(dict.fromkeys(pool_address_list))
        since_utc = since.astimezone(timezone.utc) if since is not None else None
        try:
            for current_pool_address in pool_address_list:
                for page in range(max_pages):
                    data = self._post(
                        query,
                        {
                            "buyerAddress": caip_wallet,
                            "poolAddress": current_pool_address,
                            "skip": page * take,
                            "take": take,
                            "sortOrder": "DESC",
                        },
                    )
                    items = data.get("tokenSwaps", {}).get("items", [])
                    if not items:
                        break
                    reached_older_items = False
                    for item in items:
                        item_date_raw = str(item.get("date") or "").strip()
                        if since_utc is not None and item_date_raw:
                            item_date = datetime.fromisoformat(item_date_raw.replace("Z", "+00:00"))
                            if item_date < since_utc:
                                reached_older_items = True
                                continue
                        amount_usd = Decimal(str(item.get("amountUsd") or "0"))
                        if amount_usd < 0:
                            amount_usd = -amount_usd
                        total_volume_usd += amount_usd
                    if len(items) < take or reached_older_items:
                        break
            return total_volume_usd
        except Exception:
            return self.fetch_wallet_pool_volume_usd_from_explorer(
                wallet_address=wallet_address,
                pool_address=pool_address,
                pool_addresses=pool_address_list,
                tracked_token_symbol=tracked_token_symbol,
                quote_token_symbol=quote_token_symbol,
                max_pages=max_pages,
                since=since,
            )

    def fetch_wallet_pool_volume_usd_from_explorer(
        self,
        wallet_address: str,
        pool_address: str,
        tracked_token_symbol: str = "",
        pool_addresses: Optional[List[str]] = None,
        quote_token_symbol: str = "USDC.E",
        max_pages: int = 20,
        since: Optional[datetime] = None,
    ) -> Decimal:
        wallet_lc = str(wallet_address).strip().lower()
        pool_set = {
            str(addr).strip().lower()
            for addr in (pool_addresses or [pool_address])
            if str(addr).strip()
        }
        tracked_symbol = str(tracked_token_symbol or "").strip().upper()
        quote_symbol = str(quote_token_symbol or "USDC.E").strip().upper()
        since_utc = since.astimezone(timezone.utc) if since is not None else None

        tx_groups: Dict[str, List[dict]] = {}
        next_params: Optional[dict] = None
        reached_older_items = False

        for _page in range(max_pages):
            data = self._explorer_get(
                f"addresses/{wallet_address}/token-transfers",
                params=next_params,
            )
            items = data.get("items", [])
            if not items:
                break
            for item in items:
                item_date_raw = str(item.get("timestamp") or "").strip()
                if since_utc is not None and item_date_raw:
                    item_date = datetime.fromisoformat(item_date_raw.replace("Z", "+00:00"))
                    if item_date < since_utc:
                        reached_older_items = True
                        continue
                tx_hash = str(item.get("transaction_hash") or "").strip().lower()
                if not tx_hash:
                    continue
                tx_groups.setdefault(tx_hash, []).append(item)
            if reached_older_items:
                break
            next_params = data.get("next_page_params")
            if not next_params:
                break

        total_volume_usd = Decimal("0")
        for items in tx_groups.values():
            symbols = {
                str((item.get("token") or {}).get("symbol") or "").strip().upper()
                for item in items
            }
            if quote_symbol not in symbols:
                continue
            tx_touches_pool = any(
                str(((item.get("from") or {}).get("hash")) or "").strip().lower() in pool_set
                or str(((item.get("to") or {}).get("hash")) or "").strip().lower() in pool_set
                for item in items
            )
            if not tx_touches_pool:
                continue
            has_pool_touch = False
            quote_amount = Decimal("0")
            for item in items:
                from_hash = str(((item.get("from") or {}).get("hash")) or "").strip().lower()
                to_hash = str(((item.get("to") or {}).get("hash")) or "").strip().lower()
                token_meta = item.get("token") or {}
                symbol = str(token_meta.get("symbol") or "").strip().upper()
                decimals = int(token_meta.get("decimals") or (item.get("total") or {}).get("decimals") or 18)
                value_raw = int(str((item.get("total") or {}).get("value") or "0"))
                wallet_involved = from_hash == wallet_lc or to_hash == wallet_lc
                counterparty_is_pool = (
                    (from_hash == wallet_lc and to_hash in pool_set)
                    or (from_hash in pool_set and to_hash == wallet_lc)
                )
                if wallet_involved and counterparty_is_pool:
                    has_pool_touch = True
                if wallet_involved and symbol == quote_symbol:
                    quote_amount += Decimal(value_raw) / (Decimal(10) ** decimals)
            if (has_pool_touch or tx_touches_pool) and quote_amount > 0:
                total_volume_usd += quote_amount
        return total_volume_usd

    def fetch_wallet_total_swap_volume_usd_from_explorer(
        self,
        wallet_address: str,
        quote_token_symbol: str = "USDC.E",
        pool_addresses: Optional[List[str]] = None,
        max_pages: int = 80,
        since: Optional[datetime] = None,
    ) -> Decimal:
        wallet_lc = str(wallet_address).strip().lower()
        quote_symbol = str(quote_token_symbol or "USDC.E").strip().upper()
        pool_set = {
            str(addr).strip().lower()
            for addr in (pool_addresses or [])
            if str(addr).strip()
        }
        since_utc = since.astimezone(timezone.utc) if since is not None else None

        tx_groups: Dict[str, List[dict]] = {}
        next_params: Optional[dict] = None
        reached_older_items = False

        for _page in range(max_pages):
            data = self._explorer_get(
                f"addresses/{wallet_address}/token-transfers",
                params=next_params,
            )
            items = data.get("items", [])
            if not items:
                break
            for item in items:
                item_date_raw = str(item.get("timestamp") or "").strip()
                if since_utc is not None and item_date_raw:
                    item_date = datetime.fromisoformat(item_date_raw.replace("Z", "+00:00"))
                    if item_date < since_utc:
                        reached_older_items = True
                        continue
                tx_hash = str(item.get("transaction_hash") or "").strip().lower()
                if not tx_hash:
                    continue
                tx_groups.setdefault(tx_hash, []).append(item)
            if reached_older_items:
                break
            next_params = data.get("next_page_params")
            if not next_params:
                break

        total_volume_usd = Decimal("0")
        for items in tx_groups.values():
            quote_amount = Decimal("0")
            has_quote_wallet_transfer = False
            has_other_wallet_transfer = False
            has_pool_like_quote_transfer = False

            for item in items:
                from_hash = str(((item.get("from") or {}).get("hash")) or "").strip().lower()
                to_hash = str(((item.get("to") or {}).get("hash")) or "").strip().lower()
                token_meta = item.get("token") or {}
                symbol = str(token_meta.get("symbol") or "").strip().upper()
                decimals = int(token_meta.get("decimals") or (item.get("total") or {}).get("decimals") or 18)
                value_raw = int(str((item.get("total") or {}).get("value") or "0"))
                wallet_involved = from_hash == wallet_lc or to_hash == wallet_lc
                if not wallet_involved:
                    continue
                if symbol == quote_symbol:
                    has_quote_wallet_transfer = True
                    quote_amount += Decimal(value_raw) / (Decimal(10) ** decimals)
                    # Direct ETH/WETH <-> USDC.E swaps can appear as a single USDC.E wallet transfer.
                    if from_hash in pool_set and to_hash == wallet_lc:
                        has_pool_like_quote_transfer = True
                    if from_hash == wallet_lc and to_hash in pool_set:
                        has_pool_like_quote_transfer = True
                else:
                    has_other_wallet_transfer = True

            if has_quote_wallet_transfer and quote_amount > 0 and (has_other_wallet_transfer or has_pool_like_quote_transfer):
                total_volume_usd += quote_amount
        return total_volume_usd

    def fetch_wallet_transactions_from_explorer(
        self,
        wallet_address: str,
        max_pages: int = 80,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
    ) -> List[dict]:
        since_utc = since.astimezone(timezone.utc) if since is not None else None
        until_utc = until.astimezone(timezone.utc) if until is not None else None
        out: List[dict] = []
        next_params: Optional[dict] = None

        for _page in range(max_pages):
            data = self._explorer_get(
                f"addresses/{wallet_address}/transactions",
                params=next_params,
            )
            items = data.get("items", [])
            if not items:
                break
            reached_older_items = False
            for item in items:
                item_date_raw = str(item.get("timestamp") or "").strip()
                if (since_utc is not None or until_utc is not None) and item_date_raw:
                    item_date = datetime.fromisoformat(item_date_raw.replace("Z", "+00:00"))
                    if since_utc is not None and item_date < since_utc:
                        reached_older_items = True
                        continue
                    if until_utc is not None and item_date >= until_utc:
                        continue
                out.append(item)
            if reached_older_items:
                break
            next_params = data.get("next_page_params")
            if not next_params:
                break
        return out

    def fetch_wallet_token_transfers_from_explorer(
        self,
        wallet_address: str,
        max_pages: int = 80,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
    ) -> List[dict]:
        since_utc = since.astimezone(timezone.utc) if since is not None else None
        until_utc = until.astimezone(timezone.utc) if until is not None else None
        out: List[dict] = []
        next_params: Optional[dict] = None

        for _page in range(max_pages):
            data = self._explorer_get(
                f"addresses/{wallet_address}/token-transfers",
                params=next_params,
            )
            items = data.get("items", [])
            if not items:
                break
            reached_older_items = False
            for item in items:
                item_date_raw = str(item.get("timestamp") or "").strip()
                if (since_utc is not None or until_utc is not None) and item_date_raw:
                    item_date = datetime.fromisoformat(item_date_raw.replace("Z", "+00:00"))
                    if since_utc is not None and item_date < since_utc:
                        reached_older_items = True
                        continue
                    if until_utc is not None and item_date >= until_utc:
                        continue
                out.append(item)
            if reached_older_items:
                break
            next_params = data.get("next_page_params")
            if not next_params:
                break
        return out

    def fetch_wallet_internal_transactions_from_explorer(
        self,
        wallet_address: str,
        max_pages: int = 80,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
    ) -> List[dict]:
        since_utc = since.astimezone(timezone.utc) if since is not None else None
        until_utc = until.astimezone(timezone.utc) if until is not None else None
        out: List[dict] = []
        next_params: Optional[dict] = None

        for _page in range(max_pages):
            data = self._explorer_get(
                f"addresses/{wallet_address}/internal-transactions",
                params=next_params,
            )
            items = data.get("items", [])
            if not items:
                break
            reached_older_items = False
            for item in items:
                item_date_raw = str(item.get("timestamp") or "").strip()
                if (since_utc is not None or until_utc is not None) and item_date_raw:
                    item_date = datetime.fromisoformat(item_date_raw.replace("Z", "+00:00"))
                    if since_utc is not None and item_date < since_utc:
                        reached_older_items = True
                        continue
                    if until_utc is not None and item_date >= until_utc:
                        continue
                out.append(item)
            if reached_older_items:
                break
            next_params = data.get("next_page_params")
            if not next_params:
                break
        return out

    def fetch_fractional_tokens(self, take: int = 250, max_pages: int = 10) -> List[LaunchpadTokenInfo]:
        query = """
        query FractionalTokens(
          $status: FractionalTokenStatus
          $skip: Int
          $take: Int
          $sortBy: FractionalTokensSortBy
          $sortOrder: SortOrderType
          $launchStatus: LaunchStatus
          $tlds: [String!]
        ) {
          fractionalTokens(
            status: $status
            skip: $skip
            take: $take
            sortBy: $sortBy
            sortOrder: $sortOrder
            launchStatus: $launchStatus
            tlds: $tlds
          ) {
            items {
              id
              address
              poolAddress
              launchpadAddress
              status
              priceUsd
              tvlUsd
              volumeUsd
              name
              params {
                quoteToken
                name
                symbol
                decimals
                poolFeeBps
                bondingCurveModelImpl
                initialPrice
                finalPrice
              }
            }
          }
        }
        """
        out: List[LaunchpadTokenInfo] = []
        seen: set[str] = set()
        for page in range(max_pages):
            data = self._post(
                query,
                {
                    "skip": page * take,
                    "take": take,
                    "sortBy": "FDV",
                    "sortOrder": "DESC",
                },
            )
            items = data.get("fractionalTokens", {}).get("items", [])
            if not items:
                break
            for item in items:
                address = str(item.get("address") or "").strip().lower()
                if not address or address in seen:
                    continue
                seen.add(address)
                params = item.get("params") or {}
                out.append(
                    LaunchpadTokenInfo(
                        token_id=int(item["id"]),
                        name=str(item.get("name") or ""),
                        address=address,
                        launchpad_address=str(item.get("launchpadAddress") or "").strip().lower(),
                        pool_address=str(item.get("poolAddress") or "").strip().lower() or None,
                        status=str(item.get("status") or ""),
                        price_usd=Decimal(str(item.get("priceUsd") or "0")),
                        tvl_usd=Decimal(str(item.get("tvlUsd") or "0")),
                        volume_usd=Decimal(str(item.get("volumeUsd") or "0")),
                        symbol=str(params.get("symbol") or ""),
                        decimals=int(params.get("decimals") or 18),
                        quote_token_address=str(params.get("quoteToken") or "").strip().lower(),
                        pool_fee_bps=int(params.get("poolFeeBps") or 0),
                        bonding_curve_model_impl=str(params.get("bondingCurveModelImpl") or "").strip().lower(),
                        initial_price=Decimal(str(params.get("initialPrice") or "0")),
                        final_price=Decimal(str(params.get("finalPrice") or "0")),
                    )
                )
            if len(items) < take:
                break
        return out

    def fetch_universal_router_quote(
        self,
        token_in_address: str,
        token_out_address: str,
        amount_raw: int,
        chain_id: int,
        trade_type: str = "exactIn",
        slippage_tolerance_pct: Decimal = Decimal("5"),
        portion_bips: int = DOMA_INTERFACE_PORTION_BIPS,
        portion_recipient: str = DOMA_INTERFACE_PORTION_RECIPIENT,
    ) -> UniversalRouterQuote:
        if int(amount_raw) <= 0:
            raise ValueError("amount_raw must be > 0")
        def _quote_addr(addr: str) -> str:
            addr_norm = str(addr).strip()
            if addr_norm.lower() == DOMA_NATIVE_TOKEN_SENTINEL:
                return DOMA_NATIVE_TOKEN_SENTINEL
            return Web3.to_checksum_address(addr_norm)
        params = {
            "tokenInAddress": _quote_addr(token_in_address),
            "tokenInChainId": str(chain_id),
            "tokenOutAddress": _quote_addr(token_out_address),
            "tokenOutChainId": str(chain_id),
            "amount": str(int(amount_raw)),
            "type": trade_type,
            "enableUniversalRouter": "true",
            "slippageTolerance": format(Decimal(slippage_tolerance_pct).normalize(), "f"),
        }
        if portion_bips > 0 and portion_recipient:
            params["portionBips"] = str(int(portion_bips))
            params["portionRecipient"] = Web3.to_checksum_address(portion_recipient)
        resp = requests.get(
            DOMA_INTERFACE_QUOTE_URL,
            params=params,
            timeout=20,
            proxies=self.proxies,
        )
        resp.raise_for_status()
        data = resp.json()
        method = data.get("methodParameters") or {}
        to = str(method.get("to") or "").strip()
        calldata = str(method.get("calldata") or "").strip()
        value_hex = str(method.get("value") or "0x00").strip()
        if not to or not calldata:
            raise RuntimeError(f"Invalid UniversalRouter quote payload: {data}")
        return UniversalRouterQuote(
            to=to.lower(),
            calldata=calldata,
            value_raw=int(value_hex, 16) if value_hex.startswith("0x") else int(value_hex or "0"),
            quote_raw=int(str(data.get("quote") or "0")),
            quote_decimals=Decimal(str(data.get("quoteDecimals") or "0")),
            gas_use_estimate=int(str(data.get("gasUseEstimate") or "0")),
            gas_use_estimate_usd=Decimal(str(data.get("gasUseEstimateUSD") or "0")),
            price_impact_pct=Decimal(str(data.get("priceImpact") or "0")),
            route_string=str(data.get("routeString") or ""),
            quote_id=str(data.get("quoteId") or ""),
        )


class EvmExecutionClient:
    def __init__(
        self,
        rpc_url: str,
        chain_id: int,
        account_address: str,
        private_key: str,
        router_address: str,
        quoter_address: str,
        router_variant: str,
        request_proxies: Optional[Dict[str, str]] = None,
    ) -> None:
        request_kwargs: Dict[str, object] = {"timeout": 20}
        if request_proxies:
            request_kwargs["proxies"] = request_proxies
        self.web3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs=request_kwargs))
        if not self.web3.is_connected():
            raise RuntimeError(f"RPC connection failed: {rpc_url}")
        self.chain_id = chain_id
        self.account_address = Web3.to_checksum_address(account_address)
        self.private_key = private_key
        self.router_address = (
            Web3.to_checksum_address(router_address)
            if router_address and router_address != "0x0000000000000000000000000000000000000000"
            else None
        )
        self.quoter = (
            self.web3.eth.contract(address=Web3.to_checksum_address(quoter_address), abi=QUOTER_ABI)
            if quoter_address and quoter_address != "0x0000000000000000000000000000000000000000"
            else None
        )
        router_abi = (
            ROUTER_ABI_WITH_DEADLINE
            if router_variant == "with_deadline"
            else ROUTER_ABI_NO_DEADLINE
        )
        self.router_variant = router_variant
        self.router = (
            self.web3.eth.contract(address=self.router_address, abi=router_abi)
            if self.router_address
            else None
        )
        self.permit2_address = Web3.to_checksum_address(PERMIT2_ADDRESS)
        self.permit2 = self.web3.eth.contract(address=self.permit2_address, abi=PERMIT2_ABI)

    def get_native_balance(self) -> Decimal:
        wei = self.web3.eth.get_balance(self.account_address)
        return Decimal(wei) / Decimal(10**18)

    def get_chain_id(self) -> int:
        return int(self.web3.eth.chain_id)

    def get_gas_price_wei(self) -> int:
        return int(self.web3.eth.gas_price)

    def get_gas_price_gwei(self) -> Decimal:
        return Decimal(self.get_gas_price_wei()) / Decimal(10**9)

    def has_contract_code(self, address: str) -> bool:
        code = self.web3.eth.get_code(Web3.to_checksum_address(address))
        return bool(code and code.hex() != "0x")

    def get_erc20_balance(self, token_address: str, decimals: int) -> Decimal:
        token = self.web3.eth.contract(
            address=Web3.to_checksum_address(token_address),
            abi=ERC20_ABI,
        )
        raw = token.functions.balanceOf(self.account_address).call()
        return Decimal(raw) / (Decimal(10) ** decimals)

    def quote_exact_input_single(
        self,
        token_in: str,
        token_out: str,
        fee_tier: int,
        amount_in_raw: int,
    ) -> int:
        if self.quoter is None:
            raise RuntimeError("Quoter is not configured")
        return int(
            self.quoter.functions.quoteExactInputSingle(
                Web3.to_checksum_address(token_in),
                Web3.to_checksum_address(token_out),
                int(fee_tier),
                int(amount_in_raw),
                0,
            ).call()
        )

    def _send_tx(self, tx: dict) -> str:
        last_exc: Optional[Exception] = None
        current_tx = dict(tx)
        for attempt in range(3):
            try:
                signed = self.web3.eth.account.sign_transaction(current_tx, private_key=self.private_key)
                tx_hash = self.web3.eth.send_raw_transaction(signed.rawTransaction)
                return tx_hash.hex()
            except ValueError as exc:
                message = str(exc)
                if "nonce too low" not in message and "replacement transaction underpriced" not in message:
                    raise
                last_exc = exc
                time.sleep(1 + attempt)
                current_tx["nonce"] = self.web3.eth.get_transaction_count(self.account_address, "pending")
                current_tx["gasPrice"] = self.web3.eth.gas_price
        if last_exc:
            raise last_exc
        raise RuntimeError("Failed to send transaction")

    def _base_tx(self) -> dict:
        nonce = self.web3.eth.get_transaction_count(self.account_address, "pending")
        gas_price = self.web3.eth.gas_price
        return {
            "from": self.account_address,
            "nonce": nonce,
            "chainId": self.chain_id,
            "gasPrice": gas_price,
        }

    def ensure_allowance(
        self,
        token_address: str,
        required_amount_raw: int,
        spender_address: Optional[str] = None,
        approve_max: bool = False,
    ) -> Optional[str]:
        spender = spender_address or self.router_address
        if spender is None:
            raise RuntimeError("Spender is not configured")
        spender_cs = Web3.to_checksum_address(spender)
        token = self.web3.eth.contract(
            address=Web3.to_checksum_address(token_address),
            abi=ERC20_ABI,
        )
        current = token.functions.allowance(self.account_address, spender_cs).call()
        if int(current) >= int(required_amount_raw):
            return None

        approval_amount = MAX_UINT256 if approve_max else int(required_amount_raw)
        tx = token.functions.approve(spender_cs, approval_amount).build_transaction(
            self._base_tx()
        )
        tx["gas"] = int(self.web3.eth.estimate_gas(tx) * 1.2)
        return self._send_tx(tx)

    def get_permit2_allowance(self, token_address: str, spender_address: str) -> Tuple[int, int, int]:
        amount, expiration, nonce = self.permit2.functions.allowance(
            self.account_address,
            Web3.to_checksum_address(token_address),
            Web3.to_checksum_address(spender_address),
        ).call()
        return int(amount), int(expiration), int(nonce)

    def ensure_permit2_allowance(
        self,
        token_address: str,
        spender_address: str,
        required_amount_raw: int,
    ) -> Optional[str]:
        current_amount, expiration, _ = self.get_permit2_allowance(token_address, spender_address)
        if current_amount >= int(required_amount_raw) and expiration > int(time.time()) + 3600:
            return None

        tx = self.permit2.functions.approve(
            Web3.to_checksum_address(token_address),
            Web3.to_checksum_address(spender_address),
            MAX_UINT160,
            MAX_UINT48,
        ).build_transaction(self._base_tx())
        tx["gas"] = int(self.web3.eth.estimate_gas(tx) * 1.2)
        return self._send_tx(tx)

    def ensure_weth_balance(self, weth_address: str, required_amount_raw: int) -> Optional[str]:
        weth_token = self.web3.eth.contract(
            address=Web3.to_checksum_address(weth_address),
            abi=ERC20_ABI,
        )
        current = int(weth_token.functions.balanceOf(self.account_address).call())
        required = int(required_amount_raw)
        if current >= required:
            return None

        missing = required - current
        native = int(self.web3.eth.get_balance(self.account_address))
        # Keep a small gas reserve to avoid draining the account completely.
        reserve = int(Decimal("0.00001") * (Decimal(10) ** 18))
        if native < missing + reserve:
            raise RuntimeError(
                f"Insufficient native ETH to wrap into WETH: need {missing}, have {native}"
            )

        weth = self.web3.eth.contract(
            address=Web3.to_checksum_address(weth_address),
            abi=WETH_ABI,
        )
        tx = weth.functions.deposit().build_transaction(
            {
                **self._base_tx(),
                "value": missing,
            }
        )
        tx["gas"] = int(self.web3.eth.estimate_gas(tx) * 1.2)
        return self._send_tx(tx)

    def unwrap_weth(self, weth_address: str, amount_raw: int) -> Optional[str]:
        amount_raw = int(amount_raw)
        if amount_raw <= 0:
            return None
        weth = self.web3.eth.contract(
            address=Web3.to_checksum_address(weth_address),
            abi=WETH_ABI,
        )
        tx = weth.functions.withdraw(amount_raw).build_transaction(self._base_tx())
        tx["gas"] = int(self.web3.eth.estimate_gas(tx) * 1.2)
        return self._send_tx(tx)

    def execute_swap_exact_input_single(
        self,
        token_in: str,
        token_out: str,
        fee_tier: int,
        amount_in_raw: int,
        min_amount_out_raw: int,
        recipient: str,
        ttl_sec: int = 180,
    ) -> str:
        if self.router is None:
            raise RuntimeError("Router is not configured")
        recipient_cs = Web3.to_checksum_address(recipient)
        if self.router_variant == "with_deadline":
            params = (
                Web3.to_checksum_address(token_in),
                Web3.to_checksum_address(token_out),
                int(fee_tier),
                recipient_cs,
                int(time.time()) + ttl_sec,
                int(amount_in_raw),
                int(min_amount_out_raw),
                0,
            )
        else:
            params = (
                Web3.to_checksum_address(token_in),
                Web3.to_checksum_address(token_out),
                int(fee_tier),
                recipient_cs,
                int(amount_in_raw),
                int(min_amount_out_raw),
                0,
            )

        tx = self.router.functions.exactInputSingle(params).build_transaction(self._base_tx())
        tx["gas"] = int(self.web3.eth.estimate_gas(tx) * 1.2)
        return self._send_tx(tx)

    @staticmethod
    def _encode_v3_path(token_addresses: List[str], fee_tiers: List[int]) -> bytes:
        if len(token_addresses) < 2:
            raise ValueError("Path must contain at least 2 tokens")
        if len(fee_tiers) != len(token_addresses) - 1:
            raise ValueError("fee_tiers must be path_len - 1")

        path = b""
        for i, fee in enumerate(fee_tiers):
            token_bytes = bytes.fromhex(Web3.to_checksum_address(token_addresses[i])[2:])
            path += token_bytes + int(fee).to_bytes(3, "big")
        path += bytes.fromhex(Web3.to_checksum_address(token_addresses[-1])[2:])
        return path

    def execute_swap_exact_input_path(
        self,
        token_addresses: List[str],
        fee_tiers: List[int],
        amount_in_raw: int,
        min_amount_out_raw: int,
        recipient: str,
        ttl_sec: int = 180,
    ) -> str:
        if self.router is None:
            raise RuntimeError("Router is not configured")
        recipient_cs = Web3.to_checksum_address(recipient)
        path = self._encode_v3_path(token_addresses, fee_tiers)

        if self.router_variant == "with_deadline":
            params = (
                path,
                recipient_cs,
                int(time.time()) + ttl_sec,
                int(amount_in_raw),
                int(min_amount_out_raw),
            )
        else:
            params = (
                path,
                recipient_cs,
                int(amount_in_raw),
                int(min_amount_out_raw),
            )

        tx = self.router.functions.exactInput(params).build_transaction(self._base_tx())
        tx["gas"] = int(self.web3.eth.estimate_gas(tx) * 1.2)
        return self._send_tx(tx)

    def execute_launchpad_buy(
        self,
        launchpad_address: str,
        amount_in_raw: int,
        min_amount_out_raw: int,
    ) -> str:
        launchpad = self.web3.eth.contract(
            address=Web3.to_checksum_address(launchpad_address),
            abi=LAUNCHPAD_ABI,
        )
        tx = launchpad.functions.buy(
            int(amount_in_raw),
            int(min_amount_out_raw),
        ).build_transaction(self._base_tx())
        tx["gas"] = int(self.web3.eth.estimate_gas(tx) * 1.2)
        return self._send_tx(tx)

    def execute_launchpad_sell(
        self,
        launchpad_address: str,
        amount_in_raw: int,
        min_amount_out_raw: int,
    ) -> str:
        launchpad = self.web3.eth.contract(
            address=Web3.to_checksum_address(launchpad_address),
            abi=LAUNCHPAD_ABI,
        )
        tx = launchpad.functions.sell(
            int(amount_in_raw),
            int(min_amount_out_raw),
        ).build_transaction(self._base_tx())
        tx["gas"] = int(self.web3.eth.estimate_gas(tx) * 1.2)
        return self._send_tx(tx)

    def execute_prebuilt_transaction(
        self,
        to_address: str,
        calldata: str,
        value_raw: int = 0,
    ) -> str:
        tx = {
            **self._base_tx(),
            "to": Web3.to_checksum_address(to_address),
            "data": calldata,
            "value": int(value_raw),
        }
        tx["gas"] = int(self.web3.eth.estimate_gas(tx) * 1.2)
        return self._send_tx(tx)

    def get_subdomain_staking_prices(self, fractional_token_address: str) -> List[int]:
        contract = self.web3.eth.contract(
            address=Web3.to_checksum_address(DOMA_FRACTIONALIZATION_ADDRESS),
            abi=DOMA_FRACTIONALIZATION_SUBDOMAIN_ABI,
        )
        return [int(value) for value in contract.functions.getSubdomainStakingPrices(
            Web3.to_checksum_address(fractional_token_address)
        ).call()]

    def is_subdomain_claim_available(self, fractional_token_address: str, label: str) -> bool:
        fractionalization = self.web3.eth.contract(
            address=Web3.to_checksum_address(DOMA_FRACTIONALIZATION_ADDRESS),
            abi=DOMA_FRACTIONALIZATION_SUBDOMAIN_ABI,
        )
        proxy_record = self.web3.eth.contract(
            address=Web3.to_checksum_address(PROXY_DOMA_RECORD_ADDRESS),
            abi=PROXY_DOMA_RECORD_ABI,
        )
        if not bool(fractionalization.functions.isSubdomainMintingAllowed(
            Web3.to_checksum_address(fractional_token_address)
        ).call()):
            return False
        try:
            if bool(proxy_record.functions.isLabelForbidden(label.lower()).call()):
                return False
        except Exception:
            pass
        return not bool(fractionalization.functions.isSubdomainTaken(
            Web3.to_checksum_address(fractional_token_address),
            label.lower(),
        ).call())

    def claim_subdomain_and_set_dns(
        self,
        fractional_token_address: str,
        label: str,
        dns_wallet_address: str,
        voucher_contract_address: Optional[str] = None,
        staking_voucher: Optional[Tuple[str, str, str, int]] = None,
        staking_signature: Optional[str] = None,
    ) -> Tuple[Optional[str], str, Optional[int], Optional[str]]:
        fractional_token = Web3.to_checksum_address(fractional_token_address)
        label = label.lower().strip()
        if not label:
            raise RuntimeError("Subdomain label is empty")
        fractionalization = self.web3.eth.contract(
            address=Web3.to_checksum_address(voucher_contract_address or DOMA_FRACTIONALIZATION_ADDRESS),
            abi=DOMA_FRACTIONALIZATION_SUBDOMAIN_ABI,
        )
        prices = [int(value) for value in fractionalization.functions.getSubdomainStakingPrices(fractional_token).call()]
        if not prices:
            raise RuntimeError("Subdomain staking prices are not configured")
        if not bool(fractionalization.functions.isSubdomainMintingAllowed(fractional_token).call()):
            raise RuntimeError("Subdomain minting is not enabled for this token")
        label_price_index = 0 if len(prices) == 1 else min(len(label) - 1, len(prices) - 1)
        if label_price_index < 0:
            raise RuntimeError("Subdomain label length is invalid")
        staking_amount_raw = int(prices[label_price_index])
        if bool(fractionalization.functions.isSubdomainTaken(fractional_token, label).call()):
            raise RuntimeError(f"Subdomain label already taken: {label}")

        try:
            approve_hash = self.ensure_allowance(
                fractional_token,
                staking_amount_raw,
                spender_address=DOMA_FRACTIONALIZATION_ADDRESS,
                approve_max=False,
            )
            if approve_hash:
                self.web3.eth.wait_for_transaction_receipt(approve_hash, timeout=180, poll_latency=2)
        except Exception as exc:
            raise RuntimeError(f"subdomain approve failed: {exc}") from exc

        try:
            if staking_voucher is None or not staking_signature:
                raise RuntimeError("Subdomain staking voucher/signature is missing")
            record = f'"ENS1 dnsname.ens.eth {Web3.to_checksum_address(dns_wallet_address)}"'
            stake_tx = fractionalization.functions.stakeForSubdomain(
                staking_voucher,
                staking_signature,
                [record],
            ).build_transaction(self._base_tx())
            stake_tx["gas"] = int(self.web3.eth.estimate_gas(stake_tx) * 1.2)
            stake_hash = self._send_tx(stake_tx)
        except Exception as exc:
            raise RuntimeError(f"subdomain stake estimate/send failed: {exc}") from exc

        try:
            receipt = self.web3.eth.wait_for_transaction_receipt(stake_hash, timeout=180, poll_latency=2)
        except Exception as exc:
            raise RuntimeError(f"subdomain stake receipt failed | tx={stake_hash}: {exc}") from exc

        subdomain_id: Optional[int] = None
        try:
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", message=".*MismatchedABI.*")
                events = fractionalization.events.SubdomainStaked().process_receipt(receipt)
            if events:
                subdomain_id = int(events[0]["args"]["subdomainId"])
        except Exception:
            subdomain_id = None

        return approve_hash, stake_hash, subdomain_id, None

    def unstake_subdomain(self, subdomain_id: str | int) -> str:
        try:
            token_id = int(str(subdomain_id).strip())
        except Exception as exc:
            raise RuntimeError(f"invalid subdomain id: {subdomain_id}") from exc
        if token_id <= 0:
            raise RuntimeError(f"invalid subdomain id: {subdomain_id}")
        fractionalization = self.web3.eth.contract(
            address=Web3.to_checksum_address(DOMA_FRACTIONALIZATION_ADDRESS),
            abi=DOMA_FRACTIONALIZATION_SUBDOMAIN_ABI,
        )
        try:
            tx = fractionalization.functions.unstakeSubdomain(token_id).build_transaction(self._base_tx())
            tx["gas"] = int(self.web3.eth.estimate_gas(tx) * 1.2)
            tx_hash = self._send_tx(tx)
        except Exception as exc:
            raise RuntimeError(f"subdomain unstake estimate/send failed: {exc}") from exc
        try:
            receipt = self.web3.eth.wait_for_transaction_receipt(tx_hash, timeout=180, poll_latency=2)
        except Exception as exc:
            raise RuntimeError(f"subdomain unstake receipt failed | tx={tx_hash}: {exc}") from exc
        if int(getattr(receipt, "status", 0)) != 1:
            raise RuntimeError(f"subdomain unstake reverted | tx={tx_hash}")
        return tx_hash

    def execute_domain_bridge(
        self,
        proxy_doma_record_address: str,
        token_id: str,
        target_chain_id: str,
        target_owner_address: str,
    ) -> Tuple[str, int]:
        record = self.web3.eth.contract(
            address=Web3.to_checksum_address(proxy_doma_record_address),
            abi=PROXY_DOMA_RECORD_ABI,
        )
        try:
            if not bool(record.functions.isTargetChainSupported(str(target_chain_id)).call()):
                raise RuntimeError(f"Domain bridge target chain is not supported: {target_chain_id}")
        except RuntimeError:
            raise
        except Exception:
            pass
        fn = record.functions.bridge(
            int(token_id),
            str(target_chain_id),
            str(target_owner_address),
        )
        fee_candidates: List[int] = [0]
        try:
            fee = int(record.functions.getBridgeFeeInNative(str(target_chain_id)).call())
            if fee > 0 and fee not in fee_candidates:
                fee_candidates.append(fee)
        except Exception:
            pass
        try:
            bridge_operation = Web3.keccak(text="BRIDGE")
            fee = int(record.functions.getOperationFeeInNative(bridge_operation).call())
            if fee > 0 and fee not in fee_candidates:
                fee_candidates.append(fee)
        except Exception:
            pass

        native_balance = int(self.web3.eth.get_balance(self.account_address))
        last_error: Optional[Exception] = None
        for value_raw in fee_candidates:
            if value_raw > native_balance:
                continue
            tx = {
                **self._base_tx(),
                "value": int(value_raw),
            }
            try:
                tx = fn.build_transaction(tx)
                tx["gas"] = int(self.web3.eth.estimate_gas(tx) * 1.2)
                return self._send_tx(tx), int(value_raw)
            except Exception as exc:
                last_error = exc
                continue
        if last_error:
            raise last_error
        raise RuntimeError("Unable to build domain bridge transaction")


def decimal_to_raw(amount: Decimal, decimals: int) -> int:
    return int((amount * (Decimal(10) ** decimals)).to_integral_value())


def raw_to_decimal(amount_raw: int, decimals: int) -> Decimal:
    return Decimal(amount_raw) / (Decimal(10) ** decimals)


def canonical_symbol_for_api(symbol: str) -> str:
    s = (symbol or "").strip().upper()
    return "WETH" if s == "ETH" else s


def pick_token_usd_price(
    token: Token, eth_price_usd: Decimal, fallback_usd: Decimal = Decimal("0")
) -> Decimal:
    if token.symbol in {"USDC", "USDC.E", "USDT", "DAI"}:
        return Decimal("1")
    if token.derived_eth > 0 and eth_price_usd > 0:
        return token.derived_eth * eth_price_usd
    return fallback_usd
