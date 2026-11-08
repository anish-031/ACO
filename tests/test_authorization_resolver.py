import json
from types import SimpleNamespace
from unittest import mock

from acobot import authorization_resolver as resolver, config, mintapi


NFT = "0x" + "12" * 20
WALLET = "0x" + "34" * 20


def test_requirement_classifies_merkle_signature_and_unknown_phases():
    merkle = resolver.authorization_requirement({
        "phase": "allowlist", "_selected_stage": {"gated": True, "key": "allowlist"},
    })
    signed = resolver.authorization_requirement({
        "phase": "signed", "_selected_stage": {"gated": True, "key": "signed"},
    })
    unknown = resolver.authorization_requirement({
        "phase": "custom", "gated": True,
    })

    assert merkle["type"] == "merkle_proof"
    assert signed["type"] == "project_signature"
    assert unknown["type"] == "project_specific"


def test_project_report_keeps_eligibility_separate_from_authorization(tmp_path):
    with mock.patch.object(mintapi, "STORE", tmp_path / "mint_apis.json"):
        report = resolver.project_report({
            "chain": "robinhood", "contract": NFT, "phase": "allowlist", "gated": True,
            "start_ts": 100, "schedule": [{"label": "GTD", "gated": True}],
        })

    assert report["network"] == "robinhood"
    assert report["authorization_type"] == "merkle_proof"
    assert report["eligibility"].startswith("not checked")
    assert report["authorization_available"] == "automatic endpoint discovery pending"
    assert "wallet-specific Merkle proof" in report["missing"]
    assert report["schedule_possible"] is True
    assert report["transaction_construction_possible"] is False


def test_gated_report_does_not_claim_public_calldata_can_construct_the_transaction(tmp_path):
    with mock.patch.object(mintapi, "STORE", tmp_path / "mint_apis.json"):
        report = resolver.project_report({
            "chain": "ethereum", "contract": NFT, "phase": "allowlist", "gated": True,
            "mint_sig": "mintPublic(uint256)", "seadrop": True, "price_wei": 0,
            "start_ts": 100,
        })

    assert report["authorization_type"] == "merkle_proof"
    assert report["transaction_construction_possible"] is False


def test_site_inspection_reports_candidates_but_never_calls_them(monkeypatch):
    monkeypatch.setattr(resolver, "_public_https_url", lambda _url: True)
    calls = []

    class Response:
        status_code = 200
        content = b'<script src="/static/mint.js"></script>'

        def __init__(self, content):
            self.content = content

    def get(url, **_kwargs):
        calls.append(url)
        if url.endswith("mint.js"):
            return Response(b'fetch("/api/allowlist?wallet=" + wallet);')
        return Response(b'<script src="/static/mint.js"></script>')

    with mock.patch.object(resolver, "_SITE_CACHE", {}):
        report = resolver.inspect_project_site("https://project.example/mint", get=get)

    assert report["site"] == "https://project.example/mint"
    assert report["api_candidates"] == ["/api/allowlist?wallet="]
    assert calls == [
        "https://project.example/mint",
        "https://project.example/static/mint.js",
    ]


def test_site_inspection_rejects_non_https_and_local_hosts(monkeypatch):
    monkeypatch.setattr(resolver.socket, "getaddrinfo", lambda *_args, **_kwargs: [
        (None, None, None, None, ("127.0.0.1", 443)),
    ])
    assert resolver.inspect_project_site("http://project.example")["site"] is None
    assert resolver.inspect_project_site("https://localhost")["site"] is None


def test_resolver_tries_supported_eligibility_endpoints_without_manual_api(tmp_path):
    chain = config.Chain("ethereum", 1, ["rpc"], ["rpc"])
    mint = {"contract": NFT, "phase": "allowlist", "gated": True}
    probe = mock.Mock(return_value={
        "eligible": None, "authorization": "unknown", "detail": "magiceden:http 404",
    })
    with mock.patch.object(mintapi, "STORE", tmp_path / "mint_apis.json"), \
            mock.patch("acobot.eligible.probe_sync", probe), \
            mock.patch("acobot.dropmint.fetch_authorization", return_value=(None, "no indexed transaction")), \
            mock.patch.object(mintapi, "fetch_transaction") as fetch:
        tx, reason = resolver.resolve_evm_authorization(
            mint, WALLET, 1, chain, SimpleNamespace())

    assert tx is None
    assert "wallet-specific Merkle proof" in reason
    assert "supported authorization discovery" in reason
    probe.assert_called_once()
    fetch.assert_not_called()


