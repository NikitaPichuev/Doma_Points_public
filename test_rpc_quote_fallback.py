"""Offline quote fallback checks; transactions are never sent."""
import ast
import importlib.util
import os
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import requests
from eth_abi import decode
from web3 import Web3

ROOT = Path(os.environ.get("RPC_QUOTE_TEST_ROOT", Path(__file__).resolve().parent))
spec = importlib.util.spec_from_file_location("rpc_quote_fallback", ROOT / "rpc_quote_fallback.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
FEE_RECIPIENT = "0x582366f5f9c5ae5c658c30758dd24d57ce4f111a"


class QuoteTests(unittest.TestCase):
    def setUp(self):
        module._FACTORY_CACHE.clear()

    def route(self, native_in=False, native_out=False, portion=10):
        tokens = [module.WETH, module.USDC] if not native_out else [module.USDC, module.WETH]
        data, output = module.encode_router_route(tokens, [100], 1000, 2000, Decimal("2.5"),
                                                  native_in, native_out, portion, FEE_RECIPIENT)
        commands, inputs, deadline = decode(["bytes", "bytes[]", "uint256"], bytes.fromhex(data[10:]))
        self.assertEqual(data[:10], "0x3593564c")
        self.assertEqual(output, 1998 if portion else 2000)
        self.assertEqual(len(commands), len(inputs))
        return commands, inputs

    def test_native_input_wraps_and_pays_portion_then_sweeps(self):
        commands, inputs = self.route(native_in=True)
        self.assertEqual(commands, bytes([0x0b, 0x00, 0x06, 0x04]))
        self.assertEqual(decode(["address", "uint256"], inputs[0]), (module.THIS, 1000))
        recipient, amount, minimum, path, payer_user = decode(["address", "uint256", "uint256", "bytes", "bool"], inputs[1])
        self.assertEqual((recipient, amount, minimum, payer_user), (module.THIS, 1000, 1950, False))
        self.assertEqual(path[-20:].hex(), module.USDC[2:])
        self.assertEqual(decode(["address", "address", "uint256"], inputs[2]), (module.USDC, FEE_RECIPIENT, 10))

    def test_native_output_unwraps_to_sender(self):
        commands, inputs = self.route(native_out=True)
        self.assertEqual(commands, bytes([0x00, 0x06, 0x0c]))
        self.assertTrue(decode(["address", "uint256", "uint256", "bytes", "bool"], inputs[0])[-1])
        self.assertEqual(decode(["address", "uint256"], inputs[-1]), (module.CALLER, 1948))

    def test_no_fee_output_goes_to_sender(self):
        commands, inputs = self.route(portion=0)
        self.assertEqual(commands, b"\x00")
        self.assertEqual(decode(["address", "uint256", "uint256", "bytes", "bool"], inputs[0])[0], module.CALLER)

    def test_invalid_bounds_rejected(self):
        for slippage, portion in (("100", 10), ("-1", 10), ("2.5", 10000), ("2.5", -1)):
            with self.assertRaises(ValueError):
                module.encode_router_route([module.WETH, module.USDC], [100], 1000, 2000,
                    Decimal(slippage), True, False, portion, FEE_RECIPIENT)

    def test_wrong_chain_and_exact_out_rejected(self):
        for chain, mode in ((1, "exactIn"), (97477, "exactOut")):
            with self.assertRaises(ValueError):
                module.build_rpc_quote(dict(tokenInChainId=str(chain), tokenOutChainId=str(chain), type=mode))

    def test_rpc_batch_keeps_response_order_and_checks_ids(self):
        reader = module.RpcReader(None)
        self.addCleanup(reader.close)
        reader.request = Mock(return_value=[dict(id=2, result="0x02"), dict(id=1, result="0x01")])
        calls = [(module.QUOTER, "0x01"), (module.QUOTER, "0x02")]
        self.assertEqual(reader.calls(calls), ["0x01", "0x02"])
        reader.request.return_value = [dict(id=1, result="0x01")]
        with self.assertRaises(RuntimeError):
            reader.calls(calls)

    def test_best_leg_uses_quoter_v2_and_first_output_word(self):
        reader = module.RpcReader(None)
        self.addCleanup(reader.close)
        reader.calls = Mock(return_value=["0x" + (100).to_bytes(32, "big").hex() + "00" * 96, None, None, None])
        self.assertEqual(reader.best_leg(module.WETH, module.USDC, 1000), (100, 100))
        self.assertTrue(reader.calls.call_args.args[0][0][1].startswith("0xc6a5026a"))

    def test_single_read_uses_plain_rpc_request(self):
        reader = module.RpcReader(None)
        self.addCleanup(reader.close)
        reader.request = Mock(return_value=dict(id=1, result="0x01"))
        self.assertEqual(reader.calls([(module.QUOTER, "0xc45a0155")]), ["0x01"])
        self.assertIsInstance(reader.request.call_args.args[0], dict)

    def test_factory_transient_failure_retries_and_caches_across_readers(self):
        reader = module.RpcReader(None)
        other = module.RpcReader(None)
        self.addCleanup(reader.close)
        self.addCleanup(other.close)
        address = "0x2e50b586d5bcd04cb6125e028a6a669f7f3cf1c2"
        word = "0x" + "0" * 24 + address[2:]
        reader.calls = Mock(side_effect=[[None], [None], [word]])
        other.calls = Mock(side_effect=AssertionError("cache should avoid factory RPC"))
        with patch.object(module.time, "sleep"):
            self.assertEqual(reader.factory(), address)
            self.assertEqual(other.factory(), address)
        self.assertEqual(reader.calls.call_count, 3)
        other.calls.assert_not_called()

    def test_invalid_factory_never_cached(self):
        reader = module.RpcReader(None)
        self.addCleanup(reader.close)
        for word in (None, "0x" + "0" * 64, "0x" + "f" * 64, "0xnot-an-address"):
            reader.calls = Mock(return_value=[word])
            with patch.object(module.time, "sleep"), self.assertRaises(RuntimeError):
                reader.factory()
            self.assertFalse(module._FACTORY_CACHE)

    def test_metadata_error_does_not_require_whole_quote_retry(self):
        reader = module.RpcReader(None)
        self.addCleanup(reader.close)
        reader.calls = Mock(side_effect=[[None], ["0x01"]])
        with patch.object(module.time, "sleep"):
            self.assertEqual(reader.required_calls([(module.QUOTER, "0x01")], "pool", bool), ["0x01"])
        self.assertEqual(reader.calls.call_count, 2)

    def test_transport_retries_only_transient_statuses(self):
        reader = module.RpcReader({"https": "socks5h://test"})
        self.addCleanup(reader.close)
        response = Mock()
        response.json.return_value = dict(result="0x01")
        reader.session.post = Mock(side_effect=[requests.Timeout(), response])
        with patch.object(module.time, "sleep"):
            self.assertEqual(reader.request(dict(method="eth_chainId")), dict(result="0x01"))
        self.assertEqual(reader.session.post.call_count, 2)
        self.assertEqual(reader.session.post.call_args.kwargs["proxies"], reader.proxies)
        response.raise_for_status.side_effect = requests.HTTPError(response=NS(status_code=400))
        reader.session.post.reset_mock(side_effect=True)
        reader.session.post.return_value = response
        with patch.object(module.time, "sleep"), self.assertRaises(requests.HTTPError):
            reader.request(dict(method="eth_chainId"))
        self.assertEqual(reader.session.post.call_count, 1)

    def test_network_failure_switches_only_quotes_to_same_proxy_rpc(self):
        tree = ast.parse((ROOT / "doma_api.py").read_text(encoding="utf-8-sig"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DomaApiClient")
        node = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "fetch_universal_router_quote")
        ns = dict(Decimal=Decimal, Web3=Web3, requests=requests, logging=Mock(),
                  DOMA_INTERFACE_PORTION_BIPS=10, DOMA_INTERFACE_PORTION_RECIPIENT=FEE_RECIPIENT,
                  DOMA_NATIVE_TOKEN_SENTINEL=module.NATIVE, DOMA_INTERFACE_QUOTE_URL="https://quote.test",
                  UniversalRouterQuote=lambda **kw: NS(**kw))
        exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), "quote", "exec"), ns)
        client = NS(proxies={"https": "socks5h://test"})
        result = dict(methodParameters=dict(to=module.ROUTER, calldata="0x01", value="0x0"), quote="123", quoteDecimals="1.23")
        with patch.object(requests, "get", side_effect=requests.ConnectionError("SOCKS denied")) as get:
            with patch.dict("sys.modules", {"rpc_quote_fallback": module}):
                with patch.object(module, "build_rpc_quote", return_value=result) as fallback:
                    for _ in range(2):
                        q = ns["fetch_universal_router_quote"](client, module.WETH, module.USDC, 1000, 97477)
                        self.assertEqual(q.quote_raw, 123)
                    self.assertEqual(get.call_count, 1)
                    self.assertEqual(fallback.call_count, 2)
                    self.assertEqual(fallback.call_args.kwargs["proxies"], client.proxies)
        with patch.object(requests, "get") as get, patch.object(module, "build_rpc_quote") as fallback:
            get.return_value.json.return_value = result
            client = NS(proxies={"https": "socks5h://test"})
            q = ns["fetch_universal_router_quote"](client, module.WETH, module.USDC, 1000, 97477)
            self.assertEqual(q.quote_raw, 123)
            fallback.assert_not_called()
            get.return_value.raise_for_status.side_effect = requests.HTTPError("bad request")
            with self.assertRaises(requests.HTTPError):
                ns["fetch_universal_router_quote"](client, module.WETH, module.USDC, 1000, 97477)
            fallback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
