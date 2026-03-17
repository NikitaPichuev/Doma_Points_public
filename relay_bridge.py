from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, List, Optional, Tuple
import time

import requests

from web3 import Web3

from config import BotConfig


NATIVE_ETH = "0x0000000000000000000000000000000000000000"


@dataclass
class ChainToken:
    chain_id: int
    symbol: str
    address: str
    decimals: int


@dataclass
class BridgeTask:
    src_chain: str
    dst_chain: str
    src_symbol: str
    dst_symbol: str
    amount_expr: str


class RelayBridgeClient:
    def __init__(self, proxies: Optional[Dict[str, str]] = None) -> None:
        self.proxies = proxies or None
        self.chains = self._fetch_chains()
        self.chain_by_name = {c["name"].lower(): c for c in self.chains}
        self.chain_by_id = {int(c["id"]): c for c in self.chains}

    def _fetch_chains(self) -> List[dict]:
        r = requests.get("https://api.relay.link/chains", timeout=20, proxies=self.proxies)
        r.raise_for_status()
        return r.json().get("chains", [])

    def chain_id(self, name: str) -> int:
        c = self.chain_by_name.get(name.lower())
        if not c:
            raise ValueError(f"Unknown chain name: {name}")
        return int(c["id"])

    def chain_rpc(self, chain_id: int) -> str:
        return str(self.chain_by_id[chain_id]["httpRpcUrl"])

    def resolve_token(self, chain_id: int, symbol: str) -> ChainToken:
        c = self.chain_by_id[chain_id]
        sym = symbol.upper()
        if sym == "ETH":
            cur = c["currency"]
            return ChainToken(chain_id=chain_id, symbol="ETH", address=cur["address"].lower(), decimals=int(cur["decimals"]))
        for t in c.get("erc20Currencies") or []:
            if str(t.get("symbol", "")).upper() == sym:
                return ChainToken(
                    chain_id=chain_id,
                    symbol=sym,
                    address=str(t["address"]).lower(),
                    decimals=int(t["decimals"]),
                )
        raise ValueError(f"Token {symbol} not found on chain {c['name']}")

    def quote(self, user: str, recipient: str, src_chain_id: int, dst_chain_id: int, src_currency: str, dst_currency: str, amount_raw: int) -> dict:
        payload = {
            "user": user,
            "recipient": recipient,
            "originChainId": src_chain_id,
            "destinationChainId": dst_chain_id,
            "originCurrency": src_currency,
            "destinationCurrency": dst_currency,
            "amount": str(amount_raw),
            "tradeType": "EXACT_INPUT",
        }
        r = requests.post("https://api.relay.link/quote/v2", json=payload, timeout=30, proxies=self.proxies)
        r.raise_for_status()
        return r.json()

    def config(self, user: str, src_chain_id: int, dst_chain_id: int, currency_address: str = NATIVE_ETH) -> dict:
        params = {
            "originChainId": str(src_chain_id),
            "destinationChainId": str(dst_chain_id),
            "user": user,
            "currency": currency_address,
        }
        r = requests.get("https://api.relay.link/config", params=params, timeout=20, proxies=self.proxies)
        r.raise_for_status()
        return r.json()


def parse_amount_expr(expr: str) -> Tuple[str, Decimal]:
    s = (expr or "").strip().lower()
    if not s:
        raise ValueError("Empty amount expression")
    if s.endswith("%"):
        return "percent", Decimal(s[:-1].strip())
    if s.startswith("$"):
        return "usd", Decimal(s[1:].strip())
    if s.endswith("usd"):
        return "usd", Decimal(s[:-3].strip())
    return "token", Decimal(s)


def parse_bridge_task(raw: str) -> BridgeTask:
    # Format: src>dst:tokenIn>tokenOut:amount
    left, pair, amount = [x.strip() for x in raw.split(":", 2)]
    src_chain, dst_chain = [x.strip().lower() for x in left.split(">", 1)]
    src_symbol, dst_symbol = [x.strip().upper() for x in pair.split(">", 1)]
    return BridgeTask(
        src_chain=src_chain,
        dst_chain=dst_chain,
        src_symbol=src_symbol,
        dst_symbol=dst_symbol,
        amount_expr=amount,
    )


