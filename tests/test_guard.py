import unittest, sys, os, time, threading
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from acobot import bot, wsfeed


class FakeApp:
    class _B:
        async def send_message(self, *a, **k):
            return None
    bot = _B()
    updater = None


class GuardTests(unittest.TestCase):
    def setUp(self):
        bot.MINTS.clear(); bot.THREADS.clear(); bot.CANCEL.clear(); bot._FIRED.clear()
        self.sent = []
        self._orig_notify = bot._notify
        bot._notify = lambda text: self.sent.append(text)
        bot.WATCH_RETRY_WAIT_S = 0.01
        bot.MAIN_LOOP[0] = None
        self._orig_persist = bot.persist
        bot.persist = lambda: None
        # snapshot every global the fake chain env touches, so other test modules see the real ones
        from acobot import sniper as _sn, wallets as _wl
        self._saved = {
            "cfg": bot.CFG, "chain": bot.CHAIN_NAME, "rpc": bot.RPC, "head_feed": bot.head_feed,
            "zrs": bot.zero_send_reason, "fr": bot.finish_results,
            "wf": _sn.wait_and_fire, "pre": _sn.presign, "se": _wl.signing_entries, "cm": _wl.chain_map,
            "restarts": bot.WATCH_RESTARTS,
        }

    def tearDown(self):
        bot._notify = self._orig_notify
        bot.persist = self._orig_persist
        from acobot import sniper as _sn, wallets as _wl
        s = self._saved
        bot.CFG, bot.CHAIN_NAME, bot.RPC, bot.head_feed = s["cfg"], s["chain"], s["rpc"], s["head_feed"]
        bot.zero_send_reason, bot.finish_results = s["zrs"], s["fr"]
        _sn.wait_and_fire, _sn.presign = s["wf"], s["pre"]
        _wl.signing_entries, _wl.chain_map = s["se"], s["cm"]
        bot.WATCH_RESTARTS = s["restarts"]

    def _fake_chain_env(self, crashes, then_ok=True):
        """Make watch_once crash `crashes` times (fake KeyError), then return cleanly."""
        calls = {"n": 0}
        class Cfg: pass
        cfg = Cfg()
        cfg.chains = {"robinhood": type("C", (), {
            "rpc": ["http://x"], "broadcast_rpc": ["http://x"], "name": "robinhood", "chain_id": 4663})()}
        bot.CFG = cfg
        bot.CHAIN_NAME = "robinhood"
        class FakeRPC:
            def assert_chain_id(self, expected):
                assert expected == 4663
        bot.RPC = lambda urls, **_kwargs: FakeRPC()
        bot.head_feed = lambda name: None
        bot.zero_send_reason = lambda *a, **k: ""
        bot.finish_results = lambda *a, **k: None

        class FakeSniper:
            pass
        def wait_and_fire(m2, chain, rpc, box, cancel, status_cb=None, bcast=None, feed=None):
            calls["n"] += 1
            if calls["n"] <= crashes:
                raise KeyError("price_wei")
            return {"results": [{"rpc": {"http://x": "0x" + "ab" * 32}}], "skipped": []}
        bot.sniper.wait_and_fire = wait_and_fire
        bot.sniper.presign = lambda *a, **k: []
        bot.wallets.signing_entries = lambda *a, **k: []
        bot.wallets.chain_map = lambda *a, **k: {}
        return calls

    def test_watcher_retries_then_recovers(self):
        bot.MINTS[7] = {"id": 7, "status": "scheduled", "chain": "robinhood", "contract": "0x1"}
        calls = self._fake_chain_env(crashes=2)
        bot.start_watcher(7, FakeApp())
        bot.THREADS[7].join(timeout=5)
        self.assertFalse(bot.THREADS[7].is_alive())
        self.assertEqual(calls["n"], 3)            # 2 crashes + 1 success
        retry_msgs = [m for m in self.sent if "crashed, restarting" in m]
        self.assertEqual(len(retry_msgs), 2)
        self.assertIn("KeyError", retry_msgs[0])   # real error detail, not a bare "crashed"
        self.assertNotEqual(bot.MINTS[7]["status"], "error")

    def test_watcher_gives_up_after_limit_with_detail(self):
        bot.MINTS[8] = {"id": 8, "status": "scheduled", "chain": "robinhood", "contract": "0x1"}
        bot.WATCH_RESTARTS = 3
        self._fake_chain_env(crashes=99)
        bot.start_watcher(8, FakeApp())
        bot.THREADS[8].join(timeout=5)
        self.assertFalse(bot.THREADS[8].is_alive())
        self.assertEqual(bot.MINTS[8]["status"], "error")
        self.assertIn("KeyError", bot.MINTS[8]["error_detail"])
        self.assertTrue(any("giving up" in m for m in self.sent))

    def test_no_refire_after_fire_started(self):
        bot.MINTS[9] = {"id": 9, "status": "scheduled", "chain": "robinhood", "contract": "0x1"}
        def wait_and_fire(m2, *a, **k):
            m2["fire_started"] = True
            raise RuntimeError("boom after broadcast")
        self._fake_chain_env(crashes=0)
        bot.sniper.wait_and_fire = wait_and_fire
        bot.start_watcher(9, FakeApp())
        bot.THREADS[9].join(timeout=5)
        self.assertEqual(bot.MINTS[9]["status"], "error")
        self.assertTrue(any("not re-firing" in m for m in self.sent))

    def test_persisted_fire_marker_prevents_watcher_retry(self):
        bot.MINTS[10] = {"id": 10, "status": "scheduled", "chain": "robinhood", "contract": "0x1"}
        self._fake_chain_env(crashes=0)

        def wait_and_fire(m2, *args, status_cb=None, **kwargs):
            status_cb("firing", {"shot": "normal"})
            raise RuntimeError("crash after durable marker")

        bot.sniper.wait_and_fire = wait_and_fire
        bot.start_watcher(10, FakeApp())
        bot.THREADS[10].join(timeout=5)
        self.assertEqual(bot.MINTS[10]["status"], "error")
        self.assertTrue(bot.MINTS[10]["fire_started"])
        self.assertTrue(any("not re-firing" in message for message in self.sent))

    def test_restart_marks_uncertain_broadcasts_without_restarting_them(self):
        bot.MINTS[15] = {"id": 15, "status": "firing", "fire_started": True}
        bot.MINTS[16] = {"id": 16, "status": "scheduled", "fire_started": True}
        bot.MINTS[17] = {"id": 17, "status": "scheduled", "fire_started": False}
        bot.MINTS[18] = {"id": 18, "status": "fired", "fire_started": True}
        uncertain = bot.recover_interrupted_mints()
        self.assertEqual(uncertain, [15, 16])
        self.assertEqual(bot.MINTS[15]["status"], "error")
        self.assertEqual(bot.MINTS[16]["status"], "error")
        self.assertEqual(bot.MINTS[17]["status"], "scheduled")
        self.assertEqual(bot.MINTS[18]["status"], "fired")

    def test_sweep_restarts_watcherless_scheduled_mints(self):
        bot.MINTS[11] = {"id": 11, "status": "scheduled", "chain": "robinhood"}
        bot.MINTS[12] = {"id": 12, "status": "fired", "chain": "robinhood"}
        dead = threading.Thread(target=lambda: None); dead.start(); dead.join()
        bot.THREADS[11] = dead              # the watcher died
        started = []
        orig = bot.start_watcher
        bot.start_watcher = lambda mid, app: started.append(mid)
        try:
            fixed = bot.sweep_watchers(FakeApp())
        finally:
            bot.start_watcher = orig
        self.assertEqual(fixed, [11])        # fired mint is not touched
        self.assertEqual(started, [11])

    def test_watchdog_silent_when_healthy(self):
        bot.MINTS[13] = {"id": 13, "status": "scheduled", "chain": "robinhood"}
        alive = threading.Event()
        t = threading.Thread(target=alive.wait, args=(5,), daemon=True); t.start()
        bot.THREADS[13] = t
        class Feed:
            def ensure_running(self): return False
            def subscribed(self): return True
            def down_for(self, now=None): return 0.0
            last_error = None
        orig_feed = bot.head_feed
        bot.head_feed = lambda name: Feed()
        class Upd:
            running = True
            _Updater__polling_task = type("T", (), {"done": lambda self: False})()
        class A(FakeApp):
            updater = Upd()
        try:
            fixes = bot.watchdog_tick(A())
        finally:
            bot.head_feed = orig_feed
            alive.set()
        self.assertEqual(fixes, [])

    def test_ws_down_notice_once(self):
        bot.MINTS[14] = {"id": 14, "status": "scheduled", "chain": "robinhood"}
        bot._WS_NOTICED.clear()
        class Feed:
            last_error = "ConnectionClosed: -32005"
            def ensure_running(self): return False
            def subscribed(self): return False
            def down_for(self, now=None): return 999.0
        orig_feed = bot.head_feed
        bot.head_feed = lambda name: Feed()
        bot.THREADS[14] = threading.Thread(target=lambda: None); bot.THREADS[14].start()
        try:
            for _ in range(3):
                bot.watchdog_tick(FakeApp())
        finally:
            bot.head_feed = orig_feed
        notices = [m for m in self.sent if "polling fallback active" in m]
        self.assertEqual(len(notices), 1)
        self.assertIn("#14", notices[0])


class BackoffTests(unittest.TestCase):
    def test_schedule_doubles_and_caps(self):
        got = [wsfeed.backoff_delay(n) for n in range(1, 9)]
        self.assertEqual(got, [2, 4, 8, 16, 32, 60, 60, 60])

    def test_health_fields_initialised(self):
        f = wsfeed.HeadFeed(["wss://x"], name="t")
        self.assertEqual(f.failures, 0)
        self.assertEqual(f.refusals, 0)
        self.assertIsNone(f.last_error)
        self.assertGreater(f.down_for(time.time()), -1)


if __name__ == "__main__":
    unittest.main()
