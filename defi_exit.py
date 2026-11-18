"""Explicit, same-chain exits for supported DeFi positions."""
from dataclasses import dataclass

from web3 import Web3


NETWORKS = {
    "mendi": (59144, ("https://rpc.linea.build", "https://linea-rpc.publicnode.com")),
    "stargate": (8453, ("https://mainnet.base.org", "https://base-rpc.publicnode.com")),
}
MENDI_COMPTROLLER = "0x1b4d3b0421ddc1eb216d230bc01527422fb93103"
MENDI_USDC = "0x333d8b480bdb25ea7be4dd87eeb359988ce1b30d"
STARGATE_ROUTER = "0x45f1a95a4d3f3836523f5c83673c797f4d4d263b"
STARGATE_POOL = "0x4c80e24119cfb836cdf0a6b53dc23f04f7e652ca"
USDBC = "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca"


def _fn(client, address, name, inputs, output, *args):
    abi = [{
        "type": "function", "name": name, "stateMutability": "nonpayable",
        "inputs": [{"name": f"arg{i}", "type": kind} for i, kind in enumerate(inputs)],
        "outputs": [{"name": "", "type": output}],
    }]
    contract = client.web3.eth.contract(address=Web3.to_checksum_address(address), abi=abi)
    return getattr(contract.functions, name)(*args)


@dataclass
class Exit:
    action: object
    position_balance: object
    shares: int


def prepare_exit(client, protocol, wallet):
    if client.chain_id != NETWORKS[protocol][0]:
        raise ValueError("Wrong chain for DeFi exit")
    wallet = Web3.to_checksum_address(wallet)
    address = MENDI_USDC if protocol == "mendi" else STARGATE_POOL
    balance = _fn(client, address, "balanceOf", ("address",), "uint256", wallet)
    shares = balance.call()
    if shares <= 0:
        return None, "no unstaked position"
    sender = {"from": wallet}
    if protocol == "mendi":
        markets = _fn(client, MENDI_COMPTROLLER, "getAllMarkets", (), "address[]").call()
        if MENDI_USDC not in {market.lower() for market in markets}:
            raise RuntimeError("Mendi market registry did not include the expected USDC market")
        for market in markets:
            if _fn(client, market, "borrowBalanceStored", ("address",), "uint256", wallet).call() != 0:
                return None, f"Mendi debt on {market}; repay first"
        action = _fn(client, address, "redeem", ("uint256",), "uint256", shares)
        code = action.call(sender)
        if code != 0:
            return None, f"Mendi redeem simulation rejected (code {code})"
    else:
        token = _fn(client, address, "token", (), "address").call()
        pool_id = _fn(client, address, "poolId", (), "uint256").call()
        factory = _fn(client, STARGATE_ROUTER, "factory", (), "address").call()
        pool = _fn(client, factory, "getPool", ("uint256",), "address", pool_id).call()
        if token.lower() != USDBC or pool.lower() != address or not 0 < pool_id < 65536:
            raise RuntimeError("Stargate router/pool/asset mismatch")
        expected_ld = _fn(client, address, "amountLPtoLD", ("uint256",), "uint256", shares).call()
        rate = _fn(client, address, "convertRate", (), "uint256").call()
        action = _fn(client, STARGATE_ROUTER, "instantRedeemLocal",
                     ("uint16", "uint256", "address"), "uint256", pool_id, shares, wallet)
        out_sd = action.call(sender)
        if expected_ld <= 0 or rate <= 0 or out_sd <= 0 or out_sd * rate < expected_ld:
            return None, "not enough local liquidity for full instant redemption"
    return Exit(action, balance, shares), ""


def send_exit(client, plan):
    tx = client._base_tx()
    estimate = plan.action.estimate_gas({"from": client.account_address})
    tx["gas"] = (estimate * 120 + 99) // 100
    return client._send_tx(plan.action.build_transaction(tx), retry_nonce=False)
