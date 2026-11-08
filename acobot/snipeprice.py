"""Listing sniper: watch a collection's cheapest listings and alert the admin when one is under target.

Listings come from Reservoir's /orders/asks/v5 endpoint (currently configured for Ethereum).
Every hit remains behind the explicit Telegram confirmation flow.
"""
import json, time, threading, urllib.request, urllib.error, logging
from pathlib import Path

log = logging.getLogger("acobot.snipeprice")
DEFAULT_FILE = Path(__file__).resolve().parent.parent / "data" / "snipeprice.json"
RESERVOIR_HOST = {"ethereum": "https://api.reservoir.tools"}
UA = "acobot/0.1"
POLL_S = 20
BUY_UNAVAILABLE = "reservoir did not return a fillable transaction. nothing was bought."
RESERVOIR_EXECUTE = {"ethereum": "https://api.reservoir.tools"}

_lock = threading.Lock()


def load(path=DEFAULT_FILE):
    p = Path(path)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text())
    except Exception:
        return []


def save(rows, path=DEFAULT_FILE):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, indent=1))
    tmp.replace(p)


def add_target(rows, chain, contract, max_eth, by):
    """Add or replace a target for (chain, contract). Returns the new row list."""
    contract = contract.lower()
    rows = [r for r in rows if not (r["chain"] == chain and r["contract"] == contract)]
    rows.append({"chain": chain, "contract": contract, "max_eth": float(max_eth), "by": by,
                 "added_at": int(time.time()), "seen": [], "active": True})
    return rows


def fetch_cheapest(chain, contract, limit=10):
    """Cheapest active asks for a collection: list of exact token/order records."""
    host = RESERVOIR_HOST.get(chain)
    if not host:
        return None
    url = (f"{host}/orders/asks/v5?contracts={contract}&sortBy=price&limit={limit}&includePrivate=false")
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
    except Exception as e:
        log.warning("listings fetch failed: %s", type(e).__name__)
        return None
    out = []
    for o in data.get("orders") or []:
        try:
            p = o["price"]["amount"]["decimal"]
            token_set = o.get("tokenSetId") or ""
            token_parts = token_set.split(":")
            contract = token_parts[1] if len(token_parts) == 3 and token_parts[0] == "token" else None
            token_id = token_parts[2] if contract else None
            out.append({"id": o["id"], "price_eth": float(p),
                        "contract": contract, "token_id": token_id or None, "token_set_id": token_set,
                        "maker": o.get("maker"), "source": (o.get("source") or {}).get("name"),
                        "fetched_via": "reservoir", "protocol_address": o.get("protocol_address") or o.get("protocolAddress"),
                        "order_hash": o.get("order_hash") or o.get("orderHash") or o.get("id")})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def find_hits(rows_listings, max_eth, seen):
    """Listings at or under max_eth not already alerted. Returns (hits, updated_seen_ids)."""
    hits = [l for l in rows_listings if l["price_eth"] <= max_eth and l["id"] not in seen]
    return hits, list(seen) + [h["id"] for h in hits]


def confirm_text(target, listing, usd_per_eth, name, mask_fn=None):
    """Plain confirm panel for one listing. HTML-safe text, no raw error dumps."""
    usd = f"~${listing['price_eth'] * usd_per_eth:,.2f}" if usd_per_eth else "usd n/a"
    item = f"#{listing['token_id']}" if listing.get("token_id") else "item"
    market = listing.get("source") or "marketplace"
    return "\n".join([
        f"<b>snipe hit: {name or target['contract'][:10]}</b>",
        f"item {item} on {market}",
        f"price {listing['price_eth']:g} ETH ({usd})",
        f"your max {target['max_eth']:g} ETH",
        "",
        "tap buy to confirm. nothing is bought until you tap it.",
    ])


def _reservoir_post(url, payload, opener=None, api_key=None, timeout=8):
    body = json.dumps(payload).encode()
    headers = {"User-Agent": UA, "Accept": "application/json", "Content-Type": "application/json"}
    if api_key:
        headers["x-api-key"] = api_key
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    open_url = opener or urllib.request.urlopen
    with open_url(req, timeout=timeout) as response:
        return json.loads(response.read())


