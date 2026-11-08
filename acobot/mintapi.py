"""Official site-mint API: per contract, a URL that returns wallet-specific mint data.

Templates may use {wallet}, {phase}, {stage_index}, and {qty}. Gated transactions must
include their complete calldata and payment value; authorization is never synthesized here.
Stored in data/mint_apis.json. Never raises from fetch helpers.
"""
import json, re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
import httpx

STORE = Path(__file__).resolve().parent.parent / "data" / "mint_apis.json"
HEX_RE = re.compile(r"^0x[0-9a-fA-F]*$")
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"


def load(path=None):
    p = Path(path) if path else STORE
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def save(contract, template, path=None):
    """admin-set mint api for one contract. template must be https and hold {wallet}."""
    if not isinstance(contract, str) or not ADDRESS_RE.fullmatch(contract):
        raise ValueError("contract must be a 20-byte hex address")
    if "{wallet}" not in template or not template.startswith("https://"):
        raise ValueError("template must be an https url containing {wallet}")
    p = Path(path) if path else STORE
    data = load(p)
    data[contract.lower()] = template
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2))


def get_template(contract, path=None):
    return load(path).get((contract or "").lower())


def supports_phase(contract, path=None):
    template = get_template(contract, path)
    return bool(template and ("{phase}" in template or "{stage_index}" in template))


def _uint(value, field):
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and re.fullmatch(r"(?:[0-9]+|0x[0-9a-fA-F]+)", value):
        number = int(value, 16) if value.startswith("0x") else int(value)
    else:
        raise ValueError(f"{field} must be an integer")
    if number < 0:
        raise ValueError(f"{field} must not be negative")
    return number


def _mint_params(raw):
    if not isinstance(raw, dict):
        raise ValueError("authorization omitted SeaDrop mintParams")
    names = (
        "mintPrice", "maxTotalMintableByWallet", "startTime", "endTime",
        "dropStageIndex", "maxTokenSupplyForStage", "feeBps",
    )
    values = []
    for name in names:
        snake = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
        if name not in raw and snake not in raw:
            raise ValueError(f"authorization omitted mintParams.{name}")
        values.append(_uint(raw.get(name, raw.get(snake)), f"mintParams.{name}"))
    restrict = raw.get("restrictFeeRecipients", raw.get("restrict_fee_recipients"))
    if not isinstance(restrict, bool):
        raise ValueError("mintParams.restrictFeeRecipients must be a boolean")
    return tuple(values) + (restrict,)


def _bytes32_array(raw):
    if not isinstance(raw, list):
        raise ValueError("authorization proof must be a list")
    proof = []
    for value in raw:
        if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", value):
            raise ValueError("authorization proof contains an invalid bytes32 value")
        proof.append(bytes.fromhex(value[2:]))
    return proof


def _expiry(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value) if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value) else \
                datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _validate_response_context(body, transaction, authorization, *, contract, wallet,
                               phase, stage_index, expected_chain_id):
    contexts = (body, transaction, authorization)
    for data in contexts:
        if not isinstance(data, dict):
            continue
        response_wallet = next((data.get(key) for key in
                                ("wallet", "walletAddress", "authorizedWallet", "minter", "from")
                                if data.get(key)), None)
        if response_wallet and (not isinstance(response_wallet, str)
                                or response_wallet.lower() != wallet.lower()):
            return "mint api authorization is for a different wallet"
        response_contract = next((data.get(key) for key in
                                  ("contract", "contractAddress", "nft", "nftAddress", "tokenAddress")
                                  if data.get(key)), None)
        if response_contract and (not isinstance(response_contract, str)
                                  or response_contract.lower() != contract.lower()):
            return "mint api authorization is for a different contract"
        response_phase = data.get("phase")
        if phase and response_phase is not None:
            expected_phases = (
                {str(value).lower() for value in phase if value}
                if isinstance(phase, (tuple, list, set)) else {str(phase).lower()}
            )
            if str(response_phase).lower() not in expected_phases:
                return "mint api authorization is for a different phase"
        response_stage = data.get("stage_index", data.get("stageIndex"))
        if stage_index is not None and response_stage is not None:
            try:
                if _uint(response_stage, "stage index") != int(stage_index):
                    return "mint api authorization is for a different phase"
            except (TypeError, ValueError):
                return "mint api returned an invalid stage index"
        response_chain = data.get("chainId", data.get("chain_id"))
        if response_chain is not None:
            try:
                parsed_chain = _uint(response_chain, "chain id") if not (
                    isinstance(response_chain, str) and response_chain.startswith("0x")
                ) else int(response_chain, 16)
            except (TypeError, ValueError):
                return "mint api returned an invalid chain id"
            if expected_chain_id is not None and parsed_chain != expected_chain_id:
                return "mint api transaction is for a different chain"
    return None


