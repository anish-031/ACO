from types import SimpleNamespace
from unittest import mock

import pytest

from acobot import authorization_resolver, bot, config, mint_engine, sniper


NFT = "0x" + "12" * 20
WALLET = "0x" + "34" * 20
PROGRAM = "11111111111111111111111111111111"


@pytest.mark.parametrize(
    ("name", "chain_id"),
    [("ethereum", 1), ("robinhood", 4663), ("ink", 57073)],
)
def test_evm_analysis_is_bound_to_selected_chain(name, chain_id, monkeypatch):
    chain = config.Chain(name, chain_id, ["rpc"], ["rpc"])
    class FakeRPC:
        def __init__(self):
            self.checked = []

        def assert_chain_id(self, expected):
            self.checked.append(expected)

    rpc = FakeRPC()
    facts = {"contract": NFT, "seadrop": True, "stages": []}
    monkeypatch.setattr(mint_engine.mintread, "detect", mock.Mock(return_value=facts))

    result = mint_engine.analyze_evm(NFT, chain, rpc)

    assert rpc.checked == [chain_id]
    assert result["chain"] == name
    assert result["chain_id"] == chain_id
    assert result["protocol"] == "SeaDrop"
    assert result["transaction_adapter"] == "SeaDrop"
    assert result["transaction_construction_possible"] is True


def test_evm_analysis_rejects_mixed_case_bad_checksum_before_rpc():
    chain = config.Chain("ethereum", 1, ["rpc"], ["rpc"])
    rpc = mock.Mock(spec=[])

    with pytest.raises(mint_engine.MintEngineError, match="checksum"):
        mint_engine.analyze_evm("0x" + "aA" * 20, chain, rpc)

    assert not rpc.mock_calls


def test_evm_analysis_rejects_selected_chain_mismatch_before_rpc():
    chain = config.Chain("robinhood", 1, ["rpc"], ["rpc"])
    rpc = mock.Mock(spec=[])

    with pytest.raises(mint_engine.MintEngineError, match="must use chain ID 4663"):
        mint_engine.analyze_evm(NFT, chain, rpc)

    assert not rpc.mock_calls


def test_evm_analysis_fails_closed_when_rpc_identity_is_wrong(monkeypatch):
    chain = config.Chain("ethereum", 1, ["rpc"], ["rpc"])
    class FakeRPC:
        def assert_chain_id(self, _expected):
            raise RuntimeError("wrong chain")

    rpc = FakeRPC()
    detect = mock.Mock()
    monkeypatch.setattr(mint_engine.mintread, "detect", detect)

    with pytest.raises(mint_engine.MintEngineError, match="could not be verified"):
        mint_engine.analyze_evm(NFT, chain, rpc)

    detect.assert_not_called()


def test_evm_builder_preserves_simple_mint_route():
    mint = {"chain": "robinhood", "contract": NFT, "mint_sig": "mint(uint256)"}

    to, data, value = mint_engine.build_transaction("robinhood", mint, WALLET, 2)

    assert to == NFT
    assert data == sniper.encode_mint("mint(uint256)", 2)
    assert value is None


def test_builder_rejects_cross_chain_mint_analysis():
    with pytest.raises(mint_engine.MintEngineError, match="different selected network"):
        mint_engine.build_transaction(
            "ink", {"chain": "robinhood", "seadrop": True}, WALLET, 1)


def test_builder_accepts_chain_object_attached_by_the_mint_watcher():
    selected_chain = config.Chain("robinhood", 4663, ["rpc"], ["rpc"])
    mint = {"chain": selected_chain, "contract": NFT, "mint_sig": "mint(uint256)"}

    to, data, value = mint_engine.build_transaction("robinhood", mint, WALLET, 1)

    assert (to, data, value) == (
        NFT, sniper.encode_mint("mint(uint256)", 1), None)


def test_sol_analysis_is_solana_only_and_explicitly_reports_missing_adapter(monkeypatch):
    inspected = {}

    def inspect(program_id, *, rpc_url=None):
        inspected.update(program_id=program_id, rpc_url=rpc_url)
        return {
            "program_id": program_id,
            "name": "sample_program",
            "instructions": [{"name": "mint", "args": ["quantity"]}],
            "authorization_type": "unknown",
            "missing": ["verified Solana mint adapter"],
        }

    monkeypatch.setattr(authorization_resolver, "inspect_solana_program", inspect)
    result = mint_engine.analyze_solana(PROGRAM, rpc_url="https://solana.example")

    assert inspected == {"program_id": PROGRAM, "rpc_url": "https://solana.example"}
    assert result["chain"] == "solana"
    assert result["chain_id"] == "mainnet-beta"
    assert result["transaction_adapter"] is None
    assert result["transaction_construction_possible"] is False
    assert result["missing"] == ["verified Solana mint adapter"]


def test_invalid_solana_address_does_not_invoke_inspector(monkeypatch):
    inspect = mock.Mock()
    monkeypatch.setattr(authorization_resolver, "inspect_solana_program", inspect)

    with pytest.raises(mint_engine.MintEngineError, match="invalid Solana"):
        mint_engine.analyze_solana("not-a-program")

    inspect.assert_not_called()


def test_registry_accepts_chain_specific_adapter_without_mixing_families():
    registry = mint_engine.AdapterRegistry()
    registry.register(mint_engine.MintAdapter(
        name="sample-solana",
        chain_family="solana",
        matches=lambda mint: mint.get("protocol") == "sample",
        build=lambda _mint, wallet, qty: (wallet, qty),
    ))

    assert mint_engine.build_transaction(
        "solana", {"chain": "solana", "protocol": "sample"}, WALLET, 3,
        adapters=registry,
    ) == (WALLET, 3)
    with pytest.raises(mint_engine.MintEngineError, match="no transaction adapter"):
        mint_engine.build_transaction(
            "ethereum", {"chain": "ethereum", "protocol": "sample"}, WALLET, 3,
            adapters=registry,
        )


def test_mint_input_validation_uses_the_selected_chain(monkeypatch):
    chains = {
        name: config.Chain(name, chain_id, ["rpc"], ["rpc"])
        for name, chain_id in (("ethereum", 1), ("robinhood", 4663), ("ink", 57073))
    }
    monkeypatch.setattr(
        bot, "CFG", SimpleNamespace(chains=chains, solana_rpc_urls=["solana-rpc"]))

    assert bot.enabled_mint_chains() == ["ethereum", "robinhood", "ink", "solana"]
    assert bot.valid_mint_address(NFT, "robinhood")
    assert not bot.valid_mint_address(PROGRAM, "robinhood")
    assert bot.valid_mint_address(PROGRAM, "solana")
    assert not bot.valid_mint_address(NFT, "solana")
