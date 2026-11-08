"""Fetch a real allowlist or public mint transaction from indexed drop APIs.

OpenSea's Drops API is the live source: POST /api/v2/drops/{slug}/mint returns
the unsigned transaction for the active stage, including GTD and FCFS, and
needs only an API key. Reservoir's host no longer resolves and Magic Eden's
EVM execute/mint route currently answers "Not Found"; both stay as later
fallbacks. This module never builds calldata itself.
"""
import os
import re

import httpx

from acobot.snipeprice import _transaction_steps

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,80}$")
OPENSEA_MINT = "https://api.opensea.io/api/v2/drops/{slug}/mint"
RESERVOIR_EXECUTE = {
    "ethereum": "https://api.reservoir.tools/execute/mint/v1",
}
MAGIC_EDEN_EXECUTE = {
    "ethereum": "https://api-mainnet.magiceden.dev/v3/rtp/ethereum/execute/mint/v1",
}


def api_key():
    return (os.environ.get("RESERVOIR_API_KEY") or os.environ.get("MAGICEDEN_API_KEY") or "").strip()


def indexed_mint_supported(chain_name):
    name = str(chain_name or "").lower()
    return name in RESERVOIR_EXECUTE or name in MAGIC_EDEN_EXECUTE


def _post(url, payload, timeout=8.0):
    headers = {"User-Agent": UA, "Accept": "application/json", "Content-Type": "application/json"}
    if "api.opensea.io" in url:
        from acobot.opensea import ensure_api_key
        key = ensure_api_key()
        if not key:
            raise RuntimeError("opensea key unavailable")
        headers["X-API-KEY"] = key
    else:
        key = api_key()
        if key:
            headers["x-api-key"] = key
    response = httpx.post(url, json=payload, headers=headers, timeout=timeout, follow_redirects=False)
    if response.status_code != 200:
        raise RuntimeError(f"http {response.status_code}")
    body = response.json()
    if not isinstance(body, dict):
        raise RuntimeError("not json object")
    return body


def _chain_name(chain, mint):
    if isinstance(chain, str):
        return chain.lower()
    name = getattr(chain, "name", None)
    return str(name or (mint or {}).get("chain") or "").lower()


def _collection_slug(mint, chain_name, lookup):
    slug = mint.get("slug") or mint.get("opensea_slug")
    if isinstance(slug, str) and SLUG_RE.fullmatch(slug.strip()):
        return slug.strip()
    if lookup is None or chain_name != "ethereum":
        return None
    contract = mint.get("contract")
    if not isinstance(contract, str) or not contract:
        return None
    try:
        from acobot import opensea
        found = opensea.cached_slug(chain_name, contract)
        if isinstance(found, str) and SLUG_RE.fullmatch(found):
            return found
        found = lookup(chain_name, contract)
    except Exception:
        return None
    if isinstance(found, str) and SLUG_RE.fullmatch(found):
        return found
    return None


def fetch_authorization(mint, wallet, qty, chain, *, post=None):
    """Return (response, source) or (None, reason). response is ready for mintapi."""
    mint = mint or {}
    chain_name = _chain_name(chain, mint)
    if chain_name not in RESERVOIR_EXECUTE and chain_name not in MAGIC_EDEN_EXECUTE:
        return None, "no indexed mint API for this chain"
    contract = mint.get("contract")
    if not isinstance(contract, str) or not contract:
        return None, "indexed mint API needs a contract"
    lookup = None
    if post is None:
        from acobot import opensea
        lookup = opensea._slug_for
    slug = _collection_slug(mint, chain_name, lookup)
    endpoints = []
    if slug:
        endpoints.append(("opensea", OPENSEA_MINT.format(slug=slug)))
    if chain_name in RESERVOIR_EXECUTE:
        endpoints.append(("reservoir", RESERVOIR_EXECUTE[chain_name]))
    if chain_name in MAGIC_EDEN_EXECUTE:
        endpoints.append(("magiceden", MAGIC_EDEN_EXECUTE[chain_name]))
    indexed_payload = {
        "items": [{"collection": contract, "quantity": int(qty or 1)}],
        "taker": wallet,
        "skipBalanceCheck": True,
    }
    opensea_payload = {
        "minter": wallet,
        "quantity": int(qty or 1),
        "skip_balance_check": True,
    }
    poster = post or _post
    errors = []
    phase = mint.get("phase") or mint.get("phase_label")
    for source, url in endpoints:
        payload = opensea_payload if source == "opensea" else indexed_payload
        try:
            body = poster(url, payload)
        except Exception as exc:
            errors.append(f"{source}:{type(exc).__name__}")
            continue
        transaction = _first_transaction(body)
        if transaction is None:
            errors.append(f"{source}:no transaction")
            continue
        response = {
            "transaction": transaction,
            "wallet": wallet,
            "contract": contract,
            "source": source,
        }
        if phase:
            response["phase"] = phase
        return response, source
    return None, "; ".join(errors) or "indexed mint API returned no transaction"


def _flat_transaction(body):
    if not isinstance(body, dict):
        return None
    data = body.get("data") or body.get("input_data")
    target = body.get("to")
    if "value" not in body or not target or not isinstance(data, str) or not data.startswith("0x"):
        return None
    return {"to": target, "data": data, "value": body.get("value")}


def _first_transaction(body):
    direct = _flat_transaction(body)
    if direct is not None:
        return direct
    nested = body.get("transaction") if isinstance(body, dict) else None
    nested_tx = _flat_transaction(nested)
    if nested_tx is not None:
        return nested_tx
    for tx in _transaction_steps(body or {}):
        data = tx.get("data")
        target = tx.get("to")
        if "value" not in tx or not target or not isinstance(data, str) or not data.startswith("0x"):
            continue
        return {"to": target, "data": data, "value": tx.get("value")}
    return None
