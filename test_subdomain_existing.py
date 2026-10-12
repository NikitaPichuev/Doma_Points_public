"""Offline option 11 tests: no keys, RPC requests or real transactions."""
import ast
import os
import random
import unittest
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

ROOT = Path(os.environ.get("SUBDOMAIN_TEST_ROOT", Path(__file__).resolve().parent))


def load(*names):
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8-sig"))
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert len(nodes) == len(names)
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes], type_ignores=[])
    ns = dict(Decimal=Decimal, random=random, time=NS(sleep=Mock()),
              ROUND_FLOOR=ROUND_FLOOR, ROUND_CEILING=ROUND_CEILING,
              CHEAP_BUY_TOKEN_BLOCKLIST=set(), _format_decimal_plain=str)
    exec(compile(ast.fix_missing_locations(module), "main.py", "exec"), ns)
    return ns


def token(name):
    return NS(address=name, name=name, symbol=name, decimals=18, price_usd=Decimal("10"), pool_address=None)


class ExistingTests(unittest.TestCase):
    def setUp(self):
        self.ns = load("_claim_existing_subdomain")
        self.claim = Mock(return_value=(1, 0))
        self.ns.update(_claim_subdomains_for_domain_token=self.claim,
                       _token_from_launchpad_price=lambda info, price: info)
        self.client = Mock()
        self.client.get_erc20_balance.return_value = Decimal("1")
        self.cfg = NS(chain_id=97477)

    def run_existing(self, catalog):
        return self.ns["_claim_existing_subdomain"](self.cfg, Mock(), NS(), Mock(),
            self.client, "wallet", catalog, token("usdc"), token("weth"))

    def test_held_token_without_pool_or_cheap_price_no_topup(self):
        self.assertTrue(self.run_existing([token("held")]))
        self.assertFalse(self.claim.call_args.kwargs["allow_topup"])
        self.assertEqual(self.claim.call_args.kwargs["max_claims"], 1)

    def test_try_all_held_tokens_before_fallback(self):
        self.claim.side_effect = [(0, 0), (0, 1), (1, 0)]
        self.assertTrue(self.run_existing([token("small"), token("inactive"), token("enough")]))
        self.assertEqual(self.claim.call_count, 3)

    def test_no_held_balance_requests_fallback(self):
        self.client.get_erc20_balance.return_value = Decimal("0")
        self.assertFalse(self.run_existing([token("empty")]))
        self.claim.assert_not_called()

    def test_insufficient_and_unsupported_allow_fallback(self):
        self.claim.side_effect = [(0, 0), RuntimeError("minting disabled")]
        self.assertFalse(self.run_existing([token("small"), token("disabled")]))

    def test_skip_duplicates_and_quote_assets(self):
        self.claim.return_value = (0, 0)
        self.run_existing([token("usdc"), token("weth"), token("a"), token("A")])
        self.client.get_erc20_balance.assert_called_once_with("a", 18)

    def test_balance_read_error_blocks_purchase(self):
        self.client.get_erc20_balance.side_effect = RuntimeError("RPC unavailable")
        with self.assertRaisesRegex(RuntimeError, "scan incomplete"):
            self.run_existing([token("held")])

    def test_submitted_stake_error_blocks_retry(self):
        self.claim.side_effect = RuntimeError("subdomain stake receipt failed | tx=abc: timeout")
        with self.assertRaisesRegex(RuntimeError, "tx=abc"):
            self.run_existing([token("held"), token("other")])
        self.claim.assert_called_once()


