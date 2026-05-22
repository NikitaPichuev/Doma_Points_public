from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from web3 import Web3


MAX_UINT256 = (1 << 256) - 1
MIN_TICK = -887272
MAX_TICK = 887272
FEE_TICK_SPACING = {
    100: 1,
    500: 10,
    3000: 60,
    10000: 200,
}


ERC20_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "owner", "type": "address"},
            {"internalType": "address", "name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "spender", "type": "address"},
            {"internalType": "uint256", "name": "amount", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]


POSITION_MANAGER_ABI = [
    {
        "inputs": [{"internalType": "address", "name": "owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "owner", "type": "address"},
            {"internalType": "uint256", "name": "index", "type": "uint256"},
        ],
        "name": "tokenOfOwnerByIndex",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "uint256", "name": "tokenId", "type": "uint256"}],
        "name": "ownerOf",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "uint256", "name": "tokenId", "type": "uint256"}],
        "name": "positions",
        "outputs": [
            {"internalType": "uint96", "name": "nonce", "type": "uint96"},
            {"internalType": "address", "name": "operator", "type": "address"},
            {"internalType": "address", "name": "token0", "type": "address"},
            {"internalType": "address", "name": "token1", "type": "address"},
            {"internalType": "uint24", "name": "fee", "type": "uint24"},
            {"internalType": "int24", "name": "tickLower", "type": "int24"},
            {"internalType": "int24", "name": "tickUpper", "type": "int24"},
            {"internalType": "uint128", "name": "liquidity", "type": "uint128"},
            {"internalType": "uint256", "name": "feeGrowthInside0LastX128", "type": "uint256"},
            {"internalType": "uint256", "name": "feeGrowthInside1LastX128", "type": "uint256"},
            {"internalType": "uint128", "name": "tokensOwed0", "type": "uint128"},
            {"internalType": "uint128", "name": "tokensOwed1", "type": "uint128"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {
                "components": [
                    {"internalType": "uint256", "name": "tokenId", "type": "uint256"},
                    {"internalType": "uint128", "name": "liquidity", "type": "uint128"},
                    {"internalType": "uint256", "name": "amount0Min", "type": "uint256"},
                    {"internalType": "uint256", "name": "amount1Min", "type": "uint256"},
                    {"internalType": "uint256", "name": "deadline", "type": "uint256"},
                ],
                "internalType": "struct INonfungiblePositionManager.DecreaseLiquidityParams",
                "name": "params",
                "type": "tuple",
            }
        ],
        "name": "decreaseLiquidity",
        "outputs": [
            {"internalType": "uint256", "name": "amount0", "type": "uint256"},
            {"internalType": "uint256", "name": "amount1", "type": "uint256"},
        ],
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "inputs": [
            {
                "components": [
                    {"internalType": "uint256", "name": "tokenId", "type": "uint256"},
                    {"internalType": "address", "name": "recipient", "type": "address"},
                    {"internalType": "uint128", "name": "amount0Max", "type": "uint128"},
                    {"internalType": "uint128", "name": "amount1Max", "type": "uint128"},
                ],
                "internalType": "struct INonfungiblePositionManager.CollectParams",
                "name": "params",
                "type": "tuple",
            }
        ],
        "name": "collect",
        "outputs": [
            {"internalType": "uint256", "name": "amount0", "type": "uint256"},
            {"internalType": "uint256", "name": "amount1", "type": "uint256"},
        ],
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "uint256", "name": "tokenId", "type": "uint256"}],
        "name": "burn",
        "outputs": [],
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "inputs": [
            {
                "components": [
                    {"internalType": "address", "name": "token0", "type": "address"},
                    {"internalType": "address", "name": "token1", "type": "address"},
                    {"internalType": "uint24", "name": "fee", "type": "uint24"},
                    {"internalType": "int24", "name": "tickLower", "type": "int24"},
                    {"internalType": "int24", "name": "tickUpper", "type": "int24"},
                    {"internalType": "uint256", "name": "amount0Desired", "type": "uint256"},
                    {"internalType": "uint256", "name": "amount1Desired", "type": "uint256"},
                    {"internalType": "uint256", "name": "amount0Min", "type": "uint256"},
                    {"internalType": "uint256", "name": "amount1Min", "type": "uint256"},
                    {"internalType": "address", "name": "recipient", "type": "address"},
                    {"internalType": "uint256", "name": "deadline", "type": "uint256"},
                ],
                "internalType": "struct INonfungiblePositionManager.MintParams",
                "name": "params",
                "type": "tuple",
            }
        ],
        "name": "mint",
        "outputs": [
            {"internalType": "uint256", "name": "tokenId", "type": "uint256"},
            {"internalType": "uint128", "name": "liquidity", "type": "uint128"},
            {"internalType": "uint256", "name": "amount0", "type": "uint256"},
            {"internalType": "uint256", "name": "amount1", "type": "uint256"},
        ],
        "stateMutability": "payable",
        "type": "function",
    },
]


