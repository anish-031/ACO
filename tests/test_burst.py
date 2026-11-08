import time
from acobot import sniper


class FakeRpc:
    def __init__(self, fail=0):
        self.fail = fail
        self.calls = []

    def broadcast(self, raw):
        self.calls.append((raw, time.perf_counter()))
        if self.fail > 0:
            self.fail -= 1
            raise RuntimeError("nonce too low")
        return {"result": "0xabc"}


class FakeFeed:
    def available(self):
        return True

    def wait_next(self, timeout):
        return 1


def _txs(n):
    return [{"label": f"w{i}", "address": f"0x{i}", "raw": f"0xraw{i}"} for i in range(n)]


def test_stagger_spaces_broadcasts():
    rpc = FakeRpc()
    res = sniper.fire(_txs(3), rpc, stagger_ms=60)
    assert len(res["results"]) == 3
    times = sorted(t for _, t in rpc.calls)
    assert times[2] - times[0] >= 0.10


def test_no_stagger_is_parallel():
    rpc = FakeRpc()
    res = sniper.fire(_txs(3), rpc, stagger_ms=0)
    assert res["elapsed_s"] < 0.5


def test_retry_once_when_all_fail():
    rpc = FakeRpc(fail=2)
    res = sniper.fire(_txs(2), rpc)
    out = sniper._retry_failed(res, _txs(2), rpc, FakeFeed())
    assert out.get("retry_of") is not None
    assert all("error" not in (r["rpc"] or {}) for r in out["results"])


def test_no_retry_when_some_succeeded():
    rpc = FakeRpc(fail=1)
    res = sniper.fire(_txs(2), rpc)
    out = sniper._retry_failed(res, _txs(2), rpc, FakeFeed())
    assert out is res