def test_resolver_fetches_exact_wallet_scope_and_simulates_before_return(tmp_path):
    chain = config.Chain("robinhood", 4663, ["rpc"], ["rpc"])
    mint = {
        "contract": NFT, "phase": "allowlist", "gated": True,
        "phase_label": "GTD", "stage_index": 2, "seadrop": True,
        "mint_to": "0x" + "56" * 20,
    }
    rpc = mock.Mock()
    transaction = {
        "to": mint["mint_to"], "data": "0x12345678", "value": 20,
        "authorization_expires_at": None, "authorization_cacheable": False,
    }
    with mock.patch.object(mintapi, "STORE", tmp_path / "mint_apis.json"), \
            mock.patch.object(mintapi, "supports_phase", return_value=True), \
            mock.patch.object(mintapi, "fetch_transaction", return_value=(transaction, "ok")) as fetch, \
            mock.patch.object(resolver.mint_router, "validate_api_selector"), \
            mock.patch.object(resolver, "_AUTH_CACHE", {}):
        result, reason = resolver.resolve_evm_authorization(mint, WALLET, 2, chain, rpc)

    assert reason == "ok"
    assert result == transaction
    fetch.assert_called_once_with(
        NFT, WALLET, phase="GTD", phase_key="allowlist", stage_index=2, qty=2,
        expected_to=mint["mint_to"], expected_chain_id=4663, gated=True,
        seadrop=True, expected_fee_recipient=None, expected_price_wei=None,
    )
    rpc.call.assert_called_once()
    assert rpc.call.call_args.args[1][0]["from"] == WALLET


def test_resolver_uses_returned_endpoint_authorization_without_manual_configuration(tmp_path):
    chain = config.Chain("robinhood", 4663, ["rpc"], ["rpc"])
    mint = {
        "contract": NFT, "phase": "allowlist", "phase_label": "GTD",
        "stage_index": 2, "gated": True, "seadrop": True,
        "mint_to": "0x" + "56" * 20,
    }
    payload = {
        "eligible": False,
        "wallet": WALLET,
        "contract": NFT,
        "phase": "allowlist",
        "authorization": {
            "type": "allowlist",
            "proof": [],
            "mintParams": {
                "mintPrice": 0, "maxTotalMintableByWallet": 2, "startTime": 100,
                "endTime": 200, "dropStageIndex": 2, "maxTokenSupplyForStage": 50,
                "feeBps": 0, "restrictFeeRecipients": False,
            },
            "quantity": 1,
        },
    }
    transaction = {
        "to": mint["mint_to"], "data": "0x12345678", "value": 0,
        "authorization_expires_at": None, "authorization_cacheable": False,
    }
    rpc = mock.Mock()
    with mock.patch.object(mintapi, "STORE", tmp_path / "mint_apis.json"), \
            mock.patch("acobot.eligible.probe_sync", return_value={
                "eligible": False, "authorization_payload": payload,
                "source": "magiceden", "detail": "eligible=False",
            }), \
            mock.patch.object(mintapi, "transaction_from_response",
                              return_value=(transaction, "ok")) as parse, \
            mock.patch.object(resolver.mint_router, "validate_api_selector"), \
            mock.patch.object(resolver, "_AUTH_CACHE", {}):
        result, reason = resolver.resolve_evm_authorization(
            mint, WALLET, 1, chain, rpc)

    assert (result, reason) == (transaction, "ok")
    parse.assert_called_once_with(
        payload, NFT, WALLET, phase="GTD", phase_key="allowlist",
        stage_index=2, qty=1, expected_to=mint["mint_to"],
        expected_chain_id=4663, gated=True, seadrop=True,
        expected_fee_recipient=None, expected_price_wei=None,
    )
    rpc.call.assert_called_once()


