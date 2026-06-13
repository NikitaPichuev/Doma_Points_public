"""Privy SIWE auto-login for Doma daily rollcall.

Mints a fresh Privy access token from a wallet PRIVATE KEY. The current
captcha provider and site key are read from Privy's public app config.

Reverse-engineered from app.doma.xyz (2026-06-09):
  - Privy app id  : cm9jd3vun03ptju0knmkls1zp
  - Auth base     : https://auth.privy.io
  - Login         : POST /api/v1/siwe/init  -> POST /api/v1/siwe/authenticate
  - Refresh       : POST /api/v1/sessions   (NO captcha)
  - Captcha       : provider and site key are loaded dynamically
  - Access token  : ~60 min lifetime

COST OPTIMISATION:
  /siwe/init requires a captcha token (Turnstile) — that's the only step that
  costs a 2captcha solve. /api/v1/sessions (refresh) does NOT. So after the
  first login per wallet we persist the refresh_token and mint subsequent
  daily tokens via /sessions for free, until the refresh token expires (weeks).

Public entry point:
  mint_access_token(wallet, private_key, *, twocaptcha_key, chain_id=97477,
                    proxies=None, refresh_store=None, logger=None) -> str
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Optional

import requests
from eth_account import Account
from eth_account.messages import encode_defunct
from web3 import Web3

# --- Reverse-engineered Privy constants for the Doma app ---
PRIVY_APP_ID = "cm9jd3vun03ptju0knmkls1zp"
PRIVY_CLIENT_ID = "client-WY5iumRYaVR7RsCRhMD2ACF3MssqVB59ebRgJbpZwUxZd"
PRIVY_API_BASE_URL = "https://auth.privy.io"
PRIVY_CLIENT_VERSION = "react-auth:3.25.0"

DOMA_PAGE_URL = "https://app.doma.xyz"
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/137.0.0.0 Safari/537.36"
)

TWOCAPTCHA_IN = "https://2captcha.com/in.php"
TWOCAPTCHA_RES = "https://2captcha.com/res.php"
CAPTCHASONIC_API_URL = "https://api.captchasonic.com"


def check_proxy(proxies: Optional[Dict[str, str]], timeout: float = 10.0) -> bool:
    """Cheap connectivity check (NO captcha): GET the public Privy app config
    through `proxies`. Returns True if the proxy can reach auth.privy.io.
    Used to skip dead proxies BEFORE spending a 2captcha solve."""
    try:
        r = requests.get(
            f"{PRIVY_API_BASE_URL}/api/v1/apps/{PRIVY_APP_ID}",
            headers=_privy_headers(),
            timeout=timeout,
            proxies=proxies,
        )
        return r.status_code == 200
    except Exception:
        return False


def find_working_proxy(candidates, logger=None):
    """candidates: list of (label, proxies_dict_or_None). Returns the first that
    passes check_proxy(), as (label, proxies). (None, None) if none work."""
    for label, pdict in candidates:
        if check_proxy(pdict):
            if logger:
                logger.info("[ROLLCALL] proxy ok: %s", label)
            return label, pdict
        if logger:
            logger.warning("[ROLLCALL] proxy dead: %s", label)
    return None, None


def _privy_headers() -> Dict[str, str]:
    return {
        "accept": "application/json",
        "content-type": "application/json",
        "origin": DOMA_PAGE_URL,
        "referer": DOMA_PAGE_URL + "/",
        "privy-app-id": PRIVY_APP_ID,
        "privy-client-id": PRIVY_CLIENT_ID,
        "privy-client": PRIVY_CLIENT_VERSION,
        "user-agent": BROWSER_USER_AGENT,
    }


# ----------------------------------------------------------------------------
# Live Privy captcha configuration and 2captcha solver
# ----------------------------------------------------------------------------
def fetch_privy_captcha_config(
    proxies: Optional[Dict[str, str]] = None,
) -> tuple[str, str]:
    """Return the currently configured Privy captcha provider and site key."""
    resp = requests.get(
        f"{PRIVY_API_BASE_URL}/api/v1/apps/{PRIVY_APP_ID}",
        headers=_privy_headers(),
        timeout=20,
        proxies=proxies,
    )
    resp.raise_for_status()
    data = resp.json()
    provider = str(data.get("enabled_captcha_provider") or "").strip().lower()
    sitekey = str(data.get("captcha_site_key") or "").strip()
    if ":" in sitekey and sitekey.split(":", 1)[0].lower() in {"h", "t"}:
        sitekey = sitekey.split(":", 1)[1]
    if provider not in {"hcaptcha", "turnstile"} or not sitekey:
        raise RuntimeError(
            "Unsupported or missing Privy captcha config: "
            f"provider={provider!r}, sitekey_present={bool(sitekey)}"
        )
    return provider, sitekey


def solve_captcha_2captcha(
    api_key: str,
    *,
    provider: str,
    sitekey: str,
    pageurl: str = DOMA_PAGE_URL,
    action: Optional[str] = None,
    cdata: Optional[str] = None,
    proxies: Optional[Dict[str, str]] = None,
    poll_interval: float = 5.0,
    timeout: float = 180.0,
    logger=None,
) -> str:
    """Submit a Turnstile job to 2captcha and poll until solved.
    Returns the Turnstile response token (the value Privy expects in init `token`)."""
    if not api_key:
        raise RuntimeError("2captcha API key is empty (set TWOCAPTCHA_API_KEY in .env)")

    provider = (provider or "").strip().lower()
    method = {"turnstile": "turnstile"}.get(provider)
    if not method:
        raise RuntimeError(f"Unsupported captcha provider: {provider!r}")

    payload = {
        "key": api_key,
        "method": method,
        "sitekey": sitekey,
        "pageurl": pageurl,
        "json": 1,
    }
    if action:
        payload["action"] = action
    if cdata:
        payload["data"] = cdata

    sub = requests.post(TWOCAPTCHA_IN, data=payload, timeout=30).json()
    if str(sub.get("status")) != "1":
        raise RuntimeError(f"2captcha submit failed: {sub}")
    captcha_id = sub["request"]
    if logger:
        logger.info(
            "[ROLLCALL] 2captcha %s job submitted id=%s - waiting for solve",
            provider,
            captcha_id,
        )

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(poll_interval)
        res = requests.get(
            TWOCAPTCHA_RES,
            params={"key": api_key, "action": "get", "id": captcha_id, "json": 1},
            timeout=30,
        ).json()
        status = str(res.get("status"))
        if status == "1":
            return res["request"]
        if res.get("request") != "CAPCHA_NOT_READY":
            raise RuntimeError(f"2captcha solve error: {res}")
    raise RuntimeError(f"2captcha timeout after {timeout:.0f}s (id={captcha_id})")


def solve_hcaptcha_captchasonic(
    api_key: str,
    *,
    sitekey: str,
    pageurl: str = DOMA_PAGE_URL,
    proxies: Optional[Dict[str, str]] = None,
    poll_interval: float = 3.0,
    timeout: float = 180.0,
    logger=None,
) -> tuple[str, str]:
    """Solve Privy's hCaptcha through CaptchaSonic browser automation."""
    if not api_key:
        raise RuntimeError("CaptchaSonic API key is empty (set CAPTCHASONIC_API_KEY in .env)")

    proxy = (proxies or {}).get("https") or (proxies or {}).get("http") or ""
    if not proxy:
        raise RuntimeError("CaptchaSonic hCaptcha requires the wallet-bound proxy")

    # CaptchaSonic's token API uses PopularTask for hCaptcha. The older
    # hcaptchatask alias can be accepted by createTask but produces tokens that
    # Privy rejects as invalid_captcha.
    task = {
        "type": "PopularTask",
        "websiteURL": pageurl,
        "websiteKey": sitekey,
        "proxy": proxy,
    }

    sub = requests.post(
        f"{CAPTCHASONIC_API_URL}/createTask",
        json={"apiKey": api_key, "task": task},
        timeout=30,
    ).json()
    if sub.get("errorId") or not sub.get("taskId"):
        raise RuntimeError(f"CaptchaSonic submit failed: {sub}")
    task_id = sub["taskId"]
    if logger:
        logger.info("[ROLLCALL] CaptchaSonic hcaptcha job submitted id=%s - waiting for solve", task_id)

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(poll_interval)
        result = requests.post(
            f"{CAPTCHASONIC_API_URL}/getTaskResult",
            json={"apiKey": api_key, "taskId": task_id},
            timeout=30,
        ).json()
        if result.get("errorId"):
            raise RuntimeError(f"CaptchaSonic solve error: {result}")
        if result.get("status") != "ready":
            continue
        raw_solution = result.get("solution") or {}
        solution = raw_solution if isinstance(raw_solution, dict) else {}
        token = next(
            (
                str(solution.get(key) or "").strip()
                for key in ("gRecaptchaResponse", "captchaToken", "token", "response")
                if str(solution.get(key) or "").strip()
            ),
            "",
        )
        if not token and isinstance(raw_solution, str):
            token = raw_solution.strip()
        if not token:
            raise RuntimeError(f"CaptchaSonic returned no token: {result}")
        solver_user_agent = str(
            solution.get("userAgent")
            or solution.get("user_agent")
            or BROWSER_USER_AGENT
        ).strip()
        if logger:
            logger.info(
                "[ROLLCALL] CaptchaSonic hcaptcha solved | solution_fields=%s",
                ",".join(sorted(str(key) for key in solution.keys())) if solution else "token",
            )
        return token, solver_user_agent
    raise RuntimeError(f"CaptchaSonic timeout after {timeout:.0f}s (id={task_id})")


