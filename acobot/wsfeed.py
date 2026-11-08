"""Hot websocket newHeads feed per chain.

One daemon thread runs an asyncio loop holding a persistent wss connection with keepalive pings and
reconnect/backoff. Every new header updates `latest` and wakes anyone waiting on the feed (the sniper's
fire loop and the receipt tracker). The feed never fires or sends anything itself: waiters do, on the
header they were woken by, so a slow consumer can't stall the socket.

If the feed is not connected, or has been silent past STALE_S, `available()` is False and callers keep
using the original HTTP polling path. Nothing else changes for them.
"""
import asyncio, json, logging, ssl, threading, time

log = logging.getLogger("acobot.wsfeed")
STALE_S = 15.0
BACKOFF_BASE_S = 2.0      # first retry after 2s, then 4, 8, 16, 32, capped at BACKOFF_MAX_S
BACKOFF_MAX_S = 60.0


def backoff_delay(failures):
    """Seconds to wait after the Nth consecutive failure (1-based). 2, 4, 8, ... capped at 60."""
    n = max(1, int(failures))
    return min(BACKOFF_MAX_S, BACKOFF_BASE_S * (2 ** (n - 1)))


def _hex_int(x):
    return int(x, 16) if isinstance(x, str) and x.startswith("0x") else int(x or 0)


def _ssl_context():
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


class HeadFeed:
    def __init__(self, urls, name="chain", expected_chain_id=None):
        self.urls = list(urls)
        self.name = name
        self.expected_chain_id = expected_chain_id
        self.latest = None          # {"number", "timestamp", "seen_at"}
        self.block_time_s = None    # rolling avg seconds between heads (chain time)
        self.connected_url = None
        self.seq = 0
        self._cond = threading.Condition()
        self._started = False
        self._prev_ts = None
        self.failures = 0           # consecutive failed connect/subscribe attempts (drives backoff)
        self.down_since = time.time()   # when the feed last lost its subscription (None while subscribed)
        self.refusals = 0          # total subscribe refusals seen (rate limit etc.), for logs/tests
        self.last_error = None     # short text of the last drop/refusal

    # ---- lifecycle -------------------------------------------------------
    def start(self):
        if self._started or not self.urls:
            return self
        self._started = True
        self._spawn()
        return self

    def _spawn(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"wsfeed-{self.name}")
        self._thread.start()

    def thread_alive(self):
        th = getattr(self, "_thread", None)
        return bool(th and th.is_alive())

    def _verify_chain_id(self, response):
        if response.get("id") != 0 or "error" in response:
            raise RuntimeError("chain identity check was refused")
        actual = _hex_int(response.get("result"))
        if actual != self.expected_chain_id:
            raise RuntimeError(
                f"websocket reported chain ID {actual}; expected {self.expected_chain_id}")

    def ensure_running(self):
        """Watchdog hook: restart the feed thread if it died. Returns True when it had to restart it."""
        if not self.urls or not self._started or self.thread_alive():
            return False
        log.warning("wsfeed %s thread was dead; restarting", self.name)
        self._spawn()
        return True

    def _run(self):
        try:
            asyncio.run(self._main())
        except Exception as e:  # never let the feed kill the bot; the watchdog restarts the thread
            self.last_error = f"{type(e).__name__}: {str(e)[:160]}"
            log.warning("wsfeed %s stopped: %s", self.name, self.last_error)

    async def _main(self):
        import websockets
        tls = _ssl_context()
        i = 0          # index into urls (rotates on failure)
        while True:
            url = self.urls[i % len(self.urls)]
            try:
                async with websockets.connect(url, ssl=tls, open_timeout=8, ping_interval=20, ping_timeout=20,
                                              max_size=2**22, close_timeout=3) as ws:
                    if self.expected_chain_id is not None:
                        await ws.send(json.dumps({"jsonrpc": "2.0", "id": 0, "method": "eth_chainId",
                                                  "params": []}))
                        identity = json.loads(await asyncio.wait_for(ws.recv(), 8))
                        self._verify_chain_id(identity)
                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_subscribe",
                                              "params": ["newHeads"]}))
                    # the ack is the reply with id 1; a head may arrive before it, so skip non-acks
                    while True:
                        ack = json.loads(await asyncio.wait_for(ws.recv(), 8))
                        if ack.get("id") == 1:
                            break
                        if ack.get("method") == "eth_subscription":
                            self._on_head(ack["params"]["result"])
                    if "error" in ack or not ack.get("result"):
                        self.refusals += 1
                        raise RuntimeError(f"subscribe refused: {str(ack.get('error'))[:120]}")
                    self.connected_url = url
                    self.down_since = None
                    self.failures = 0          # only a real subscription resets the backoff
                    self.last_error = None
                    log.info("wsfeed %s subscribed (%s)", self.name, _host(url))
                    while True:
                        msg = json.loads(await ws.recv())
                        if msg.get("method") == "eth_subscription":
                            self._on_head(msg["params"]["result"])
            except Exception as e:
                self.connected_url = None
                if self.down_since is None:
                    self.down_since = time.time()
                self.failures += 1
                self.last_error = f"{type(e).__name__}: {str(e)[:160]}"
                wait = backoff_delay(self.failures)
                # first failure of a run is logged; repeats are logged at debug so the log doesn't flood
                lvl = log.info if self.failures <= 1 else log.debug
                lvl("wsfeed %s dropped (%s): %s; retry in %.0fs (failure #%d)",
                    self.name, _host(url), self.last_error, wait, self.failures)
                await asyncio.sleep(wait)
                i += 1

    # ---- head handling ---------------------------------------------------
    def _on_head(self, h):
        num = _hex_int(h.get("number"))
        ts = _hex_int(h.get("timestamp"))
        now = time.time()
        with self._cond:
            if self._prev_ts is not None and ts > self._prev_ts:
                bt = float(ts - self._prev_ts)
                self.block_time_s = bt if self.block_time_s is None else 0.7 * self.block_time_s + 0.3 * bt
            self._prev_ts = ts
            self.latest = {"number": num, "timestamp": ts, "seen_at": now}
            self.seq += 1
            self._cond.notify_all()

    # ---- consumer API ----------------------------------------------------
    def subscribed(self):
        return bool(self.connected_url)

    def down_for(self, now=None):
        """Seconds since the subscription was lost (0 when subscribed)."""
        if self.connected_url or self.down_since is None:
            return 0.0
        return (now or time.time()) - self.down_since

    def available(self):
        l = self.latest
        return bool(self.connected_url and l and time.time() - l["seen_at"] < STALE_S)

    def wait_next(self, timeout):
        """Block until the next header arrives or timeout. Returns the head dict or None."""
        with self._cond:
            start = self.seq
            self._cond.wait_for(lambda: self.seq != start, timeout)
            return self.latest if self.seq != start else None


def _host(url):
    return url.split("//", 1)[-1].split("/", 1)[0]
