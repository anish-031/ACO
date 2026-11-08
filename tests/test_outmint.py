"""tests for the bars /outmint engine."""
from unittest.mock import MagicMock
from acobot import outmint
from acobot.tokenbuy import _selector
import json
import pytest


@pytest.fixture(autouse=True)
def isolate_outmint_config(tmp_path, monkeypatch):
    monkeypatch.setattr(outmint, "CONFIG_PATH", str(tmp_path / "bars_config.json"))


def test_bar_cost_ascending():
    assert outmint.bar_cost(1) == 1
    assert outmint.bar_cost(10) == 10


def test_bar_cost_invalid():
    for bad in (0, -1):
        try:
            outmint.bar_cost(bad)
            assert False
        except ValueError:
            pass


def test_no_contract_refuses():
    cfg = outmint.load_config()
    cfg["contract"] = None
    try:
        outmint.prepare(cfg, "cast", 1, 1, "0x" + "c" * 40, MagicMock())
        assert False
    except RuntimeError as e:
        assert "no verified bar contract" in str(e)


def test_count_cap():
    cfg = outmint.load_config()
    cfg["contract"] = "0x" + "d" * 40
    rpc = MagicMock()
    rpc.call.return_value = hex(1)
    try:
        outmint.prepare(cfg, "cast", 1, 11, "0x" + "c" * 40, rpc)
        assert False
    except ValueError:
        pass


class ChainRPC:
    def __init__(self, chain_id_hex):
        self.chain_id_hex = chain_id_hex
        self.calls = []

    def call(self, method, params):
        self.calls.append(method)
        if method == "eth_chainId":
            return self.chain_id_hex
        if method == "eth_getTransactionCount":
            return hex(5)
        if method == "eth_gasPrice":
            return hex(10**9)
        if method == "eth_estimateGas":
            return hex(120000)
        if method == "eth_call":
            return "0x" + "0" * 64
        raise AssertionError(method)


def test_prepare_builds_tx_per_bar():
    cfg = outmint.load_config()
    cfg["contract"] = "0x" + "d" * 40
    cfg["chain_id"] = 1
    rpc = ChainRPC(hex(1))
    p = outmint.prepare(cfg, "cast", 3, 2, "0x" + "c" * 40, rpc)
    assert p["kind"] == "bars_cast"
    assert p["unit_cost"] == 3 and p["total_cost"] == 6
    assert len(p["txs"]) == 2
    assert p["txs"][0]["nonce"] == 5 and p["txs"][1]["nonce"] == 6


def test_calldata_is_mint_selector():
    cfg = outmint.load_config()
    data = outmint.calldata(cfg, "cast", 4)
    assert data[2:10] == _selector(cfg["mint_signature"]).hex()


def test_calldata_melt():
    cfg = outmint.load_config()
    data = outmint.calldata(cfg, "melt", 2)
    assert data[2:10] == _selector(cfg["melt_signature"]).hex()


def test_chain_id_mismatch_refuses():
    cfg = outmint.load_config()
    cfg["contract"] = "0x" + "d" * 40
    cfg["chain_id"] = 1
    try:
        outmint.prepare(cfg, "cast", 1, 1, "0x" + "c" * 40, ChainRPC(hex(4663)))
        assert False
    except ValueError:
        pass


def test_execute_wallet_mismatch():
    cfg = outmint.load_config()
    cfg["contract"] = "0x" + "d" * 40
    cfg["chain_id"] = 1
    p = outmint.prepare(cfg, "cast", 1, 1, "0x" + "c" * 40, ChainRPC(hex(1)))
    ok, msg = outmint.execute_prepared(p, "0x" + "e" * 64, MagicMock())
    assert not ok


def test_execute_broadcasts_all():
    cfg = outmint.load_config()
    cfg["contract"] = "0x" + "d" * 40
    cfg["chain_id"] = 1
    from eth_account import Account
    priv = "0x" + "e" * 64
    wallet = Account.from_key(priv).address
    p = outmint.prepare(cfg, "cast", 1, 3, wallet, ChainRPC(hex(1)))
    bcast = MagicMock()
    txid = "0x" + "1" * 64
    bcast.broadcast.return_value = {"result": txid}
    ok, ids = outmint.execute_prepared(p, priv, bcast)
    assert ok and len(ids) == 3


def test_config_roundtrip():
    cfg = outmint.load_config()
    cfg["contract"] = "0x" + "d" * 40
    outmint.save_config(cfg)
    assert outmint.load_config()["contract"] == "0x" + "d" * 40
    cfg["contract"] = None
    outmint.save_config(cfg)
