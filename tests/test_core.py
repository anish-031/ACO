import unittest, sys, os
import threading
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from acobot import abi, scheduler, rpc as rpcmod, wallets

class T(unittest.TestCase):
    def test_selectors_from_push4(self):
        # PUSH4 aabbccdd, PUSH4 11223344, STOP
        code = "0x63aabbccdd6311223344" + "00"
        self.assertEqual(abi.selectors_from_bytecode(code), {"aabbccdd", "11223344"})

    def test_analyze_filters_mint(self):
        code = "0x63aabbccdd63deadbeef00"
        names = {"aabbccdd": "mint(uint256)", "deadbeef": "transferOwnership(address)"}
        r = abi.analyze(code, resolve=lambda s: names.get(s))
        self.assertEqual(r["mint_functions"], ["mint(uint256)"])

    def test_is_live_rules(self):
        m = scheduler.ScheduledMint(contract="0x0", chain="ethereum", start_ts=100)
        self.assertFalse(scheduler.is_live(m, 99))
        self.assertTrue(scheduler.is_live(m, 100))
        self.assertTrue(scheduler.is_live(scheduler.ScheduledMint("0x0","e"), 0, True))
        m.fired = True
        self.assertFalse(scheduler.is_live(m, 500))

    def test_broadcast_fanout_reports_errors(self):
        r = rpcmod.RPC(["http://127.0.0.1:1", "http://127.0.0.1:2"], timeout=1)
        res = r.broadcast("0x00")
        self.assertEqual(len(res), 2)
        self.assertTrue(all(v.startswith("ERR") for v in res.values()))

    def test_broadcast_returns_on_first_accepted_hash(self):
        slow_started = threading.Event()
        release_slow = threading.Event()
        fast_hash = "0x" + "ab" * 32
        r = rpcmod.RPC(["http://slow", "http://fast"])

        def fake_call(method, params, url=None):
            if url == "http://slow":
                slow_started.set()
                release_slow.wait(2)
                return "0x" + "cd" * 32
            return fast_hash

        r.call = fake_call
        with ThreadPoolExecutor(max_workers=1) as executor:
            try:
                future = executor.submit(r.broadcast, "0x00")
                self.assertTrue(slow_started.wait(1))
                self.assertEqual(future.result(timeout=1), {"http://fast": fast_hash})
            finally:
                release_slow.set()

    def test_broadcast_wait_all_waits_for_every_endpoint(self):
        slow_started = threading.Event()
        release_slow = threading.Event()
        slow_hash = "0x" + "cd" * 32
        fast_hash = "0x" + "ab" * 32
        r = rpcmod.RPC(["http://slow", "http://fast"])

        def fake_call(method, params, url=None):
            if url == "http://slow":
                slow_started.set()
                release_slow.wait(2)
                return slow_hash
            return fast_hash

        r.call = fake_call
        with ThreadPoolExecutor(max_workers=1) as executor:
            try:
                future = executor.submit(r.broadcast, "0x00", True)
                self.assertTrue(slow_started.wait(1))
                self.assertFalse(future.done())
                release_slow.set()
                self.assertEqual(future.result(timeout=1),
                                 {"http://fast": fast_hash, "http://slow": slow_hash})
            finally:
                release_slow.set()

    def test_key_validation_and_mask(self):
        self.assertTrue(wallets.validate_key("0x" + "ab" * 32))
        self.assertFalse(wallets.validate_key("not a key"))
        self.assertEqual(wallets.mask("0x1234567890abcdef1234567890abcdef12345678"), "0x1234567890abcdef1234567890abcdef12345678")

if __name__ == "__main__":
    unittest.main()


class T2(unittest.TestCase):
    def test_encode_mint_args(self):
        from acobot import sniper
        self.assertEqual(sniper.encode_mint("mint()", 1), "0x" + abi.selector_of("mint()"))
        d = sniper.encode_mint("mint(uint256)", 3)
        self.assertEqual(d[:10], "0x" + abi.selector_of("mint(uint256)"))
        self.assertEqual(int(d[10:], 16), 3)

    def test_fire_skips_unsigned(self):
        from acobot import sniper
        class FakeRPC:
            def broadcast(self, raw): return {"u": "0xabc"}
        r = sniper.fire([{"label": "a", "raw": None}, {"label": "b", "raw": "0x01"}], FakeRPC())
        self.assertEqual(len(r["results"]), 1)
        self.assertEqual(len(r["skipped"]), 1)

    def test_address_of(self):
        from acobot import wallets
        a = wallets.address_of("0x" + "11" * 32)
        self.assertTrue(a.startswith("0x") and len(a) == 42)


class T3(unittest.TestCase):
    def test_seadrop_mint_encoding(self):
        from acobot import sniper
        nft = "0x95e72f45dfdb5004a4733210d6034480d6c5637a"
        fee = "0x0000a26b00c1f0df003000390027140000faa719"
        minter = "0x" + "11" * 20
        d = sniper.encode_seadrop_mint(nft, fee, minter, 1)
        self.assertEqual(d[:10], "0x161ac21f")
        self.assertEqual(d[10:74], nft[2:].rjust(64, "0"))
        self.assertEqual(d[74:138], fee[2:].rjust(64, "0"))
        self.assertEqual(d[138:202], minter[2:].rjust(64, "0"))
        self.assertEqual(int(d[202:], 16), 1)

    def test_stage_line_gated_and_public(self):
        from acobot import bot
        self.assertIn("need allowlist spot", bot._stage_line({"key": "allowlist", "gated": True, "merkle_root": "0x" + "ab" * 32}))
        s = bot._stage_line({"key": "public", "gated": False, "price_wei": 0, "start_ts": 1793075400, "end_ts": 1793082600, "per_wallet": 1})
        self.assertIn("free", s)
        self.assertIn("snipeable", s)
