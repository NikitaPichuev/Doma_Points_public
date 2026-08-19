from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from eth_account import Account
from eth_account.messages import encode_defunct


GALXE_GRAPHQL_URL = "https://graphigo.prd.galaxy.eco/query"
GALXE_DEFAULT_CAMPAIGN_ID = "GCLw6tZ6jC"
GALXE_DEFAULT_QUEST_URL = f"https://app.galxe.com/quest/D3/{GALXE_DEFAULT_CAMPAIGN_ID}"
GALXE_DEFAULT_CHAIN = "ETHEREUM"
GALXE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/137.0.0.0 Safari/537.36"
)


@dataclass
class GalxeCondition:
    cred_id: str
    name: str
    source: str
    eligible: bool
    reference_link: str = ""


@dataclass
class GalxeCampaignStatus:
    campaign_id: str
    number_id: int
    name: str
    participation_status: str
    claimed_times: int
    conditions: List[GalxeCondition]

    @property
    def eligible(self) -> bool:
        return bool(self.conditions) and all(c.eligible for c in self.conditions)

    @property
    def missing_conditions(self) -> List[GalxeCondition]:
        return [c for c in self.conditions if not c.eligible]


class GalxeApiClient:
    def __init__(
        self,
        *,
        graphql_url: str = GALXE_GRAPHQL_URL,
        proxies: Optional[Dict[str, str]] = None,
        timeout: int = 30,
    ) -> None:
        self.graphql_url = graphql_url
        self.proxies = proxies
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "accept": "*/*",
                "content-type": "application/json",
                "origin": "https://app.galxe.com",
                "referer": GALXE_DEFAULT_QUEST_URL,
                "user-agent": GALXE_USER_AGENT,
            }
        )

    def graphql(
        self,
        query: str,
        variables: Optional[Dict[str, Any]] = None,
        *,
        token: str = "",
    ) -> Dict[str, Any]:
        headers = {}
        if token:
            headers["authorization"] = token
        resp = self.session.post(
            self.graphql_url,
            json={"query": query, "variables": variables or {}},
            headers=headers,
            proxies=self.proxies,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            raise RuntimeError(f"Galxe API error: {data['errors']}")
        return data.get("data") or {}

    def nonce(self) -> str:
        return str(self.graphql("mutation { nonce }")["nonce"])

    def signin(self, private_key: str, *, chain_id: int = 1) -> str:
        acct = Account.from_key(private_key)
        nonce = self.nonce()
        issued_at = datetime.now(timezone.utc).replace(microsecond=0)
        expires_at = issued_at + timedelta(days=1)
        message = (
            "app.galxe.com wants you to sign in with your Ethereum account:\n"
            f"{acct.address}\n\n"
            "Sign in with Ethereum to the app.\n\n"
            "URI: https://app.galxe.com\n"
            "Version: 1\n"
            f"Chain ID: {chain_id}\n"
            f"Nonce: {nonce}\n"
            f"Issued At: {issued_at.isoformat().replace('+00:00', 'Z')}\n"
            f"Expiration Time: {expires_at.isoformat().replace('+00:00', 'Z')}"
        )
        signature = Account.sign_message(encode_defunct(text=message), acct.key).signature.hex()
        if not signature.startswith("0x"):
            signature = "0x" + signature
        data = self.graphql(
            "mutation Signin($input: Auth) { signin(input:$input) }",
            {
                "input": {
                    "address": acct.address,
                    "addressType": "EVM",
                    "message": message,
                    "signature": signature,
                }
            },
        )
        return str(data["signin"])

    def campaign_status(self, campaign_id: str, address: str, *, token: str) -> GalxeCampaignStatus:
        query = """
        query CampaignStatus($id: ID!, $address: String!) {
          campaign(id: $id) {
            id
            numberID
            name
            participationStatus(address:$address)
            claimedTimes(address:$address)
            taskConfig(address: $address) {
              rewardConfigs {
                conditions {
                  eligible
                  cred {
                    id
                    name
                    credType
                    credSource
                    referenceLink
                    eligible(address:$address, campaignId:$id)
                  }
                }
              }
            }
          }
        }
        """
        data = self.graphql(query, {"id": campaign_id, "address": address}, token=token)
        campaign = data.get("campaign") or {}
        conditions: List[GalxeCondition] = []
        for reward_cfg in ((campaign.get("taskConfig") or {}).get("rewardConfigs") or []):
            for cond in reward_cfg.get("conditions") or []:
                cred = cond.get("cred") or {}
                conditions.append(
                    GalxeCondition(
                        cred_id=str(cred.get("id") or ""),
                        name=str(cred.get("name") or ""),
                        source=str(cred.get("credSource") or cred.get("credType") or ""),
                        eligible=bool(cond.get("eligible") or cred.get("eligible")),
                        reference_link=str(cred.get("referenceLink") or ""),
                    )
                )
        return GalxeCampaignStatus(
            campaign_id=str(campaign.get("id") or campaign_id),
            number_id=int(campaign.get("numberID") or 0),
            name=str(campaign.get("name") or ""),
            participation_status=str(campaign.get("participationStatus") or ""),
            claimed_times=int(campaign.get("claimedTimes") or 0),
            conditions=conditions,
        )

    def prepare_participate(
        self,
        *,
        campaign_id: str,
        address: str,
        token: str,
        captcha: Dict[str, Any],
        chain: str = GALXE_DEFAULT_CHAIN,
    ) -> Dict[str, Any]:
        query = """
        mutation PrepareParticipate($input: PrepareParticipateInput!) {
          prepareParticipate(input: $input) {
            allow
            disallowReason
            signature
            nonce
            spaceStationInfo { address chain version }
            mintFuncInfo {
              funcName
              nftCoreAddress
              verifyIDs
              powahs
              cap
              claimFeeAmount
            }
          }
        }
        """
        data = self.graphql(
            query,
            {
                "input": {
                    "signature": "",
                    "campaignID": campaign_id,
                    "address": address,
                    "addressType": "EVM",
                    "mintCount": 1,
                    "chain": chain,
                    "captcha": captcha,
                }
            },
            token=token,
        )
        return data["prepareParticipate"]

    def participate(
        self,
        *,
        campaign_id: str,
        address: str,
        token: str,
        signature: str,
        tx_hash: str,
        verify_ids: List[str],
        nonce: str,
        chain: str = GALXE_DEFAULT_CHAIN,
    ) -> Dict[str, Any]:
        query = """
        mutation Participate($input: ParticipateInput!) {
          participate(input: $input) {
            participated
            failReason
          }
        }
        """
        data = self.graphql(
            query,
            {
                "input": {
                    "signature": signature,
                    "address": address,
                    "tx": tx_hash,
                    "verifyIDs": verify_ids,
                    "chain": chain,
                    "campaignID": campaign_id,
                    "nonce": nonce,
                }
            },
            token=token,
        )
        return data["participate"]


def get_galxe_captcha_input(
    *,
    quest_url: str = GALXE_DEFAULT_QUEST_URL,
    helper_path: Path = Path("galxe_captcha.mjs"),
    proxy_url: str = "",
    timeout_sec: int = 120,
) -> Dict[str, Any]:
    cmd = ["node", str(helper_path), quest_url, "PrepareParticipate"]
    if proxy_url:
        cmd.append(proxy_url)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
    stdout = (proc.stdout or "").strip()
    if proc.returncode != 0:
        raise RuntimeError((stdout or proc.stderr or "Galxe captcha helper failed").strip())
    try:
        data = json.loads(stdout.splitlines()[-1])
    except Exception as exc:
        raise RuntimeError(f"failed to parse Galxe captcha helper output: {exc}; stdout={stdout}") from exc
    if not data.get("ok"):
        raise RuntimeError(str(data.get("error") or "Galxe captcha helper failed"))
    captcha = data.get("captcha") or {}
    required = ["lotNumber", "captchaOutput", "passToken", "genTime"]
    missing = [key for key in required if not captcha.get(key)]
    if missing:
        raise RuntimeError(f"Galxe captcha helper returned incomplete captcha: missing {', '.join(missing)}")
    captcha.setdefault("encryptedData", "")
    return captcha
