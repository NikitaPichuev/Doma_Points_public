from __future__ import annotations

import inspect
import unittest
from unittest.mock import Mock, patch
from decimal import Decimal

import requests

from main import (
    _execute_launchpad_sell,
    _domain_quest_gas_reserve_eth,
    _domain_quest_spendable_native_eth,
    _fetch_fractional_tokens_with_same_proxy_retry,
    _launch_buy_min_out_raw,
    _is_wallet_start_log,
    _prepare_all_usdce_for_bonding_daily,
    _select_bonding_token_by_tvl,
    _wallet_record_progress_label,
    parse_trade_amount_expression,
    resolve_trade_amount,
)


class RiskLogicTests(unittest.TestCase):
    def test_fast_launch_does_not_reuse_prelaunch_price_floor(self) -> None:
        self.assertEqual(
            _launch_buy_min_out_raw(Decimal("100"), 18, fast_launch=True),
            1,
        )

    def test_regular_launch_buy_keeps_slippage_floor(self) -> None:
        self.assertEqual(
            _launch_buy_min_out_raw(Decimal("100"), 6),
            70_000_000,
        )

    def test_wallet_progress_uses_run_position_and_source_line(self) -> None:
        label = _wallet_record_progress_label(0, 51, 23, 61, "0xignored")

        self.assertEqual(label, "1/51 | wallet#24")

    def test_domain_quest_keeps_twenty_cents_of_eth_for_gas(self) -> None:
        self.assertEqual(_domain_quest_gas_reserve_eth(Decimal("2000")), Decimal("0.0001"))
        self.assertEqual(
            _domain_quest_spendable_native_eth(Decimal("0.001"), Decimal("2000")),
            Decimal("0.0009"),
        )
        self.assertEqual(
            _domain_quest_spendable_native_eth(Decimal("0.00005"), Decimal("2000")),
            Decimal("0"),
        )

    def test_wallet_start_log_is_detected_for_console_highlight(self) -> None:
        self.assertTrue(_is_wallet_start_log("[QUEST brag.com] wallet 13/61 | wallet#3"))
        self.assertTrue(_is_wallet_start_log("[BRIDGE] wallet 1/1 | wallet#42"))

    def test_regular_wallet_log_is_not_detected_as_wallet_start(self) -> None:
        self.assertFalse(_is_wallet_start_log("[QUEST brag.com] wallet=wallet#3 cycle=1"))
        self.assertFalse(_is_wallet_start_log("[QUEST brag.com] delay before next wallet: 4.28 sec"))

    def test_parse_percent(self) -> None:
        mode, value = parse_trade_amount_expression("25%")
        self.assertEqual(mode, "percent")
        self.assertEqual(value, Decimal("25"))

    def test_parse_usd(self) -> None:
        mode, value = parse_trade_amount_expression("$10")
        self.assertEqual(mode, "usd")
        self.assertEqual(value, Decimal("10"))

    def test_parse_token(self) -> None:
        mode, value = parse_trade_amount_expression("0.15")
        self.assertEqual(mode, "token")
        self.assertEqual(value, Decimal("0.15"))

    def test_resolve_percent(self) -> None:
        amount, usd = resolve_trade_amount("50%", Decimal("2"), Decimal("100"))
        self.assertEqual(amount, Decimal("1"))
        self.assertEqual(usd, Decimal("100"))

    def test_resolve_usd(self) -> None:
        amount, usd = resolve_trade_amount("20usd", Decimal("100"), Decimal("10"))
        self.assertEqual(amount, Decimal("2"))
        self.assertEqual(usd, Decimal("20"))

    def test_resolve_token(self) -> None:
        amount, usd = resolve_trade_amount("0.3", Decimal("1"), Decimal("200"))
        self.assertEqual(amount, Decimal("0.3"))
        self.assertEqual(usd, Decimal("60"))

    @patch("main.time.sleep")
    def test_catalog_fetch_retries_timeout_on_same_client(self, sleep: Mock) -> None:
        api = Mock()
        expected = [Mock()]
        api.fetch_fractional_tokens.side_effect = [requests.ReadTimeout("slow"), expected]

        result = _fetch_fractional_tokens_with_same_proxy_retry(
            api,
            Mock(),
            "BONDING_BUY",
            retry_delay=0.01,
        )

        self.assertIs(result, expected)
        self.assertEqual(api.fetch_fractional_tokens.call_count, 2)
        sleep.assert_called_once_with(0.01)

    @patch("main.time.sleep")
    def test_catalog_fetch_does_not_retry_permanent_error(self, sleep: Mock) -> None:
        api = Mock()
        api.fetch_fractional_tokens.side_effect = RuntimeError("bad response")

        with self.assertRaisesRegex(RuntimeError, "bad response"):
            _fetch_fractional_tokens_with_same_proxy_retry(api, Mock(), "BONDING_BUY")

        self.assertEqual(api.fetch_fractional_tokens.call_count, 1)
        sleep.assert_not_called()

    def test_launchpad_sell_accepts_configured_approve_delay(self) -> None:
        parameters = inspect.signature(_execute_launchpad_sell).parameters

        self.assertIn("post_approve_delay_range", parameters)
        self.assertIn("failed_launch_min_out_retry", parameters)

    def test_bonding_daily_selects_active_token_with_highest_tvl(self) -> None:
        low = Mock(tvl_usd=Decimal("100"), volume_usd=Decimal("1000"), price_usd=Decimal("1"))
        high = Mock(tvl_usd=Decimal("500"), volume_usd=Decimal("10"), price_usd=Decimal("0.1"))
        middle = Mock(tvl_usd=Decimal("300"), volume_usd=Decimal("5000"), price_usd=Decimal("2"))

        self.assertIs(_select_bonding_token_by_tvl([low, high, middle]), high)

    @patch("main._wait_tx_receipt", return_value=True)
    @patch("main._execute_trade_via_doma_ui_route")
    def test_bonding_daily_bootstraps_eth_when_usdc_is_below_one_dollar(
        self,
        execute_trade: Mock,
        wait_receipt: Mock,
    ) -> None:
        state = Mock(last_tx_hash="")

        def mark_sent(**_kwargs: object) -> bool:
            state.last_tx_hash = "0xbootstrap"
            return True

        execute_trade.side_effect = mark_sent
        exec_client = Mock()
        exec_client.get_erc20_balance.side_effect = [Decimal("0.000991"), Decimal("1.85")]
        exec_client.get_native_balance.return_value = Decimal("0.001")
        quote_token = Mock(address="0xusdc", decimals=6)
        weth_token = Mock(address="0xweth", decimals=18)

        ok, reason, available = _prepare_all_usdce_for_bonding_daily(
            Mock(),
            Mock(),
            state,
            Mock(),
            exec_client,
            quote_token,
            weth_token,
            "wallet#1",
            Decimal("2000"),
        )

        self.assertTrue(ok)
        self.assertEqual(reason, "")
        self.assertEqual(available, Decimal("1.85"))
        execute_trade.assert_called_once()
        wait_receipt.assert_called_once_with(exec_client, "0xbootstrap", timeout_sec=180)

    @patch("main._wait_tx_receipt", return_value=True)
    @patch("main._execute_trade_via_doma_ui_route")
    def test_bonding_all_usdc_consolidates_eth_when_usdc_is_at_least_one_dollar(
        self,
        execute_trade: Mock,
        wait_receipt: Mock,
    ) -> None:
        state = Mock(last_tx_hash="")

        def mark_sent(**_kwargs: object) -> bool:
            state.last_tx_hash = "0xbootstrap"
            return True

        execute_trade.side_effect = mark_sent
        exec_client = Mock()
        exec_client.get_erc20_balance.side_effect = [Decimal("1.25"), Decimal("3.10")]
        exec_client.get_native_balance.return_value = Decimal("0.001")

        ok, reason, available = _prepare_all_usdce_for_bonding_daily(
            Mock(),
            Mock(),
            state,
            Mock(),
            exec_client,
            Mock(address="0xusdc", decimals=6),
            Mock(address="0xweth", decimals=18),
            "wallet#1",
            Decimal("2000"),
            log_prefix="BONDING_BUY",
            consolidate_spendable_eth=True,
        )

        self.assertTrue(ok)
        self.assertEqual(reason, "")
        self.assertEqual(available, Decimal("3.10"))
        execute_trade.assert_called_once()
        wait_receipt.assert_called_once_with(exec_client, "0xbootstrap", timeout_sec=180)

    @patch("main._execute_trade_via_doma_ui_route")
    def test_bonding_all_usdc_keeps_eth_for_non_daily_mode(
        self,
        execute_trade: Mock,
    ) -> None:
        exec_client = Mock()
        exec_client.get_erc20_balance.return_value = Decimal("1.25")

        ok, reason, available = _prepare_all_usdce_for_bonding_daily(
            Mock(),
            Mock(),
            Mock(),
            Mock(),
            exec_client,
            Mock(address="0xusdc", decimals=6),
            Mock(address="0xweth", decimals=18),
            "wallet#1",
            Decimal("2000"),
            log_prefix="BONDING_BUY",
        )

        self.assertTrue(ok)
        self.assertEqual(reason, "")
        self.assertEqual(available, Decimal("1.25"))
        execute_trade.assert_not_called()

    @patch("main._execute_trade_via_doma_ui_route")
    def test_bonding_all_usdc_rejects_dust_without_spendable_eth(
        self,
        execute_trade: Mock,
    ) -> None:
        exec_client = Mock()
        exec_client.get_erc20_balance.return_value = Decimal("0.001551")
        exec_client.get_native_balance.return_value = Decimal("0.00004")

        ok, reason, available = _prepare_all_usdce_for_bonding_daily(
            Mock(),
            Mock(),
            Mock(),
            Mock(),
            exec_client,
            Mock(address="0xusdc", decimals=6),
            Mock(address="0xweth", decimals=18),
            "wallet#3",
            Decimal("2000"),
            log_prefix="BONDING_BUY",
        )

        self.assertFalse(ok)
        self.assertIn("no spendable ETH", reason)
        self.assertEqual(available, Decimal("0.001551"))
        execute_trade.assert_not_called()


if __name__ == "__main__":
    unittest.main()
