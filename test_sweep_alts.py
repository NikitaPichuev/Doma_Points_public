"""Offline sweep selection tests. No keys, RPC or real sales."""
import ast
import os
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from decimal import Decimal, getcontext, localcontext
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

ROOT = Path(os.environ.get("SWEEP_TEST_ROOT", Path(__file__).resolve().parent))


def token(symbol, price="1", address=None, name=None):
    return NS(symbol=symbol, name=name or symbol, address=address or symbol.lower(),
              decimals=18, price_usd=Decimal(price), pool_address="pool", launchpad_address="",
              quote_token_address="usdc")


class SweepTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8-sig"))
        names = {"get_sweep_menu_input", "run_sweep_tokens_to_usdce_once", "_is_sweep_protected_asset",
                 "canonical_symbol", "_is_sweep_token_excluded", "_run_quest_wallet_workers"}
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names
                 or isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in
                    {"SWEEP_PROTECTED_SYMBOLS", "SWEEP_MIN_TOKEN_VALUE_USD"} for t in n.targets)]
        self.ns = dict(Decimal=Decimal, print=Mock(), ThreadPoolExecutor=ThreadPoolExecutor,
                       as_completed=as_completed, deepcopy=deepcopy, getcontext=getcontext, localcontext=localcontext)
        module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes], type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), "main.py", "exec"), self.ns)
        self.cfg = NS(sweep_token_exclusions_file=Path("unused"), token_address_overrides={"USDC.E": "usdc", "WETH": "weth", "USDT": "stable-address"},
                      wallet_delay_max_sec=0, chain_id=97477)
        self.client, self.api, self.wallet_api = Mock(), Mock(), Mock()
        self.api.fetch_top_pools_by_tvl.return_value = []
        self.catalog = []
        self.balances = {}
        self.wallet_api.fetch_wallet_token_addresses.side_effect = RuntimeError("mock index offline")
        self.client.get_erc20_balance.side_effect = lambda address, decimals: self.balances.get(address, Decimal("1"))
        self.client.get_erc20_balances.side_effect = lambda tokens: {t.address.lower(): self.balances.get(t.address, Decimal("1")) for t in tokens}
        self.ns.update(get_sweep_menu_input=Mock(return_value=("ALTS_TO_USDC.E", "0")),
            _parse_decimal_input=Decimal, _load_sweep_token_exclusions=Mock(return_value=set()),
            _build_wallet_key_records=Mock(return_value=[(0, "wallet", "fake")]),
            _apply_wallet_start_selection=lambda r: (r, 0, 1),
            _fetch_fractional_tokens_with_wallet_proxy_fallback=Mock(return_value=(self.api, self.catalog, None)),
            _usdce_token_from_config=Mock(return_value=token("USDC.E", address="usdc")),
            _token_from_config_override=Mock(return_value=token("WETH")),
            _fetch_eth_price_via_doma_quote=Mock(return_value=Decimal("2000")),
            _format_decimal_plain=str, _proxy_for_line=Mock(return_value=({"https": "test-proxy"}, False)),
            _doma_api_client_for_proxies=Mock(return_value=self.wallet_api),
            _wallet_record_progress_label=Mock(return_value="wallet#1"),
            _build_exec_client_with_rpc_fallback=Mock(return_value=self.client),
            _native_gas_reserve_eth=Mock(return_value=Decimal("0.00002")),
            _token_from_launchpad_price=lambda info, price: info,
            _execute_trade_via_doma_ui_route=Mock(return_value=True),
            _random_swap_delay_sec=Mock(return_value=0), time=NS(sleep=Mock()),
            _print_mode_summary=Mock())

    def run_sweep(self):
        self.ns["run_sweep_tokens_to_usdce_once"](self.cfg, Mock(), NS(daily_volume_usd=Decimal("0"), last_tx_hash="", last_error=""))

    def test_index_limits_batch_to_existing_tokens(self):
        self.catalog.extend([token("HELD"), token("NOT_HELD")])
        self.wallet_api.fetch_wallet_token_addresses.side_effect = None
        self.wallet_api.fetch_wallet_token_addresses.return_value = {"held"}
        self.run_sweep()
        self.assertEqual([t.address for t in self.client.get_erc20_balances.call_args.args[0]], ["held"])
        self.client.get_erc20_balance.assert_not_called()
        self.ns["_execute_trade_via_doma_ui_route"].assert_called_once()

    def test_unknown_batch_balance_is_failed_not_empty(self):
        self.catalog.append(token("HELD"))
        self.balances["held"] = None
        self.run_sweep()
        self.ns["_execute_trade_via_doma_ui_route"].assert_not_called()
        self.assertEqual(self.ns["_print_mode_summary"].call_args.args[2:5], (0, 1, 0))

    def test_strict_threshold_protected_assets_and_wallet_proxy(self):
        self.catalog.extend([token("ABOVE", "0.050001"), token("EXACT", "0.05"), token("BELOW", "0.049999"),
                             token("USDT"), token("DAI"), token("USDC.E"), token("ETH"), token("WETH"),
                             token("RENAMED", address="stable-address"), token("RENAMED2", address="weth")])
        self.run_sweep()
        sale = self.ns["_execute_trade_via_doma_ui_route"]
        sale.assert_called_once()
        self.assertEqual(sale.call_args.kwargs["token_in"].symbol, "ABOVE")
        self.assertEqual(sale.call_args.kwargs["trade_amount_expr"], "100%")
        self.assertIs(sale.call_args.kwargs["doma_api"], self.wallet_api)
        self.assertEqual(sale.call_args.kwargs["token_out"].address, "usdc")
        self.client.get_native_balance.assert_not_called()
        self.assertFalse(any(c.args[0] in {"weth", "stable-address", "eth", "usdt", "dai"}
                             for c in self.client.get_erc20_balance.call_args_list))

    def test_total_holding_value_not_unit_price(self):
        self.catalog.extend([token("CHEAP", "0.01"), token("EXPENSIVE", "1")])
        self.balances.update(cheap=Decimal("10"), expensive=Decimal("0.04"))
        self.run_sweep()
        self.assertEqual(self.ns["_execute_trade_via_doma_ui_route"].call_args.kwargs["token_in"].symbol, "CHEAP")
        self.ns["_execute_trade_via_doma_ui_route"].assert_called_once()

    def test_exceptions_unknown_prices_and_duplicates(self):
        self.catalog.extend([token("EXCLUDED"), token("ZERO", "0"), token("INVALID", "NaN"), token("SELL"), token("SELL")])
        self.ns["_load_sweep_token_exclusions"].return_value = {"excluded"}
        self.run_sweep()
        self.ns["_execute_trade_via_doma_ui_route"].assert_called_once()

    def test_nothing_eligible_skips_wallet(self):
        self.catalog.extend([token("DUST", "0.05"), token("USDT")])
        self.run_sweep()
        self.ns["_execute_trade_via_doma_ui_route"].assert_not_called()
        self.assertEqual(self.ns["_print_mode_summary"].call_args.args[2:5], (0, 0, 1))

    def test_new_menu_requires_no_eth_amount_input(self):
        ns = self.ns
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8-sig"))
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "get_sweep_menu_input")
        code = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node], type_ignores=[])
        exec(compile(ast.fix_missing_locations(code), "menu", "exec"), ns)
        ns["input"] = Mock(side_effect=["4"])
        self.assertEqual(ns["get_sweep_menu_input"](NS()), ("ALTS_TO_USDC.E", "0"))
        ns["input"].assert_called_once()


if __name__ == "__main__":
    unittest.main()