def _encode_seadrop_authorization(auth, contract, wallet, fee_recipient, requested_qty,
                                  expected_phase=None, expected_stage_index=None,
                                  expected_price_wei=None):
    """Encode only proof/signature fields actually supplied by the official endpoint."""
    from eth_abi import encode
    from eth_utils import keccak

    if not isinstance(auth, dict):
        raise ValueError("authorization payload is missing")
    kind = str(auth.get("type") or auth.get("kind") or "").lower().replace("-", "_")
    if kind in {"merkle", "merkle_proof", "allow_list"}:
        kind = "allowlist"
    expected_kind = str(expected_phase or "").lower()
    expected_kind = "allowlist" if expected_kind in {"allowlist", "allow_list"} else expected_kind
    expected_kind = "signed" if expected_kind in {"signed", "signature"} else expected_kind
    if expected_kind in {"allowlist", "signed"}:
        accepted = {expected_kind, "signature"} if expected_kind == "signed" else {expected_kind}
        if kind not in accepted:
            raise ValueError(f"{expected_kind} phase received {kind or 'untyped'} authorization")
    params = _mint_params(auth.get("mintParams") or auth.get("mint_params"))
    if expected_stage_index is not None and params[4] != int(expected_stage_index):
        raise ValueError("authorization mintParams.dropStageIndex does not match the selected phase")
    if expected_price_wei is not None and params[0] != int(expected_price_wei):
        raise ValueError("authorization mintParams.mintPrice does not match the selected phase price")
    fee = auth.get("feeRecipient") or auth.get("fee_recipient") or fee_recipient
    if not isinstance(fee, str) or not ADDRESS_RE.fullmatch(fee):
        raise ValueError("authorization omitted a valid fee recipient")
    prefix = ["address", "address", "address", "uint256",
              "(uint256,uint256,uint256,uint256,uint256,uint256,uint256,bool)"]
    authorized_qty = _uint(auth.get("quantity", auth.get("qty", requested_qty or 1)), "quantity")
    if requested_qty is not None and authorized_qty != requested_qty:
        raise ValueError("authorization quantity does not match the selected quantity")
    if not ADDRESS_RE.fullmatch(contract or "") or not ADDRESS_RE.fullmatch(wallet or ""):
        raise ValueError("selected contract or wallet address is invalid")
    values = [fee, contract, wallet, authorized_qty, params]
    if kind == "allowlist":
        raw_proof = auth.get("proof")
        if raw_proof is None:
            raw_proof = auth.get("merkleProof", auth.get("merkle_proof"))
        proof = _bytes32_array(raw_proof)
        signature = "mintAllowList(address,address,address,uint256,(uint256,uint256,uint256,uint256,uint256,uint256,uint256,bool),bytes32[])"
        types = prefix + ["bytes32[]"]
        values.append(proof)
    elif kind in ("signed", "signature"):
        salt = _uint(auth.get("salt"), "salt")
        raw_signature = auth.get("signature")
        if not isinstance(raw_signature, str) or not re.fullmatch(r"0x(?:[0-9a-fA-F]{2})*", raw_signature):
            raise ValueError("authorization omitted a valid signature")
        signature = "mintSigned(address,address,address,uint256,(uint256,uint256,uint256,uint256,uint256,uint256,uint256,bool),uint256,bytes)"
        types = prefix + ["uint256", "bytes"]
        values.extend((salt, bytes.fromhex(raw_signature[2:])))
    else:
        raise ValueError("authorization type must be allowlist or signed")
    selector = keccak(text=signature)[:4]
    return "0x" + (selector + encode(types, values)).hex(), params[0] * values[3]


