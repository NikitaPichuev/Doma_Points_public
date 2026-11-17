"""Read-only batched ERC20 balances. Missing reads remain unknown, never zero."""
import time
from decimal import Decimal, localcontext

import requests
from eth_abi import decode, encode
from eth_abi.exceptions import DecodingError
from web3 import Web3

MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"


def _multicall(session, rpc_url, payload, proxies):
    calls = [(p["params"][0]["to"], True, bytes.fromhex(p["params"][0]["data"][2:])) for p in payload]
    data = bytes(Web3.keccak(text="aggregate3((address,bool,bytes)[])")[:4]) + encode(["(address,bool,bytes)[]"], [calls])
    for attempt in range(3):
        try:
            response = session.post(rpc_url, json=dict(jsonrpc="2.0", id=1, method="eth_call", params=[
                {"to": MULTICALL3, "data": "0x" + data.hex()}, "latest"]), proxies=proxies, timeout=(5, 15))
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError("Invalid Multicall RPC response")
            if result.get("error"):
                if result["error"].get("code") == 3:
                    return None
                raise ValueError("Multicall RPC error")
            raw = result.get("result")
            if not raw or raw == "0x":
                return None
            items = decode(["(bool,bytes)[]"], bytes.fromhex(raw[2:]))[0]
            if len(items) != len(payload):
                return None
            return [dict(id=p["id"], result="0x" + value.hex()) if ok and len(value) == 32
                    else dict(id=p["id"], error={"message": "balance read failed"})
                    for p, (ok, value) in zip(payload, items)]
        except (requests.RequestException, ValueError, DecodingError, TypeError):
            if attempt < 2:
                time.sleep(attempt + 1)
    return None


def fetch_erc20_balances(rpc_url, wallet, tokens, proxies=None, batch_size=60, use_multicall=True):
    if not Web3.is_address(wallet) or batch_size < 1:
        raise ValueError("Invalid wallet or balance batch size")
    balances, requests_by_id, decimals_by_id = {}, {}, {}
    for address, decimals in tokens:
        key = address.lower()
        if key in balances:
            continue
        balances[key] = None
        if not Web3.is_address(address) or type(decimals) is not int or not 0 <= decimals <= 36:
            continue
        identifier = len(requests_by_id) + 1
        requests_by_id[identifier] = dict(jsonrpc="2.0", id=identifier, method="eth_call", params=[
            {"to": Web3.to_checksum_address(address), "data": "0x70a08231" + wallet[2:].lower().zfill(64)}, "latest"])
        decimals_by_id[identifier] = (key, decimals)
    identifiers = list(requests_by_id)
    with requests.Session() as session:
        session.trust_env = False
        for offset in range(0, len(identifiers), batch_size):
            pending = identifiers[offset:offset + batch_size]
            for attempt in range(3):
                payload = [requests_by_id[i] for i in pending]
                try:
                    answers = _multicall(session, rpc_url, payload, proxies) if use_multicall else None
                    if answers is None:
                        response = session.post(rpc_url, json=payload[0] if len(payload) == 1 else payload,
                                                proxies=proxies, timeout=(5, 15))
                        response.raise_for_status()
                        answers = response.json()
                    if isinstance(answers, dict):
                        answers = [answers]
                    if not isinstance(answers, list):
                        raise ValueError("Invalid balance RPC response")
                    seen = set()
                    for item in answers:
                        identifier = item.get("id") if isinstance(item, dict) else None
                        if identifier not in pending or identifier in seen:
                            continue
                        seen.add(identifier)
                        value = item.get("result")
                        if not isinstance(value, str) or len(value) != 66 or not value.startswith("0x"):
                            continue
                        try:
                            raw = int(value, 16)
                        except ValueError:
                            continue
                        key, decimals = decimals_by_id[identifier]
                        with localcontext() as context:
                            context.prec = 100
                            balances[key] = Decimal(raw) / (Decimal(10) ** decimals)
                    pending = [i for i in pending if balances[decimals_by_id[i][0]] is None]
                    if not pending:
                        break
                except (requests.RequestException, ValueError):
                    pass
                if attempt < 2:
                    time.sleep(0.25 * (attempt + 1))
    return balances
