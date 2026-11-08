from unittest import mock
import json
from acobot import mintapi, opensea, bot as botmod  # noqa: F401  (import ensures bot wiring compiles)

C = "0x9f757dc80547ea792d0221618dcb8c5d4d4cbecd"
W = "0x1111111111111111111111111111111111111111"


def test_mintapi_save_rejects_bad_template(tmp_path):
    p = tmp_path / "m.json"
    try:
        mintapi.save("not-a-contract", "https://x.example/{wallet}", path=p)
        assert False
    except ValueError:
        pass
    for bad in ["http://x/{wallet}", "https://x/no-placeholder"]:
        try:
            mintapi.save(C, bad, path=p)
            assert False
        except ValueError:
            pass
    mintapi.save(C, "https://x.example/mint/{wallet}", path=p)
    assert mintapi.get_template(C, path=p) == "https://x.example/mint/{wallet}"


def test_mintapi_fetch_returns_calldata(tmp_path):
    p = tmp_path / "m.json"
    mintapi.save(C, "https://x.example/mint/{wallet}", path=p)
    resp = mock.Mock(status_code=200)
    resp.json.return_value = {"data": "0xabcdef12", "to": C}
    with mock.patch("acobot.mintapi.httpx.get", return_value=resp) as g:
        d, to, why = mintapi.fetch(C, W, path=p)
    assert (d, to, why) == ("0xabcdef12", C, "ok")
    assert g.call_args[0][0].endswith(W)


def test_mintapi_fetch_no_template_and_bad_json(tmp_path):
    p = tmp_path / "m.json"
    assert mintapi.fetch(C, W, path=p)[0] is None
    mintapi.save(C, "https://x.example/mint/{wallet}", path=p)
    resp = mock.Mock(status_code=200)
    resp.json.return_value = {"data": "not hex"}
    with mock.patch("acobot.mintapi.httpx.get", return_value=resp):
        assert mintapi.fetch(C, W, path=p)[0] is None


def test_phase_api_fetches_complete_transaction_with_phase_and_qty(tmp_path):
    p = tmp_path / "m.json"
    template = "https://x.example/mint/{wallet}?phase={phase}&stage={stage_index}&qty={qty}"
    mintapi.save(C, template, path=p)
    resp = mock.Mock(status_code=200)
    resp.json.return_value = {
        "transaction": {"to": C, "data": "0xabcdef12", "value": "0x2"},
        "chainId": 4663,
    }
    with mock.patch("acobot.mintapi.httpx.get", return_value=resp) as get:
        tx, reason = mintapi.fetch_transaction(
            C, W, phase="GTD round", stage_index=2, qty=2,
            expected_to=C, expected_chain_id=4663, gated=True, path=p)
    assert reason == "ok"
    assert tx == {
        "to": C, "data": "0xabcdef12", "value": 2, "chain_id": 4663,
        "authorization_expires_at": None, "authorization_cacheable": False,
    }
    assert "phase=GTD%20round" in get.call_args.args[0]
    assert "stage=2" in get.call_args.args[0]
    assert "qty=2" in get.call_args.args[0]
    assert mintapi.supports_phase(C, path=p)


def test_gated_api_requires_value_and_rejects_target_mismatch(tmp_path):
    p = tmp_path / "m.json"
    mintapi.save(C, "https://x.example/{wallet}?phase={phase}", path=p)
    resp = mock.Mock(status_code=200)
    resp.json.return_value = {"to": C, "data": "0xabcdef12"}
    with mock.patch("acobot.mintapi.httpx.get", return_value=resp):
        tx, reason = mintapi.fetch_transaction(C, W, phase="allowlist", gated=True, path=p)
    assert tx is None and "omitted" in reason

    resp.json.return_value = {"to": W, "data": "0xabcdef12", "value": 0}
    with mock.patch("acobot.mintapi.httpx.get", return_value=resp):
        tx, reason = mintapi.fetch_transaction(
            C, W, phase="allowlist", gated=True, expected_to=C, path=p)
    assert tx is None and "target does not match" in reason

    resp.json.return_value = {"to": C, "data": "0xabcdef12", "value": 1.5}
    with mock.patch("acobot.mintapi.httpx.get", return_value=resp):
        tx, reason = mintapi.fetch_transaction(
            C, W, phase="allowlist", gated=True, expected_to=C, path=p)
    assert tx is None and "invalid transaction value" in reason


def test_mintapi_rejects_mismatched_wallet_contract_and_phase(tmp_path):
    p = tmp_path / "m.json"
    mintapi.save(C, "https://x.example/mint/{wallet}?phase={phase}", path=p)
    resp = mock.Mock(status_code=200)
    base = {"to": C, "data": "0xabcdef12", "value": 0}
    for context, expected in (
        ({"wallet": "0x" + "22" * 20}, "different wallet"),
        ({"contractAddress": "0x" + "22" * 20}, "different contract"),
        ({"phase": "public"}, "different phase"),
        ({"chainId": 1}, "different chain"),
    ):
        resp.json.return_value = {**base, **context}
        with mock.patch("acobot.mintapi.httpx.get", return_value=resp):
            tx, reason = mintapi.fetch_transaction(
                C, W, phase="allowlist", stage_index=2, gated=True,
                expected_chain_id=4663, path=p)
        assert tx is None and expected in reason


