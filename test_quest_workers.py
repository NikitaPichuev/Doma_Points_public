"""Offline quest concurrency tests. No wallets, RPC or real transactions."""
import ast
import csv
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from decimal import Decimal, getcontext, localcontext
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

ROOT = Path(os.environ.get("QUEST_TEST_ROOT", Path(__file__).resolve().parent))


def load(name):
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8-sig"))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    ns = dict(ThreadPoolExecutor=ThreadPoolExecutor, as_completed=as_completed,
              deepcopy=deepcopy, getcontext=getcontext, localcontext=localcontext,
              csv=csv, CSV_WRITE_LOCK=threading.Lock())
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "main.py", "exec"), ns)
    return ns[name], ns


def state():
    return NS(daily_volume_usd=Decimal("7"), last_tx_hash="old", last_error="old",
              used_bridge_amounts=[], bootstrap_completed=[])


def records(count):
    return [(i, f"wallet-{i}", "fake") for i in range(count)]


class QuestWorkersTests(unittest.TestCase):
    def setUp(self):
        self.run_workers, self.ns = load("_run_quest_wallet_workers")

    def test_worker_count_boundaries(self):
        for count, workers in ((0, 1), (1, 1), (30, 1), (31, 2), (60, 2), (61, 3), (90, 3), (120, 3)):
            logger = Mock()
            result = self.run_workers(records(count), state(), logger, lambda *args: (1, 0, 0, []))
            self.assertEqual(logger.info.call_args.args[2], workers)
            self.assertEqual(len(result), count)

    def test_parallelism_and_isolated_transactions(self):
        gate = threading.Barrier(3, timeout=5)
        lock = threading.Lock()
        active, peak = 0, 0
        seen = []
        parent = state()

        def worker(index, record, local):
            nonlocal active, peak
            self.assertEqual(local.last_tx_hash, "")
            self.assertEqual(local.used_bridge_amounts, [])
            local.last_tx_hash = f"tx-{index}"
            local.used_bridge_amounts.append(str(index))
            with lock:
                active += 1
                peak = max(peak, active)
            if index < 3:
                gate.wait()
            time.sleep(0.002)
            self.assertEqual(local.last_tx_hash, f"tx-{index}")
            local.daily_volume_usd += Decimal("0.5")
            with lock:
                seen.append(record[1])
                active -= 1
            return 1, 0, 0, []

        result = self.run_workers(records(61), parent, Mock(), worker)
        self.assertEqual(peak, 3)
        self.assertEqual(len(set(seen)), 61)
        self.assertEqual(sum(r[0] for r in result), 61)
        self.assertEqual(parent.daily_volume_usd, Decimal("37.5"))
        self.assertEqual(parent.used_bridge_amounts, [])

    def test_duplicate_wallets_rejected_before_work(self):
        worker = Mock()
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            self.run_workers([(0, "abc", "fake"), (1, "ABC", "fake")], state(), Mock(), worker)
        worker.assert_not_called()

    def test_failed_wallet_does_not_cancel_others_or_lose_volume(self):
        parent = state()

        def worker(index, record, local):
            local.daily_volume_usd += 1
            if index == 1:
                raise RuntimeError("mock failure")
            return 1, 0, 0, []

        result = self.run_workers(records(31), parent, Mock(), worker)
        self.assertEqual(sum(r[0] for r in result), 30)
        self.assertEqual(sum(r[1] for r in result), 1)
        self.assertEqual([label for r in result for label in r[3]], ["wallet#2"])
        self.assertEqual(parent.daily_volume_usd, Decimal("38"))

    def test_decimal_context_is_inherited(self):
        with localcontext() as ctx:
            ctx.prec = 50
            seen = []

            def worker(*args):
                seen.append(getcontext().prec)
                return 1, 0, 0, []

            self.run_workers(records(31), state(), Mock(), worker)
            self.assertEqual(seen, [50] * 31)

    def test_csv_concurrent_rows_remain_intact(self):
        append, _ = load("append_csv")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rows.csv"
            with ThreadPoolExecutor(max_workers=3) as executor:
                list(executor.map(lambda i: append(path, [i, "x" * 10000]), range(90)))
            with path.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.reader(handle))
            self.assertEqual(len(rows), 90)
            self.assertEqual({int(row[0]) for row in rows}, set(range(90)))
            self.assertTrue(all(row[1] == "x" * 10000 for row in rows))


class QuestFlowTests(unittest.TestCase):
    def test_real_quest_loop_aggregates_parallel_skips_and_failures(self):
        run, ns = load("run_domain_quest_volume_once")
        scheduler, _ = load("_run_quest_wallet_workers")
        token = NS(address="token", decimals=18, symbol="TEST.COM")
        api = Mock()
        api.fetch_fractional_token_by_name.return_value = NS(
            symbol="TEST.COM", name="test.com", pool_address="pool", quote_token_address="usdc",
            price_usd=Decimal("1"), token_id=1)
        api.fetch_wallet_fractional_token_volume_usd.return_value = Decimal("10")
        api.fetch_wallet_pool_volume_usd.return_value = Decimal("0")
        client = Mock()
        client.get_erc20_balance.return_value = Decimal("0")

        def build_client(**kwargs):
            if kwargs["wallet"] == "wallet-5":
                raise RuntimeError("mock RPC failed")
            return client

        ns.update(Decimal=Decimal, DOMAIN_QUEST_MIN_BUY_USD={}, DOMAIN_QUEST_COMPLETION_THRESHOLD_USD=Decimal("100"),
            _parse_decimal_input=Decimal, _current_week_start_utc=Mock(return_value=NS(isoformat=lambda: "test-week")),
            _build_wallet_key_records=Mock(return_value=records(61)),
            _apply_wallet_start_selection=lambda rows: (rows, 0, len(rows)),
            _proxy_for_line=Mock(return_value=(None, False)), DomaApiClient=Mock(return_value=api),
            canonical_symbol=lambda s: s, Token=lambda **kw: NS(**kw),
            _token_from_config_override=Mock(return_value=token), _fetch_eth_price_via_doma_quote=Mock(return_value=Decimal("2000")),
            _token_from_launchpad_price=Mock(return_value=token), _format_decimal_plain=str,
            _run_quest_wallet_workers=scheduler, _wallet_record_progress_label=Mock(return_value="test"),
            _build_exec_client_with_rpc_fallback=build_client, _is_proxy_connectivity_error=lambda exc: False,
            _print_mode_summary=Mock())
        cfg = NS(doma_api_url="test", doma_api_key="", doma_api_keys=[], file_api_keys=[], wallet_delay_max_sec=0)
        run(cfg, Mock(), state(), preset=("test.com", "95", "99", "10", "ETH", "volume_only", "check_skip"))
        args = ns["_print_mode_summary"].call_args.args
        self.assertEqual(args[1:5], (61, 0, 1, 60))
        self.assertEqual(args[5], ["wallet#6"])


if __name__ == "__main__":
    unittest.main()