def _token_usd_hint(symbol: str, eth_price: Decimal) -> Decimal:
    s = symbol.upper()
    if s in {"USDC", "USDC.E", "USDT", "DAI"}:
        return Decimal("1")
    if s == "ETH":
        return eth_price
    return Decimal("0")


def _rpc_client(rpc_url: str, proxies: Optional[Dict[str, str]]) -> Web3:
    kwargs = {"timeout": 20}
    if proxies:
        kwargs["proxies"] = proxies
    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs=kwargs))
    if not w3.is_connected():
        raise RuntimeError(f"RPC not connected: {rpc_url}")
    return w3


def _rpc_urls_for_chain(relay: RelayBridgeClient, chain_id: int) -> List[str]:
    urls: List[str] = []
    primary = relay.chain_rpc(chain_id)
    if primary:
        urls.append(primary)
    # Stable public backups for better reliability.
    if chain_id == 8453:
        urls.extend(["https://mainnet.base.org", "https://base.drpc.org"])
    if chain_id == 97477:
        urls.extend(["https://doma.drpc.org", "https://rpc.doma.xyz"])
    dedup: List[str] = []
    for u in urls:
        if u and u not in dedup:
            dedup.append(u)
    return dedup


def _source_balance(w3: Web3, wallet: str, token: ChainToken) -> Decimal:
    wallet_cs = Web3.to_checksum_address(wallet)
    if token.address == NATIVE_ETH:
        return Decimal(w3.eth.get_balance(wallet_cs)) / (Decimal(10) ** token.decimals)
    erc20_abi = [
        {
            "constant": True,
            "inputs": [{"name": "_owner", "type": "address"}],
            "name": "balanceOf",
            "outputs": [{"name": "balance", "type": "uint256"}],
            "type": "function",
        }
    ]
    c = w3.eth.contract(address=Web3.to_checksum_address(token.address), abi=erc20_abi)
    raw = int(c.functions.balanceOf(wallet_cs).call())
    return Decimal(raw) / (Decimal(10) ** token.decimals)


def _resolve_amount_raw(expr: str, balance_dec: Decimal, token: ChainToken, eth_price: Decimal) -> int:
    mode, value = parse_amount_expr(expr)
    if value <= 0:
        raise ValueError("Amount must be > 0")
    if mode == "percent":
        if value > 100:
            raise ValueError("Percent amount > 100")
        amount_dec = balance_dec * value / Decimal("100")
    elif mode == "usd":
        usd_price = _token_usd_hint(token.symbol, eth_price)
        if usd_price <= 0:
            raise ValueError("USD amount unsupported for this token")
        amount_dec = value / usd_price
    else:
        amount_dec = value
    if amount_dec > balance_dec:
        raise ValueError(f"Insufficient balance: need {amount_dec}, have {balance_dec}")
    return int((amount_dec * (Decimal(10) ** token.decimals)).to_integral_value())