def _transaction_steps(response):
    """Return only API steps that contain a transaction to submit.

    Reservoir puts kind on the step. Older payloads put kind on each item.
    Both shapes are accepted, and signature steps are ignored.
    """
    found = []
    for step in response.get("steps") or []:
        step_kind = step.get("kind")
        for item in step.get("items") or []:
            if item.get("kind", step_kind) != "transaction":
                continue
            data = item.get("data") or {}
            tx = data.get("transaction") if isinstance(data.get("transaction"), dict) else data
            if tx.get("to") and tx.get("data"):
                found.append(tx)
    return found


def fetch_top_offer(chain, contract, token_id, *, api_key=None, opener=None):
    """Fetch current per-token bids from Reservoir, retaining only live-quoted orders."""
    host = RESERVOIR_EXECUTE.get(chain)
    if not host:
        return None
    token = f"{contract}:{token_id}"
    url = f"{host}/tokens/{token}/bids/v1?limit=10"
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if api_key:
        headers["x-api-key"] = api_key
    try:
        req = urllib.request.Request(url, headers=headers)
        with (opener or urllib.request.urlopen)(req, timeout=8) as response:
            data = json.loads(response.read())
    except Exception as exc:
        log.warning("reservoir bids fetch failed: %s", type(exc).__name__)
        return None
    bids = []
    for order in data.get("orders") or data.get("bids") or []:
        try:
            price = order.get("price") or {}
            amount = price.get("amount") or {}
            net = price.get("netAmount") or {}
            value = amount.get("decimal")
            if value is None:
                value = net.get("decimal")
            if value is None:
                continue
            bids.append({"id": order["id"], "price_eth": float(value),
                         "source": (order.get("source") or {}).get("name"),
                         "contract": contract, "token_id": str(token_id),
                         "order_hash": order.get("order_hash") or order.get("orderHash") or order.get("id"),
                         "protocol_address": order.get("protocol_address") or order.get("protocolAddress"),
                         "chain": chain})
        except (KeyError, TypeError, ValueError):
            continue
    return max(bids, key=lambda row: row["price_eth"], default=None)


def execute_sell(offer, floor_eth, wallet, private_key, *, chain, api_key=None,
                 opener=None, bcast=None, read_rpc=None):
    """Accept the exact Reservoir bid only when the selected quote clears user floor."""
    if not offer or not offer.get("id") or not offer.get("contract") or not offer.get("token_id"):
        return False, "offer lacks exact token/order data. nothing was sold."
    if float(offer.get("price_eth", 0)) < float(floor_eth):
        return False, "top offer is below your floor. nothing was sold."
    if not wallet or not private_key or not chain:
        return False, "saved seller wallet or chain configuration is missing. nothing was sold."
    host = RESERVOIR_EXECUTE.get(chain.name)
    is_opensea = str(offer.get("source") or "").lower() == "opensea"
    if chain.chain_id != 1 or (not host and not is_opensea):
        return False, f"no verified marketplace fulfillment adapter for {chain.name}. nothing was sold."
    payload = {"items": [{"token": f"{offer['contract']}:{offer['token_id']}",
                            "quantity": 1, "orderId": offer["id"]}],
               "taker": wallet, "excludeEOA": True}
    try:
        if is_opensea:
            from acobot import opensea
            tx = opensea.fulfill_offer(offer, wallet, key=opensea.api_key() or None, opener=opener)
        else:
            txs = _transaction_steps(_reservoir_post(host + "/execute/sell/v7", payload,
                                                       opener=opener, api_key=api_key))
            if len(txs) != 1:
                return False, "reservoir did not return a single fill transaction. nothing was sold."
            tx = txs[0]
        if tx.get("chainId") is not None and int(tx["chainId"]) != chain.chain_id:
            return False, "marketplace returned a transaction for another chain. nothing was sold."
        from eth_account import Account
        from eth_utils import to_checksum_address
        if Account.from_key(private_key).address.lower() != wallet.lower():
            return False, "selected wallet does not match the saved signing key. nothing was sold."
        if tx.get("from") and tx["from"].lower() != wallet.lower():
            return False, "reservoir transaction names another seller wallet. nothing was sold."
        value_raw = tx.get("value", 0)
        value = int(value_raw, 0) if isinstance(value_raw, str) else int(value_raw)
        unsigned = {"chainId": chain.chain_id, "to": to_checksum_address(tx["to"]),
                    "data": tx["data"], "value": value}
        if tx.get("nonce") is None or tx.get("gas", tx.get("gasLimit")) is None:
            if read_rpc is None:
                from acobot.rpc import RPC
                read_rpc = RPC(chain)
        nonce = tx.get("nonce")
        if nonce is None:
            nonce = read_rpc.call("eth_getTransactionCount", [wallet, "pending"])
        unsigned["nonce"] = int(nonce, 0) if isinstance(nonce, str) else int(nonce)
        gas = tx.get("gas", tx.get("gasLimit"))
        if gas is None:
            gas = read_rpc.call("eth_estimateGas", [{"from": wallet, "to": unsigned["to"],
                                                       "data": unsigned["data"], "value": hex(value)}, "pending"])
        unsigned["gas"] = int(gas, 0) if isinstance(gas, str) else int(gas)
        for name in ("maxFeePerGas", "maxPriorityFeePerGas", "gasPrice"):
            if tx.get(name) is not None:
                unsigned[name] = int(tx[name], 0) if isinstance(tx[name], str) else int(tx[name])
        raw = Account.sign_transaction(unsigned, private_key).raw_transaction.hex()
        if bcast is None:
            from acobot.rpc import RPC
            bcast = RPC(chain, broadcast=True)
        accepted = bcast.broadcast(raw)
        hashes = [v for v in accepted.values() if isinstance(v, str) and v.startswith("0x")]
        if not hashes:
            return False, "rpc did not accept the sale transaction. nothing was sold."
        log.info("reservoir offer accepted chain=%s token=%s tx=%s", chain.name, offer["token_id"], hashes[0])
        return True, f"sale submitted: <code>{hashes[0]}</code>"
    except Exception as exc:
        log.warning("reservoir sell fill failed: %s", type(exc).__name__)
        return False, "reservoir could not prepare this sale. nothing was sold."



