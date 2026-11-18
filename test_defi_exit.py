import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import defi_exit as defi


WALLET = "0x" + "1" * 40
MARKET = "0x" + "2" * 40


class DefiExitTests(unittest.TestCase):
    def prepare(self, protocol, supplied):
        calls = {}

        def function(_client, _address, name, _inputs, _output, *args):
            result = supplied[name]
            fn = Mock()
            fn.call.return_value = result(*args) if callable(result) else result
            calls[name] = (args, fn)
            return fn

        client = SimpleNamespace(chain_id=defi.NETWORKS[protocol][0])
        with patch.object(defi, "_fn", side_effect=function):
            result = defi.prepare_exit(client, protocol, WALLET)
        return result, calls

    def test_mendi_does_not_redeem_when_another_market_has_debt(self):
        (plan, reason), calls = self.prepare("mendi", {
            "balanceOf": 55, "getAllMarkets": [defi.MENDI_USDC, MARKET],
            "borrowBalanceStored": 1,
        })
        self.assertIsNone(plan)
        self.assertIn("repay first", reason)
        self.assertNotIn("redeem", calls)

    def test_mendi_compound_error_code_is_not_success(self):
        (plan, reason), _ = self.prepare("mendi", {
            "balanceOf": 55, "getAllMarkets": [defi.MENDI_USDC, MARKET],
            "borrowBalanceStored": 0, "redeem": 12,
        })
        self.assertIsNone(plan)
        self.assertIn("code 12", reason)

    def test_stargate_routes_to_wallet_and_rejects_partial_redemption(self):
        info = {"balanceOf": 55, "token": defi.USDBC, "poolId": 21, "factory": MARKET,
                "getPool": defi.STARGATE_POOL, "amountLPtoLD": 500, "convertRate": 10,
                "instantRedeemLocal": 49}
        (plan, reason), _ = self.prepare("stargate", info)
        self.assertIsNone(plan)
        self.assertIn("full", reason)
        info["instantRedeemLocal"] = 50
        (plan, _), calls = self.prepare("stargate", info)
        self.assertEqual(plan.shares, 55)
        self.assertEqual(calls["instantRedeemLocal"][0], (21, 55, WALLET))

    def test_wrong_chain_rejected_before_reads(self):
        with self.assertRaisesRegex(ValueError, "Wrong chain"):
            defi.prepare_exit(SimpleNamespace(chain_id=1), "mendi", WALLET)

    def test_send_uses_estimate_and_disables_nonce_retry(self):
        client = Mock(account_address=WALLET)
        client._base_tx.return_value = {"from": WALLET, "nonce": 1}
        plan = Mock()
        plan.action.estimate_gas.return_value = 100001
        defi.send_exit(client, plan)
        self.assertEqual(plan.action.build_transaction.call_args.args[0]["gas"], 120002)
        client._send_tx.assert_called_once_with(plan.action.build_transaction.return_value, retry_nonce=False)
