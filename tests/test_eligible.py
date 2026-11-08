import asyncio
import httpx
from acobot import eligible as E

C = "0x9f757dc80547ea792d0221618dcb8c5d4d4cbecd"
W = "0x1111111111111111111111111111111111111111"


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), headers=E.UA)


def test_bad_address_is_unknown():
    r = E.probe_sync("nope", "robinhood", W, {}, client=_client(lambda q: httpx.Response(200)))
    assert r["eligible"] is None and r["source"] is None


def test_all_endpoints_fail_is_unknown_not_false():
    r = E.probe_sync(C, "robinhood", W, {}, client=_client(lambda q: httpx.Response(404)))
    assert r["eligible"] is None and r["authorization"] == "unknown" and r["source"] is None
    assert "http 404" in r["detail"]


def test_magiceden_bool_true():
    def h(req):
        assert "robinhood" in str(req.url) and W in str(req.url)
        return httpx.Response(200, json={"eligible": True})
    r = E.probe_sync(C, "robinhood", W, {}, client=_client(h))
    assert r["eligible"] is True and r["source"] == "magiceden"


def test_mint_site_signature_counts_as_eligible():
    def h(req):
        if "magiceden" in str(req.url):
            return httpx.Response(404)
        return httpx.Response(200, json={"signature": "0xabc"})
    st = {"mint_site": "https://mint.example"}
    r = E.probe_sync(C, "robinhood", W, st, client=_client(h))
    assert r["eligible"] is None and r["source"] == "mint_site"
    assert r["authorization_payload"] == {"signature": "0xabc"}
    assert r["authorization"] == "available"


def test_eligibility_response_preserves_full_wallet_authorization_payload():
    payload = {
        "eligible": True,
        "wallet": W,
        "contract": C,
        "phase": "signed",
        "authorization": {
            "type": "signed",
            "salt": 9,
            "signature": "0x1234",
            "mintParams": {"mintPrice": 1},
        },
    }

    r = E.probe_sync(
        C, "robinhood", W, {},
        client=_client(lambda _request: httpx.Response(200, json=payload)))

    assert r["eligible"] is True
    assert r["authorization_payload"] == payload


def test_explicit_false_is_false():
    def h(req):
        return httpx.Response(200, json={"data": {"isWhitelisted": False}})
    r = E.probe_sync(C, "robinhood", W, {}, client=_client(h))
    assert r["eligible"] is False and r["source"] == "magiceden"


def test_explicit_ineligible_verdict_does_not_discard_returned_authorization():
    payload = {
        "eligible": False,
        "authorization": {
            "type": "allowlist",
            "proof": ["0x" + "00" * 32],
            "mintParams": {"mintPrice": 0},
        },
    }
    r = E.probe_sync(
        C, "robinhood", W, {},
        client=_client(lambda _request: httpx.Response(200, json=payload)))

    assert r["eligible"] is False
    assert r["authorization_payload"] == payload


def test_merkle_file_membership():
    def h(req):
        if "allowlist.json" in str(req.url):
            return httpx.Response(200, json={"addresses": [W.upper().replace("0X", "0x")]})
        return httpx.Response(404)
    st = {"mint_site": "https://mint.example", "merkle_root": "0xroot"}
    r = E.probe_sync(C, "robinhood", W, st, client=_client(h))
    assert r["eligible"] is True and r["source"] == "merkle_file"


def test_network_error_is_unknown():
    def h(req):
        raise httpx.ConnectError("boom")
    r = E.probe_sync(C, "robinhood", W, {}, client=_client(h))
    assert r["eligible"] is None


def test_probe_fleet_and_panel():
    wallets = [{"label": "Wallet 1", "address": W}, {"label": "Wallet 2", "address": "0x" + "2" * 40}]
    orig = E.probe_sync
    E.probe_sync = lambda c, ch, w, st=None, client=None: {"eligible": w == W, "source": "magiceden", "detail": ""}
    try:
        res = asyncio.run(E.probe_fleet(C, "robinhood", wallets, {}))
    finally:
        E.probe_sync = orig
    txt = E.panel_text("allowlist", res)
    assert "✅ eligible: Wallet 1" in txt
    assert "❌ not eligible: Wallet 2" in txt
    assert "mint authorization: not provided" in txt
    assert "wallet selection is unchanged" in txt
    assert "auto-selected as mint targets" not in txt


def test_manual_source_used_and_saved(tmp_path, monkeypatch):
    path = tmp_path / "eligible_sources.json"
    E.save_source(C, "https://mint.example/allow/{wallet}", path=path)
    monkeypatch.setattr(E, "SOURCES_FILE", path)
    seen = []

    def h(req):
        seen.append(str(req.url))
        return httpx.Response(200, json={"allowed": True})
    r = E.probe_sync(C, "robinhood", W, {}, client=_client(h))
    assert seen and seen[0] == f"https://mint.example/allow/{W}"
    assert r["eligible"] is None or r["source"] == "manual"


def test_manual_source_rejects_bad_template(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        E.save_source(C, "https://mint.example/allow", path=tmp_path / "x.json")
