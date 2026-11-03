"""Read-only checker tests with mocked HTTP, no keys or transactions."""
import ast
import csv
import importlib.util
import os
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

ROOT = Path(os.environ.get("DOMAPUNKS_TEST_ROOT", Path(__file__).resolve().parent))
spec = importlib.util.spec_from_file_location("checker", ROOT / "domapunks_checker.py")
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)
WALLET = "0x" + "1" * 40


def payload(status="eligible", claims="open"):
    return dict(chainId=97477, block=123, claims=claims, price="100000000", claimed="189", supply="1000",
                paymentToken=dict(symbol="DOMAPUNKS.com", decimals=6),
                quoteToken=dict(symbol="USDC.e", decimals=6),
                wallet=dict(account=WALLET, eligibility=status, balance="123456789", quoteBalance="1590000", tokenId="190"))


class CheckerTests(unittest.TestCase):
    def test_statuses_and_balances(self):
        for status in ("eligible", "claimed", "ineligible"):
            result = checker.parse_domapunks_claim(payload(status), WALLET, 97477)
            self.assertEqual(result["eligibility"], status)
            self.assertEqual(result["punk_id"], "190" if status == "claimed" else "")
            self.assertEqual(result["price"], Decimal("100"))
            self.assertEqual(result["quote_balance"], Decimal("1.59"))
            self.assertEqual(result["balance"], Decimal("123.456789"))

    def test_closed_and_sold_out_still_distinguish_eligibility(self):
        for availability in ("closed", "sold_out", "unavailable"):
            result = checker.parse_domapunks_claim(payload(claims=availability), WALLET, 97477)
            self.assertEqual((result["eligibility"], result["claims"]), ("eligible", availability))

    def test_invalid_responses_are_not_ineligible(self):
        cases = [None, {}, dict(status="unconfigured")]
        for key, value in (("chainId", 1), ("wallet", None), ("claims", "new-status"), ("block", None), ("price", "NaN")):
            data = payload()
            data[key] = value
            cases.append(data)
        for key, value in (("account", "wrong-wallet"), ("eligibility", "pending"), ("balance", "-1")):
            data = payload()
            data["wallet"][key] = value
            cases.append(data)
        for data in cases:
            with self.assertRaises(ValueError):
                checker.parse_domapunks_claim(data, WALLET, 97477)

    def test_http_uses_public_get_and_wallet_proxy(self):
        with patch.object(checker.requests, "get") as get:
            get.return_value.json.return_value = payload()
            proxy = {"https": "http://test-proxy"}
            checker.fetch_domapunks_claim(WALLET, proxies=proxy)
            get.assert_called_once()
            self.assertEqual(get.call_args.args, (checker.CLAIM_URL,))
            self.assertEqual(get.call_args.kwargs["params"], {"account": WALLET})
            self.assertEqual(get.call_args.kwargs["proxies"], proxy)
            get.return_value.raise_for_status.assert_called_once()

    def test_invalid_wallet_does_not_request(self):
        with patch.object(checker.requests, "get") as get:
            with self.assertRaises(ValueError):
                checker.fetch_domapunks_claim("not-an-address")
            get.assert_not_called()

    def test_http_error_propagates(self):
        with patch.object(checker.requests, "get") as get:
            get.return_value.raise_for_status.side_effect = checker.requests.HTTPError("503")
            with self.assertRaises(checker.requests.HTTPError):
                checker.fetch_domapunks_claim(WALLET)

    def test_menu_flow_preserves_rows_and_errors(self):
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8-sig"))
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in
                 {"run_domapunks_check_once", "run_points_menu_once"}]
        module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes], type_ignores=[])
        with tempfile.TemporaryDirectory() as tmp:
            rows = []
            fetch = Mock(side_effect=[checker.parse_domapunks_claim(payload(), WALLET, 97477), RuntimeError("offline")])
            ns = dict(datetime=datetime, timezone=timezone, fetch_domapunks_claim=fetch,
                      _prompt_start_wallet_number=Mock(return_value=1), _prompt_end_wallet_number=Mock(return_value=2),
                      _prompt_wallet_order=Mock(return_value="in_order"), _proxy_for_line=Mock(return_value=(None, False)),
                      ensure_csv=Mock(), append_csv=lambda path, row, **kwargs: rows.append(row),
                      time=NS(sleep=Mock()), random=NS(uniform=Mock(return_value=0)), print=Mock(),
                      get_points_menu_choice=Mock(return_value="4"))
            exec(compile(ast.fix_missing_locations(module), "main.py", "exec"), ns)
            cfg = NS(points_wallets=[WALLET, "0x" + "2" * 40], account_address="", points_csv_file=Path(tmp) / "points.csv",
                     chain_id=97477, csv_delimiter=";")
            ns["run_points_menu_once"](cfg, Mock(), NS())
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(len(row) == 16 for row in rows))
            self.assertEqual(rows[0][3], "eligible")
            self.assertEqual(rows[1][3], "unknown")
            self.assertEqual(rows[1][-1], "RuntimeError")
            self.assertEqual([c.args[1] for c in ns["_proxy_for_line"].call_args_list], [0, 1])


if __name__ == "__main__":
    unittest.main()
