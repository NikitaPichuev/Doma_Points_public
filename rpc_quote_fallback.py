"""Read-only V3 quoting and UniversalRouter calldata for Doma mainnet."""
import os
import time
from decimal import Decimal

import requests
from eth_abi import encode
from web3 import Web3

CHAIN_ID = 97477
WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x31eef89d5215c305304a2fa5376a1f1b6c5dc477"
QUOTER = "0x2023adf9ff50219c34989baabd76a0f5cfe432f4"
ROUTER = "0x5089863e97196773038f98459262d866f2281f58"
NATIVE = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
CALLER = "0x0000000000000000000000000000000000000001"
THIS = "0x0000000000000000000000000000000000000002"
FEES = (100, 500, 3000, 10000)


def _calldata(signature, types, values):
    return "0x" + (Web3.keccak(text=signature)[:4] + encode(types, values)).hex()


class RpcReader:
    def __init__(self, proxies):
        self.url = os.getenv("DOMA_QUOTE_RPC_URL") or os.getenv("RPC_URL") or "https://rpc.doma.xyz/"
        self.proxies = proxies
        self.session = requests.Session()
        self.session.trust_env = False

    def close(self):
        self.session.close()

    def request(self, payload):
        response = self.session.post(self.url, json=payload, proxies=self.proxies, timeout=20)
        response.raise_for_status()
        return response.json()

    def verify_chain(self):
        data = self.request(dict(jsonrpc="2.0", id=1, method="eth_chainId", params=[]))
        if not isinstance(data, dict) or int(data.get("result") or "0", 16) != CHAIN_ID:
            raise RuntimeError("RPC quote fallback: wrong chain")

    def calls(self, calls):
        payload = [dict(jsonrpc="2.0", id=i + 1, method="eth_call", params=[
            {"to": Web3.to_checksum_address(address), "data": calldata}, "latest"])
            for i, (address, calldata) in enumerate(calls)]
        response = self.request(payload)
        if not isinstance(response, list):
            raise RuntimeError("RPC quote fallback: batch calls unavailable")
        answers = {item.get("id"): item for item in response if isinstance(item, dict)}
        if len(answers) != len(payload) or set(answers) != set(range(1, len(payload) + 1)):
            raise RuntimeError("RPC quote fallback: incomplete batch response")
        return [None if answers[i].get("error") else answers[i].get("result") for i in range(1, len(payload) + 1)]

    def best_leg(self, token_in, token_out, amount):
        calls = [(QUOTER, _calldata("quoteExactInputSingle((address,address,uint256,uint24,uint160))",
                  ["(address,address,uint256,uint24,uint160)"],
                  [(token_in, token_out, amount, fee, 0)])) for fee in FEES]
        answers = self.calls(calls)
        candidates = [(int(value[2:66], 16), fee) for value, fee in zip(answers, FEES) if value and len(value) >= 66]
        return max(candidates, default=(0, 0))

    def spot_output(self, tokens, fees, amount):
        factory_answer = self.calls([(QUOTER, "0xc45a0155")])[0]
        if not factory_answer:
            raise RuntimeError("RPC quote fallback: factory unavailable")
        factory = "0x" + factory_answer[-40:]
        pools = self.calls([(factory, _calldata("getPool(address,address,uint24)",
                            ["address", "address", "uint24"], [a, b, fee]))
                            for a, b, fee in zip(tokens, tokens[1:], fees)])
        if any(not pool or int(pool, 16) == 0 for pool in pools):
            raise RuntimeError("RPC quote fallback: pool unavailable")
        states = self.calls([("0x" + pool[-40:], "0x3850c7bd") for pool in pools])
        output = Decimal(amount)
        for a, b, state in zip(tokens, tokens[1:], states):
            sqrt_price = int(state[2:66], 16) if state else 0
            if sqrt_price <= 0:
                raise RuntimeError("RPC quote fallback: pool price unavailable")
            ratio = Decimal(sqrt_price) ** 2 / Decimal(2**192)
            output *= ratio if int(a, 16) < int(b, 16) else 1 / ratio
        return output


