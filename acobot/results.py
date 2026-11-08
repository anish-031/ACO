"""Mint results: receipt polling, gas cost, revert-reason decoding, per-mint storage.

Pure helpers are at the top. Network calls take an RPC object (read rpc) and never sign.
Times: fire->broadcast is measured locally (time.time() around the broadcast call).
fire->receipt is measured locally: the moment the receipt was first seen minus fire time.
The receipt's block timestamp is stored too, for reference (chain seconds, 1s resolution).
"""
import json, os, time, urllib.request
from pathlib import Path
from eth_utils import keccak

EXPLORER_TX = {
    "ethereum": "https://etherscan.io/tx/{}",
    "robinhood": "https://robinhoodchain.blockscout.com/tx/{}",
}
RECEIPT_TIMEOUT_S = 300
RECEIPT_POLL_S = 1.0
PANIC_CODES = {
    0x00: "generic panic", 0x01: "assert failed", 0x11: "arithmetic overflow",
    0x12: "divide by zero", 0x21: "invalid enum value", 0x22: "bad storage encoding",
    0x31: "pop on empty array", 0x32: "array index out of bounds", 0x41: "too much memory",
    0x51: "call to empty function",
}


DUAL_SHOTS = ("prefire", "normal")
SAME_NONCE_TEXT = ("nonce too low", "replacement transaction underpriced", "replacement underpriced",
                   "already known", "known transaction", "already imported")
SUPERSEDED_REASON = "replaced (same nonce), fee not spent"


def is_same_nonce_reject(text):
    """Same-nonce rejects are EXPECTED in two-shot mode: the other shot of the pair already took the nonce."""
    t = (text or "").lower()
    return any(n in t for n in SAME_NONCE_TEXT)


def _settle_same_nonce(rows, pending):
    """Once one tx of a wallet has a receipt, its sibling shot (same nonce) can never mine: mark it superseded."""
    done = {(r.get("address") or "").lower() for r in rows if r.get("status") in ("minted", "failed")}
    for h, r in list(pending.items()):
        if (r.get("address") or "").lower() in done and r.get("shot") in DUAL_SHOTS:
            pending.pop(h)
            r["status"] = "superseded"
            r["reason"] = SUPERSEDED_REASON
            r["error"] = None


def explorer_url(chain, tx_hash):
    tpl = EXPLORER_TX.get(chain)
    return tpl.format(tx_hash) if tpl and tx_hash else None


def _h(x):
    return int(x, 16) if isinstance(x, str) and x.startswith("0x") else int(x or 0)


def gas_cost_wei(receipt):
    """gasUsed * effectiveGasPrice from a receipt dict. None if the receipt lacks either."""
    if not receipt:
        return None
    try:
        return _h(receipt["gasUsed"]) * _h(receipt["effectiveGasPrice"])
    except (KeyError, TypeError, ValueError):
        return None


def decode_revert(data_hex):
    """Human reason from revert return data. Error(string), Panic(uint256), or the custom selector."""
    if not data_hex or data_hex in ("0x", ""):
        return "reverted with no reason"
    h = data_hex[2:] if data_hex.startswith("0x") else data_hex
    try:
        raw = bytes.fromhex(h)
    except ValueError:
        return "reverted (unreadable data)"
    if raw[:4] == keccak(text="Error(string)")[:4] and len(raw) >= 68:
        off = int.from_bytes(raw[4:36], "big")
        n = int.from_bytes(raw[4 + off:4 + off + 32], "big")
        msg = raw[4 + off + 32:4 + off + 32 + n].decode("utf-8", "replace").strip()
        return msg or "reverted with empty message"
    if raw[:4] == keccak(text="Panic(uint256)")[:4] and len(raw) >= 36:
        code = int.from_bytes(raw[4:36], "big")
        return "panic: " + PANIC_CODES.get(code, f"code 0x{code:x}")
    return f"custom error 0x{raw[:4].hex()}"