def full_range_ticks(fee_tier: int) -> tuple[int, int]:
    spacing = FEE_TICK_SPACING.get(int(fee_tier))
    if not spacing:
        raise ValueError(f"Unsupported Uniswap V3 fee tier for full range: {fee_tier}")
    tick_lower = ((MIN_TICK + spacing - 1) // spacing) * spacing
    tick_upper = (MAX_TICK // spacing) * spacing
    return tick_lower, tick_upper


class PositionManagerClient:
    def __init__(
        self,
        rpc_url: str,
        chain_id: int,
        account_address: str,
        private_key: str,
        position_manager_address: str,
        request_proxies: Optional[Dict[str, str]] = None,
    ) -> None:
        kwargs: Dict[str, object] = {"timeout": 20}
        if request_proxies:
            kwargs["proxies"] = request_proxies
        self.web3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs=kwargs))
        if not self.web3.is_connected():
            raise RuntimeError(f"RPC connection failed: {rpc_url}")
        self.chain_id = chain_id
        self.account_address = Web3.to_checksum_address(account_address)
        self.private_key = private_key
        self.position_manager_address = Web3.to_checksum_address(position_manager_address)
        self.contract = self.web3.eth.contract(
            address=self.position_manager_address,
            abi=POSITION_MANAGER_ABI,
        )

    def list_owner_token_ids(self, owner: Optional[str] = None, limit: int = 50) -> List[int]:
        owner_cs = Web3.to_checksum_address(owner or self.account_address)
        count = int(self.contract.functions.balanceOf(owner_cs).call())
        out: List[int] = []
        for i in range(min(count, limit)):
            out.append(int(self.contract.functions.tokenOfOwnerByIndex(owner_cs, i).call()))
        return out

    @dataclass
    class PositionInfo:
        token_id: int
        token0: str
        token1: str
        fee: int
        tick_lower: int
        tick_upper: int
        liquidity: int
        tokens_owed0: int
        tokens_owed1: int

    def owner_of(self, token_id: int) -> str:
        return str(self.contract.functions.ownerOf(int(token_id)).call())

    def position_liquidity(self, token_id: int) -> int:
        p = self.contract.functions.positions(int(token_id)).call()
        return int(p[7])  # liquidity

    def get_position_info(self, token_id: int) -> "PositionManagerClient.PositionInfo":
        p = self.contract.functions.positions(int(token_id)).call()
        return PositionManagerClient.PositionInfo(
            token_id=int(token_id),
            token0=str(p[2]).lower(),
            token1=str(p[3]).lower(),
            fee=int(p[4]),
            tick_lower=int(p[5]),
            tick_upper=int(p[6]),
            liquidity=int(p[7]),
            tokens_owed0=int(p[10]),
            tokens_owed1=int(p[11]),
        )

    def list_owner_positions(
        self,
        owner: Optional[str] = None,
        limit: int = 100,
        only_active: bool = True,
    ) -> List["PositionManagerClient.PositionInfo"]:
        token_ids = self.list_owner_token_ids(owner=owner, limit=limit)
        out: List[PositionManagerClient.PositionInfo] = []
        for token_id in token_ids:
            info = self.get_position_info(token_id)
            if only_active and info.liquidity <= 0:
                continue
            out.append(info)
        return out

    def _base_tx(self) -> dict:
        nonce = self.web3.eth.get_transaction_count(self.account_address, "pending")
        return {
            "from": self.account_address,
            "nonce": nonce,
            "chainId": self.chain_id,
        }

    def _send_tx(self, tx: dict) -> str:
        signed = self.web3.eth.account.sign_transaction(tx, private_key=self.private_key)
        tx_hash = self.web3.eth.send_raw_transaction(signed.rawTransaction)
        return tx_hash.hex()

    def ensure_allowance(self, token_address: str, required_amount_raw: int, approve_max: bool = True) -> Optional[str]:
        token = self.web3.eth.contract(
            address=Web3.to_checksum_address(token_address),
            abi=ERC20_ABI,
        )
        current = int(token.functions.allowance(self.account_address, self.position_manager_address).call())
        if current >= int(required_amount_raw):
            return None
        amount = MAX_UINT256 if approve_max else int(required_amount_raw)
        tx = token.functions.approve(self.position_manager_address, amount).build_transaction(self._base_tx())
        tx["gas"] = int(self.web3.eth.estimate_gas(tx) * 1.2)
        if "maxFeePerGas" not in tx and "maxPriorityFeePerGas" not in tx:
            tx["gasPrice"] = int(self.web3.eth.gas_price)
        return self._send_tx(tx)

    def mint_full_range(
        self,
        token0: str,
        token1: str,
        fee_tier: int,
        amount0_desired: int,
        amount1_desired: int,
        recipient: Optional[str] = None,
        amount0_min: int = 0,
        amount1_min: int = 0,
        deadline_sec: int = 600,
    ) -> str:
        tick_lower, tick_upper = full_range_ticks(int(fee_tier))
        params = (
            Web3.to_checksum_address(token0),
            Web3.to_checksum_address(token1),
            int(fee_tier),
            int(tick_lower),
            int(tick_upper),
            int(amount0_desired),
            int(amount1_desired),
            int(amount0_min),
            int(amount1_min),
            Web3.to_checksum_address(recipient or self.account_address),
            int(time.time()) + int(deadline_sec),
        )
        tx = self.contract.functions.mint(params).build_transaction(self._base_tx())
        tx["gas"] = int(self.web3.eth.estimate_gas(tx) * 1.2)
        if "maxFeePerGas" not in tx and "maxPriorityFeePerGas" not in tx:
            tx["gasPrice"] = int(self.web3.eth.gas_price)
        return self._send_tx(tx)

    def decrease_liquidity(self, token_id: int, liquidity_to_remove: int, deadline_sec: int = 600) -> str:
        params = (
            int(token_id),
            int(liquidity_to_remove),
            0,
            0,
            int(time.time()) + int(deadline_sec),
        )
        tx = self.contract.functions.decreaseLiquidity(params).build_transaction(self._base_tx())
        tx["gas"] = int(self.web3.eth.estimate_gas(tx) * 1.2)
        if "maxFeePerGas" not in tx and "maxPriorityFeePerGas" not in tx:
            tx["gasPrice"] = int(self.web3.eth.gas_price)
        return self._send_tx(tx)

    def collect_all(self, token_id: int, recipient: Optional[str] = None) -> str:
        max_u128 = (1 << 128) - 1
        params = (
            int(token_id),
            Web3.to_checksum_address(recipient or self.account_address),
            int(max_u128),
            int(max_u128),
        )
        tx = self.contract.functions.collect(params).build_transaction(self._base_tx())
        tx["gas"] = int(self.web3.eth.estimate_gas(tx) * 1.2)
        if "maxFeePerGas" not in tx and "maxPriorityFeePerGas" not in tx:
            tx["gasPrice"] = int(self.web3.eth.gas_price)
        return self._send_tx(tx)

    def burn(self, token_id: int) -> str:
        tx = self.contract.functions.burn(int(token_id)).build_transaction(self._base_tx())
        tx["gas"] = int(self.web3.eth.estimate_gas(tx) * 1.2)
        if "maxFeePerGas" not in tx and "maxPriorityFeePerGas" not in tx:
            tx["gasPrice"] = int(self.web3.eth.gas_price)
        return self._send_tx(tx)