def encode_router_route(tokens, fees, amount, gross_output, slippage, native_in, native_out,
                        portion_bips, portion_recipient):
    if not 0 <= portion_bips < 10000 or not Decimal("0") <= slippage < Decimal("100"):
        raise ValueError("RPC quote fallback: invalid slippage or portion")
    net_output = gross_output - gross_output * portion_bips // 10000
    gross_minimum = int(Decimal(gross_output) * (100 - slippage) / 100)
    net_minimum = int(Decimal(net_output) * (100 - slippage) / 100)
    if min(gross_minimum, net_minimum) <= 0:
        raise ValueError("RPC quote fallback: output too small")
    commands, inputs = [], []

    def command(number, types, values):
        commands.append(number)
        inputs.append(encode(types, values))

    if native_in:
        command(0x0b, ["address", "uint256"], [THIS, amount])
    path = bytes.fromhex(tokens[0][2:])
    for fee, token in zip(fees, tokens[1:]):
        path += fee.to_bytes(3, "big") + bytes.fromhex(token[2:])
    router_output = native_out or portion_bips > 0
    command(0x00, ["address", "uint256", "uint256", "bytes", "bool"],
            [THIS if router_output else CALLER, amount, gross_minimum, path, not native_in])
    if portion_bips:
        command(0x06, ["address", "address", "uint256"], [tokens[-1], portion_recipient, portion_bips])
    if native_out:
        command(0x0c, ["address", "uint256"], [CALLER, net_minimum])
    elif router_output:
        command(0x04, ["address", "address", "uint256"], [tokens[-1], CALLER, net_minimum])
    return _calldata("execute(bytes,bytes[],uint256)", ["bytes", "bytes[]", "uint256"],
                     [bytes(commands), inputs, int(time.time()) + 180]), net_output


def build_rpc_quote(params, proxies=None):
    chain = int(params["tokenInChainId"])
    if chain != CHAIN_ID or int(params["tokenOutChainId"]) != CHAIN_ID or params["type"] != "exactIn":
        raise ValueError("RPC quote fallback only supports Doma mainnet exact-input swaps")
    native_in = params["tokenInAddress"].lower() == NATIVE
    native_out = params["tokenOutAddress"].lower() == NATIVE
    token_in = WETH if native_in else Web3.to_checksum_address(params["tokenInAddress"]).lower()
    token_out = WETH if native_out else Web3.to_checksum_address(params["tokenOutAddress"]).lower()
    if token_in == token_out:
        raise ValueError("RPC quote fallback: identical wrapped tokens")
    amount = int(params["amount"])
    if amount <= 0:
        raise ValueError("RPC quote fallback: invalid input amount")
    reader = RpcReader(proxies)
    try:
        reader.verify_chain()
        candidates = []
        for tokens in ([token_in, token_out], *[[token_in, middle, token_out] for middle in (WETH, USDC)
                                               if middle not in (token_in, token_out)]):
            output, fees = amount, []
            for a, b in zip(tokens, tokens[1:]):
                output, fee = reader.best_leg(a, b, output)
                if output <= 0:
                    break
                fees.append(fee)
            if output > 0 and len(fees) == len(tokens) - 1:
                candidates.append((output, tokens, fees))
        if not candidates:
            raise RuntimeError("RPC quote fallback: no liquid V3 route")
        gross, tokens, fees = max(candidates, key=lambda item: item[0])
        spot_output = reader.spot_output(tokens, fees, amount)
        decimals_answer = reader.calls([(token_out, "0x313ce567")])[0]
        if not decimals_answer:
            raise RuntimeError("RPC quote fallback: token decimals unavailable")
        decimals = int(decimals_answer, 16)
        if not 0 <= decimals <= 36:
            raise ValueError("RPC quote fallback: invalid token decimals")
    finally:
        reader.close()
    portion = int(params.get("portionBips", 0))
    recipient = params.get("portionRecipient")
    calldata, output = encode_router_route(tokens, fees, amount, gross, Decimal(params["slippageTolerance"]),
                                           native_in, native_out, portion, recipient)
    price_impact = max(Decimal("0"), (1 - Decimal(output) / spot_output) * 100)
    return dict(methodParameters=dict(to=ROUTER, calldata=calldata, value=hex(amount if native_in else 0)),
                quote=str(output), quoteDecimals=str(Decimal(output) / (10**decimals)),
                gasUseEstimate="0", gasUseEstimateUSD="0", priceImpact=str(price_impact),
                routeString="[RPC V3] " + " -> ".join(tokens) + " fees=" + str(fees), quoteId="rpc-fallback")