def test_resolver_never_caches_authorization_without_explicit_reuse_and_expiry(tmp_path):
    chain = config.Chain("ethereum", 1, ["rpc"], ["rpc"])
    mint = {"contract": NFT, "phase": "signed", "gated": True}
    transaction = {
        "to": NFT, "data": "0x12345678", "value": 0,
        "authorization_expires_at": None, "authorization_cacheable": True,
    }
    rpc = mock.Mock()
    with mock.patch.object(mintapi, "STORE", tmp_path / "mint_apis.json"), \
            mock.patch.object(mintapi, "supports_phase", return_value=True), \
            mock.patch.object(mintapi, "fetch_transaction", return_value=(transaction, "ok")) as fetch, \
            mock.patch.object(resolver.mint_router, "validate_api_selector"), \
            mock.patch.object(resolver, "_AUTH_CACHE", {}):
        resolver.resolve_evm_authorization(mint, WALLET, 1, chain, rpc)
        resolver.resolve_evm_authorization(mint, WALLET, 1, chain, rpc)

    assert fetch.call_count == 2


def test_resolver_uses_indexed_allowlist_transaction_before_eligibility(tmp_path):
    chain = config.Chain("ethereum", 1, ["rpc"], ["rpc"])
    sea_drop = "0x00005ea00ac477b1030ce78506496e8c2de24bf5"
    fee = "0x" + "11" * 20
    params = {
        "mintPrice": 0, "maxTotalMintableByWallet": 2, "startTime": 100,
        "endTime": 200, "dropStageIndex": 2, "maxTokenSupplyForStage": 50,
        "feeBps": 0, "restrictFeeRecipients": False,
    }
    data, value = mintapi._encode_seadrop_authorization(
        {"type": "allowlist", "mintParams": params, "proof": [], "quantity": 1,
         "feeRecipient": fee},
        NFT, WALLET, fee, 1, expected_phase="allowlist", expected_stage_index=2,
    )
    indexed = {
        "transaction": {"to": sea_drop, "data": data, "value": value},
        "wallet": WALLET, "contract": NFT, "phase": "allowlist", "source": "reservoir",
    }
    mint = {
        "contract": NFT, "phase": "allowlist", "phase_label": "GTD", "list_mode": "gtd",
        "stage_index": 2, "gated": True, "seadrop": True, "mint_to": sea_drop,
        "chain": "ethereum",
    }
    rpc = mock.Mock()
    probe = mock.Mock()
    with mock.patch.object(mintapi, "STORE", tmp_path / "mint_apis.json"), \
            mock.patch("acobot.dropmint.fetch_authorization", return_value=(indexed, "reservoir")), \
            mock.patch("acobot.eligible.probe_sync", probe), \
            mock.patch.object(resolver, "_AUTH_CACHE", {}):
        tx, reason = resolver.resolve_evm_authorization(mint, WALLET, 1, chain, rpc)

    assert reason == "ok"
    assert tx["to"] == sea_drop
    assert tx["value"] == 0
    assert tx["data"].startswith("0x")
    probe.assert_not_called()
    rpc.call.assert_called_once()


def test_resolver_rejects_public_calldata_for_gtd_and_keeps_searching(tmp_path):
    chain = config.Chain("ethereum", 1, ["rpc"], ["rpc"])
    from eth_abi import encode
    from eth_utils import keccak

    selector = keccak(text="mintPublic(address,address,address,uint256)")[:4]
    data = "0x" + (selector + encode(
        ["address", "address", "address", "uint256"],
        ["0x" + "11" * 20, NFT, WALLET, 1],
    )).hex()
    indexed = {
        "transaction": {"to": "0x" + "56" * 20, "data": data, "value": 0},
        "wallet": WALLET, "contract": NFT, "phase": "allowlist",
    }
    mint = {
        "contract": NFT, "phase": "allowlist", "gated": True, "seadrop": True,
        "mint_to": "0x" + "56" * 20, "chain": "ethereum",
    }
    probe = mock.Mock(return_value={"eligible": None, "detail": "no payload"})
    with mock.patch.object(mintapi, "STORE", tmp_path / "mint_apis.json"), \
            mock.patch("acobot.dropmint.fetch_authorization", return_value=(indexed, "reservoir")), \
            mock.patch("acobot.eligible.probe_sync", probe), \
            mock.patch.object(resolver, "_AUTH_CACHE", {}):
        tx, reason = resolver.resolve_evm_authorization(mint, WALLET, 1, chain, mock.Mock())

    assert tx is None
    assert "refusing public SeaDrop calldata" in reason
    probe.assert_called_once()
