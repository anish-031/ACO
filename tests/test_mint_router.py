import pytest
from unittest import mock
from eth_utils import keccak

from acobot import mint_router, sniper


NFT = "0x" + "11" * 20
SEA_DROP = "0x" + "22" * 20
FEE_RECIPIENT = "0x" + "33" * 20
WALLET = "0x" + "44" * 20


def test_gated_seadrop_never_falls_back_to_public_mint():
    mint = {
        "seadrop": True,
        "gated": True,
        "phase": "signed",
        "contract": NFT,
        "mint_to": SEA_DROP,
        "fee_recipient": FEE_RECIPIENT,
    }
    with pytest.raises(mint_router.MintRouteError, match="wallet-specific authorization"):
        mint_router.build_transaction(mint, WALLET, 1)


def test_public_seadrop_route_remains_unchanged():
    mint = {
        "seadrop": True,
        "phase": "public",
        "contract": NFT,
        "mint_to": SEA_DROP,
        "fee_recipient": FEE_RECIPIENT,
    }
    to, data, value = mint_router.build_transaction(mint, WALLET, 2)
    assert to == SEA_DROP
    assert data == sniper.encode_seadrop_mint(NFT, FEE_RECIPIENT, WALLET, 2)
    assert value is None


def test_authorized_mints_wait_until_phase_open_to_fetch_proof():
    assert sniper.presign_lead({"authorization_at_fire": True}) == 0
    assert sniper.presign_lead({"authorization_at_fire": False}) == sniper.PRESIGN_LEAD_S


def test_gated_mint_waits_for_chain_timestamp_not_wall_clock():
    class FakeRPC:
        timestamp = 99

        def call(self, method, params):
            assert method == "eth_getBlockByNumber"
            return {"timestamp": hex(self.timestamp)}

    rpc = FakeRPC()
    mint = {"authorization_at_fire": True, "start_ts": 100}
    assert not sniper._ready(mint, rpc, now=100)
    rpc.timestamp = 100
    assert sniper._ready(mint, rpc, now=100.3)


def test_gated_api_route_uses_wallet_specific_authorization():
    selector = keccak(text=(
        "mintAllowList(address,address,address,uint256,"
        "(uint256,uint256,uint256,uint256,uint256,uint256,uint256,bool),bytes32[])"
    ))[:4].hex()
    mint = {
        "seadrop": True,
        "gated": True,
        "phase": "allowlist",
        "contract": NFT,
        "_api_calldata": {
            WALLET.lower(): {"to": SEA_DROP, "data": "0x" + selector, "value": 0},
        },
    }
    assert mint_router.build_transaction(mint, WALLET, 1) == (
        SEA_DROP, "0x" + selector, 0)


def test_gated_api_route_does_not_accept_public_selector():
    public_selector = keccak(text="mintPublic(address,address,address,uint256)")[:4].hex()
    mint = {
        "seadrop": True,
        "gated": True,
        "phase": "allowlist",
        "contract": NFT,
        "mint_to": SEA_DROP,
        "_api_calldata": {
            WALLET.lower(): {"to": SEA_DROP, "data": "0x" + public_selector, "value": 0},
        },
    }
    with pytest.raises(mint_router.MintRouteError, match="public SeaDrop calldata"):
        mint_router.build_transaction(mint, WALLET, 1)


@pytest.mark.parametrize(
    ("phase", "signature"),
    [
        ("allowlist", "mintAllowList(address,address,address,uint256,(uint256,uint256,uint256,uint256,uint256,uint256,uint256,bool),bytes32[])"),
        ("signed", "mintSigned(address,address,address,uint256,(uint256,uint256,uint256,uint256,uint256,uint256,uint256,bool),uint256,bytes)"),
    ],
)
def test_gated_route_requires_the_method_for_the_selected_phase(phase, signature):
    selector = keccak(text=signature)[:4].hex()
    mint = {
        "seadrop": True,
        "gated": True,
        "phase": phase,
        "contract": NFT,
        "_api_calldata": {
            WALLET.lower(): {"to": SEA_DROP, "data": "0x" + selector, "value": 0},
        },
    }

    assert mint_router.build_transaction(mint, WALLET, 1) == (
        SEA_DROP, "0x" + selector, 0)


