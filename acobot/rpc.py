import time, threading, concurrent.futures as cf
from web3 import Web3
from acobot.config import Chain

_providers = {}           # url + timeout -> cached web3.py HTTP provider
_providers_lock = threading.Lock()
_pool = cf.ThreadPoolExecutor(max_workers=64, thread_name_prefix="rpc")   # covers 20-wallet bursts across RPC endpoints
WARM_EVERY_S = 15


def err_text(e):
    """Verbatim rpc message from an exception. RuntimeError from call() carries the json-rpc error dict."""
    if isinstance(e, RuntimeError) and e.args and isinstance(e.args[0], dict):
        d = e.args[0]
        code, msg = d.get("code"), d.get("message")
        data = d.get("data")
        out = f"code {code}: {msg}" if msg else f"code {code}"
        if isinstance(data, str) and data:
            out += f" (data {data})"
        return out
    txt = str(e).strip()
    return f"{type(e).__name__}: {txt}" if txt else type(e).__name__


def _provider(url, timeout):
    """Reuse web3.py's HTTP provider and keep-alive session for each endpoint."""
    key = (url, timeout)
    with _providers_lock:
        provider = _providers.get(key)
        if provider is None:
            provider = Web3.HTTPProvider(
                url, request_kwargs={"timeout": timeout})
            _providers[key] = provider
        return provider


class RPC:
    def __init__(self, urls, timeout=5, *, expected_chain_id=None, broadcast=False):
        if isinstance(urls, Chain):
            expected_chain_id = urls.chain_id
            urls = (urls.broadcast_rpc or urls.rpc) if broadcast else urls.rpc
        elif broadcast:
            raise TypeError("broadcast=True requires a configured Chain")
        if isinstance(urls, str):
            urls = [urls]
        self.urls = list(urls)
        self.timeout = timeout
        self.expected_chain_id = expected_chain_id
        self._chain_verified = False
        self._identity_lock = threading.Lock()

    def _verify_chain(self, *, force=False):
        if self.expected_chain_id is None:
            return
        with self._identity_lock:
            if self._chain_verified and not force:
                return
            verified = []
            mismatches = []
            failures = []
            for endpoint in self.urls:
                try:
                    actual = int(self.call("eth_chainId", [], url=endpoint), 16)
                except Exception as exc:
                    failures.append(type(exc).__name__)
                    continue
                if actual != self.expected_chain_id:
                    mismatches.append(actual)
                    continue
                verified.append(endpoint)
            if not verified:
                details = []
                if mismatches:
                    details.append(
                        "reported chain ID(s) "
                        + ", ".join(map(str, sorted(set(mismatches))))
                        + f"; expected {self.expected_chain_id}")
                if failures:
                    details.append(f"{len(failures)} endpoint verification failure(s)")
                suffix = f" ({'; '.join(details)})" if details else ""
                raise RuntimeError(
                    f"no RPC endpoint could be verified for selected chain ID "
                    f"{self.expected_chain_id}{suffix}")
            self.urls = verified
            self._chain_verified = True

    def call(self, method, params, url=None):
        """Try the endpoints in order; failover on any error (dead endpoint, rate limit, bad reply).
        Explicit url arg pins to one endpoint (broadcast paths use that deliberately)."""
        if method != "eth_chainId":
            self._verify_chain()
        if url and self.expected_chain_id is not None and url not in self.urls:
            raise RuntimeError("RPC endpoint is not verified for the selected network")
        if url:
            urls = [url]
        else:
            urls = list(self.urls)
        last_exc = None
        # two passes over the endpoints: a 429/rate-limit on one host often clears in a beat
        for attempt in range(2):
            for u in urls:
                try:
                    res = _provider(u, self.timeout).make_request(method, params)
                    if "error" in res:
                        raise RuntimeError(res["error"])
                    if "result" not in res:
                        raise RuntimeError(f"rpc {method} reply had no result: {str(res)[:160]}")
                    return res["result"]
                except Exception as exc:
                    if isinstance(exc, RuntimeError) and exc.args and isinstance(exc.args[0], dict):
                        last_exc = exc
                    elif isinstance(exc, RuntimeError):
                        last_exc = exc
                    else:
                        last_exc = RuntimeError(f"RPC transport failed: {type(exc).__name__}")
                    continue
            if attempt == 0:
                time.sleep(1.2)
        raise last_exc if last_exc else RuntimeError("no rpc endpoints configured")

    def assert_chain_id(self, expected):
        """Verify endpoints and retain only those reporting the selected EVM network."""
        if self.expected_chain_id is not None and self.expected_chain_id != expected:
            raise RuntimeError(
                f"RPC was configured for chain ID {self.expected_chain_id}, not {expected}")
        self.expected_chain_id = expected
        self._chain_verified = False
        self._verify_chain(force=True)
        return expected

    def block_number(self):
        return int(self.call("eth_blockNumber", []), 16)

    def get_code(self, addr):
        return self.call("eth_getCode", [addr, "latest"])

    def eth_call(self, to, data):
        return self.call("eth_call", [{"to": to, "data": data}, "latest"])

    def broadcast(self, raw_tx, wait_all=False):
        """Send the same signed tx to every broadcast endpoint at once (warm keep-alive sockets).

        Default: return as soon as the FIRST endpoint accepts (tx is in a node's mempool, spreading
        from there) — stragglers keep running in the background. wait_all=True waits for every
        endpoint (used only for diagnostics). Same-nonce replacement races still favor the fastest.
        """
        self._verify_chain()
        futs = {_pool.submit(self.call, "eth_sendRawTransaction", [raw_tx], u): u for u in self.urls}
        results = {}
        for f in cf.as_completed(futs):
            try:
                results[futs[f]] = f.result()
            except Exception as e:
                results[futs[f]] = "ERR " + err_text(e)
            result = results[futs[f]]
            if (not wait_all and isinstance(result, str) and result.startswith("0x")
                    and len(result) == 66 and all(c in "0123456789abcdefABCDEF" for c in result[2:])):
                # JSON-RPC success is a transaction-hash string; errors are stored as "ERR ...".
                # First acceptance wins; remaining endpoint requests continue in the background.
                break
        return results

    def warm(self):
        """Ping every endpoint (eth_blockNumber) so the socket is open and hot. Returns {url: ms|error}."""
        out = {}
        for u in self.urls:
            t = time.perf_counter()
            try:
                self.call("eth_blockNumber", [], url=u)
                out[u] = round((time.perf_counter() - t) * 1000, 1)
            except Exception as e:
                out[u] = "ERR " + err_text(e)
        return out


_warm_started = set()


def start_warmers(urls, expected_chain_id=None):
    """Background keep-alive: ping each endpoint every WARM_EVERY_S so fire never meets a cold/closed socket."""
    new = [u for u in urls if u not in _warm_started]
    if not new:
        return
    _warm_started.update(new)

    def loop(u):
        while True:
            try:
                RPC([u], expected_chain_id=expected_chain_id).call("eth_blockNumber", [], url=u)
            except Exception:
                pass
            time.sleep(WARM_EVERY_S)

    for u in new:
        threading.Thread(target=loop, args=(u,), daemon=True, name="rpc-warm").start()
