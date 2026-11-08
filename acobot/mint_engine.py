"""Chain-aware mint analysis and protocol adapter dispatch.

The orchestration layer selects a chain-specific analyzer and transaction adapter.
It never infers a chain from an address and never routes Solana data through EVM code.
"""
from dataclasses import dataclass
from typing import Callable

from eth_utils import is_address, is_checksum_address, to_checksum_address

from acobot import config, mintread, mint_router


class MintEngineError(ValueError):
    """The selected chain or mint route cannot be handled safely."""


@dataclass(frozen=True)
class MintAdapter:
    name: str
    chain_family: str
    matches: Callable[[dict], bool]
    build: Callable[[dict, str, int], tuple] | None = None


class AdapterRegistry:
    """Ordered registry for chain-specific transaction builders."""

    def __init__(self):
        self._adapters = []

    def register(self, adapter):
        if not isinstance(adapter, MintAdapter):
            raise TypeError("adapter must be a MintAdapter")
        self._adapters.append(adapter)

    def find(self, chain_family, mint):
        return next((adapter for adapter in self._adapters
                     if adapter.chain_family == chain_family and adapter.matches(mint)), None)

    def build(self, chain_family, mint, wallet, qty):
        adapter = self.find(chain_family, mint)
        if adapter is None or adapter.build is None:
            raise MintEngineError(
                f"no transaction adapter is available for {mint.get('protocol') or 'this mint'}")
        return adapter.build(mint, wallet, qty)


def _evm_build(mint, wallet, qty):
    return mint_router.build_transaction(mint, wallet, qty)


DEFAULT_ADAPTERS = AdapterRegistry()
DEFAULT_ADAPTERS.register(MintAdapter(
    name="SeaDrop",
    chain_family="evm",
    matches=lambda mint: bool(mint.get("seadrop")),
    build=_evm_build,
))
DEFAULT_ADAPTERS.register(MintAdapter(
    name="simple-evm",
    chain_family="evm",
    matches=lambda mint: bool(mint.get("mint_sig") or mint.get("_api_calldata")),
    build=_evm_build,
))


def validate_evm_address(address):
    if not isinstance(address, str) or not is_address(address):
        raise MintEngineError("invalid EVM contract address")
    if not (address[2:].islower() or address[2:].isupper()) and not is_checksum_address(address):
        raise MintEngineError("mixed-case EVM address has an invalid checksum")
    return to_checksum_address(address)


def validate_solana_program(program_id):
    try:
        from solders.pubkey import Pubkey
        return str(Pubkey.from_string(str(program_id)))
    except Exception as exc:
        raise MintEngineError("invalid Solana program address") from exc


def analyze_evm(contract, chain, rpc, *, verify_chain=True):
    """Analyze the contract only on the explicitly selected, configured EVM chain."""
    expected_chain_id = config.SUPPORTED_EVM_CHAIN_IDS.get(getattr(chain, "name", None))
    if expected_chain_id is None:
        raise MintEngineError("selected network is not a supported EVM chain")
    if chain.chain_id != expected_chain_id:
        raise MintEngineError(
            f"selected chain {chain.name} must use chain ID {expected_chain_id}")
    contract = validate_evm_address(contract)
    if verify_chain:
        try:
            rpc.assert_chain_id(expected_chain_id)
        except Exception as exc:
            raise MintEngineError(
                f"RPC could not be verified for {chain.name} (chain ID {expected_chain_id})") from exc

    facts = mintread.detect(contract, rpc)
    facts["chain"] = chain.name
    facts["chain_id"] = expected_chain_id
    facts["network"] = chain.name
    if facts.get("seadrop"):
        facts["protocol"] = "SeaDrop"
    elif facts.get("mint_sig"):
        facts["protocol"] = "simple EVM"
    else:
        facts["protocol"] = "unidentified EVM contract"
    adapter = DEFAULT_ADAPTERS.find("evm", facts)
    facts["transaction_adapter"] = adapter.name if adapter else None
    facts["transaction_construction_possible"] = adapter is not None
    if adapter is None:
        facts.setdefault("notes", []).append(
            "no registered protocol adapter can construct this contract's mint transaction")
    return facts


def analyze_solana(program_id, *, rpc_url=None):
    """Inspect a Solana program with Solana RPC and return analysis-only capabilities."""
    program_id = validate_solana_program(program_id)
    from acobot import authorization_resolver

    facts = authorization_resolver.inspect_solana_program(program_id, rpc_url=rpc_url)
    name = str(facts.get("name") or "").lower()
    if "candy" in name and "machine" in name:
        protocol = "Metaplex Candy Machine (Anchor IDL detected)"
    elif facts.get("name"):
        protocol = "Anchor program (IDL detected)"
    else:
        protocol = "unidentified Solana program"
    facts.update({
        "chain": "solana",
        "chain_id": "mainnet-beta",
        "protocol": protocol,
        "transaction_adapter": None,
        "transaction_construction_possible": False,
    })
    return facts


def build_transaction(chain_name, mint, wallet, qty, *, adapters=DEFAULT_ADAPTERS):
    """Dispatch only to the selected chain family's registered transaction adapter."""
    chain_name = str(chain_name or "").lower()
    if chain_name == "solana":
        chain_family = "solana"
    elif chain_name in config.SUPPORTED_EVM_CHAIN_IDS:
        chain_family = "evm"
    else:
        raise MintEngineError(
            f"no transaction builder is registered for the selected {chain_name or 'unknown'} network")
    mint_chain = mint.get("chain")
    if isinstance(mint_chain, config.Chain):
        if (mint_chain.name != chain_name
                or mint_chain.chain_id != config.SUPPORTED_EVM_CHAIN_IDS.get(chain_name)):
            raise MintEngineError("mint analysis belongs to a different selected network")
    elif mint_chain and str(mint_chain).lower() != chain_name:
        raise MintEngineError("mint analysis belongs to a different selected network")
    return adapters.build(chain_family, mint, wallet, qty)
