import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from acobot import bot, tokenbuy


class FakeRPC:
    def __init__(self):
        self.calls = []

    def call(self, method, params):
        self.calls.append((method, params))
        if method == "eth_call":
            from eth_abi import encode
            return "0x" + encode(["uint256", "uint160", "uint32", "uint256"],
                                 [2_000_000_000_000_000_000, 0, 0, 0]).hex()
        return {"eth_estimateGas": "0x186a0", "eth_getTransactionCount": "0x3",
                "eth_gasPrice": "0x3b9aca00", "eth_chainId": hex(tokenbuy.CHAIN_ID)}[method]


class FakeBroadcast:
    def __init__(self): self.raw = None
    def broadcast(self, raw):
        self.raw = raw
        return {"result": "0x" + "ab" * 32}


class Q:
    def __init__(self, data):
        self.data = data
        self.from_user = type("U", (), {"id": bot.ADMIN_ID, "username": None})()
        self.message = type("M", (), {"chat_id": bot.ADMIN_ID, "message_id": 10})()
        self.answer = AsyncMock()
        self.edit_message_text = AsyncMock()


@pytest.mark.asyncio
async def test_tokenbuy_two_tap_approval_and_broadcast_mocked(monkeypatch):
    key = "e2e-test"
    signer = "0x" + "11" * 32
    account = tokenbuy.Account.from_key(signer)
    fake_rpc = FakeRPC()
    fake_bcast = FakeBroadcast()
    prepared = tokenbuy.prepare_buy("0x" + "22" * 20, "0.01", account.address, fake_rpc)
    assert prepared["min_out"] == prepared["amount_out"] * 98 // 100
    assert not hasattr(fake_bcast, "raw") or fake_bcast.raw is None
    bot.TOKEN_PENDING[key] = {"prepared": prepared, "wallet": account.address,
        "chain": type("C", (), {"broadcast_rpc": []})(), "expires": time.time()+60,
        "confirm_text": "confirm panel"}
    monkeypatch.setattr(bot.wallets, "signing_entries", lambda *_a, **_k: [(None, None, None, signer)])
    monkeypatch.setattr(bot, "RPC", lambda *_a, **_k: fake_bcast)
    actual_execute = tokenbuy.execute_prepared
    monkeypatch.setattr(tokenbuy, "execute_prepared", lambda p, pk, _rpc: actual_execute(p, pk, fake_bcast))
    for action in ("c", "y"):
        q=Q(f"stb:{action}:{key}")
        update=type("U", (), {"callback_query":q, "effective_chat": type("Chat", (), {"type": bot.ChatType.PRIVATE})()})()
        await bot.on_tokenbuy_cb(update, type("C", (), {})())
    assert fake_bcast.raw and fake_bcast.raw.startswith("0x")
    assert key not in bot.TOKEN_PENDING
    assert fake_rpc.calls and any(m == "eth_estimateGas" for m,_ in fake_rpc.calls)


def test_only_one_stb_handler_registration_and_owned_pattern():
    from pathlib import Path
    source=Path(bot.__file__).read_text()
    assert source.count('CallbackQueryHandler(on_tokenbuy_cb, pattern=r"^stb:")') == 1
    assert source.count('elif data.startswith("stb:")') == 0