# ----------------------------------------------------------------------------
# Refresh-token persistence (JSON dict keyed by lowercase wallet)
# ----------------------------------------------------------------------------
class RefreshTokenStore:
    """Persists {wallet: {"refresh": ..., "access": ...}}.

    /api/v1/sessions requires BOTH the (possibly expired) access token as
    Authorization Bearer AND the refresh_token in the body — so we keep both.
    Backward compatible with the old {wallet: "<refresh string>"} format.
    """
    def __init__(self, path: Path):
        self.path = Path(path)
        self._data: Dict[str, Dict[str, str]] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8")) or {}
            except Exception:
                raw = {}
            # migrate old format (value was a plain refresh string)
            for k, v in raw.items():
                if isinstance(v, str):
                    self._data[k] = {"refresh": v, "access": ""}
                elif isinstance(v, dict):
                    self._data[k] = {"refresh": v.get("refresh", ""), "access": v.get("access", "")}

    def get_refresh(self, wallet: str) -> str:
        return self._data.get((wallet or "").lower(), {}).get("refresh", "")

    def get_access(self, wallet: str) -> str:
        return self._data.get((wallet or "").lower(), {}).get("access", "")

    def set(self, wallet: str, refresh_token: str = "", access_token: str = "") -> None:
        key = (wallet or "").lower()
        entry = self._data.get(key, {"refresh": "", "access": ""})
        if refresh_token:
            entry["refresh"] = refresh_token
        if access_token:
            entry["access"] = access_token
        self._data[key] = entry
        self._save()

    def drop(self, wallet: str) -> None:
        if (wallet or "").lower() in self._data:
            del self._data[(wallet or "").lower()]
            self._save()

    def _save(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        tmp.replace(self.path)


# ----------------------------------------------------------------------------
# Core SIWE flow
# ----------------------------------------------------------------------------
def _build_siwe_message(checksum_wallet: str, nonce: str, chain_id: int) -> str:
    issued_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return (
        "app.doma.xyz wants you to sign in with your Ethereum account:\n"
        f"{checksum_wallet}\n\n"
        "By signing, you are proving you own this wallet and logging in. "
        "This does not initiate a transaction or cost any fees.\n\n"
        "URI: https://app.doma.xyz\n"
        "Version: 1\n"
        f"Chain ID: {int(chain_id)}\n"
        f"Nonce: {nonce}\n"
        f"Issued At: {issued_at}\n"
        "Resources:\n"
        "- https://privy.io"
    )


def _extract_access_token(data: dict) -> str:
    return str(
        data.get("token")
        or data.get("access_token")
        or data.get("privy_access_token")
        or data.get("accessToken")
        or ""
    ).strip()


def _extract_refresh_token(data: dict) -> str:
    return str(
        data.get("refresh_token")
        or data.get("privy_refresh_token")
        or data.get("refreshToken")
        or ""
    ).strip()


def _refresh_via_sessions(
    refresh_token: str,
    stale_access_token: str,
    proxies: Optional[Dict[str, str]] = None,
    logger=None,
) -> tuple[str, str]:
    """Exchange a refresh_token for a fresh access token. NO captcha.

    Privy's /api/v1/sessions requires BOTH:
      - Authorization: Bearer <access_token>   (may be expired — identifies session)
      - body { refresh_token }
    Returns (access_token, new_refresh_token). Raises on failure."""
    headers = dict(_privy_headers())
    if stale_access_token:
        headers["Authorization"] = f"Bearer {stale_access_token}"
    resp = requests.post(
        f"{PRIVY_API_BASE_URL}/api/v1/sessions",
        json={"refresh_token": refresh_token},
        headers=headers,
        timeout=20,
        proxies=proxies,
    )
    resp.raise_for_status()
    data = resp.json()
    access = _extract_access_token(data)
    if not access:
        raise RuntimeError(f"sessions refresh: no access token in {sorted(data.keys())}")
    new_refresh = _extract_refresh_token(data) or refresh_token
    return access, new_refresh


def _full_siwe_login(
    wallet: str,
    private_key: str,
    chain_id: int,
    twocaptcha_key: str = "",
    captchasonic_key: str = "",
    proxies: Optional[Dict[str, str]] = None,
    logger=None,
) -> tuple[str, str]:
    """Full login: captcha -> init -> sign -> authenticate.
    Returns (access_token, refresh_token). Raises on failure."""
    checksum_wallet = Web3.to_checksum_address(wallet)
    headers = _privy_headers()

    # Read the live provider/site key because Doma can change captcha providers
    # without releasing a new client version.
    captcha_provider, captcha_sitekey = fetch_privy_captcha_config(proxies=proxies)

    # Use one paid solution per login attempt. Retrying rejected tokens here can
    # charge the user twice without changing the underlying request context.
    if captcha_provider == "hcaptcha":
        captcha_token, captcha_user_agent = solve_hcaptcha_captchasonic(
            captchasonic_key,
            sitekey=captcha_sitekey,
            proxies=proxies,
            logger=logger,
        )
        headers["user-agent"] = captcha_user_agent
    else:
        captcha_token = solve_captcha_2captcha(
            twocaptcha_key,
            provider=captcha_provider,
            sitekey=captcha_sitekey,
            proxies=proxies,
            logger=logger,
        )
    init_resp = requests.post(
        f"{PRIVY_API_BASE_URL}/api/v1/siwe/init",
        json={"address": checksum_wallet, "token": captcha_token},
        headers=headers,
        timeout=20,
        proxies=proxies,
    )
    if init_resp.status_code in (401, 403) or '"code"' in init_resp.text and "captcha" in init_resp.text.lower():
        raise RuntimeError(f"Privy init rejected (captcha?): {init_resp.status_code} {init_resp.text[:200]}")
    init_resp.raise_for_status()
    nonce = str(init_resp.json().get("nonce") or "").strip()
    if not nonce:
        raise RuntimeError("Privy SIWE nonce missing in init response")

    # 3. sign SIWE
    message = _build_siwe_message(checksum_wallet, nonce, chain_id)
    signed = Account.sign_message(encode_defunct(text=message), private_key=private_key)
    signature = Web3.to_hex(signed.signature)

    # 4. authenticate
    auth_resp = requests.post(
        f"{PRIVY_API_BASE_URL}/api/v1/siwe/authenticate",
        json={
            "message": message,
            "signature": signature,
            "chainId": f"eip155:{int(chain_id)}",
            "walletClientType": "metamask",
            "connectorType": "injected",
            "mode": "login-or-sign-up",
        },
        headers=headers,
        timeout=20,
        proxies=proxies,
    )
    auth_resp.raise_for_status()
    data = auth_resp.json()
    access = _extract_access_token(data)
    if not access:
        raise RuntimeError(f"authenticate: no access token in {sorted(data.keys())}")
    refresh = _extract_refresh_token(data)
    return access, refresh


def mint_access_token(
    wallet: str,
    private_key: str,
    *,
    twocaptcha_key: str = "",
    captchasonic_key: str = "",
    chain_id: int = 97477,
    proxies: Optional[Dict[str, str]] = None,
    refresh_store: Optional[RefreshTokenStore] = None,
    logger=None,
) -> str:
    """Get a fresh Privy access token for `wallet`.

    Strategy (cheapest first):
      1. If a saved refresh_token exists -> /sessions refresh (NO captcha).
      2. Otherwise (or if refresh failed) -> full SIWE login with 2captcha.
    On full login the new refresh_token is persisted for next time.
    """
    # 1. Try refresh-first (NO captcha)
    if refresh_store is not None:
        saved_refresh = refresh_store.get_refresh(wallet)
        saved_access = refresh_store.get_access(wallet)
        if saved_refresh and saved_access:
            try:
                access, new_refresh = _refresh_via_sessions(
                    saved_refresh, saved_access, proxies=proxies, logger=logger
                )
                refresh_store.set(wallet, refresh_token=new_refresh, access_token=access)
                if logger:
                    logger.info("[ROLLCALL] %s token via refresh (no captcha)", wallet)
                return access
            except Exception as exc:
                if logger:
                    logger.warning("[ROLLCALL] %s refresh failed (%s) -> full login", wallet, exc)
                refresh_store.drop(wallet)

    # 2. Full login (captcha)
    access, refresh = _full_siwe_login(
        wallet,
        private_key,
        chain_id,
        twocaptcha_key,
        captchasonic_key,
        proxies=proxies,
        logger=logger,
    )
    if refresh_store is not None:
        refresh_store.set(wallet, refresh_token=refresh, access_token=access)
    if logger:
        logger.info("[ROLLCALL] %s token via full SIWE login (captcha solved)", wallet)
    return access


# ----------------------------------------------------------------------------
# CLI test:  python privy_auth.py <private_key> <2captcha_key> [chain_id]
# ----------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print("usage: python privy_auth.py <private_key> <2captcha_api_key> [chain_id]")
        raise SystemExit(2)
    pk = sys.argv[1].strip()
    cap = sys.argv[2].strip()
    cid = int(sys.argv[3]) if len(sys.argv) > 3 else 97477
    addr = Account.from_key(pk).address
    print(f"wallet: {addr}")
    store = RefreshTokenStore(Path("privy_refresh_tokens.json"))

    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    log = logging.getLogger("privy_test")

    tok = mint_access_token(addr, pk, twocaptcha_key=cap, chain_id=cid,
                            refresh_store=store, logger=log)
    print(f"\nACCESS TOKEN (first 40 chars): {tok[:40]}...")
    # decode exp
    import base64
    try:
        p = tok.split(".")[1]
        p += "=" * ((4 - len(p) % 4) % 4)
        claims = json.loads(base64.urlsafe_b64decode(p))
        print(f"exp in: {round((claims.get('exp',0) - time.time())/60)} min")
    except Exception as e:
        print(f"(could not decode exp: {e})")
    print("\nSecond call (should use refresh, no captcha):")
    tok2 = mint_access_token(addr, pk, twocaptcha_key=cap, chain_id=cid,
                             refresh_store=store, logger=log)
    print(f"ACCESS TOKEN 2 (first 40): {tok2[:40]}...")
