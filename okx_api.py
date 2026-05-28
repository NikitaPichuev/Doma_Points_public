from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import requests


class OkxApiError(RuntimeError):
    pass


class OkxApiClient:
    def __init__(
        self,
        api_key: str,
        secret_key: str,
        passphrase: str,
        base_url: str = "https://www.okx.com",
        proxies: Optional[Dict[str, str]] = None,
        timeout: int = 30,
    ) -> None:
        self.api_key = api_key.strip()
        self.secret_key = secret_key.strip()
        self.passphrase = passphrase.strip()
        self.base_url = base_url.rstrip("/")
        self.proxies = proxies
        self.timeout = timeout
        if not self.api_key or not self.secret_key or not self.passphrase:
            raise ValueError("OKX API key, secret key and passphrase are required")

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def _sign(self, timestamp: str, method: str, request_path: str, body: str) -> str:
        prehash = f"{timestamp}{method.upper()}{request_path}{body}"
        digest = hmac.new(self.secret_key.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256).digest()
        return base64.b64encode(digest).decode("ascii")

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        method = method.upper()
        request_path = path
        if params:
            query = urlencode({k: v for k, v in params.items() if v is not None})
            if query:
                request_path = f"{path}?{query}"
        body_text = json.dumps(body or {}, separators=(",", ":")) if method != "GET" else ""
        timestamp = self._timestamp()
        headers = {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": self._sign(timestamp, method, request_path, body_text),
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
        }
        response = requests.request(
            method,
            f"{self.base_url}{request_path}",
            headers=headers,
            data=body_text if body_text else None,
            timeout=self.timeout,
            proxies=self.proxies,
        )
        try:
            payload = response.json()
        except Exception as exc:
            raise OkxApiError(f"OKX non-JSON response HTTP {response.status_code}: {response.text[:500]}") from exc
        if response.status_code >= 400:
            raise OkxApiError(f"OKX HTTP {response.status_code}: {payload}")
        if str(payload.get("code", "")) != "0":
            raise OkxApiError(f"OKX error code={payload.get('code')} msg={payload.get('msg')} data={payload.get('data')}")
        return payload

    def get_currencies(self, ccy: str) -> List[Dict[str, Any]]:
        payload = self._request("GET", "/api/v5/asset/currencies", params={"ccy": ccy.upper()})
        return list(payload.get("data") or [])

    def get_min_withdraw_fee(self, ccy: str, chain: str) -> Decimal:
        chain_key = chain.strip().lower()
        for item in self.get_currencies(ccy):
            if str(item.get("chain") or "").strip().lower() != chain_key:
                continue
            fee_raw = str(item.get("minFee") or item.get("minWdFee") or "").strip()
            if not fee_raw:
                raise OkxApiError(f"OKX currency entry has no min fee for {ccy} {chain}: {item}")
            return Decimal(fee_raw)
        raise OkxApiError(f"OKX chain not found for {ccy}: {chain}")

    def withdraw(
        self,
        ccy: str,
        chain: str,
        amount: Decimal,
        fee: Decimal,
        to_address: str,
        client_id: str = "",
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "ccy": ccy.upper(),
            "amt": str(amount),
            "dest": "4",
            "toAddr": to_address,
            "chain": chain,
            "fee": str(fee),
        }
        if client_id:
            body["clientId"] = client_id[:32]
        payload = self._request("POST", "/api/v5/asset/withdrawal", body=body)
        data = payload.get("data") or []
        return dict(data[0] or {}) if data else {}