def fmt_usd_val(x):
    if x is None:
        return None
    return f"${x:.2f}" if x >= 0.01 else f"${x:.4f}"


def summarize(wallets, fired_at=None):
    """wallets: list of per-wallet dicts (see collect). Returns the summary dict."""
    n = len(wallets)
    minted = sum(1 for w in wallets if w.get("status") == "minted")
    usd_vals = [w["gas_usd"] for w in wallets if w.get("gas_usd") is not None]
    total_usd = sum(usd_vals) if usd_vals else None
    lat = [w["receipt_s"] for w in wallets if w.get("receipt_s") is not None]
    avg = round(sum(lat) / len(lat), 1) if lat else None
    return {"wallets": n, "minted": minted, "total_gas_usd": total_usd, "avg_fire_to_receipt_s": avg}


def summary_line(s):
    gas = f"${s['total_gas_usd']:.2f}" if s.get("total_gas_usd") is not None else "n/a"
    avg = f"{s['avg_fire_to_receipt_s']:.1f}s" if s.get("avg_fire_to_receipt_s") is not None else "n/a"
    return f"{s['wallets']} wallets, {s['minted']} minted, total gas {gas}, avg fire→receipt {avg}"


def get_receipt(rpc, tx_hash, timeout=RECEIPT_TIMEOUT_S, poll=RECEIPT_POLL_S, sleep=time.sleep, clock=time.time):
    """Poll until the receipt exists or timeout. Returns (receipt|None, seen_at|None)."""
    end = clock() + timeout
    while clock() < end:
        try:
            r = rpc.call("eth_getTransactionReceipt", [tx_hash])
        except Exception:
            r = None
        if r:
            return r, clock()
        sleep(poll)
    return None, None


def block_timestamp(rpc, block_number_hex):
    try:
        b = rpc.call("eth_getBlockByNumber", [block_number_hex, False])
        return _h(b["timestamp"]) if b else None
    except Exception:
        return None


HINTS = [
    ("insufficient funds", "insufficient funds for gas or mint price, top up the wallet"),
    ("intrinsic gas too low", "gas limit too low for this tx"),
    ("gas too low", "gas limit too low for this tx"),
    ("underpriced", "gas price too low for the network right now"),
    ("max fee per gas less than", "max fee below the network base fee, retry with a higher speed"),
    ("nonce too low", "nonce already used, an earlier tx from this wallet went through"),
    ("nonce too high", "nonce gap, an earlier tx from this wallet did not go through"),
    ("replacement transaction", "a pending tx from this wallet with the same nonce is in the way"),
    ("already known", "the node already has this tx (not an error, it is in the mempool)"),
    ("exceeds block gas limit", "gas limit above the block gas limit"),
    ("sale not open", "sale not open yet"),
    ("not started", "sale not open yet"),
    ("sold out", "sold out"),
    ("max per wallet", "already minted max for this wallet"),
    ("exceeds max", "already minted max for this wallet"),
    ("allowlist", "wallet is not on the allowlist"),
    ("not allowlisted", "wallet is not on the allowlist"),
    ("insufficient", "not enough funds for the mint price"),
    ("execution reverted", "the contract rejected the mint (see reason)"),
    ("reverted", "the contract rejected the mint (see reason)"),
]


def hint_for(text):
    """Short plain-English hint for an rpc or revert message. None when nothing matches."""
    t = (text or "").lower()
    for needle, hint in HINTS:
        if needle in t:
            return hint
    return None


