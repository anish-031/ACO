import pytest
from eth_abi import encode
from eth_account import Account

from acobot import evmswap


KEY = "0x" + "11" * 32
WALLET = Account.from_key(KEY).address
TOKEN = "0x" + "22" * 20
ROUTER = "0x" + "33" * 20


class ReadRPC:
    def __init__(self, chain_id=1):
        self.chain_id = chain_id
        self.calls = []

    def call(self, method, params):
        self.calls.append((method, params))
        if method == "eth_chainId":
            return hex(self.chain_id)
        if method == "eth_estimateGas":
            return hex(100_000)
        if method == "eth_getTransactionCount":
            return hex(7)
        if method == "eth_gasPrice":
            return hex(1_000_000_000)
        raise AssertionError(method)


class Broadcast:
    def __init__(self, receipt=None):
        self.raw = []
        self.receipt = receipt

    def broadcast(self, raw):
        self.raw.append(raw)
        return {"rpc": "0x" + "ab" * 32}

    def call(self, *_args):
        return self.receipt


def _quote_buy(_cfg, _token, amount_raw, *_args, sell=False):
    return {"amount_out": 1_000_000, "min_out": 980_000, "to": ROUTER,
            "data": "0x1234", "value": 0 if sell else amount_raw,
            "venue": "uniswap_v4" if _cfg["chain_id"] == 57073 else "uniswap_v3", "extra": {}}


def _quote_sell(*_args, sell=False):
    return {"amount_out": 1_000_000, "min_out": 980_000, "to": ROUTER,
            "data": "0x1234", "value": 0, "venue": "uniswap_v3", "extra": {}}


def test_prepare_ethereum_buy_is_read_only_and_estimates_tx(monkeypatch):
    rpc = ReadRPC()
    monkeypatch.setattr(evmswap, "_v3_quote", _quote_buy)
    prepared = evmswap.prepare("ethereum", TOKEN, "0.01", WALLET, rpc)
    assert prepared["kind"] == "ethereum_buy"
    assert prepared["amount_in"] == 10**16
    assert prepared["amount_out"] == 1_000_000
    assert prepared["gas"] == 120_000
    assert prepared["nonce"] == 7
    assert prepared["value"] == 10**16
    assert all(method != "eth_sendRawTransaction" for method, _ in rpc.calls)


def test_prepare_ink_buy_uses_v4_quote(monkeypatch):
    rpc = ReadRPC(chain_id=57073)
    monkeypatch.setattr(evmswap, "_v4_quote", _quote_buy)
    prepared = evmswap.prepare("ink", TOKEN, "0.01", WALLET, rpc)
    assert prepared["kind"] == "ink_buy"
    assert prepared["venue"] == "uniswap_v4"
    assert prepared["amount_in"] == 10**16
    assert prepared["value"] == 10**16


def test_prepare_ethereum_sell_checks_balance_and_approval(monkeypatch):
    rpc = ReadRPC()
    monkeypatch.setattr(evmswap, "token_meta", lambda *_a: (6, "USD"))
    monkeypatch.setattr(evmswap, "balance_raw", lambda *_a: 5_000_000)
    monkeypatch.setattr(evmswap, "allowance", lambda *_a: 1)
    monkeypatch.setattr(evmswap, "_v3_quote", _quote_sell)
    prepared = evmswap.prepare("ethereum", TOKEN, "all", WALLET, rpc, mode="sell")
    assert prepared["kind"] == "ethereum_sell"
    assert prepared["amount_in"] == 5_000_000
    assert prepared["needs_approval"] is True
    assert prepared["value"] == 0


def test_prepare_rejects_ink_sell_until_token_input_v4_route_exists(monkeypatch):
    class InkRPC(ReadRPC):
        def call(self, method, params):
            if method == "eth_call":
                data = bytes.fromhex(params[0]["data"][2:])
                if data.startswith(evmswap._selector("getSlot0(bytes32)")):
                    return "0x" + encode(["uint160", "int24", "uint24", "uint24"],
                                         [1, 0, 0, 0]).hex()
                if data.startswith(evmswap._selector(
                        "quoteExactInputSingle(((address,address,uint24,int24,address),bool,uint128,bytes))")):
                    return "0x" + encode(["uint256", "uint256"], [100, 0]).hex()
                raise AssertionError("unexpected eth_call")
            return super().call(method, params)

    rpc = InkRPC(chain_id=57073)
    monkeypatch.setattr(evmswap, "token_meta", lambda *_a: (6, "USD"))
    monkeypatch.setattr(evmswap, "balance_raw", lambda *_a: 5_000_000)
    with pytest.raises(ValueError, match="sell on ink is not supported"):
        evmswap.prepare("ink", TOKEN, "all", WALLET, rpc, mode="sell")


