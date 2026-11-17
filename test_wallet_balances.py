"""Offline indexed and batched balance tests. No keys or transactions."""
import ast
import importlib.util
import os
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from web3 import Web3
from eth_abi import encode

ROOT = Path(os.environ.get("BALANCE_TEST_ROOT", Path(__file__).resolve().parent))
spec = importlib.util.spec_from_file_location("wallet_balances", ROOT / "wallet_balances.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
WALLET = "0x" + "1" * 40
TOKEN = "0x" + "2" * 40
OTHER = "0x" + "3" * 40


class BalancesTests(unittest.TestCase):
    def test_batch_decoding_and_proxy_out_of_order(self):
        with patch.object(module.requests, "Session") as factory:
            session = factory.return_value.__enter__.return_value
            session.post.return_value.json.return_value = [dict(id=2, result="0x" + (10**18).to_bytes(32, "big").hex()),
                                                         dict(id=1, result="0x" + (1234567).to_bytes(32, "big").hex())]
            proxy = {"https": "test-proxy"}
            result = module.fetch_erc20_balances("rpc", WALLET, [(TOKEN, 6), (OTHER, 18)], proxy, use_multicall=False)
            self.assertEqual(result, {TOKEN: Decimal("1.234567"), OTHER: Decimal("1")})
            self.assertEqual(session.post.call_args.kwargs["proxies"], proxy)
            self.assertEqual(session.post.call_args.kwargs["json"][0]["method"], "eth_call")
            self.assertFalse(session.trust_env)

    def test_retry_only_missing_balances_unknown_is_not_zero(self):
        with patch.object(module.requests, "Session") as factory, patch.object(module.time, "sleep"):
            session = factory.return_value.__enter__.return_value
            session.post.return_value.json.side_effect = [
                [dict(id=1, result="0x" + "0" * 64), dict(id=2, error={"message": "busy"})],
                dict(id=2, error={"message": "busy"}), dict(id=2, error={"message": "busy"})]
            result = module.fetch_erc20_balances("rpc", WALLET, [(TOKEN, 6), (OTHER, 18)], use_multicall=False)
            self.assertEqual(result[TOKEN], 0)
            self.assertIsNone(result[OTHER])
            self.assertEqual(session.post.call_count, 3)
            self.assertEqual(session.post.call_args.kwargs["json"]["id"], 2)

    def test_catalog_of_794_needs_14_requests(self):
        with patch.object(module.requests, "Session") as factory:
            session = factory.return_value.__enter__.return_value

            def post(url, json, **kwargs):
                payload = json if isinstance(json, list) else [json]
                result = [dict(id=p["id"], result="0x" + "0" * 64) for p in payload]
                return NS(raise_for_status=lambda: None, json=lambda: result)

            session.post.side_effect = post
            tokens = [("0x" + f"{i+1:040x}", 18) for i in range(794)]
            result = module.fetch_erc20_balances("rpc", WALLET, tokens, use_multicall=False)
            self.assertEqual(session.post.call_count, 14)
            self.assertEqual(len(result), 794)

    def test_multicall_reads_two_tokens_with_one_request(self):
        with patch.object(module.requests, "Session") as factory:
            session = factory.return_value.__enter__.return_value
            data = encode(["(bool,bytes)[]"], [[(True, (1234567).to_bytes(32, "big")), (True, (0).to_bytes(32, "big"))]])
            session.post.return_value.json.return_value = dict(id=1, result="0x" + data.hex())
            result = module.fetch_erc20_balances("rpc", WALLET, [(TOKEN, 6), (OTHER, 18)])
            self.assertEqual(result, {TOKEN: Decimal("1.234567"), OTHER: Decimal("0")})
            session.post.assert_called_once()
            self.assertEqual(session.post.call_args.kwargs["json"]["params"][0]["to"], module.MULTICALL3)

    def test_missing_multicall_uses_regular_rpc(self):
        with patch.object(module.requests, "Session") as factory:
            session = factory.return_value.__enter__.return_value
            session.post.return_value.json.side_effect = [dict(id=1, result="0x"), dict(id=1, result="0x" + "0" * 64)]
            result = module.fetch_erc20_balances("rpc", WALLET, [(TOKEN, 6)])
            self.assertEqual(result[TOKEN], Decimal("0"))
            self.assertEqual(session.post.call_count, 2)

    def index_method(self):
        tree = ast.parse((ROOT / "doma_api.py").read_text(encoding="utf-8-sig"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DomaApiClient")
        node = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "fetch_wallet_token_addresses")
        ns = dict(Web3=Web3, Decimal=Decimal)
        code = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node], type_ignores=[])
        exec(compile(ast.fix_missing_locations(code), "index", "exec"), ns)
        return ns["fetch_wallet_token_addresses"]

    def item(self, token=TOKEN, balance=1):
        return dict(address=WALLET, balance=balance, deploymentAddress=token,
                    chain=dict(networkId="eip155:97477"), fractionalToken=dict(address=token))

    def test_all_index_pages_and_positive_only(self):
        api = NS(_post=Mock(side_effect=[
            dict(fractionalTokenBalances=dict(hasNextPage=True, items=[self.item(), self.item(OTHER, 0)])),
            dict(fractionalTokenBalances=dict(hasNextPage=False, items=[self.item(OTHER, 20)]))]))
        self.assertEqual(self.index_method()(api, WALLET, 97477), {TOKEN, OTHER})
        self.assertEqual(api._post.call_args.args[1]["skip"], 100)

    def test_partial_or_wrong_owner_index_is_rejected(self):
        for data in ({}, dict(fractionalTokenBalances=dict(items=[])),
                     dict(fractionalTokenBalances=dict(hasNextPage=True, items=[]))):
            with self.assertRaises(RuntimeError):
                self.index_method()(NS(_post=Mock(return_value=data)), WALLET, 97477)
        item = self.item()
        item["address"] = OTHER
        with self.assertRaises(RuntimeError):
            self.index_method()(NS(_post=Mock(return_value=dict(fractionalTokenBalances=dict(hasNextPage=False, items=[item])))), WALLET, 97477)


if __name__ == "__main__":
    unittest.main()
