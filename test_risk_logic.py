from __future__ import annotations

import unittest
from unittest.mock import Mock, patch
from decimal import Decimal

import requests

from main import (
    _fetch_fractional_tokens_with_same_proxy_retry,
    parse_trade_amount_expression,
    resolve_trade_amount,
)


class RiskLogicTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