def _resolve_amount_raw_via_config(
    relay: RelayBridgeClient,
    wallet_cs: str,
    src_chain_id: int,
    dst_chain_id: int,
    token: ChainToken,
    expr: str,
    eth_price_usd: Decimal,
) -> int:
    mode, value = parse_amount_expr(expr)
    if value <= 0:
        raise ValueError("Amount must be > 0")

    currency_for_config = NATIVE_ETH if token.symbol.upper() == "ETH" else (token.address or NATIVE_ETH)
    cfg = relay.config(
        user=wallet_cs,
        src_chain_id=src_chain_id,
        dst_chain_id=dst_chain_id,
        currency_address=currency_for_config,
    )
    user_cfg = cfg.get("user") or {}
    bal_raw = int(str(user_cfg.get("balance") or "0"))
    if bal_raw <= 0:
        # Some routes may return zero from config; fallback to direct chain balance.
        balance_dec = Decimal("0")
        rpc_errors: List[str] = []
        for rpc_url in _rpc_urls_for_chain(relay, src_chain_id):
            try:
                w3 = _rpc_client(rpc_url, relay.proxies)
                balance_dec = _source_balance(w3, wallet_cs, token)
                if balance_dec > 0:
                    bal_raw = int((balance_dec * (Decimal(10) ** token.decimals)).to_integral_value())
                    break
            except Exception as rpc_exc:
                rpc_errors.append(f"{rpc_url}: {rpc_exc}")
        if bal_raw <= 0:
            for rpc_url in _rpc_urls_for_chain(relay, src_chain_id):
                try:
                    w3 = _rpc_client(rpc_url, None)
                    balance_dec = _source_balance(w3, wallet_cs, token)
                    if balance_dec > 0:
                        bal_raw = int((balance_dec * (Decimal(10) ** token.decimals)).to_integral_value())
                        break
                except Exception as rpc_exc:
                    rpc_errors.append(f"{rpc_url} (no-proxy): {rpc_exc}")
        if bal_raw <= 0:
            raise ValueError("Zero balance for selected bridge source token")
    balance_dec = Decimal(bal_raw) / (Decimal(10) ** token.decimals)

    if mode == "percent":
        if value > 100:
            raise ValueError("Percent amount > 100")
        out_raw = int((Decimal(bal_raw) * value / Decimal("100")).to_integral_value())
        if out_raw <= 0:
            raise ValueError("Calculated amount is too small")
        return out_raw

    if mode == "usd":
        usd_price = _token_usd_hint(token.symbol, eth_price_usd)
        if usd_price <= 0:
            raise ValueError("USD amount unsupported for this token")
        out_dec = value / usd_price
        out_raw = int((out_dec * (Decimal(10) ** token.decimals)).to_integral_value())
        if out_raw <= 0:
            raise ValueError("Calculated amount is too small")
        if out_raw > bal_raw:
            raise ValueError(f"Insufficient balance: need {out_dec}, have {balance_dec}")
        return out_raw

    # token amount mode
    out_dec = value
    out_raw = int((out_dec * (Decimal(10) ** token.decimals)).to_integral_value())
    if out_raw <= 0:
        raise ValueError("Calculated amount is too small")
    if out_raw > bal_raw:
        raise ValueError(f"Insufficient balance: need {out_dec}, have {balance_dec}")
    return out_raw


def _poll_intent_status(
    logger,
    proxies: Optional[Dict[str, str]],
    endpoint: str,
    method: str = "GET",
    timeout_sec: int = 180,
    interval_sec: int = 5,
) -> Optional[str]:
    if not endpoint:
        return None
    method_up = (method or "GET").strip().upper()
    url = endpoint.strip()
    if url.startswith("/"):
        url = "https://api.relay.link" + url

    deadline = time.time() + max(1, timeout_sec)
    last_status = ""
    while time.time() < deadline:
        try:
            if method_up == "POST":
                r = requests.post(url, timeout=20, proxies=proxies)
            else:
                r = requests.get(url, timeout=20, proxies=proxies)
            r.raise_for_status()
            body = r.json() if r.content else {}
            status = str(body.get("status", "")).lower().strip()
            if status and status != last_status:
                logger.info("[BRIDGE] intent status: %s | %s", status, url)
                last_status = status
            if status in {"success", "submitted", "refund", "failure", "fallback"}:
                return status
        except Exception as exc:
            logger.warning("[BRIDGE] intent status poll failed: %s", exc)
        time.sleep(max(1, interval_sec))
    return last_status or None