def _validate_seadrop_calldata(data, contract, wallet, qty, phase_key,
                               stage_index, fee_recipient, expected_price_wei):
    from eth_abi import decode
    from eth_abi.exceptions import DecodingError
    from eth_utils import keccak

    allowlist_signature = (
        "mintAllowList(address,address,address,uint256,"
        "(uint256,uint256,uint256,uint256,uint256,uint256,uint256,bool),bytes32[])")
    signed_signature = (
        "mintSigned(address,address,address,uint256,"
        "(uint256,uint256,uint256,uint256,uint256,uint256,uint256,bool),uint256,bytes)")
    allowlist_selector = keccak(text=allowlist_signature)[:4].hex()
    signed_selector = keccak(text=signed_signature)[:4].hex()
    public_selector = keccak(text="mintPublic(address,address,address,uint256)")[:4].hex()
    selector = data[2:10].lower()
    phase = str(phase_key or "").lower()
    if phase in {"allowlist", "allow_list"}:
        expected = allowlist_selector
    elif phase in {"signed", "signature"}:
        expected = signed_selector
    else:
        expected = None
    allowed = {expected} if expected else {allowlist_selector, signed_selector}
    if selector not in allowed:
        if selector == public_selector:
            return "refusing public SeaDrop calldata for a gated phase"
        return "gated SeaDrop phase requires the matching mintAllowList or mintSigned method"

    types = [
        "address", "address", "address", "uint256",
        "(uint256,uint256,uint256,uint256,uint256,uint256,uint256,bool)",
        "bytes32[]" if selector == allowlist_selector else "uint256",
    ]
    if selector == signed_selector:
        types.append("bytes")
    try:
        values = decode(types, bytes.fromhex(data[10:]))
    except (DecodingError, TypeError, ValueError) as exc:
        return f"gated SeaDrop calldata could not be decoded: {type(exc).__name__}"
    if str(values[1]).lower() != contract.lower():
        return "gated SeaDrop calldata targets a different NFT contract"
    if str(values[2]).lower() != wallet.lower():
        return "gated SeaDrop authorization is for a different wallet"
    if int(values[3]) != int(qty or 1):
        return "gated SeaDrop authorization quantity does not match the selected quantity"
    params = values[4]
    if int(params[1]) > 0 and int(values[3]) > int(params[1]):
        return "gated SeaDrop quantity exceeds the phase wallet limit"
    if stage_index is not None and int(params[4]) != int(stage_index):
        return "gated SeaDrop authorization is for a different phase"
    if expected_price_wei is not None and int(params[0]) != int(expected_price_wei):
        return "gated SeaDrop authorization price does not match the selected phase"
    if fee_recipient and str(values[0]).lower() != fee_recipient.lower():
        return "gated SeaDrop calldata uses a different fee recipient"
    return None, int(params[0]) * int(values[3])