def _replay_data(rpc, tx, block_number):
    """eth_call the tx at the block before it. Returns (decoded_reason|None, raw_hex|None, msg|None, replay_ok)."""
    call = {"from": tx.get("from"), "to": tx["to"], "data": tx.get("input") or tx.get("data") or "0x",
            "value": hex(int(tx.get("value") or 0))}
    blk = hex(max(0, int(block_number) - 1))
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [call, blk]}).encode()
    req = urllib.request.Request(rpc.urls[0], data=body,
                                 headers={"Content-Type": "application/json", "User-Agent": "acobot/0.1"})
    with urllib.request.urlopen(req, timeout=5) as r:
        res = json.loads(r.read())
    err = res.get("error")
    if not err:
        return None, None, None, True  # replay succeeded: state differed at the tx's index
    data = err.get("data") if isinstance(err, dict) else None
    if isinstance(data, dict):
        data = data.get("data")
    msg = err.get("message") if isinstance(err, dict) else None
    raw = data if isinstance(data, str) and data.startswith("0x") else None
    reason = decode_revert(raw) if raw else None
    return reason, raw, msg, False


def revert_detail(rpc, tx, block_number):
    """Full revert picture for a mined, reverted tx. Never raises.

    Returns {reason, raw, node_message, replay, hint}. reason is the decoded revert
    (Error(string) / Panic / custom selector); raw is the revert data hex; node_message the
    rpc's own text; replay is one of ok|reverted|error|skipped.
    """
    out = {"reason": None, "raw": None, "node_message": None, "replay": "skipped", "hint": None}
    if not tx or not tx.get("to") or block_number is None:
        return out
    try:
        reason, raw, msg, ok = _replay_data(rpc, tx, block_number)
    except Exception as e:
        out["replay"] = "error"
        out["node_message"] = f"replay failed: {type(e).__name__}"
        return out
    out["raw"], out["node_message"] = raw, msg
    if ok:
        out["replay"] = "ok"
        out["reason"] = "replay succeeded at block %s (state changed inside that block)" % (int(block_number) - 1)
    else:
        out["replay"] = "reverted"
        out["reason"] = reason or (msg.replace("execution reverted: ", "").strip() if msg else None) or "reverted with no reason"
    out["hint"] = hint_for(out["reason"]) or hint_for(msg)
    return out


def revert_reason(rpc, tx, block_number):
    """Replay the tx with eth_call at the block before it. Returns decoded reason or None."""
    if not tx or not tx.get("to"):
        return None
    call = {"from": tx.get("from"), "to": tx["to"], "data": tx.get("input") or tx.get("data") or "0x",
            "value": hex(int(tx.get("value") or 0))}
    try:
        blk = hex(max(0, int(block_number) - 1))
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [call, blk]}).encode()
        req = urllib.request.Request(rpc.urls[0], data=body,
                                     headers={"Content-Type": "application/json", "User-Agent": "acobot/0.1"})
        with urllib.request.urlopen(req, timeout=5) as r:
            res = json.loads(r.read())
        err = res.get("error") or {}
        data = err.get("data") if isinstance(err, dict) else None
        if isinstance(data, dict):
            data = data.get("data")
        if isinstance(data, str):
            return decode_revert(data)
        msg = err.get("message") if isinstance(err, dict) else None
        if msg:
            return msg.replace("execution reverted: ", "").strip() or "reverted"
        return "reverted"
    except Exception:
        return None


def store_path(data_dir, mid):
    d = Path(data_dir) / "results"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{int(mid)}.json"


def save_result(data_dir, mid, record):
    p = store_path(data_dir, mid)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, default=str))
    os.replace(tmp, p)


def load_result(data_dir, mid):
    p = store_path(data_dir, mid)
    if not p.exists():
        return None
    return json.loads(p.read_text())


def list_result_ids(data_dir):
    d = Path(data_dir) / "results"
    if not d.exists():
        return []
    return sorted(int(p.stem) for p in d.glob("*.json") if p.stem.isdigit())


def first_hash(rpc_map):
    """The tx hash from a broadcast result: first 0x value (every accepting endpoint returns the same hash)."""
    for v in (rpc_map or {}).values():
        if isinstance(v, str) and v.startswith("0x") and len(v) == 66:
            return v
    return None


