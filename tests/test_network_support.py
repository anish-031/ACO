import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest
from solders.pubkey import Pubkey

from acobot import bot, config, rpc, solana, wallets
from acobot.wsfeed import HeadFeed


def test_example_config_exposes_only_supported_evm_mainnets(monkeypatch):
    for key in (
        "ALCHEMY_API_KEY", "ETHEREUM_RPC_URL", "ETHEREUM_RPC_BACKUP_URLS",
        "ROBINHOOD_RPC_URL", "ROBINHOOD_RPC_BACKUP_URLS", "INK_RPC_URL",
        "INK_RPC_BACKUP_URLS", "SOLANA_RPC_URL", "SOLANA_RPC_BACKUP_URLS",
        "HELIUS_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    example = Path(__file__).resolve().parents[1] / "config.example.toml"
    loaded = config.load(str(example))

    assert {name: chain.chain_id for name, chain in loaded.chains.items()} == {
        "ethereum": 1,
        "robinhood": 4663,
        "ink": 57073,
    }
    assert loaded.solana_rpc_url == solana.RPC_URL
    assert loaded.chains["ethereum"].rpc[-1] == "https://ethereum-rpc.publicnode.com"
    assert loaded.chains["robinhood"].rpc[-1] == "https://rpc.mainnet.chain.robinhood.com"
    assert loaded.chains["ink"].rpc[-1] == "https://rpc-gel.inkonchain.com"


def test_alchemy_provider_is_preferred_when_key_is_configured(monkeypatch):
    monkeypatch.setenv("ALCHEMY_API_KEY", "test-key")
    example = Path(__file__).resolve().parents[1] / "config.example.toml"
    loaded = config.load(str(example))

    assert loaded.chains["ethereum"].rpc[0] == "https://eth-mainnet.g.alchemy.com/v2/test-key"
    assert loaded.chains["robinhood"].rpc[0] == "https://robinhood-mainnet.g.alchemy.com/v2/test-key"
    assert loaded.solana_rpc_url == "https://solana-mainnet.g.alchemy.com/v2/test-key"


def test_alchemy_is_added_first_for_legacy_rpc_lists(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(textwrap.dedent("""
        [chains.ethereum]
        chain_id = 1
        rpc = ["https://ethereum-rpc.publicnode.com"]

        [chains.robinhood]
        chain_id = 4663
        rpc = ["https://rpc.mainnet.chain.robinhood.com"]
    """))
    monkeypatch.setenv("ALCHEMY_API_KEY", "test-key")

    loaded = config.load(str(path))

    assert loaded.chains["ethereum"].rpc[0].endswith("/v2/test-key")
    assert loaded.chains["robinhood"].rpc[0].endswith("/v2/test-key")
    assert loaded.chains["ethereum"].broadcast_rpc[0] == loaded.chains["ethereum"].rpc[0]
    assert loaded.chains["robinhood"].ws_rpc[0].startswith("wss://")


def test_robinhood_testnet_config_is_ignored(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(textwrap.dedent("""
        [chains.robinhood]
        chain_id = 4663
        rpc = ["https://rpc.mainnet.chain.robinhood.com"]

        [chains.robinhood_testnet]
        chain_id = 46630
        rpc = ["https://rpc.testnet.chain.robinhood.com"]
    """))

    loaded = config.load(str(path))

    assert list(loaded.chains) == ["robinhood"]


def test_config_rejects_chain_id_mismatch(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(textwrap.dedent("""
        [chains.robinhood]
        chain_id = 1
        rpc = ["https://rpc.mainnet.chain.robinhood.com"]
    """))

    with pytest.raises(ValueError, match="must use chain ID 4663"):
        config.load(str(path))


def test_network_rpc_environment_overrides_and_backups(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(textwrap.dedent("""
        [chains.robinhood]
        chain_id = 4663

        [solana]
        rpc_url = "https://config-solana.example"
        backup_rpc_urls = ["https://config-backup.example"]
    """))
    monkeypatch.delenv("ALCHEMY_API_KEY", raising=False)
    monkeypatch.delenv("HELIUS_API_KEY", raising=False)
    monkeypatch.setenv("ROBINHOOD_RPC_URL", "https://rh-primary.example")
    monkeypatch.setenv("ROBINHOOD_RPC_BACKUP_URLS", "https://rh-backup-1.example, https://rh-backup-2.example")
    monkeypatch.setenv("SOLANA_RPC_URL", "https://solana-primary.example")
    monkeypatch.setenv("SOLANA_RPC_BACKUP_URLS", "https://solana-backup.example")

    loaded = config.load(str(path))

    assert loaded.chains["robinhood"].rpc == [
        "https://rh-primary.example", "https://rh-backup-1.example",
        "https://rh-backup-2.example",
    ]
    assert loaded.chains["robinhood"].broadcast_rpc == loaded.chains["robinhood"].rpc
    assert loaded.solana_rpc_url == "https://solana-primary.example"
    assert loaded.solana_rpc_urls == [
        "https://solana-primary.example", "https://config-solana.example",
        "https://config-backup.example",
        "https://solana-backup.example",
    ]


def test_helius_is_optional_solana_backup_and_explicit_rpc_stays_primary(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(textwrap.dedent("""
        [chains.ethereum]
        chain_id = 1
        rpc = ["https://ethereum-rpc.publicnode.com"]

        [solana]
        rpc_url = "https://dedicated-solana.example"
    """))
    monkeypatch.delenv("ALCHEMY_API_KEY", raising=False)
    monkeypatch.delenv("SOLANA_RPC_URL", raising=False)
    monkeypatch.setenv("HELIUS_API_KEY", "test-helius-key")

    loaded = config.load(str(path))

    assert loaded.solana_rpc_url == "https://mainnet.helius-rpc.com/?api-key=test-helius-key"
    assert loaded.solana_rpc_urls == [
        "https://mainnet.helius-rpc.com/?api-key=test-helius-key",
        "https://dedicated-solana.example",
    ]


def test_chain_bound_rpc_filters_wrong_network_before_using_it(monkeypatch):
    chain = config.Chain(
        "robinhood", 4663, ["wrong", "right"], ["wrong", "right"])
    called = []

    class FakeProvider:
        def __init__(self, endpoint):
            self.endpoint = endpoint

        def make_request(self, method, _params):
            if method == "eth_chainId":
                return {"result": hex(1 if self.endpoint == "wrong" else 4663)}
            called.append((self.endpoint, method))
            return {"result": "0x123"}

    monkeypatch.setattr(rpc, "_provider", lambda endpoint, _timeout: FakeProvider(endpoint))
    selected = rpc.RPC(chain)

    assert selected.call("eth_blockNumber", []) == "0x123"
    assert selected.urls == ["right"]
    assert called == [("right", "eth_blockNumber")]


def test_chain_bound_rpc_fails_closed_when_no_endpoint_matches(monkeypatch):
    chain = config.Chain("ethereum", 1, ["wrong"], ["wrong"])

    class FakeProvider:
        def make_request(self, method, _params):
            return {"result": hex(4663) if method == "eth_chainId" else "0x123"}

    monkeypatch.setattr(rpc, "_provider", lambda *_args: FakeProvider())
    with pytest.raises(RuntimeError, match="expected 1"):
        rpc.RPC(chain).call("eth_blockNumber", [])


def test_websocket_feed_requires_selected_chain_identity():
    feed = HeadFeed(["wss://rpc.example.invalid"], expected_chain_id=4663)

    feed._verify_chain_id({"jsonrpc": "2.0", "id": 0, "result": "0x1237"})
    with pytest.raises(RuntimeError, match="expected 4663"):
        feed._verify_chain_id({"jsonrpc": "2.0", "id": 0, "result": "0x1"})


def test_unknown_evm_network_is_rejected(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(textwrap.dedent("""
        [chains.arbitrum]
        chain_id = 42161
        rpc = ["https://rpc.example"]
    """))

    with pytest.raises(ValueError, match="unsupported EVM network"):
        config.load(str(path))


def test_chain_picker_filters_to_configured_mainnets(monkeypatch):
    chains = {
        name: config.Chain(name, chain_id, ["https://rpc.example"], ["https://rpc.example"])
        for name, chain_id in (("ethereum", 1), ("robinhood", 4663), ("ink", 57073))
    }
    chains["robinhood_testnet"] = config.Chain(
        "robinhood_testnet", 46630, ["https://rpc.example"], ["https://rpc.example"])
    monkeypatch.setattr(bot, "CFG", SimpleNamespace(chains=chains))

    assert bot.enabled_chains() == ["ethereum", "robinhood", "ink"]


def test_selected_rpc_endpoint_must_match_network():
    selected = rpc.RPC(["good", "offline"])

    def fake_call(method, _params, url=None):
        assert method == "eth_chainId"
        if url == "offline":
            raise OSError("offline")
        return hex(4663)

    selected.call = fake_call

    assert selected.assert_chain_id(4663) == 4663
    assert selected.urls == ["good"]


def test_selected_rpc_rejects_wrong_network():
    selected = rpc.RPC(["wrong"])
    selected.call = lambda *_args, **_kwargs: hex(1)

    with pytest.raises(RuntimeError, match="expected 4663"):
        selected.assert_chain_id(4663)


def test_rpc_transport_errors_do_not_expose_provider_url(monkeypatch):
    class BrokenProvider:
        def make_request(self, *_args):
            raise OSError("failed at https://rpc.example/secret-key")

    monkeypatch.setattr(rpc, "_provider", lambda *_args: BrokenProvider())
    monkeypatch.setattr(rpc.time, "sleep", lambda *_args: None)
    with pytest.raises(RuntimeError) as error:
        rpc.RPC(["https://rpc.example/secret-key"]).call("eth_chainId", [])

    assert "secret-key" not in str(error.value)


def test_rpc_call_uses_web3_http_provider(monkeypatch):
    class FakeProvider:
        def make_request(self, method, params):
            return {"result": "0x1234"}

    monkeypatch.setattr(rpc, "_provider", lambda *_args: FakeProvider())
    assert rpc.RPC(["endpoint"]).call("eth_chainId", []) == "0x1234"


def test_contract_analysis_verifies_selected_network_before_reading(monkeypatch):
    chain = config.Chain("ethereum", 1, ["https://rpc.example"], ["https://rpc.example"])
    monkeypatch.setattr(bot, "CFG", SimpleNamespace(chains={"ethereum": chain}))

    class WrongNetworkRPC:
        def assert_chain_id(self, expected):
            assert expected == 1
            raise RuntimeError("RPC endpoint reported chain ID 4663; expected 1")

    monkeypatch.setattr(bot, "RPC", lambda _urls: WrongNetworkRPC())
    monkeypatch.setattr(bot.mintread, "detect", lambda *_args: pytest.fail("contract read ran before chain check"))

    draft, error = bot.analyze_contract_sync("0x" + "12" * 20, "ethereum")

    assert draft is None
    assert "couldn't verify the configured RPC is ethereum" in error


def test_wallets_do_not_share_robinhood_mainnet_with_testnet():
    assert wallets.supports_chain("robinhood", "robinhood")
    assert not wallets.supports_chain("robinhood_testnet", "robinhood")
    assert "robinhood_testnet" not in wallets.CHAINS


def test_solana_rpc_url_is_configurable_and_validated():
    old_urls = list(solana._rpc_urls)
    try:
        solana.configure_rpc_urls(["https://rpc.example.invalid", "https://backup.example.invalid"])
        assert solana._rpc_url == "https://rpc.example.invalid"
        assert solana._rpc_urls == ["https://rpc.example.invalid", "https://backup.example.invalid"]
        with pytest.raises(ValueError, match="HTTP or HTTPS"):
            solana.configure_rpc_url("wss://rpc.example.invalid")
    finally:
        solana.configure_rpc_urls(old_urls)


def test_solana_rpc_uses_solana_py_client(monkeypatch):
    class Response:
        def __init__(self, result):
            self.result = result

        def to_json(self):
            import json
            return json.dumps({"jsonrpc": "2.0", "result": self.result, "id": 1})

    class FakeClient:
        def get_genesis_hash(self):
            return Response(solana.MAINNET_GENESIS_HASH)

        def get_balance(self, address, commitment):
            assert str(address) == "11111111111111111111111111111111"
            assert commitment == "confirmed"
            return Response({"value": 123})

    monkeypatch.setattr(solana, "_client", lambda _url: FakeClient())
    old_urls = list(solana._rpc_urls)
    try:
        solana.configure_rpc_urls(["https://rpc.example.invalid"])
        assert solana.rpc_call(
            "getBalance", ["11111111111111111111111111111111"]) == {"value": 123}
    finally:
        solana.configure_rpc_urls(old_urls)


def test_solana_rpc_fails_over_only_to_mainnet_and_supports_parsed_account_info(monkeypatch):
    class Response:
        def __init__(self, result):
            self.result = result

        def to_json(self):
            import json
            return json.dumps({"jsonrpc": "2.0", "result": self.result, "id": 1})

    class FakeClient:
        def __init__(self, mainnet):
            self.mainnet = mainnet

        def get_genesis_hash(self):
            value = solana.MAINNET_GENESIS_HASH if self.mainnet else "wrong-cluster"
            return Response(value)

        def get_account_info_json_parsed(self, _address, commitment):
            assert commitment == "confirmed"
            return Response({"value": {"owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"}})

    clients = {
        "https://wrong.example": FakeClient(False),
        "https://mainnet.example": FakeClient(True),
    }
    monkeypatch.setattr(solana, "_client", clients.__getitem__)
    old_urls = list(solana._rpc_urls)
    try:
        solana.configure_rpc_urls(["https://wrong.example", "https://mainnet.example"])
        result = solana.rpc_call(
            "getAccountInfo",
            ["11111111111111111111111111111111", {"encoding": "jsonParsed"}],
        )
        assert result["value"]["owner"] == "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
    finally:
        solana.configure_rpc_urls(old_urls)


def test_solana_latest_blockhash_rpc_and_confirmation_tracking(monkeypatch):
    class Response:
        def __init__(self, result):
            self.result = result

        def to_json(self):
            import json
            return json.dumps({"result": self.result})

    class FakeClient:
        def get_genesis_hash(self):
            return Response(solana.MAINNET_GENESIS_HASH)

        def get_latest_blockhash(self, commitment):
            assert commitment == "confirmed"
            return Response({"value": {"blockhash": "blockhash", "lastValidBlockHeight": 77}})

    old_urls = list(solana._rpc_urls)
    try:
        monkeypatch.setattr(solana, "_client", lambda _url: FakeClient())
        solana.configure_rpc_urls(["https://rpc.example.invalid"])
        result = solana.rpc_call("getLatestBlockhash", [{"commitment": "confirmed"}])
        assert result["value"]["lastValidBlockHeight"] == 77
    finally:
        solana.configure_rpc_urls(old_urls)

    statuses = iter([
        {"value": [None]},
        {"value": [{"err": None, "confirmationStatus": "confirmed"}]},
    ])
    calls = []

    def fake_call(method, params, *, rpc_url=None):
        calls.append((method, params, rpc_url))
        return next(statuses)

    monkeypatch.setattr(solana.time, "sleep", lambda _seconds: None)
    assert solana.wait_for_confirmation("signature", call=fake_call, timeout_s=1) is True
    assert [call[0] for call in calls] == ["getSignatureStatuses", "getSignatureStatuses"]


def test_solana_program_inspection_reads_anchor_idl_without_building_a_mint():
    import base64
    import json
    import zlib

    program = Pubkey.from_string("11111111111111111111111111111111")
    signer = Pubkey.find_program_address([], program)[0]
    idl_address = Pubkey.create_with_seed(signer, "anchor:idl", program)
    idl = {
        "name": "sample_program",
        "version": "1.0.0",
        "instructions": [{
            "name": "mint",
            "accounts": [{"name": "payer"}],
            "args": [{"name": "quantity"}],
        }],
    }
    payload = zlib.compress(json.dumps(idl).encode())
    raw = bytes(Pubkey.default()) + len(payload).to_bytes(4, "little") + payload
    encoded = base64.b64encode(raw).decode()

    def fake_call(method, params, *, rpc_url=None):
        assert method == "getAccountInfo"
        assert rpc_url is None
        if params[0] == str(program):
            return {"value": {"executable": True}}
        assert params[0] == str(idl_address)
        return {"value": {"owner": str(program), "data": [encoded, "base64"]}}

    result = solana.inspect_program(str(program), call=fake_call)

    assert result["name"] == "sample_program"
    assert result["idl_address"] == str(idl_address)
    assert result["instructions"] == [
        {"name": "mint", "accounts": ["payer"], "args": ["quantity"]}
    ]
