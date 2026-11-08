"""Select a mint transaction route without guessing authorization.

Adapters can be added here as protocol-specific calldata builders become available.
SeaDrop public calls and simple custom calls are built locally. Gated routes require
validated wallet-specific calldata and an explicit transaction value.
"""

import re

from eth_utils import keccak


class MintRouteError(ValueError):
    """The selected phase has no safe transaction route."""


def is_gated(mint):
    return bool(mint.get("gated") or mint.get("phase") in ("allowlist", "signed"))


def route_name(mint):
    if is_gated(mint):
        return "authorized-api"
    if mint.get("_api_calldata"):
        return "project-api"
    if mint.get("seadrop"):
        return "seadrop-public"
    if mint.get("mint_sig"):
        return "simple-contract"
    return "unsupported"


def build_transaction(mint, address, qty):
    """Return (to, calldata, value_override) for one wallet or refuse safely."""
    api_transactions = mint.get("_api_calldata") or {}
    api_tx = api_transactions.get(address.lower())
    if api_tx:
        if isinstance(api_tx, dict):
            data = api_tx.get("data")
            value = api_tx.get("value")
            if is_gated(mint) and value is None:
                raise MintRouteError("gated authorization transaction omitted its payment value")
            validate_api_selector(data, gated=is_gated(mint), seadrop=bool(mint.get("seadrop")),
                                  phase=mint.get("phase"))
            return api_tx.get("to") or mint.get("mint_to") or mint.get("contract"), data, value
        if isinstance(api_tx, (tuple, list)) and len(api_tx) == 2:
            if is_gated(mint):
                raise MintRouteError("gated authorization transaction omitted its payment value")
            to, data = api_tx
            validate_api_selector(data, gated=is_gated(mint), seadrop=bool(mint.get("seadrop")),
                                  phase=mint.get("phase"))
            return to or mint.get("contract"), data, None
        raise MintRouteError("invalid API transaction payload")

    if is_gated(mint):
        raise MintRouteError("gated phase requires wallet-specific authorization calldata")
    if mint.get("seadrop"):
        from acobot import sniper
        to = mint.get("mint_to")
        if not to or not mint.get("fee_recipient"):
            raise MintRouteError("SeaDrop public mint configuration is incomplete")
        data = sniper.encode_seadrop_mint(mint["contract"], mint["fee_recipient"], address, qty)
        return to, data, None
    if mint.get("mint_sig"):
        from acobot import sniper
        return mint.get("contract"), sniper.encode_mint(mint["mint_sig"], qty), None
    raise MintRouteError("no supported mint route detected")


def validate_api_selector(data, *, gated, seadrop, phase=None):
    """Validate that gated SeaDrop calldata uses the selected protocol method."""
    if not gated or not seadrop:
        return
    if not isinstance(data, str) or not re.fullmatch(r"0x[0-9a-fA-F]{8,}", data):
        raise MintRouteError("gated SeaDrop authorization returned invalid calldata")
    from eth_utils import keccak

    public = keccak(text="mintPublic(address,address,address,uint256)")[:4].hex()
    allowlist = keccak(text=(
        "mintAllowList(address,address,address,uint256,"
        "(uint256,uint256,uint256,uint256,uint256,uint256,uint256,bool),bytes32[])"
    ))[:4].hex()
    signed = keccak(text=(
        "mintSigned(address,address,address,uint256,"
        "(uint256,uint256,uint256,uint256,uint256,uint256,uint256,bool),uint256,bytes)"
    ))[:4].hex()
    selector = data[2:10].lower()
    if selector == public:
        raise MintRouteError("refusing public SeaDrop calldata for a gated phase")
    phase = str(phase or "").lower()
    if phase in {"allowlist", "allow_list"} and selector != allowlist:
        raise MintRouteError("allowlist phase requires mintAllowList authorization calldata")
    if phase in {"signed", "signature"} and selector != signed:
        raise MintRouteError("signed phase requires mintSigned authorization calldata")
    if phase not in {"allowlist", "allow_list", "signed", "signature"} and selector not in {
            allowlist, signed}:
        raise MintRouteError("gated SeaDrop phase requires mintAllowList or mintSigned authorization calldata")