class ClaimTests(unittest.TestCase):
    def setUp(self):
        self.ns = load("_claim_subdomains_for_domain_token", "_pick_affordable_subdomain_label",
                       "_subdomain_staking_amount_for_length")
        self.ns.update(raw_to_decimal=lambda raw, decimals: Decimal(raw) / 10**decimals,
                       _pick_available_subdomain_label=lambda client, address, length: "a" * length,
                       _top_up_domain_token_for_subdomain=Mock(side_effect=AssertionError("unexpected swap")))
        self.cfg = NS(dry_run=False, paper_mode=False, enable_execution=True)
        self.client, self.api = Mock(), Mock()
        self.client.get_subdomain_staking_prices.return_value = [10**18] * 39 + [1]
        self.client.get_erc20_balance.return_value = Decimal("0.000001")
        self.api.sign_fractional_staking_subdomain_voucher.return_value = ("contract", "voucher", "signature")
        self.client.claim_subdomain_and_set_dns.return_value = (None, "tx", 1, None)

    def run_claim(self):
        return self.ns["_claim_subdomains_for_domain_token"](
            self.cfg, Mock(), NS(), self.api, self.client, 97477, "wallet", token("held"),
            token("usdc"), token("weth"), Decimal("0"), "held", 1, 1,
            Decimal("0"), Decimal("0"), max_claims=1, allow_topup=False)

    def test_chooses_affordable_length_not_random_expensive_one(self):
        self.assertEqual(self.run_claim(), (1, 0))
        self.assertEqual(len(self.client.claim_subdomain_and_set_dns.call_args.args[1]), 40)

    def test_insufficient_balance_never_tops_up(self):
        self.client.get_subdomain_staking_prices.return_value = [10**18]
        self.assertEqual(self.run_claim(), (0, 0))
        self.client.claim_subdomain_and_set_dns.assert_not_called()

    def test_empty_prices_never_claim(self):
        self.client.get_subdomain_staking_prices.return_value = []
        self.assertEqual(self.run_claim(), (0, 0))
        self.client.claim_subdomain_and_set_dns.assert_not_called()

    def test_inactive_voucher_does_not_claim_or_swap(self):
        self.api.sign_fractional_staking_subdomain_voucher.side_effect = RuntimeError("inactive")
        self.assertEqual(self.run_claim(), (0, 1))
        self.client.claim_subdomain_and_set_dns.assert_not_called()

    def test_execution_flags_block_stake(self):
        for attr, value in (("dry_run", True), ("paper_mode", True), ("enable_execution", False)):
            self.cfg.dry_run, self.cfg.paper_mode, self.cfg.enable_execution = False, False, True
            setattr(self.cfg, attr, value)
            self.assertEqual(self.run_claim(), (0, 0))
        self.client.claim_subdomain_and_set_dns.assert_not_called()


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.ns = load("run_cheap_token_buy_once")
        self.cfg = NS(doma_api_url="api", doma_api_key="", doma_api_keys=[], file_api_keys=[],
                      chain_id=97477, dry_run=False, paper_mode=False, enable_execution=True)
        self.api, self.client = Mock(), Mock()
        self.api.fetch_fractional_tokens.return_value = [token("held")]
        self.client.get_erc20_balance.return_value = Decimal("1")
        self.ns.update(get_cheap_token_buy_menu_input=Mock(return_value=("0.01", "0.01", "0.01", "1", "1", "1", "1", "0", "0")),
            _parse_decimal_input=Decimal, _build_wallet_key_records=Mock(return_value=[(0, "wallet", "fake")]),
            _apply_wallet_start_selection=lambda records: (records, 0, 1),
            _usdce_token_from_config=lambda cfg: token("usdc"),
            _token_from_config_override=lambda *args: token("weth"),
            _proxy_for_line=Mock(return_value=({"https": "proxy"}, False)), DomaApiClient=Mock(return_value=self.api),
            _wallet_record_progress_label=Mock(return_value="wallet#1"),
            _build_exec_client_with_rpc_fallback=Mock(return_value=self.client),
            _is_daily_subdomain_stake_quest_completed=Mock(return_value=False),
            _wait_daily_subdomain_stake_quest_completed=Mock(return_value=True),
            _claim_existing_subdomain=Mock(return_value=True),
            _fetch_eth_price_via_doma_quote=Mock(return_value=Decimal("2000")),
            _select_cheap_tokens_with_subdomains=Mock(return_value=[token("new")]),
            _top_up_usdce_from_eth_for_cheap_buy=Mock(return_value=(True, "", 0, 0)),
            _token_from_launchpad_price=lambda info, price: info,
            _random_decimal_between=Mock(return_value=Decimal("0.01")),
            _execute_trade_via_doma_ui_route=Mock(return_value=True),
            _claim_subdomains_for_domain_token=Mock(return_value=(1, 0)),
            _print_mode_summary=Mock(), _redact_wallet_addresses=lambda s: s, print=Mock())

    def run_flow(self):
        self.ns["run_cheap_token_buy_once"](self.cfg, Mock(), NS())

    def test_existing_success_never_fetches_price_or_buys(self):
        self.run_flow()
        self.ns["_fetch_eth_price_via_doma_quote"].assert_not_called()
        self.ns["_execute_trade_via_doma_ui_route"].assert_not_called()
        self.assertEqual(self.ns["_print_mode_summary"].call_args.kwargs["success"], 1)

    def test_pending_quest_after_existing_stake_never_buys(self):
        self.ns["_wait_daily_subdomain_stake_quest_completed"].return_value = False
        self.run_flow()
        self.ns["_execute_trade_via_doma_ui_route"].assert_not_called()

    def test_fallback_buys_penny_only_after_scan(self):
        events = []
        self.ns["_claim_existing_subdomain"].side_effect = lambda *a: events.append("scan") or False
        self.ns["_execute_trade_via_doma_ui_route"].side_effect = lambda **kw: events.append("buy") or True
        self.run_flow()
        self.assertEqual(events, ["scan", "buy"])
        self.assertEqual(self.ns["_execute_trade_via_doma_ui_route"].call_args.kwargs["trade_amount_expr"], "$0.01")
        self.assertFalse(self.ns["_claim_subdomains_for_domain_token"].call_args.kwargs["allow_topup"])

    def test_completed_quest_skips_scan_and_buy(self):
        self.ns["_is_daily_subdomain_stake_quest_completed"].return_value = True
        self.run_flow()
        self.ns["_claim_existing_subdomain"].assert_not_called()
        self.ns["_execute_trade_via_doma_ui_route"].assert_not_called()

    def test_incomplete_scan_blocks_fallback(self):
        self.ns["_claim_existing_subdomain"].side_effect = RuntimeError("scan incomplete")
        self.run_flow()
        self.ns["_execute_trade_via_doma_ui_route"].assert_not_called()
        self.assertEqual(self.ns["_print_mode_summary"].call_args.kwargs["failed"], 1)


if __name__ == "__main__":
    unittest.main()