def test_mintapi_response_expiry_and_cacheability_are_explicit(tmp_path):
    p = tmp_path / "m.json"
    mintapi.save(C, "https://x.example/mint/{wallet}?phase={phase}", path=p)
    resp = mock.Mock(status_code=200)
    resp.json.return_value = {
        "to": C, "data": "0xabcdef12", "value": 0,
        "authorization": {"expires_at": "2099-01-01T00:00:00Z", "cacheable": True},
    }
    with mock.patch("acobot.mintapi.httpx.get", return_value=resp):
        tx, reason = mintapi.fetch_transaction(C, W, phase="allowlist", gated=True, path=p)

    assert reason == "ok"
    assert tx["authorization_cacheable"] is True
    assert tx["authorization_expires_at"] > 0


def test_gated_seadrop_api_must_return_allowlist_or_signed_call(tmp_path):
    from eth_utils import keccak
    p = tmp_path / "m.json"
    mintapi.save(C, "https://x.example/{wallet}?phase={phase}", path=p)
    public_selector = keccak(text="mintPublic(address,address,address,uint256)")[:4].hex()
    resp = mock.Mock(status_code=200)
    resp.json.return_value = {"to": C, "data": "0x" + public_selector, "value": 0}
    with mock.patch("acobot.mintapi.httpx.get", return_value=resp):
        tx, reason = mintapi.fetch_transaction(
            C, W, phase="allowlist", gated=True, seadrop=True, expected_to=C, path=p)
    assert tx is None and "refusing public SeaDrop calldata" in reason


def test_seadrop_merkle_authorization_is_encoded_from_api_data(tmp_path):
    from eth_abi import decode

    p = tmp_path / "m.json"
    mintapi.save(C, "https://x.example/{wallet}?phase={phase}", path=p)
    sea_drop = "0x" + "22" * 20
    params = {
        "mintPrice": 123,
        "maxTotalMintableByWallet": 2,
        "startTime": 100,
        "endTime": 200,
        "dropStageIndex": 2,
        "maxTokenSupplyForStage": 500,
        "feeBps": 500,
        "restrictFeeRecipients": True,
    }
    resp = mock.Mock(status_code=200)
    resp.json.return_value = {
        "to": sea_drop,
        "authorization": {"type": "allowlist", "mintParams": params, "proof": [], "quantity": 2},
    }
    with mock.patch("acobot.mintapi.httpx.get", return_value=resp):
        tx, reason = mintapi.fetch_transaction(
            C, W, phase="GTD", qty=2, expected_to=sea_drop, expected_fee_recipient=C,
            gated=True, seadrop=True, path=p)
    assert reason == "ok"
    assert tx["value"] == 246
    decoded = decode(
        ["address", "address", "address", "uint256",
         "(uint256,uint256,uint256,uint256,uint256,uint256,uint256,bool)", "bytes32[]"],
        bytes.fromhex(tx["data"][10:]),
    )
    assert decoded[:4] == (C, C, W, 2)
    assert decoded[4] == (123, 2, 100, 200, 2, 500, 500, True)
    assert decoded[5] == ()


def test_seadrop_signed_authorization_is_encoded_from_api_data(tmp_path):
    from eth_abi import decode
    from eth_utils import keccak

    p = tmp_path / "m.json"
    mintapi.save(C, "https://x.example/{wallet}?phase={phase}", path=p)
    sea_drop = "0x" + "22" * 20
    params = {
        "mintPrice": 5,
        "maxTotalMintableByWallet": 1,
        "startTime": 100,
        "endTime": 200,
        "dropStageIndex": 3,
        "maxTokenSupplyForStage": 100,
        "feeBps": 0,
        "restrictFeeRecipients": False,
    }
    resp = mock.Mock(status_code=200)
    resp.json.return_value = {
        "to": sea_drop,
        "authorization": {
            "type": "signed", "mintParams": params, "salt": 9,
            "signature": "0x1234", "quantity": 1,
        },
    }
    with mock.patch("acobot.mintapi.httpx.get", return_value=resp):
        tx, reason = mintapi.fetch_transaction(
            C, W, phase="GTD", qty=1, expected_to=sea_drop,
            expected_fee_recipient=C, gated=True, seadrop=True, path=p)
    expected_selector = keccak(text=(
        "mintSigned(address,address,address,uint256,"
        "(uint256,uint256,uint256,uint256,uint256,uint256,uint256,bool),uint256,bytes)"
    ))[:4].hex()
    assert reason == "ok" and tx["data"].startswith("0x" + expected_selector)
    assert tx["value"] == 5
    decoded = decode(
        ["address", "address", "address", "uint256",
         "(uint256,uint256,uint256,uint256,uint256,uint256,uint256,bool)", "uint256", "bytes"],
        bytes.fromhex(tx["data"][10:]),
    )
    assert decoded[5:] == (9, b"\x12\x34")


