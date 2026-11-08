"""Fetch a real allowlist or public mint transaction from indexed drop APIs.

Reservoir and Magic Eden return the unsigned transaction for one wallet. This
module never builds calldata itself. Unsupported chains and empty responses
are reported and left for the caller's other authorization sources.
"""
import os

import httpx

from acobot.snipeprice import _transaction_steps

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
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


def fetch_authorization(mint, wallet, qty, chain, *, post=None):
    """Return (response, source) or (None, reason). response is ready for mintapi."""
    mint = mint or {}
    chain_name = _chain_name(chain, mint)
    endpoints = []
    if chain_name in RESERVOIR_EXECUTE:
        endpoints.append(("reservoir", RESERVOIR_EXECUTE[chain_name]))
    if chain_name in MAGIC_EDEN_EXECUTE:
        endpoints.append(("magiceden", MAGIC_EDEN_EXECUTE[chain_name]))
    if not endpoints:
        return None, "no indexed mint API for this chain"
    contract = mint.get("contract")
    if not isinstance(contract, str) or not contract:
        return None, "indexed mint API needs a contract"
    payload = {
        "items": [{"collection": contract, "quantity": int(qty or 1)}],
        "taker": wallet,
        "skipBalanceCheck": True,
    }
    poster = post or _post
    errors = []
    phase = mint.get("phase") or mint.get("phase_label")
    for source, url in endpoints:
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


def _first_transaction(body):
    for tx in _transaction_steps(body or {}):
        data = tx.get("data")
        target = tx.get("to")
        if "value" not in tx or not target or not isinstance(data, str) or not data.startswith("0x"):
            continue
        return {"to": target, "data": data, "value": tx.get("value")}
    return None
