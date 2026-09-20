"""Offline deposit tests: no keys, RPC access, exchange API calls or transactions."""
import ast
import os
import re
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

ROOT = Path(os.environ.get("OKX_TEST_ROOT", Path(__file__).resolve().parent))
A = "0x" + "1" * 40
B = "0x" + "2" * 40
W1 = "0x" + "3" * 40
W2 = "0x" + "4" * 40


class DepositTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "okx_deposit_addresses.txt"
        self.path.write_text(A + "\n" + B + "\n")
        names = {"_load_okx_deposit_addresses", "_validate_okx_deposit_mapping", "_is_valid_evm_address",
                 "run_okx_transfers_once", "run_exchange_deposit_once", "_l2_deposit_networks"}
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8-sig"))
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
        self.assertEqual(len(nodes), len(names))
        module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes], type_ignores=[])
        self.ns = dict(Path=Path, Decimal=Decimal, re=re, print=Mock(), time=NS(sleep=Mock()), random=NS(uniform=Mock(return_value=0)))
        exec(compile(ast.fix_missing_locations(module), "main.py", "exec"), self.ns)
        self.cfg = NS(okx_deposit_addresses_file=self.path, wallets_file=Path(self.tmp.name) / "wallets.txt",
                      dry_run=False, paper_mode=False, enable_execution=True)
        self.records = [(0, W1, "test-key"), (1, W2, "test-key")]
        self.client = Mock()
        self.client.get_native_balance.return_value = Decimal("1")
        self.client.send_native.return_value = "tx-test"
        self.ns.update(_build_wallet_key_records=Mock(return_value=self.records),
            _prompt_positive_decimal=Mock(side_effect=[Decimal("0.1"), Decimal("1"), Decimal("1")]),
            _prompt_start_wallet_number=Mock(return_value=1), _prompt_end_wallet_number=Mock(return_value=2),
            _prompt_wallet_order=Mock(return_value="random"), _apply_wallet_order=lambda records, order: list(reversed(records)),
            _proxy_for_line=Mock(return_value=(None, False)), _build_exec_client_for_chain_rpcs=Mock(return_value=self.client),
            _wallet_record_progress_label=Mock(return_value="test"), _append_exchange_deposit_csv=Mock(),
            _format_decimal_plain=str, decimal_to_raw=lambda amount, decimals: int(amount * 10 ** decimals),
            _print_mode_summary=Mock(), input=Mock(side_effect=["1", "1", "BASE"]))
        self.ns["_fetch_deposit_native_price_usd"] = Mock(return_value=Decimal("2000"))

    def run_deposit(self):
        self.ns["run_exchange_deposit_once"](self.cfg, Mock(), NS(), okx_deposit_file=self.path)

    def test_base_random_wallet_order_preserves_destination_mapping(self):
        self.run_deposit()
        self.assertEqual([c.args[0] for c in self.client.send_native.call_args_list], [B, A])
        self.assertEqual(self.client.send_native.call_args.args[1], 10 ** 17)
        for call in self.ns["_build_exec_client_for_chain_rpcs"].call_args_list:
            self.assertEqual(call.args[4], 8453)

    def test_empty_line_does_not_shift_addresses(self):
        self.path.write_text(A + "\n\n" + B)
        self.assertEqual(self.ns["_load_okx_deposit_addresses"](self.path), [A, "", B])
        with self.assertRaisesRegex(ValueError, "line 2"):
            self.run_deposit()
        self.client.send_native.assert_not_called()

    def test_single_address_not_broadcast_to_every_wallet(self):
        self.path.write_text(A)
        with self.assertRaisesRegex(ValueError, "line 2"):
            self.run_deposit()
        self.client.send_native.assert_not_called()

    def test_missing_file_created_empty(self):
        path = Path(self.tmp.name) / "new.txt"
        self.assertEqual(self.ns["_load_okx_deposit_addresses"](path), [])
        self.assertEqual(path.read_text(), "")

    def test_zero_and_self_addresses_rejected(self):
        for address in ("0x" + "0" * 40, W1, "garbage"):
            with self.assertRaises(ValueError):
                self.ns["_validate_okx_deposit_mapping"]([address], self.records[:1])

    def test_cancel_or_wrong_network_sends_nothing(self):
        for answer in ("", "ARBITRUM"):
            self.setUpInputs(answer)
            self.run_deposit()
        self.ns["_build_exec_client_for_chain_rpcs"].assert_not_called()

    def setUpInputs(self, answer="BASE", mode="1", amount="0.1"):
        self.ns["input"] = Mock(side_effect=["1", mode, answer])
        self.ns["_prompt_positive_decimal"] = Mock(side_effect=[Decimal(amount), Decimal("1"), Decimal("1")])

    def test_execution_switches_never_broadcast(self):
        for attr, value in (("dry_run", True), ("paper_mode", True), ("enable_execution", False)):
            self.cfg.dry_run, self.cfg.paper_mode, self.cfg.enable_execution = False, False, True
            setattr(self.cfg, attr, value)
            self.setUpInputs()
            self.run_deposit()
        self.client.send_native.assert_not_called()
        self.assertEqual(self.ns["_append_exchange_deposit_csv"].call_args.args[1], "planned")

    def test_percent_retains_reserve(self):
        self.setUpInputs(mode="3", amount="100")
        self.run_deposit()
        self.assertEqual(self.client.send_native.call_args.args[1], int(Decimal("0.99998") * 10**18))

    def test_insufficient_balance_no_send(self):
        self.client.get_native_balance.return_value = Decimal("0.00001")
        self.run_deposit()
        self.client.send_native.assert_not_called()

    def test_deposit_usd_boundary(self):
        for amount, sent in (("0.0000000000000000001", False), ("0.00049999999", False), ("0.0005", True), ("0.00050001", True)):
            self.client.send_native.reset_mock()
            self.setUpInputs(amount=amount)
            self.run_deposit()
            self.assertEqual(self.client.send_native.called, sent)
            if not sent:
                self.assertEqual(self.ns["_append_exchange_deposit_csv"].call_args.args[1], "skipped")
                self.assertEqual(self.ns["_print_mode_summary"].call_args.args[4], 2)

    def test_gas_reserve_reduces_balance_below_dollar(self):
        self.client.get_native_balance.return_value = Decimal("0.00051")
        self.setUpInputs(mode="3", amount="100")
        self.run_deposit()
        self.client.send_native.assert_not_called()
        self.assertEqual(self.ns["_append_exchange_deposit_csv"].call_args.args[1], "skipped")

    def test_price_failure_skips_without_guessing(self):
        self.ns["_fetch_deposit_native_price_usd"].side_effect = RuntimeError("price unavailable")
        self.run_deposit()
        self.client.send_native.assert_not_called()
        self.assertEqual(self.ns["_print_mode_summary"].call_args.args[4], 2)

    def test_price_is_refreshed_for_each_wallet(self):
        self.ns["_fetch_deposit_native_price_usd"].side_effect = [Decimal("2000"), Decimal("1900")]
        self.setUpInputs(amount="0.0005")
        self.run_deposit()
        self.client.send_native.assert_called_once_with(B, 500000000000000)

    def test_mantle_uses_mnt_price(self):
        self.ns["input"] = Mock(side_effect=["5", "1", "MANTLE"])
        self.ns["_fetch_deposit_native_price_usd"].return_value = Decimal("0.5")
        self.run_deposit()
        self.ns["_fetch_deposit_native_price_usd"].assert_called_with("MNT", None)
        self.client.send_native.assert_not_called()

    def test_price_response_validation(self):
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8-sig"))
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_fetch_deposit_native_price_usd")
        module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node], type_ignores=[])
        requests = Mock()
        ns = dict(Decimal=Decimal, requests=requests)
        exec(compile(ast.fix_missing_locations(module), "main.py", "exec"), ns)
        fetch = ns["_fetch_deposit_native_price_usd"]
        for price in ("0", "-1", "NaN", "Infinity", "bad"):
            requests.get.return_value.json.return_value = {"data": {"currency": "ETH", "rates": {"USD": price}}}
            with self.assertRaises(Exception):
                fetch("ETH")
        requests.get.return_value.json.return_value = {"data": {"currency": "MNT", "rates": {"USD": "0.5"}}}
        self.assertEqual(fetch("MNT"), Decimal("0.5"))
        with self.assertRaisesRegex(ValueError, "mismatch"):
            fetch("ETH")

    def test_source_file_line_mismatch_blocks_batch(self):
        self.cfg.wallets_file.write_text("\n" + W1 + "\n" + W2)
        with self.assertRaisesRegex(ValueError, "numbering"):
            self.run_deposit()
        self.client.send_native.assert_not_called()

    def test_submenu_routes_and_preserves_withdrawal(self):
        deposit, withdraw = Mock(), Mock()
        self.ns.update(run_exchange_deposit_once=deposit, run_okx_withdrawals_once=withdraw)
        for choice in ("1", "2", "3"):
            self.ns["input"] = Mock(return_value=choice)
            self.ns["run_okx_transfers_once"](self.cfg, Mock(), NS())
        withdraw.assert_called_once()
        deposit.assert_called_once()
        self.assertEqual(deposit.call_args.kwargs["okx_deposit_file"], self.path)


if __name__ == "__main__":
    unittest.main()