def execute_relay_swap(
    logger,
    wallet: str,
    private_key: str,
    chain_id: int,
    origin_currency: str,
    destination_currency: str,
    amount_raw: int,
    proxies: Optional[Dict[str, str]] = None,
) -> bool:
    wallet_cs = Web3.to_checksum_address(wallet)
    payload = {
        "user": wallet_cs,
        "recipient": wallet_cs,
        "originChainId": int(chain_id),
        "destinationChainId": int(chain_id),
        "originCurrency": origin_currency,
        "destinationCurrency": destination_currency,
        "amount": str(int(amount_raw)),
        "tradeType": "EXACT_INPUT",
    }
    r = requests.post("https://api.relay.link/execute/swap", json=payload, timeout=30, proxies=proxies)
    r.raise_for_status()
    data = r.json()
    steps = data.get("steps") or []
    if not steps:
        raise RuntimeError("Relay execute/swap returned no steps")
    step = steps[0]
    item = ((step.get("items") or [])[0] if step else None) or {}
    txd = item.get("data") or {}
    check = item.get("check") or {}
    request_id = step.get("requestId", "")
    logger.info(
        "[RELAY_SWAP] chain=%s %s -> %s amount_raw=%s request_id=%s",
        chain_id,
        origin_currency,
        destination_currency,
        amount_raw,
        request_id,
    )
    if not txd:
        raise RuntimeError("Relay swap has no tx data")

    rpc_errors: List[str] = []
    sent = False
    for rpc_url in _rpc_urls_for_chain(RelayBridgeClient(proxies=proxies), int(chain_id)):
        try:
            w3 = _rpc_client(rpc_url, proxies)
            nonce = w3.eth.get_transaction_count(wallet_cs, "pending")
            tx = {
                "from": wallet_cs,
                "to": Web3.to_checksum_address(txd["to"]),
                "data": txd["data"],
                "value": int(txd.get("value", "0")),
                "chainId": int(txd["chainId"]),
                "nonce": nonce,
                "gas": int(txd.get("gas", "350000")),
            }
            if "maxFeePerGas" in txd and "maxPriorityFeePerGas" in txd:
                tx["maxFeePerGas"] = int(txd["maxFeePerGas"])
                tx["maxPriorityFeePerGas"] = int(txd["maxPriorityFeePerGas"])
            else:
                tx["gasPrice"] = int(w3.eth.gas_price)

            signed = w3.eth.account.sign_transaction(tx, private_key=private_key)
            tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction).hex()
            logger.info("[RELAY_SWAP] tx sent via %s: %s", rpc_url, tx_hash)
            endpoint = str(check.get("endpoint") or "").strip()
            method = str(check.get("method") or "GET").strip().upper()
            final_status = None
            if endpoint:
                final_status = _poll_intent_status(
                    logger=logger,
                    proxies=proxies,
                    endpoint=endpoint,
                    method=method,
                    timeout_sec=180,
                    interval_sec=5,
                )
            if final_status in {"success", "submitted"}:
                logger.info("[RELAY_SWAP] final status: %s", final_status)
                sent = True
                break
            if final_status in {"failure", "fallback", "refund"}:
                raise RuntimeError(f"Relay swap final status: {final_status}")
            # If no endpoint status, consider tx sent as best-effort success.
            sent = True
            break
        except Exception as rpc_exc:
            rpc_errors.append(f"{rpc_url}: {rpc_exc}")

    if not sent:
        raise RuntimeError("All swap RPC attempts failed: " + " | ".join(rpc_errors))
    return True


