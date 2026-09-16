"""Offline metadata and selection tests, without loading wallets or sending trades."""
import ast
import logging
import os
import time
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

ROOT = Path(os.environ.get("BONDING_TEST_ROOT", Path(__file__).resolve().parent))


def load(filename, names):
    tree = ast.parse((ROOT / filename).read_text(encoding="utf-8-sig"))
    nodes = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in names]
    assert len(nodes) == len(names)
    ns = dict(Decimal=Decimal, time=time, CHEAP_BUY_TOKEN_BLOCKLIST=set(), LaunchpadTokenInfo=NS)
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), filename, "exec"), ns)
    return ns


def token(name="fyi.xyz", tvl="2700", **fields):
    data = dict(name=name, address=name, launchpad_address="launch", pool_address=None,
                status="FRACTIONALIZED", price_usd=Decimal("1"), tvl_usd=Decimal(tvl),
                volume_usd=Decimal("10"), launch_start_time=0, quote_token_address="USDC")
    data.update(fields)
    return NS(**data)


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.ns = load("main.py", {"_bonding_token_rejection_reason", "_is_currently_bonding_token",
                                    "_analyze_active_bonding_tokens", "_select_bonding_token_by_tvl"})
        self.api = Mock()
        self.quote = NS(address="usdc")
        self.logger = Mock()
        self.run = lambda: self.ns["_analyze_active_bonding_tokens"](self.api, self.quote, self.logger)

    def test_reanalyzes_every_invocation_and_ranks_refreshed_tvl(self):
        a, b = token(), token("other.xyz", "2000")
        self.api.fetch_fractional_tokens.return_value = [a, b]
        self.api.fetch_fractional_token_by_name.side_effect = [token(tvl="1000"), b, a, b]
        self.assertEqual(self.run()[0].name, b.name)
        self.assertEqual(self.run()[0].name, a.name)
        self.assertEqual(self.api.fetch_fractional_tokens.call_count, 2)
        self.api.fetch_fractional_tokens.assert_called_with(take=100, max_pages=100, bonding_only=True)

    def test_rejects_stale_top_candidate_and_uses_next(self):
        b = token("other.xyz", "2000")
        self.api.fetch_fractional_tokens.return_value = [token(), b]
        self.api.fetch_fractional_token_by_name.side_effect = [token(pool_address="pool"), b]
        self.assertEqual(self.run(), [b])
        self.assertIn("pool", self.logger.warning.call_args.args[-1])

    def test_missing_or_failed_lookup_does_not_reuse_catalog(self):
        self.api.fetch_fractional_tokens.return_value = [token(), token("other.xyz")]
        self.api.fetch_fractional_token_by_name.side_effect = [None, RuntimeError("API down")]
        self.assertEqual(self.run(), [])

    def test_changed_identity_rejected(self):
        self.api.fetch_fractional_tokens.return_value = [token()]
        self.api.fetch_fractional_token_by_name.return_value = token(address="different")
        self.assertEqual(self.run(), [])

    def test_filters_future_wrong_quote_blocked_invalid_price_and_deduplicates(self):
        self.ns["CHEAP_BUY_TOKEN_BLOCKLIST"].add("blocked.xyz")
        a = token()
        self.api.fetch_fractional_tokens.return_value = [a, a,
            token("future.xyz", launch_start_time=time.time() + 1000),
            token("quote.xyz", quote_token_address="other"), token("blocked.xyz"),
            token("price.xyz", price_usd=Decimal("NaN")), token("status.xyz", status="LIQUID")]
        self.api.fetch_fractional_token_by_name.return_value = a
        self.assertEqual(self.run(), [a])
        self.api.fetch_fractional_token_by_name.assert_called_once_with(a.name)

    def test_incomplete_catalog_fails_closed(self):
        self.api.fetch_fractional_tokens.side_effect = RuntimeError("pagination limit")
        with self.assertRaisesRegex(RuntimeError, "pagination"):
            self.run()
        self.api.fetch_fractional_token_by_name.assert_not_called()

    def test_wallet_reselection_and_empty_batch_stop(self):
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8-sig"))
        func = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run_bonding_token_buy_once")
        fallback = next(n for n in ast.walk(func) if isinstance(n, ast.If)
                        and isinstance(n.test, ast.BoolOp)
                        and "daily_quest" in ast.unparse(n.test)
                        and "_is_currently_bonding_token" in ast.unparse(n.test))
        module = ast.Module(body=[ast.For(target=ast.Name(id="unused", ctx=ast.Store()),
            iter=ast.List(elts=[ast.Constant(1)], ctx=ast.Load()), body=[fallback], orelse=[])], type_ignores=[])
        code = compile(ast.fix_missing_locations(module), "fallback", "exec")
        for candidates in ([token("replacement.xyz")], []):
            ns = dict(self.ns, selection="daily_quest", current=None, selected_candidate=token(),
                      quote_token=self.quote, logger=self.logger, doma_api=self.api,
                      wallet_records=[1, 2, 3], position=2, skipped_wallets=0, failed_entries=[])
            ns["_analyze_active_bonding_tokens"] = Mock(return_value=candidates)
            exec(code, ns)
            if candidates:
                self.assertIs(ns["selected_candidate"], candidates[0])
                self.assertIs(ns["current"], candidates[0])
            else:
                self.assertEqual(ns["skipped_wallets"], 2)


class LookupTests(unittest.TestCase):
    def setUp(self):
        self.ns = load("doma_api.py", {"fetch_fractional_token_by_name", "fetch_fractional_tokens"})
        self.api = Mock()

    def item(self, name):
        return dict(id="1", name=name, address="token", launchpadAddress="launch",
                    params={"launchStartTime": 123})

    def test_exact_name_beyond_first_five_and_next_page(self):
        self.api._post.side_effect = [{"fractionalTokens": {"items": [self.item("prefix-fyi.xyz")] * 100}},
                                     {"fractionalTokens": {"items": [self.item("FYI.XYZ")]}}]
        result = self.ns["fetch_fractional_token_by_name"](self.api, "fyi.xyz")
        self.assertEqual(result.name, "FYI.XYZ")
        self.assertEqual(result.launch_start_time, 123)
        self.assertEqual(self.api._post.call_args.args[1]["skip"], 100)

    def test_absent_exact_name_is_not_substring_match(self):
        self.api._post.return_value = {"fractionalTokens": {"items": [self.item("prefix-fyi.xyz")]}}
        self.assertIsNone(self.ns["fetch_fractional_token_by_name"](self.api, "fyi.xyz"))

    def test_lookup_limit_is_error_not_inactive_token(self):
        self.api._post.return_value = {"fractionalTokens": {"items": [self.item("other")] * 100}}
        with self.assertRaisesRegex(RuntimeError, "pagination limit"):
            self.ns["fetch_fractional_token_by_name"](self.api, "fyi.xyz")

    def test_bonding_catalog_filter_time_and_exhaustion(self):
        self.api._post.return_value = {"fractionalTokens": {"items": [self.item("fyi.xyz")]}}
        result = self.ns["fetch_fractional_tokens"](self.api, take=100, bonding_only=True)
        self.assertEqual(result[0].launch_start_time, 123)
        self.assertEqual(self.api._post.call_args.args[1]["status"], "FRACTIONALIZED")
        with self.assertRaisesRegex(RuntimeError, "pagination limit"):
            self.ns["fetch_fractional_tokens"](self.api, take=1, max_pages=1, bonding_only=True)


if __name__ == "__main__":
    unittest.main()
