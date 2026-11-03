"""Read-only Doma Punks claim checker using the claim page's public API."""
import re
from decimal import Decimal, localcontext

import requests


CLAIM_URL = "https://domapunks.com/api/chain/claim"


def _uint(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,78}", value):
        raise ValueError("Invalid integer in Doma Punks response")
    number = int(value)
    if number >= 2**256:
        raise ValueError("Doma Punks integer exceeds uint256")
    return number


def _token_amount(raw, decimals):
    if type(decimals) is not int or not 0 <= decimals <= 36:
        raise ValueError("Invalid token decimals in Doma Punks response")
    with localcontext() as context:
        context.prec = 120
        return Decimal(_uint(raw)) / (Decimal(10) ** decimals)


def parse_domapunks_claim(payload, wallet, chain_id):
    if not isinstance(payload, dict) or payload.get("chainId") != chain_id:
        raise ValueError("Unexpected Doma Punks chain or response")
    account = payload.get("wallet")
    if not isinstance(account, dict) or str(account.get("account", "")).lower() != wallet.lower():
        raise ValueError("Doma Punks response wallet mismatch")
    status = account.get("eligibility")
    if status not in {"eligible", "ineligible", "claimed"}:
        raise ValueError("Unknown Doma Punks eligibility status")
    claims = payload.get("claims")
    if claims not in {"open", "closed", "unavailable", "sold_out"}:
        raise ValueError("Unknown Doma Punks claim availability")
    payment, quote = payload.get("paymentToken"), payload.get("quoteToken")
    if not isinstance(payment, dict) or not isinstance(quote, dict):
        raise ValueError("Missing Doma Punks token metadata")
    if not isinstance(payment.get("symbol"), str) or not isinstance(quote.get("symbol"), str):
        raise ValueError("Missing Doma Punks token symbols")
    block = payload.get("block")
    if type(block) is not int or block < 0:
        raise ValueError("Missing Doma Punks block number")
    price = _token_amount(payload.get("price"), payment.get("decimals"))
    balance = _token_amount(account.get("balance"), payment.get("decimals"))
    quote_balance = _token_amount(account.get("quoteBalance"), quote.get("decimals"))
    return dict(eligibility=status, claims=claims,
                punk_id=str(_uint(account.get("tokenId"))) if status == "claimed" else "",
                payment_symbol=payment["symbol"], price=price, balance=balance,
                quote_symbol=quote["symbol"], quote_balance=quote_balance,
                claimed=_uint(payload.get("claimed")), supply=_uint(payload.get("supply")),
                block=block, chain_id=chain_id)


def fetch_domapunks_claim(wallet, chain_id=97477, proxies=None):
    if not isinstance(wallet, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", wallet):
        raise ValueError("Invalid wallet address for Doma Punks checker")
    response = requests.get(CLAIM_URL, params={"account": wallet}, proxies=proxies,
                            headers={"Accept": "application/json", "Cache-Control": "no-cache"}, timeout=25)
    response.raise_for_status()
    return parse_domapunks_claim(response.json(), wallet, chain_id)