def _send_reason(rpc_map):
    if not isinstance(rpc_map, dict):
        return "unknown"
    e = rpc_map.get("error")
    if e:
        return str(e)
    for v in rpc_map.values():
        if isinstance(v, str) and v.startswith("ERR"):
            return "rpc rejected it (" + v[4:].strip() + ")"
    return "no rpc accepted it"


def track(read_rpc, chain_name, fire_res, timeout=RECEIPT_TIMEOUT_S, poll=RECEIPT_POLL_S,
          sleep=time.sleep, clock=time.time, usd_per_eth=None, feed=None):
    """Poll receipts for every broadcast tx of one fire. Returns one row per wallet.

    feed: optional HeadFeed. When connected, receipts are rechecked on every new header (live, no
    fixed poll sleep); otherwise the original poll interval.
    Row keys: label, address, sent_s (fire->broadcast returned), tx_hash, explorer,
    status (minted|failed|pending), receipt_s (fire->receipt first seen), block, block_ts,
    gas_eth, gas_usd, revert, error, plus the failure detail: reason (plain message),
    hint (short plain-English), rpc_error (rpc's own message verbatim, broadcast stage),
    revert_raw (revert data hex), tx_status (receipt status field, 0x0/0x1), replay.
    """
    t0 = fire_res.get("fired_at") or clock()
    rows, pending = [], {}
    for r in fire_res.get("results", []):
        h = first_hash(r.get("rpc"))
        row = {"label": r.get("label"), "address": r.get("address"), "sent_s": r.get("sent_s"),
               "tx_hash": h, "explorer": explorer_url(chain_name, h), "status": None,
               "receipt_s": None, "block": None, "block_ts": None, "gas_eth": None,
               "gas_usd": None, "revert": None, "error": None,
               "reason": None, "hint": None, "rpc_error": None, "revert_raw": None,
               "tx_status": None, "replay": None, "shot": r.get("shot"),
               "bot_ms": r.get("bot_ms") if r.get("bot_ms") is not None else (round((r.get("sent_s") or 0) * 1000, 1) if r.get("sent_s") is not None else None),
               "fire_at": r.get("fire_at") or t0 or clock()}
        if h:
            pending[h] = row
        else:
            row["rpc_error"] = _send_reason(r.get("rpc"))
            if row["shot"] in DUAL_SHOTS and is_same_nonce_reject(row["rpc_error"]):
                row["status"] = "superseded"          # expected: the other shot holds this nonce
                row["reason"] = SUPERSEDED_REASON
            else:
                row["status"] = "failed"
                row["error"] = row["rpc_error"]
                row["reason"] = row["rpc_error"]
                row["hint"] = hint_for(row["rpc_error"])
        rows.append(row)
    for sk in fire_res.get("skipped", []):
        rows.append({"label": sk.get("label"), "address": sk.get("address"), "sent_s": None,
                     "tx_hash": None, "explorer": None, "status": "failed", "receipt_s": None,
                     "block": None, "block_ts": None, "gas_eth": None, "gas_usd": None, "revert": None,
                     "error": "skipped: " + str(sk.get("error") or "not signed"),
                     "reason": "skipped before send: " + str(sk.get("error") or "not signed"),
                     "hint": hint_for(sk.get("error")), "rpc_error": None, "revert_raw": None,
                     "tx_status": None, "replay": None})
    end = clock() + timeout
    while pending and clock() < end:
        for h in list(pending):
            try:
                rc = read_rpc.call("eth_getTransactionReceipt", [h])
            except Exception:
                rc = None
            if not rc:
                continue
            row = pending.pop(h)
            row["receipt_s"] = round(clock() - row["fire_at"], 2)
            row["block"] = _h(rc.get("blockNumber"))
            row["block_ts"] = block_timestamp(read_rpc, rc.get("blockNumber")) if rc.get("blockNumber") else None
            row["tx_status"] = hex(_h(rc.get("status"))) if rc.get("status") is not None else None
            g = gas_cost_wei(rc)
            if g is not None:
                row["gas_eth"] = g / 1e18
                row["gas_usd"] = row["gas_eth"] * usd_per_eth if usd_per_eth else None
            if _h(rc.get("status")) == 1:
                row["status"] = "minted"
            else:
                row["status"] = "failed"
                try:
                    tx = read_rpc.call("eth_getTransactionByHash", [h])
                except Exception:
                    tx = None
                d = revert_detail(read_rpc, tx, row["block"]) if tx else {}
                row["revert"] = d.get("reason") or "reverted"
                row["reason"] = row["revert"]
                row["revert_raw"] = d.get("raw")
                row["replay"] = d.get("replay")
                row["rpc_error"] = d.get("node_message")
                row["hint"] = d.get("hint") or hint_for(row["revert"])
        _settle_same_nonce(rows, pending)
        if pending:
            if feed is not None and feed.available():
                feed.wait_next(timeout=poll)   # wake on the next header, or after poll as a backstop
            else:
                sleep(poll)
    for row in pending.values():
        row["status"] = "pending"
        row["error"] = f"no receipt after {int(timeout)}s"
    return rows


