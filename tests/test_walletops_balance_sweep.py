import os, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from acobot import wallets, walletops
from acobot.config import Chain

KEY = "0x" + "11" * 32
ADDR = wallets.address_of(KEY)
DEST = "0x" + "22" * 20
CHAIN = Chain(name="ethereum", chain_id=1, rpc=["mock"], broadcast_rpc=["mock"], priority_fee_gwei=2.0)

class FakeRPC:
    def __init__(self, balance): self.balance = balance; self.sent = []
    def call(self, method, params, url=None):
        if method == "eth_getBalance": return hex(self.balance)
        if method == "eth_getBlockByNumber": return {"baseFeePerGas": hex(10**9)}
        if method == "eth_maxPriorityFeePerGas": return hex(10**9)
        if method == "eth_getTransactionCount": return "0x0"
        raise RuntimeError(method)

class Bcast:
    def __init__(self): self.sent = []
    def broadcast(self, raw):
        self.sent.append(raw)
        return {"mock": "0x" + "ab" * 32}

class BalanceAndSweepTests(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp())
        wallets.STORE = d / "wallets.json"
        wallets.MASTER = d / "master.key"
        wallets.add_wallet(7, KEY, chain="ethereum")

    def test_balance_filter_selected_wallets(self):
        rows = [
            {"label": "W1", "address": "0xAa"},
            {"label": "W2", "address": "0xBb"},
            {"label": "W3", "address": "0xCc"},
        ]
        chosen = walletops.filter_selected_wallets(rows, ["0xbb", "0xcc"])
        self.assertEqual([row["label"] for row in chosen], ["W2", "W3"])
        self.assertEqual(walletops.filter_selected_wallets(rows, None), rows)

    def test_balance_wallet_multi_select_toggle(self):
        selected = walletops.toggle_wallet_selection(None, "0xAa")
        self.assertEqual(selected, ["0xaa"])
        selected = walletops.toggle_wallet_selection(selected, "0xBb")
        self.assertEqual(selected, ["0xaa", "0xbb"])
        selected = walletops.toggle_wallet_selection(selected, "0xAa")
        self.assertEqual(selected, ["0xbb"])

    def test_balance_total_is_selected_wallet_sum(self):
        rows = [
            {"label": "W1", "address": "0xAa"},
            {"label": "W2", "address": "0xBb"},
            {"label": "W3", "address": "0xCc"},
        ]
        selected = walletops.filter_selected_wallets(rows, ["0xbb", "0xcc"])
        amounts = {"0xaa": 8, "0xbb": 1.25, "0xcc": 2.5}
        result = walletops.aggregate_balances(
            [(w["label"], w["address"]) for w in selected],
            lambda address: {"amount": amounts[address.lower()], "usd": None, "symbol": "ETH"})
        self.assertEqual(str(result["total"]), "3.75")
        self.assertEqual([r["address"] for r in result["rows"]], ["0xBb", "0xCc"])

    def test_aggregation_one_line_failure_and_usd(self):
        def read(address):
            if address == "bad": raise OSError("rpc down")
            return {"amount": 1.25, "symbol": "ETH", "usd": 2500}
        result = walletops.aggregate_balances([("ok", ADDR), ("bad", "bad")], read)
        self.assertEqual(str(result["total"]), "1.25")
        self.assertEqual(str(result["usd_total"]), "2500")
        self.assertEqual(result["rows"][1]["error"], "OSError")

    def test_aggregation_hides_incomplete_usd_total_but_keeps_zero(self):
        incomplete = walletops.aggregate_balances(
            [("priced", ADDR), ("unpriced", "other")],
            lambda address: {"amount": 1, "symbol": "ETH", "usd": 100 if address == ADDR else None})
        self.assertIsNone(incomplete["usd_total"])

        zero = walletops.aggregate_balances(
            [("empty", ADDR)],
            lambda _address: {"amount": 0, "symbol": "ETH", "usd": 0})
        self.assertEqual(zero["usd_total"], 0)

    def test_native_plan_skips_wallet_without_gas(self):
        rpc = FakeRPC(10**18)
        token = {"kind": "native", "symbol": "ETH", "decimals": 18}
        plan = walletops.plan_sweep(rpc, CHAIN, token, [("main", ADDR)], DEST)
        self.assertEqual(plan["wallet_count"], 1)
        self.assertGreater(plan["gas_wei"], 0)
        poor = walletops.plan_sweep(FakeRPC(1), CHAIN, token, [("poor", ADDR)], DEST)
        self.assertEqual(poor["wallet_count"], 0)
        self.assertEqual(poor["skipped"][0]["label"], "poor")

    def test_execution_requires_final_confirmation(self):
        plan = {"rows": [{"label": "x", "address": ADDR, "to": DEST, "raw": 1, "ok": True}]}
        rpc = FakeRPC(10**18); bcast = Bcast()
        with self.assertRaises(PermissionError):
            walletops.execute_sweep(7, plan, {"kind": "native", "symbol": "ETH", "decimals": 18}, CHAIN, rpc, bcast)
        self.assertEqual(bcast.sent, [])

    def test_sol_plan_respects_rent_and_fee_and_confirm_gate(self):
        plan = walletops.plan_solana_sweep([("s", "So11111111111111111111111111111111111111112")], "dest", {"kind": "native", "symbol": "SOL", "decimals": 9}, get_balance=lambda a: 1_000_000_000, get_token_balance=lambda a,m: {}, rent_exempt=lambda a: 890_880, fee_lamports=5000)
        self.assertEqual(plan["rows"][0]["raw"], 1_000_000_000 - 890_880 - 5000)
        with self.assertRaises(PermissionError):
            walletops.execute_solana_sweep(7, plan, {"kind": "native"}, "dest", send_native=lambda *a: None, send_token=lambda *a: None)

if __name__ == "__main__": unittest.main()