def test_gated_route_does_not_accept_the_other_seadrop_gated_method():
    signed_selector = keccak(text=(
        "mintSigned(address,address,address,uint256,"
        "(uint256,uint256,uint256,uint256,uint256,uint256,uint256,bool),uint256,bytes)"
    ))[:4].hex()
    mint = {
        "seadrop": True,
        "gated": True,
        "phase": "allowlist",
        "contract": NFT,
        "_api_calldata": {
            WALLET.lower(): {"to": SEA_DROP, "data": "0x" + signed_selector, "value": 0},
        },
    }

    with pytest.raises(mint_router.MintRouteError, match="mintAllowList"):
        mint_router.build_transaction(mint, WALLET, 1)


def test_gated_route_requires_explicit_payment_value():
    selector = keccak(text=(
        "mintAllowList(address,address,address,uint256,"
        "(uint256,uint256,uint256,uint256,uint256,uint256,uint256,bool),bytes32[])"
    ))[:4].hex()
    mint = {
        "seadrop": True,
        "gated": True,
        "phase": "allowlist",
        "contract": NFT,
        "_api_calldata": {
            WALLET.lower(): {"to": SEA_DROP, "data": "0x" + selector},
        },
    }

    with pytest.raises(mint_router.MintRouteError, match="omitted its payment value"):
        mint_router.build_transaction(mint, WALLET, 1)


@pytest.mark.parametrize("mode", ["prefire", "dual"])
def test_authorized_gated_mint_keeps_selected_firing_mode(mode):
    assert sniper.fire_mode({
        "authorization_at_fire": True,
        "gated": True,
        "fire_mode": mode,
    }) == mode


def test_gated_watcher_retries_authorization_and_only_fires_after_valid_data(monkeypatch):
    class FakeLoop:
        ticks = 0

        def __init__(self, txs, *_args):
            self.txs = txs

        def start(self):
            return self

        def snapshot(self):
            return self.txs

        def stop(self):
            return None

    mint = {"authorization_at_fire": True, "start_ts": 100}
    calls = []

    def entries():
        calls.append(True)
        if len(calls) == 1:
            mint["_authorization_pending"] = True
            mint["_zero_reason"] = "missing wallet-specific proof"
            return []
        mint["_authorization_pending"] = False
        return [{"label": "wallet", "address": WALLET, "raw": "0xsigned"}]

    times = iter((100, 105))
    monkeypatch.setattr(sniper, "_ready", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(sniper.time, "time", lambda: next(times))
    monkeypatch.setattr(sniper.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(sniper, "FeeLoop", FakeLoop)
    monkeypatch.setattr(sniper, "refresh", lambda txs, *_args: txs)
    broadcast = mock.Mock(return_value={"results": [{"address": WALLET}], "skipped": []})
    monkeypatch.setattr(sniper, "fire", broadcast)
    events = []
    mint["_entries_fn"] = entries

    result = sniper.wait_and_fire(
        mint, object(), object(), {}, lambda: False,
        status_cb=lambda kind, payload: events.append((kind, payload)),
    )

    assert len(calls) == 2
    assert [kind for kind, _payload in events].count("waiting_authorization") == 1
    broadcast.assert_called_once()
    assert result["results"][0]["address"] == WALLET


def test_watcher_stops_with_error_when_no_signed_transaction_is_ready(monkeypatch):
    class FakeLoop:
        ticks = 0

        def __init__(self, txs, *_args):
            self.txs = txs

        def start(self):
            return self

        def snapshot(self):
            return self.txs

        def stop(self):
            return None

    mint = {
        "authorization_at_fire": False,
        "start_ts": 100,
        "_entries_fn": lambda: [{"label": "wallet", "error": "signing failed", "raw": None}],
    }
    monkeypatch.setattr(sniper, "_ready", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(sniper.time, "time", lambda: 100)
    monkeypatch.setattr(sniper, "FeeLoop", FakeLoop)
    monkeypatch.setattr(sniper, "refresh", lambda txs, *_args: txs)
    broadcast = mock.Mock()
    monkeypatch.setattr(sniper, "fire", broadcast)
    events = []

    result = sniper.wait_and_fire(
        mint, object(), object(), {}, lambda: False,
        status_cb=lambda kind, payload: events.append((kind, payload)),
    )

    assert result is None
    broadcast.assert_not_called()
    no_tx_event = next(payload for kind, payload in events if kind == "no_transactions")
    assert "signing failed" in no_tx_event["reason"]
