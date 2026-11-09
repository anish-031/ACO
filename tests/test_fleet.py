import os, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from acobot import wallets, fleet, sniper
from acobot.config import Chain

KEY = "0x" + "11" * 32          # throwaway test key, no funds anywhere
KEY2 = "0x" + "22" * 32
CHAIN = Chain(name="ethereum", chain_id=1, rpc=["x"], broadcast_rpc=["x"], priority_fee_gwei=2.0)


def fix_store(tc):
    d = Path(tempfile.mkdtemp())
    wallets.STORE = d / "wallets.json"
    wallets.MASTER = d / "master.key"


class FakeRPC:
    def __init__(self, bal=10**18, minted=0, balance_of=0, token_ids=None, est_fail=True):
        self.bal = bal; self.minted = minted; self.balance_of = balance_of
        self.token_ids = token_ids or []; self.est_fail = est_fail; self.sent = []

    def call(self, method, params, url=None):
        if method == "eth_getBalance": return hex(self.bal)
        if method == "eth_getBlockByNumber": return {"baseFeePerGas": hex(10**9)}
        if method == "eth_maxPriorityFeePerGas": return hex(10**9)
        if method == "eth_getTransactionCount": return "0x0"
        if method == "eth_estimateGas":
            if self.est_fail: raise RuntimeError("revert")
            return hex(100000)
        if method == "eth_sendRawTransaction":
            self.sent.append(params[0]); return "0x" + "ab" * 32
        raise RuntimeError("unexpected " + method)

    def eth_call(self, to, data):
        sel = data[2:10]
        if sel == fleet.abi.selector_of("getMintStats(address,address)"):
            return "0x" + format(self.minted, "064x") + "0" * 128
        if sel == fleet.abi.selector_of("balanceOf(address)"):
            return "0x" + format(self.balance_of, "064x")
        if sel == fleet.abi.selector_of("tokenOfOwnerByIndex(address,uint256)"):
            i = int(data[-64:], 16)
            return "0x" + format(self.token_ids[i], "064x")
        raise RuntimeError("no selector")


class Wallets(unittest.TestCase):
    def setUp(self):
        fix_store(self)

    def test_chain_tag_and_cap(self):
        a, _, label = wallets.add_wallet(1, KEY, 1, chain="robinhood")
        self.assertEqual(label, "Wallet 1")
        self.assertEqual(wallets.list_wallets(1)[0]["chain"], "robinhood")
        self.assertEqual(wallets.chain_map(1)[a.lower()], "robinhood")
        self.assertTrue(wallets.set_chain(1, a, "ethereum"))
        self.assertEqual(wallets.chain_map(1)[a.lower()], "ethereum")
        with self.assertRaises(ValueError):
            wallets.add_wallet(1, KEY, 1)            # duplicate
        with self.assertRaises(ValueError):
            wallets.add_wallet(1, KEY2, 1, chain="solana")
        for i in range(3, 22):
            k = "0x" + format(i, "064x")
            try:
                wallets.add_wallet(1, k, 1)
            except ValueError:
                pass
        self.assertEqual(wallets.count(1), 20)
        with self.assertRaises(ValueError):
            wallets.add_wallet(1, "0x" + "ff" * 32, 1)

    def test_signing_entries_roundtrip_and_remove(self):
        a, _, _ = wallets.add_wallet(2, KEY, 2)
        ents = wallets.signing_entries([2])
        self.assertEqual(ents[0][3].lower(), KEY.lower())
        self.assertTrue(wallets.remove_wallet(2, a))
        self.assertEqual(wallets.list_wallets(2), [])

    def test_evm_mint_can_use_a_key_saved_on_another_evm_chain(self):
        wallets.add_wallet(3, KEY, 1, chain="ink")
        self.assertEqual(wallets.signing_entries([3], chain="ethereum"), [])
        found = wallets.signing_entries([3], chain="ethereum", evm_any=True)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][2].lower(), wallets.address_of(KEY).lower())
        self.assertEqual(wallets.signing_entries([3], chain="solana", evm_any=True), [])


