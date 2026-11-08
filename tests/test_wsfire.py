import os, ssl, sys, threading, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from acobot import sniper
from acobot.wsfeed import HeadFeed, _ssl_context


class FakeRpc:
    def __init__(self, live=False):
        self.live = live
        self.calls = 0

    def eth_call(self, to, data):
        self.calls += 1
        return hex(1 if self.live else 0)


class FakeFeed:
    """Stands in for HeadFeed: scripted list of headers, consumed one per wait_next."""
    def __init__(self, heads):
        self.heads = list(heads)
        self.block_time_s = 2.0
        self.connected = True

    def available(self):
        return self.connected

    def wait_next(self, timeout):
        return self.heads.pop(0) if self.heads else None


def mint_dict(start_ts, **kw):
    m = {"contract": "0x" + "11" * 20, "sale_started_fn": "saleStarted()", "start_ts": start_ts,
         "_entries_fn": lambda: [{"label": "w", "raw": "0xraw", "address": "0xa", "error": None, "priv": None}]}
    m.update(kw)
    return m


class WsFireTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.shots = []
        self.fired_on = []
        orig = sniper.fire

        def fake_fire(txs, rpc, status_cb=None, key="raw", shot=None, stagger_ms=None):
            self.sent.append(len(txs))
            self.shots.append(shot)
            return {"fired_at": time.time(), "elapsed_s": 0, "results": [], "skipped": []}
        self._orig_fire = orig
        sniper.fire = fake_fire
        self.addCleanup(lambda: setattr(sniper, "fire", orig))
        # refresh needs a real signer; stub it out for the fake-stream tests
        self._orig_refresh = sniper.refresh
        sniper.refresh = lambda txs, *a, **k: txs
        self.addCleanup(lambda: setattr(sniper, "refresh", self._orig_refresh))

    def chain(self):
        class C:
            name = "fake"
        return C()

    def test_polling_sale_probe_is_rate_limited(self):
        class CountRpc:
            calls = 0

            def eth_call(self, to, data):
                self.calls += 1
                return "0x0"

        rpc = CountRpc()
        mint = mint_dict(1000)
        self.assertFalse(sniper._ready(mint, rpc, 10))
        self.assertFalse(sniper._ready(mint, rpc, 10.1))
        self.assertFalse(sniper._ready(mint, rpc, 10.21))
        self.assertEqual(rpc.calls, 2)
        self.assertEqual(sniper.POLL_INTERVAL_S, 0.05)

    def test_fires_on_the_header_that_crosses_start_no_sleep(self):
        # heads at ts 90, 98, 104 ; start 100 -> must fire on the 104 head, not before
        feed = FakeFeed([{"number": 1, "timestamp": 90, "seen_at": 0},
                         {"number": 2, "timestamp": 98, "seen_at": 0},
                         {"number": 3, "timestamp": 104, "seen_at": 0}])
        box = {"txs": [{"label": "w", "raw": "0xraw", "address": "0xa"}]}
        m = mint_dict(100)
        t0 = time.time()
        res = sniper.wait_and_fire(m, self.chain(), FakeRpc(False), box, lambda: False, feed=feed)
        self.assertIsNotNone(res)
        self.assertEqual(self.sent, [1])
        self.assertEqual(feed.heads, [])           # consumed exactly up to the firing header
        self.assertLess(time.time() - t0, 2.0)     # no polling sleep path

    def test_fires_immediately_when_sale_started_on_header(self):
        feed = FakeFeed([{"number": 5, "timestamp": 10, "seen_at": 0}])
        box = {"txs": [{"label": "w", "raw": "0xraw", "address": "0xa"}]}
        res = sniper.wait_and_fire(mint_dict(10**12), self.chain(), FakeRpc(True), box, lambda: False, feed=feed)
        self.assertIsNotNone(res)
        self.assertEqual(self.sent, [1])

    def test_prefire_fires_one_block_early(self):
        # head ts 97, block time 2 -> next block ts 99 < start 100: no fire yet
        # head ts 98 -> next block 100 >= start: fire now (tx lands in the block mint opens)
        feed = FakeFeed([{"number": 1, "timestamp": 97, "seen_at": 0},
                         {"number": 2, "timestamp": 98, "seen_at": 0}])
        box = {"txs": [{"label": "w", "raw": "0xraw", "address": "0xa"}]}
        m = mint_dict(100, prefire=True)
        res = sniper.wait_and_fire(m, self.chain(), FakeRpc(False), box, lambda: False, feed=feed)
        self.assertIsNotNone(res)
        self.assertEqual(feed.heads, [])

    def test_gated_prefire_mode_waits_for_the_opening_block_boundary(self):
        feed = FakeFeed([{"number": 1, "timestamp": 97, "seen_at": 0},
                         {"number": 2, "timestamp": 98, "seen_at": 0}])
        box = {"txs": [{"label": "w", "raw": "0xauth", "address": "0xa"}]}
        mint = mint_dict(100, fire_mode=sniper.PREFIRE, authorization_at_fire=True)

        result = sniper.wait_and_fire(
            mint, self.chain(), FakeRpc(False), box, lambda: False, feed=feed)

        self.assertIsNotNone(result)
        self.assertEqual(self.shots, ["prefire"])
        self.assertEqual(feed.heads, [])

    def test_dual_mode_persists_fire_started_before_first_broadcast(self):
        feed = FakeFeed([{"number": 1, "timestamp": 98, "seen_at": 0},
                         {"number": 2, "timestamp": 100, "seen_at": 0}])
        box = {"txs": [{"label": "w", "raw": "0xnormal", "pre_raw": "0xpre",
                        "address": "0xa", "pre_info": {"prio_wei": 1, "max_wei": 1}}]}
        m = mint_dict(100, fire_mode=sniper.DUAL, authorization_at_fire=True)
        statuses, shots = [], []
        original_fire = sniper.fire

        def fake_fire(txs, rpc, status_cb=None, key="raw", shot=None, stagger_ms=None):
            shots.append(shot)
            return {"fired_at": time.time(), "elapsed_s": 0, "results": [], "skipped": []}

        sniper.fire = fake_fire
        try:
            res = sniper.wait_and_fire(m, self.chain(), FakeRpc(False), box, lambda: False,
                                       status_cb=lambda kind, payload: statuses.append(kind), feed=feed)
        finally:
            sniper.fire = original_fire
        self.assertIsNotNone(res)
        self.assertTrue(m["fire_started"])
        self.assertEqual(statuses, ["firing", "fired"])
        self.assertEqual(shots, ["prefire", "normal"])

    def test_known_sale_end_prevents_late_broadcast(self):
        feed = FakeFeed([{"number": 4, "timestamp": 110, "seen_at": 0}])
        box = {"txs": [{"label": "w", "raw": "0xraw", "address": "0xa"}]}
        statuses = []
        res = sniper.wait_and_fire(mint_dict(100, end_ts=105), self.chain(), FakeRpc(True), box,
                                   lambda: False, status_cb=lambda kind, payload: statuses.append(kind),
                                   feed=feed)
        self.assertIsNone(res)
        self.assertEqual(self.sent, [])
        self.assertEqual(statuses, ["expired"])

    def test_no_feed_uses_polling_path(self):
        box = {"txs": [{"label": "w", "raw": "0xraw", "address": "0xa"}]}
        res = sniper.wait_and_fire(mint_dict(time.time() - 1), self.chain(), FakeRpc(False), box,
                                   lambda: False, feed=None)
        self.assertIsNotNone(res)

    def test_feed_disconnected_falls_back_to_polling(self):
        feed = FakeFeed([])
        feed.connected = False
        box = {"txs": [{"label": "w", "raw": "0xraw", "address": "0xa"}]}
        res = sniper.wait_and_fire(mint_dict(time.time() - 1), self.chain(), FakeRpc(False), box,
                                   lambda: False, feed=feed)
        self.assertIsNotNone(res)

    def test_cancel_returns_none(self):
        box = {"txs": []}
        res = sniper.wait_and_fire(mint_dict(time.time() + 10**6), self.chain(), FakeRpc(False), box,
                                   lambda: True, feed=None)
        self.assertIsNone(res)


class HeadFeedTests(unittest.TestCase):
    def test_ssl_context_uses_verified_system_or_certifi_trust(self):
        context = _ssl_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertTrue(context.get_ca_certs())

    def test_on_head_sets_latest_and_block_time(self):
        f = HeadFeed(["wss://x"])
        f._on_head({"number": "0x10", "timestamp": "0x64"})   # 100
        f._on_head({"number": "0x11", "timestamp": "0x66"})   # 102
        self.assertEqual(f.latest["number"], 0x11)
        self.assertAlmostEqual(f.block_time_s, 2.0)
        self.assertFalse(f.available())   # no connected url yet

    def test_wait_next_wakes_on_new_head(self):
        f = HeadFeed(["wss://x"])
        threading.Timer(0.05, lambda: f._on_head({"number": "0x1", "timestamp": "0x1"})).start()
        h = f.wait_next(timeout=2)
        self.assertEqual(h["number"], 1)


if __name__ == "__main__":
    unittest.main()
