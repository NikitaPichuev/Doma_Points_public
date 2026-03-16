from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal, getcontext
from typing import Dict, List, Optional, Tuple

import requests
from web3 import Web3


getcontext().prec = 50


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
    }
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
    }
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
                    return p.token0_price
                return p.token1_price
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
        self.api_keys = merged
        self.proxies = proxies or None

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

    def fetch_points(self, wallet_address: str, rank_by: str = "POINTS") -> Optional[PointsSnapshot]:
        # walletAddress filter accepts plain 0x... string.
        query = """
        query Points($walletAddress: String, $rankBy: LeaderboardRankBy) {
          leaderboards(take: 1, walletAddress: $walletAddress, rankBy: $rankBy) {
            items {
              walletAddress
              rank
              points
              tradingVolumeUsd
              liquidAmountUsd
              referralCount
              totalSnapshotEntries
              snapshotDate
            }
          }
        }
        """
        data = self._post(query, {"walletAddress": wallet_address, "rankBy": rank_by})
        items = data.get("leaderboards", {}).get("items", [])
        if not items:
            return None
        i = items[0]
        return PointsSnapshot(
            wallet_address=str(i["walletAddress"]),
            rank=int(i["rank"]),
            points=Decimal(str(i["points"])),
            trading_volume_usd=Decimal(str(i["tradingVolumeUsd"])),
            liquid_amount_usd=Decimal(str(i["liquidAmountUsd"])),
            referral_count=int(i.get("referralCount") or 0),
            total_snapshot_entries=int(i["totalSnapshotEntries"]),
            snapshot_date=str(i["snapshotDate"]),
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
            raise RuntimeError("RPC connection failed")
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
        signed = self.web3.eth.account.sign_transaction(tx, private_key=self.private_key)
        tx_hash = self.web3.eth.send_raw_transaction(signed.rawTransaction)
        return tx_hash.hex()

    def _base_tx(self) -> dict:
        nonce = self.web3.eth.get_transaction_count(self.account_address, "pending")
        gas_price = self.web3.eth.gas_price
        return {
            "from": self.account_address,
            "nonce": nonce,
            "chainId": self.chain_id,
            "gasPrice": gas_price,
        }

    def ensure_allowance(self, token_address: str, required_amount_raw: int) -> Optional[str]:
        if self.router_address is None:
            raise RuntimeError("Router is not configured")
        token = self.web3.eth.contract(
            address=Web3.to_checksum_address(token_address),
            abi=ERC20_ABI,
        )
        current = token.functions.allowance(self.account_address, self.router_address).call()
        if int(current) >= int(required_amount_raw):
            return None

        tx = token.functions.approve(self.router_address, int(required_amount_raw)).build_transaction(
            self._base_tx()
        )
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


def decimal_to_raw(amount: Decimal, decimals: int) -> int:
    return int((amount * (Decimal(10) ** decimals)).to_integral_value())


def raw_to_decimal(amount_raw: int, decimals: int) -> Decimal:
    return Decimal(amount_raw) / (Decimal(10) ** decimals)


def pick_token_usd_price(
    token: Token, eth_price_usd: Decimal, fallback_usd: Decimal = Decimal("0")
) -> Decimal:
    if token.symbol in {"USDC", "USDC.E", "USDT", "DAI"}:
        return Decimal("1")
    if token.derived_eth > 0 and eth_price_usd > 0:
        return token.derived_eth * eth_price_usd
    return fallback_usd