def test_prepare_rejects_wrong_chain_and_unsupported_chain():
    with pytest.raises(ValueError, match="chain id mismatch"):
        evmswap.prepare("ethereum", TOKEN, "0.01", WALLET, ReadRPC(chain_id=57073))
    with pytest.raises(ValueError, match="not supported"):
        evmswap.prepare("robinhood", TOKEN, "0.01", WALLET, ReadRPC())


def test_prepare_rejects_invalid_amount(monkeypatch):
    rpc = ReadRPC()
    monkeypatch.setattr(evmswap, "_v3_quote", _quote_buy)
    with pytest.raises(ValueError, match="positive"):
        evmswap.prepare("ethereum", TOKEN, "0", WALLET, rpc)


def test_execute_prepared_signs_and_broadcasts_once():
    prepared = {"chain_id": 1, "wallet": WALLET, "nonce": 7, "to": ROUTER,
                "data": "0x1234", "value": 1, "gas": 120_000, "gas_price": 1_000_000_000}
    bcast = Broadcast()
    ok, txid = evmswap.execute_prepared(prepared, KEY, bcast)
    assert ok and txid == "0x" + "ab" * 32
    assert len(bcast.raw) == 1
    assert bcast.raw[0].startswith("0x")


def test_execute_prepared_refuses_wrong_wallet_before_broadcast():
    prepared = {"chain_id": 1, "wallet": "0x" + "44" * 20, "nonce": 7,
                "to": ROUTER, "data": "0x1234", "value": 0, "gas": 120_000}
    bcast = Broadcast()
    ok, message = evmswap.execute_prepared(prepared, KEY, bcast)
    assert not ok and message == "wallet/key mismatch"
    assert bcast.raw == []


def test_execute_approval_then_swap_with_approved_receipt():
    prepared = {"chain_id": 1, "wallet": WALLET, "nonce": 7, "to": ROUTER,
                "data": "0x1234", "value": 0, "gas": 120_000, "gas_price": 1_000_000_000,
                "token": TOKEN, "approve_data": "0x5678", "needs_approval": True}
    bcast = Broadcast(receipt={"status": "0x1"})
    ok, txid = evmswap.execute_prepared(prepared, KEY, bcast)
    assert ok and txid == "0x" + "ab" * 32
    assert len(bcast.raw) == 2


def test_execute_does_not_swap_until_approval_receipt_is_confirmed(monkeypatch):
    prepared = {"chain_id": 1, "wallet": WALLET, "nonce": 7, "to": ROUTER,
                "data": "0x1234", "value": 0, "gas": 120_000, "gas_price": 1_000_000_000,
                "token": TOKEN, "approve_data": "0x5678", "needs_approval": True}
    bcast = Broadcast(receipt=None)
    timestamps = iter((0, 46))
    monkeypatch.setattr(evmswap.time, "time", lambda: next(timestamps))
    monkeypatch.setattr(evmswap.time, "sleep", lambda _seconds: None)

    ok, message = evmswap.execute_prepared(prepared, KEY, bcast)

    assert not ok
    assert message.startswith("approval receipt not confirmed; swap was not sent")
    assert len(bcast.raw) == 1


def test_execute_uses_api_eip1559_fees():
    prepared = {"chain_id": 1, "wallet": WALLET, "nonce": 7, "to": ROUTER,
                "data": "0x1234", "value": 0, "gas": 120_000,
                "max_fee_per_gas": 2_000_000_000, "max_priority_fee_per_gas": 1_000_000_000}
    bcast = Broadcast()

    ok, txid = evmswap.execute_prepared(prepared, KEY, bcast)

    assert ok and txid == "0x" + "ab" * 32
    from eth_account import Account
    decoded = Account.recover_transaction(bcast.raw[0])
    assert decoded == WALLET


def test_execute_rejects_bad_prepared_payload():
    ok, message = evmswap.execute_prepared({}, KEY, Broadcast())
    assert not ok and message == "bad prepared swap"