class Fleet(unittest.TestCase):
    def setUp(self):
        fix_store(self)
        self.mint = {"contract": "0x" + "cc" * 20, "seadrop": True, "mint_to": sniper.encode_seadrop_mint and "0x" + "dd" * 20,
                     "fee_recipient": "0x" + "ee" * 20, "mint_sig": "mintPublic(address,address,address,uint256)",
                     "price_wei": 0, "per_wallet": 1, "speed": "fast", "gas_limit": 150000, "phase": "public"}
        self.addr = wallets.address_of(KEY)

    def test_minted_so_far_uses_seadrop_getmintstats(self):
        self.assertEqual(fleet.minted_so_far(FakeRPC(minted=1), self.mint["contract"], self.addr, seadrop=True), 1)

    def test_check_ok_and_limit_reached(self):
        r = fleet.check_wallet(FakeRPC(), CHAIN, self.mint, self.addr, "Wallet 1", 1)
        self.assertEqual(r["verdict"], "ok"); self.assertEqual(r["minted"], 0)
        r = fleet.check_wallet(FakeRPC(minted=1), CHAIN, self.mint, self.addr, "Wallet 1", 1)
        self.assertEqual(r["verdict"], "fail"); self.assertIn("already minted", r["reasons"][0])

    def test_low_balance_and_allowlist_fail(self):
        r = fleet.check_wallet(FakeRPC(bal=0), CHAIN, dict(self.mint, price_wei=10**17), self.addr, "Wallet 1", 1)
        self.assertEqual(r["verdict"], "warn")  # low balance warns, never blocks
        # gated stage + estimate reverts -> chain rejected this wallet: not on the allowlist
        r = fleet.check_wallet(FakeRPC(), CHAIN, dict(self.mint, phase="allowlist"), self.addr, "Wallet 1", 1)
        self.assertEqual(r["verdict"], "fail")
        self.assertEqual(r["eligibility"], False)
        self.assertIn("allowlist", r["reasons"][0])
        upcoming = dict(self.mint, phase="allowlist", start_ts=int(__import__("time").time()) + 3600)
        r = fleet.check_wallet(FakeRPC(), CHAIN, upcoming, self.addr, "Wallet 1", 1)
        self.assertIsNone(r["eligibility"])
        self.assertNotEqual(r["verdict"], "fail")
        # gated stage + estimate succeeds -> wallet verified on-chain
        r = fleet.check_wallet(FakeRPC(est_fail=False), CHAIN, dict(self.mint, phase="allowlist"), self.addr, "Wallet 1", 1)
        self.assertEqual(r["eligibility"], True)
        self.assertFalse(any("allowlist" in x for x in r["reasons"]))

    def test_fund_plan_short_and_ok(self):
        rpc = FakeRPC()
        ok = fleet.fund_plan(self.addr, 10**18, [("Wallet 2", "0x" + "33" * 20)], 10**16, CHAIN, rpc)
        self.assertTrue(ok["ok"]); self.assertEqual(ok["total_wei"], 10**16)
        short = fleet.fund_plan(self.addr, 0, [("Wallet 2", "0x" + "33" * 20)], 10**16, CHAIN, rpc)
        self.assertFalse(short["ok"])

    def test_execute_fund_signs_and_broadcasts(self):
        wallets.add_wallet(3, KEY, 1)
        rpc = FakeRPC()
        class B:
            def __init__(self): self.raws = []
            def broadcast(self, raw):
                self.raws.append(raw); return {"u": "0x" + "ab" * 32}
        b = B()
        out = fleet.execute_fund(3, "Wallet 1", self.addr, [("Wallet 2", "0x" + "33" * 20, 10**16)], CHAIN, rpc, b)
        self.assertTrue(out[0]["ok"]); self.assertEqual(len(b.raws), 1)

    def test_sweep_plan_and_execute(self):
        rpc = FakeRPC(balance_of=2, token_ids=[7, 9])
        rows, notes = fleet.sweep_plan(rpc, self.mint["contract"], [("Wallet 2", "0x" + "33" * 20)])
        self.assertEqual(rows, [("Wallet 2", "0x" + "33" * 20, [7, 9])]); self.assertEqual(notes, [])
        wallets.add_wallet(4, KEY2, 1)
        addr2 = wallets.address_of(KEY2)
        class B:
            def broadcast(self, raw): return {"u": "0x" + "ab" * 32}
        out = fleet.execute_sweep(4, self.mint["contract"], [("Wallet 2", addr2, [7, 9])], self.addr, CHAIN, FakeRPC(), B())
        self.assertTrue(out[0]["ok"]); self.assertEqual(out[0]["moved"], 2)


if __name__ == "__main__":
    unittest.main()


class ZeroSendReason(unittest.TestCase):
    def setUp(self):
        fix_store(self)

    def test_reasons(self):
        from acobot import bot
        self.assertIsNone(bot.zero_send_reason([1], "ethereum", set(), 1))
        self.assertIn("no keys loaded", bot.zero_send_reason([1], "ethereum", set(), 0))
        wallets.add_wallet(1, KEY, 1, chain="ethereum")
        r = bot.zero_send_reason([1], "robinhood", set(), 0)
        self.assertIn("0 wallets on robinhood", r)
        self.assertIn("ethereum", r)
        self.assertTrue(bot.zero_send_reason([1], "ethereum", set(), 0))
