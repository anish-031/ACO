"""tests for the unified /trade flow: routing, sell prep, guarded execution."""
from unittest.mock import MagicMock
from acobot import tokensell
from acobot.bot import _detect_trade_chain, _is_sol_ca


def test_detect_chain():
    assert _detect_trade_chain("0x" + "a" * 40)[0] == "evm"
    assert _detect_trade_chain("So11111111111111111111111111111111111111112")[0] == "sol"
    assert _detect_trade_chain("notanaddress")[0] is None


def test_is_sol_ca():
    assert _is_sol_ca("So11111111111111111111111111111111111111112")
    assert not _is_sol_ca("0x" + "a" * 40)


class FakeRPC:
    def __init__(self):
        self.allowance = 0

    def call(self, method, params):
        data = params[0].get("data", "") if method == "eth_call" else ""
        if method == "eth_chainId":
            return hex(4663)
        if method == "eth_gasPrice":
            return hex(1_000_000_000)
        if method == "eth_getTransactionCount":
            return hex(7)
        if method == "eth_estimateGas":
            return hex(250000)
        if method == "eth_call":
            if len(data) >= 10 and data[2:10] == tokensell._selector(tokensell.DECIMALS_SELECTOR).hex():
                return "0x" + hex(6)[2:].rjust(64, "0")
            if len(data) >= 10 and data[2:10] == tokensell._selector(tokensell.BALANCE_OF_SELECTOR).hex():
                return "0x" + hex(5_000_000)[2:].rjust(64, "0")
            if len(data) >= 10 and data[2:10] == tokensell._selector("allowance(address,address)").hex():
                return "0x" + hex(self.allowance)[2:].rjust(64, "0")
            if len(data) >= 10 and data[2:10] == tokensell._selector("quoteExactInputSingle((address,address,uint256,uint24,uint160))").hex():
                return "0x" + (hex(10**18)[2:].rjust(64, "0") + "0" * 192)
            if len(data) >= 10 and data[2:10] == tokensell._selector("symbol()").hex():
                return "0x" + (hex(3)[2:].rjust(64, "0") + b"ABC".hex().ljust(64, "0"))
        raise AssertionError(method)


def _quoted_raw(rpc):
    return tokensell.prepare_sell("0x" + "b" * 40, "all", "0x" + "c" * 40, rpc)


def test_prepare_sell_quotes_and_flags_approval():
    rpc = FakeRPC()
    prepared = _quoted_raw(rpc)
    assert prepared["kind"] == "evm_sell"
    assert prepared["needs_approval"] is True
    assert prepared["amount_in"] == 5_000_000
    assert prepared["amount_out"] == 10**18
    assert prepared["min_out"] == int(10**18 * 0.98)
    assert "multicall" in prepared["data"] or prepared["data"].startswith("0x")


def test_prepare_sell_no_balance():
    rpc = FakeRPC()
    rpc.allowance = 10**12
    try:
        tokensell.prepare_sell("0x" + "b" * 40, 0, "0x" + "c" * 40, rpc)
        assert False, "should raise"
    except ValueError:
        pass


def test_execute_sell_requires_chain_and_wallet():
    rpc = FakeRPC()
    prepared = _quoted_raw(rpc)
    ok, msg = tokensell.execute_sell({"chain_id": 1}, "0x" * 4, MagicMock())
    assert not ok and msg == "wrong chain"


def test_execute_sell_approval_flow():
    rpc = FakeRPC()
    prepared = _quoted_raw(rpc)
    bcast = MagicMock()
    txid = "0x" + "1" * 64
    bcast.broadcast.return_value = {"result": txid}
    bcast.call.return_value = {"status": "0x1"}
    priv = "0x" + "e" * 64
    from eth_account import Account
    wallet = Account.from_key(priv).address
    prepared["wallet"] = wallet
    ok, out = tokensell.execute_sell(prepared, priv, bcast)
    assert ok and out == txid
    assert bcast.broadcast.call_count == 2  # approve + swap


def test_execute_sell_does_not_swap_without_confirmed_approval(monkeypatch):
    rpc = FakeRPC()
    prepared = _quoted_raw(rpc)
    bcast = MagicMock()
    bcast.broadcast.return_value = {"rpc": "0x" + "1" * 64}
    bcast.call.return_value = None
    priv = "0x" + "e" * 64
    from eth_account import Account
    prepared["wallet"] = Account.from_key(priv).address
    timestamps = iter((0, 46))
    monkeypatch.setattr(tokensell.time, "time", lambda: next(timestamps))
    monkeypatch.setattr(tokensell.time, "sleep", lambda _seconds: None)

    ok, message = tokensell.execute_sell(prepared, priv, bcast)

    assert not ok
    assert message.startswith("approval receipt not confirmed; swap was not sent")
    assert bcast.broadcast.call_count == 1