def summarize_rows(rows):
    # two-shot: several shot rows per wallet; count wallets and mints once per wallet
    wallets = collapse_wallets(rows) if rows else []
    n = len(wallets)
    minted = sum(1 for w in wallets if w.get("status") == "minted")
    usd = [r["gas_usd"] for r in rows if r.get("gas_usd") is not None]
    gas_missing = any(r.get("gas_eth") is not None and r.get("gas_usd") is None for r in rows)
    total_usd = None if (not usd or gas_missing) else sum(usd)
    lat = [r["receipt_s"] for r in rows if r.get("receipt_s") is not None]
    avg = round(sum(lat) / len(lat), 1) if lat else None
    return {"wallets": n, "minted": minted, "total_gas_usd": total_usd, "avg_fire_to_receipt_s": avg}


def collapse_wallets(rows):
    """One row per wallet from its shot rows. Gas and mint come only from txs that were included.

    Two-shot wallets: landed = the shot that minted; the other shot is superseded (same nonce, fee not spent).
    Each wallet row carries shots: {prefire|normal: {status, tx_hash, explorer, reason, sent_s, receipt_s, gas_eth, gas_usd}}.
    """
    order, groups = [], {}
    for r in rows:
        key = (r.get("address") or r.get("label") or "").lower()
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(r)
    out = []
    for key in order:
        g = groups[key]
        landed = [r for r in g if r.get("status") == "minted"]
        pend = [r for r in g if r.get("status") == "pending"]
        if landed:
            status, main = "minted", landed[0]
        elif pend:
            status, main = "pending", pend[0]
        else:
            status = "failed"
            main = next((r for r in g if r.get("status") == "failed"), g[0])
        included = [r for r in g if r.get("gas_eth") is not None]
        usd = [r["gas_usd"] for r in included if r.get("gas_usd") is not None]
        w = dict(main)
        w.update({
            "status": status,
            "gas_eth": sum(r["gas_eth"] for r in included) if included else None,
            "gas_usd": sum(usd) if included and len(usd) == len(included) else None,
            "landed_shot": landed[0].get("shot") if landed else None,
            "shots": {r["shot"]: {"status": r.get("status"), "tx_hash": r.get("tx_hash"), "explorer": r.get("explorer"),
                                  "reason": r.get("reason"), "sent_s": r.get("sent_s"), "receipt_s": r.get("receipt_s"),
                                  "bot_ms": r.get("bot_ms"),
                                  "gas_eth": r.get("gas_eth"), "gas_usd": r.get("gas_usd")}
                      for r in g if r.get("shot") in DUAL_SHOTS},
        })
        if status == "failed" and main.get("status") == "superseded":
            w["reason"] = "nonce already used by a tx that did not mint (not one of our shots)"
            w["hint"] = hint_for("nonce too low")
        out.append(w)
    return out