def execute_buy(listing, fleet_addr=None, *, chain=None, private_key=None, api_key=None,
                opener=None, bcast=None, read_rpc=None):
    """Request Reservoir's documented fill transaction, sign it and broadcast.

    Only call after the separate Telegram confirmation callback. The caller supplies a saved,
    explicitly selected wallet and its signing key. The listing's order id binds the fill request.
    """
    if not isinstance(listing, dict) or not listing.get("token_id") or not listing.get("contract"):
        return False, "listing lacks an exact contract and token id. nothing was bought."
    if not fleet_addr or not chain or not private_key:
        return False, "a configured buyer wallet and signing key are required. nothing was bought."
    host = RESERVOIR_EXECUTE.get(chain.name)
    opensea_listing = str(listing.get("source") or "").lower() == "opensea"
    reservoir_sourced = listing.get("fetched_via") == "reservoir"
    direct_opensea = opensea_listing and not reservoir_sourced
    if chain.chain_id != 1 and not (chain.chain_id == 4663 and opensea_listing):
        return False, f"no verified marketplace fulfillment adapter for {chain.name}. nothing was bought."
    if chain.chain_id == 4663 and listing.get("chain") not in (None, "robinhood"):
        return False, "OpenSea listing chain mismatch. nothing was bought."
    if chain.chain_id == 1 and not host and not opensea_listing:
        return False, f"reservoir fill endpoint is not configured for {chain.name}. nothing was bought."
    payload = {"items": [{"token": f"{listing['contract']}:{listing['token_id']}",
                            "quantity": 1, "orderId": listing.get("id"), "fillType": "trade"}],
               "taker": fleet_addr, "excludeEOA": True}
    try:
        if chain.chain_id == 4663 and not listing.get("chain"):
            listing = dict(listing, chain="robinhood")
        if chain.chain_id == 4663 or direct_opensea or (chain.chain_id == 1 and not host):
            from acobot import opensea
            tx = opensea.fulfill_listing(listing, fleet_addr, key=opensea.api_key() or None, opener=opener)
            if not isinstance(tx, dict) or not tx.get("to") or not tx.get("data"):
                return False, "opensea did not return a usable fill transaction. nothing was bought."
            txs = [tx]
        else:
            try:
                response = _reservoir_post(host + "/execute/buy/v7", payload, opener=opener, api_key=api_key)
                txs = _transaction_steps(response)
            except Exception:
                if chain.chain_id != 1 or not opensea_listing:
                    raise
                txs = []
            # Reservoir stays first choice for all orders fetched from Reservoir. Only fall back
            # to OpenSea when its fill is absent/unusable for an OpenSea-origin order.
            if chain.chain_id == 1 and reservoir_sourced and opensea_listing:
                reservoir_usable = len(txs) == 1
                if reservoir_usable:
                    candidate = txs[0]
                    calldata = candidate.get("data")
                    try:
                        wrong_chain = candidate.get("chainId") is not None and int(candidate["chainId"]) != chain.chain_id
                    except (TypeError, ValueError):
                        wrong_chain = True
                    reservoir_usable = bool(candidate.get("to") and isinstance(calldata, str)
                                            and calldata.startswith("0x") and len(calldata) >= 10
                                            and not wrong_chain)
                if not reservoir_usable:
                    from acobot import opensea
                    fallback = opensea.fulfill_listing(listing, fleet_addr, key=opensea.api_key() or None, opener=opener)
                    if not isinstance(fallback, dict) or not fallback.get("to") or not fallback.get("data"):
                        return False, "opensea fallback did not return a usable fill transaction. nothing was bought."
                    txs = [fallback]
        if len(txs) != 1:
            log.warning("marketplace buy response had %d transaction steps", len(txs))
            return False, BUY_UNAVAILABLE
        tx = txs[0]
        if tx.get("chainId") is not None and int(tx["chainId"]) != chain.chain_id:
            return False, "marketplace returned a transaction for another chain. nothing was bought."
        from eth_account import Account
        from eth_utils import to_checksum_address
        acct = Account.from_key(private_key)
        if acct.address.lower() != fleet_addr.lower():
            return False, "selected wallet does not match the saved signing key. nothing was bought."
        if tx.get("from") and tx["from"].lower() != fleet_addr.lower():
            return False, "reservoir transaction names a different buyer wallet. nothing was bought."
        tx_data = tx.get("data")
        if not isinstance(tx_data, str) or not tx_data.startswith("0x") or len(tx_data) < 10:
            return False, "reservoir returned invalid transaction data. nothing was bought."
        value_raw = tx.get("value", 0)
        value = int(value_raw, 0) if isinstance(value_raw, str) else int(value_raw)
        # Guard against a fill quote that silently exceeds the alert's configured purchase cap.
        cap = listing.get("max_eth")
        if cap is not None and value > int(float(cap) * 10**18):
            return False, "reservoir fill total exceeds your max price. nothing was bought."
        unsigned = {"chainId": chain.chain_id, "to": to_checksum_address(tx["to"]),
                    "data": tx_data, "value": value}
        nonce_raw = tx.get("nonce")
        if nonce_raw is None:
            if read_rpc is None:
                from acobot.rpc import RPC
                read_rpc = RPC(chain)
            nonce_raw = read_rpc.call("eth_getTransactionCount", [fleet_addr, "pending"])
        unsigned["nonce"] = int(nonce_raw, 0) if isinstance(nonce_raw, str) else int(nonce_raw)
        gas_raw = tx.get("gas", tx.get("gasLimit"))
        if gas_raw is None:
            if read_rpc is None:
                from acobot.rpc import RPC
                read_rpc = RPC(chain)
            call_obj = {"from": fleet_addr, "to": unsigned["to"], "data": tx_data, "value": hex(value)}
            gas_raw = read_rpc.call("eth_estimateGas", [call_obj, "pending"])
        unsigned["gas"] = int(gas_raw, 0) if isinstance(gas_raw, str) else int(gas_raw)
        if not unsigned["gas"]:
            return False, "reservoir fill has no usable gas estimate. nothing was bought."
        for name in ("maxFeePerGas", "maxPriorityFeePerGas", "gasPrice"):
            if tx.get(name) is not None:
                v = tx[name]
                unsigned[name] = int(v, 0) if isinstance(v, str) else int(v)
        raw = Account.sign_transaction(unsigned, private_key).raw_transaction.hex()
        if bcast is None:
            from acobot.rpc import RPC
            bcast = RPC(chain, broadcast=True)
        accepted = bcast.broadcast(raw)
        hashes = [v for v in accepted.values() if isinstance(v, str) and v.startswith("0x")]
        if not hashes:
            return False, "rpc did not accept the fill transaction. nothing was bought."
        log.info("reservoir listing fill submitted chain=%s token=%s tx=%s", chain.name, listing["token_id"], hashes[0])
        return True, f"fill submitted: <code>{hashes[0]}</code>"
    except Exception as exc:
        log.warning("%s listing fill failed: %s", "opensea" if direct_opensea or (chain.chain_id == 1 and not host) else "marketplace", type(exc).__name__)
        message = ("opensea could not prepare a usable fill transaction. nothing was bought."
                   if direct_opensea or (chain.chain_id == 1 and not host) else BUY_UNAVAILABLE)
        return False, message