def run_bridge_tasks(
    cfg: BotConfig,
    logger,
    wallet: str,
    private_key: str,
    tasks: List[str],
    proxies: Optional[Dict[str, str]],
    eth_price_usd: Decimal,
    force_run: bool = False,
) -> None:
    if not tasks:
        return
    if not force_run and not cfg.bridge_enabled:
        return
    relay = RelayBridgeClient(proxies=proxies)
    wallet_cs = Web3.to_checksum_address(wallet)

    for raw_task in tasks:
        try:
            task = parse_bridge_task(raw_task)
            src_chain_id = relay.chain_id(task.src_chain)
            dst_chain_id = relay.chain_id(task.dst_chain)
            src_token = relay.resolve_token(src_chain_id, task.src_symbol)
            dst_token = relay.resolve_token(dst_chain_id, task.dst_symbol)
            amount_raw = _resolve_amount_raw_via_config(
                relay=relay,
                wallet_cs=wallet_cs,
                src_chain_id=src_chain_id,
                dst_chain_id=dst_chain_id,
                token=src_token,
                expr=task.amount_expr,
                eth_price_usd=eth_price_usd,
            )

            q = relay.quote(
                user=wallet_cs,
                recipient=wallet_cs,
                src_chain_id=src_chain_id,
                dst_chain_id=dst_chain_id,
                src_currency=src_token.address,
                dst_currency=dst_token.address,
                amount_raw=amount_raw,
            )
            step = (q.get("steps") or [])[0]
            item = ((step.get("items") or [])[0] if step else None) or {}
            txd = item.get("data") or {}
            check = item.get("check") or {}
            request_id = step.get("requestId", "")
            logger.info(
                "[BRIDGE] %s | %s(%s) -> %s(%s) %s->%s amount_raw=%s request_id=%s",
                raw_task,
                task.src_chain,
                src_chain_id,
                task.dst_chain,
                dst_chain_id,
                src_token.symbol,
                dst_token.symbol,
                amount_raw,
                request_id,
            )

            if cfg.paper_mode or cfg.dry_run or not cfg.enable_execution:
                logger.info("[BRIDGE] PAPER/DRY mode: tx not sent for task: %s", raw_task)
                continue

            if not txd:
                raise RuntimeError("Relay quote has no tx data")
            rpc_errors: List[str] = []
            sent = False
            for rpc_url in _rpc_urls_for_chain(relay, src_chain_id):
                try:
                    w3 = _rpc_client(rpc_url, proxies)
                    nonce = w3.eth.get_transaction_count(wallet_cs, "pending")
                    tx = {
                        "from": wallet_cs,
                        "to": Web3.to_checksum_address(txd["to"]),
                        "data": txd["data"],
                        "value": int(txd.get("value", "0")),
                        "chainId": int(txd["chainId"]),
                        "nonce": nonce,
                        "gas": int(txd.get("gas", "350000")),
                    }
                    if "maxFeePerGas" in txd and "maxPriorityFeePerGas" in txd:
                        tx["maxFeePerGas"] = int(txd["maxFeePerGas"])
                        tx["maxPriorityFeePerGas"] = int(txd["maxPriorityFeePerGas"])
                    else:
                        tx["gasPrice"] = int(w3.eth.gas_price)

                    signed = w3.eth.account.sign_transaction(tx, private_key=private_key)
                    tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction).hex()
                    logger.info("[BRIDGE] Tx sent for task %s via %s: %s", raw_task, rpc_url, tx_hash)
                    endpoint = str(check.get("endpoint") or "").strip()
                    method = str(check.get("method") or "GET").strip().upper()
                    if endpoint:
                        final_status = _poll_intent_status(
                            logger=logger,
                            proxies=proxies,
                            endpoint=endpoint,
                            method=method,
                        )
                        if final_status in {"failure", "fallback", "refund"}:
                            logger.warning("[BRIDGE] final status for %s: %s", raw_task, final_status)
                        elif final_status in {"success", "submitted"}:
                            logger.info("[BRIDGE] final status for %s: %s", raw_task, final_status)
                        else:
                            logger.warning("[BRIDGE] status timeout/unknown for %s", raw_task)
                    sent = True
                    break
                except Exception as rpc_exc:
                    rpc_errors.append(f"{rpc_url}: {rpc_exc}")
            if not sent:
                raise RuntimeError("All RPC attempts failed: " + " | ".join(rpc_errors))
        except Exception as exc:
            logger.warning("[BRIDGE] Task failed (%s): %s", raw_task, exc)