def transaction_from_response(body, contract, wallet, *, phase=None, phase_key=None,
                              stage_index=None, qty=None, expected_to=None,
                              expected_chain_id=None, gated=False, seadrop=False,
                              expected_fee_recipient=None, expected_price_wei=None):
    """Validate an already-received project response without requiring a configured URL."""
    if not isinstance(body, dict):
        return None, "mint authorization response is not an object"
    nested = body.get("data")
    if isinstance(nested, dict):
        body = {**nested, **{key: value for key, value in body.items() if key != "data"}}
    transaction = body.get("transaction") if isinstance(body.get("transaction"), dict) else {}
    authorization = (body.get("authorization") or body.get("auth")
                     or body.get("mintAuthorization"))
    if authorization is None:
        for key in ("signedMint", "mintSignature"):
            signed = body.get(key)
            if isinstance(signed, dict):
                authorization = signed
                break
            if isinstance(signed, str):
                authorization = {"type": "signed", "signature": signed}
                break
    if authorization is None and any(key in body for key in
                                     ("proof", "merkleProof", "merkle_proof", "signature")):
        authorization = body
    if isinstance(authorization, dict):
        authorization = dict(authorization)
        if not any(key in authorization for key in ("mintParams", "mint_params")):
            for key in ("mintParams", "mint_params"):
                if key in body:
                    authorization[key] = body[key]
        if not any(key in authorization for key in ("type", "kind")):
            if any(key in authorization for key in ("proof", "merkleProof", "merkle_proof")):
                authorization["type"] = "allowlist"
            elif "signature" in authorization:
                authorization["type"] = "signed"
    context_error = _validate_response_context(
        body, transaction, authorization, contract=contract, wallet=wallet,
        phase=(phase, phase_key) if phase and phase_key else phase or phase_key,
        stage_index=stage_index,
        expected_chain_id=expected_chain_id)
    if context_error:
        return None, context_error

    data = (body.get("data") or body.get("calldata") or transaction.get("data")
            or transaction.get("input") or transaction.get("input_data"))
    auth_value = None
    if data is None and gated and seadrop and authorization is not None:
        try:
            data, auth_value = _encode_seadrop_authorization(
                authorization, contract, wallet,
                body.get("feeRecipient") or body.get("fee_recipient") or expected_fee_recipient,
                qty, expected_phase=phase_key, expected_stage_index=stage_index,
                expected_price_wei=expected_price_wei)
        except (ValueError, TypeError) as exc:
            return None, f"mint authorization invalid: {exc}"
    if not isinstance(data, str) or not HEX_RE.fullmatch(data) or len(data) < 10:
        return None, "mint authorization response returned no valid calldata"
    to = body.get("to") or transaction.get("to") or expected_to or contract
    if not isinstance(to, str) or not ADDRESS_RE.fullmatch(to):
        return None, "mint authorization response returned an invalid transaction target"
    if expected_to and to.lower() != expected_to.lower():
        return None, "mint authorization transaction target does not match the detected mint target"

    chain_id = body.get("chainId", body.get("chain_id", transaction.get("chainId", transaction.get("chain_id"))))
    if isinstance(chain_id, str):
        try:
            chain_id = int(chain_id, 16) if chain_id.startswith("0x") else int(chain_id)
        except ValueError:
            return None, "mint authorization response returned an invalid chain id"
    if chain_id is not None and (isinstance(chain_id, bool) or not isinstance(chain_id, int)):
        return None, "mint authorization response returned an invalid chain id"
    if expected_chain_id is not None and chain_id is not None and chain_id != expected_chain_id:
        return None, "mint authorization transaction is for a different chain"

    raw_value = body.get("value", transaction.get("value"))
    if raw_value is None:
        raw_value = auth_value
    if raw_value is None:
        if gated:
            return None, "mint authorization response omitted the required transaction value"
        value = None
    else:
        try:
            value = _uint(raw_value, "transaction value")
        except (TypeError, ValueError):
            return None, "mint authorization response returned an invalid transaction value"

    if gated and seadrop:
        validation = _validate_seadrop_calldata(
            data, contract, wallet, qty or 1, phase_key, stage_index,
            expected_fee_recipient, expected_price_wei)
        if isinstance(validation, str):
            return None, validation
        _, calculated_value = validation
        if value != calculated_value:
            return None, "transaction value does not match the supplied SeaDrop mint price"

    expiry = _expiry(
        body.get("expiresAt", body.get("expires_at"))
        or (authorization.get("expiresAt", authorization.get("expires_at"))
            if isinstance(authorization, dict) else None))
    cacheable = bool(body.get("cacheable") or
                     (isinstance(authorization, dict) and authorization.get("cacheable")))
    return {
        "data": data.lower(), "to": to, "value": value, "chain_id": chain_id,
        "authorization_expires_at": expiry,
        "authorization_cacheable": cacheable,
    }, "ok"


def fetch(contract, wallet, path=None, timeout=8.0):
    """(data_hex, to, reason). data_hex None means the api gave no usable calldata."""
    tx, reason = fetch_transaction(contract, wallet, path=path, timeout=timeout)
    if tx is None:
        return None, None, reason
    return tx["data"], tx["to"], "ok"


def fetch_transaction(contract, wallet, *, phase=None, stage_index=None, qty=None,
                      expected_to=None, expected_chain_id=None, gated=False,
                      seadrop=False, expected_fee_recipient=None, expected_price_wei=None,
                      phase_key=None, path=None, timeout=8.0):
    """Fetch and validate a complete per-wallet transaction.

    The endpoint is an explicit integration supplied by the bot administrator. For
    gated mints, a complete transaction and explicit value are required; the caller
    still simulates it from the minter before it can be signed.
    """
    tpl = get_template(contract, path)
    if not tpl:
        return None, "no mint api set"
    replacements = {
        "wallet": wallet,
        "phase": phase or "",
        "stage_index": "" if stage_index is None else str(stage_index),
        "qty": "" if qty is None else str(qty),
    }
    url = tpl
    for key, value in replacements.items():
        url = url.replace("{" + key + "}", quote(value, safe=""))
    try:
        r = httpx.get(url, headers={"User-Agent": UA, "Accept": "application/json"}, timeout=timeout, follow_redirects=False)
        body = r.json()
    except Exception as e:
        return None, f"mint api failed: {type(e).__name__}"
    if r.status_code != 200 or not isinstance(body, dict):
        return None, f"mint api http {r.status_code}"
    return transaction_from_response(
        body, contract, wallet, phase=phase, phase_key=phase_key,
        stage_index=stage_index, qty=qty, expected_to=expected_to,
        expected_chain_id=expected_chain_id, gated=gated, seadrop=seadrop,
        expected_fee_recipient=expected_fee_recipient,
        expected_price_wei=expected_price_wei)