def test_discovered_eligibility_response_builds_wallet_and_phase_bound_seadrop_call():
    from eth_abi import decode

    sea_drop = "0x" + "22" * 20
    params = {
        "mintPrice": 123,
        "maxTotalMintableByWallet": 2,
        "startTime": 100,
        "endTime": 200,
        "dropStageIndex": 2,
        "maxTokenSupplyForStage": 500,
        "feeBps": 0,
        "restrictFeeRecipients": False,
    }
    response = {
        "eligible": True,
        "wallet": W,
        "contract": C,
        "phase": "allowlist",
        "authorization": {"type": "allowlist", "proof": [], "mintParams": params},
    }

    tx, reason = mintapi.transaction_from_response(
        response, C, W, phase="GTD", phase_key="allowlist", stage_index=2,
        qty=2, expected_to=sea_drop, expected_chain_id=4663, gated=True,
        seadrop=True, expected_fee_recipient=C, expected_price_wei=123,
    )

    assert reason == "ok"
    assert tx["value"] == 246
    decoded = decode(
        ["address", "address", "address", "uint256",
         "(uint256,uint256,uint256,uint256,uint256,uint256,uint256,bool)", "bytes32[]"],
        bytes.fromhex(tx["data"][10:]),
    )
    assert decoded[:4] == (C, C, W, 2)


def test_discovered_seadrop_authorization_rejects_wrong_phase_and_stage():
    params = {
        "mintPrice": 1,
        "maxTotalMintableByWallet": 2,
        "startTime": 100,
        "endTime": 200,
        "dropStageIndex": 5,
        "maxTokenSupplyForStage": 500,
        "feeBps": 0,
        "restrictFeeRecipients": False,
    }
    response = {"authorization": {"type": "signed", "salt": 1,
                                  "signature": "0x12", "mintParams": params}}

    tx, reason = mintapi.transaction_from_response(
        response, C, W, phase="allowlist", phase_key="allowlist",
        stage_index=2, qty=1, expected_to=C, gated=True, seadrop=True)

    assert tx is None
    assert "allowlist phase received signed authorization" in reason


def test_opensea_mode_keyless_without_key(monkeypatch):
    monkeypatch.delenv("OPENSEA_API_KEY", raising=False)
    monkeypatch.setattr(opensea, "api_key", lambda: "")
    assert opensea.mode() == "keyless mode, limited"
    assert opensea.fetch_api("slug") is None


def test_opensea_agent_key_is_cached_without_being_logged(monkeypatch, caplog):
    monkeypatch.setattr(opensea, "api_key", lambda: "")
    opensea._AGENT_KEY.update(value="", expires=0.0, cooldown_until=0.0)
    calls = []

    class Response:
        status_code = 200
        headers = {}

        def json(self):
            return {"api_key": "agent-secret-value", "expires_at": "2026-11-15T00:00:00Z"}

    def post(url, headers=None, timeout=None):
        calls.append(url)
        return Response()

    try:
        key = opensea.ensure_api_key(post=post, now=lambda: 1_000)
        again = opensea.ensure_api_key(post=post, now=lambda: 1_100)
    finally:
        opensea._AGENT_KEY.update(value="", expires=0.0, cooldown_until=0.0)

    assert key == again == "agent-secret-value"
    assert calls == ["https://api.opensea.io/api/v2/auth/keys"]
    assert "agent-secret-value" not in caplog.text


def test_opensea_agent_key_backs_off_when_rate_limited(monkeypatch):
    monkeypatch.setattr(opensea, "api_key", lambda: "")
    opensea._AGENT_KEY.update(value="", expires=0.0, cooldown_until=0.0)
    calls = []

    class Response:
        status_code = 429
        headers = {"Retry-After": "30"}

        def json(self):
            return {}

    def post(url, headers=None, timeout=None):
        calls.append(url)
        return Response()

    try:
        assert opensea.ensure_api_key(post=post, now=lambda: 5_000) == ""
        assert opensea.ensure_api_key(post=post, now=lambda: 5_010) == ""
    finally:
        opensea._AGENT_KEY.update(value="", expires=0.0, cooldown_until=0.0)

    assert len(calls) == 1


def test_site_mint_note_detects_site_not_contract():
    assert opensea.site_mint_note("https://opensea.io/collection/x") is None
    assert "site mint" in opensea.site_mint_note("https://digitalhuman.example/mint")
