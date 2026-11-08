"""Pre-sign mint txs for every wallet, then rebuild fees from live chain data and broadcast the moment the mint is live.

Speed tiers map to priority-fee multipliers on the chain's live market tip, with max fee sized from the live base fee.
Nothing here caps gas spend: gas limit is estimated from the chain at fire time (with buffer), fee tiers follow the live market,
and a short balance never blocks a send (an on-chain failure is reported in results instead).
Keys are decrypted only inside presign() and passed straight to the signer; never logged or displayed.
"""
import logging
log = logging.getLogger(__name__)
import re, time, concurrent.futures as cf
from eth_account import Account
from eth_utils import keccak
from eth_utils import to_checksum_address as _cs
from acobot import scheduler
from acobot.rpc import err_text
from acobot import mint_engine

PRESIGN_LEAD_S = 60
AUTO_FALLBACK_GAS = 1_000_000  # fire time: chain cannot estimate (e.g. revert) -> send with this high limit; you pay only gas used
AUTO_BUFFER = 1.3
POLL_INTERVAL_S = 0.05
SALE_PROBE_INTERVAL_S = 0.2

# Pre-sale gas estimate (panel + precheck only; exact gas is estimated at fire time).
# Basis: etherscan SeaDrop mintPublic txs (1 token: ~97k-100k gas used, 2025-2026) and ERC721A
# benchmarks (~57k raw mint for 1 token, ~+2.1k per extra token, plus contract-specific overhead).
# (base gas for qty 1, extra gas per additional token)
STATIC_GAS = {
    "seadrop": (100_000, 3_000),
    "erc721a": (90_000, 2_500),   # quantity-arg batch mint
    "erc721": (120_000, 25_000),  # no-arg / non-batch mint
}
STATIC_GAS_UNKNOWN = 150_000
MINT_SIGNATURE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\((?:uint256)?\)")


def static_gas(mint, qty):
    """Pre-sale estimate for the panel: research-based per contract type, not exact."""
    qty = max(1, int(qty or 1))
    if mint.get("seadrop"):
        kind = "seadrop"
    elif (mint.get("mint_sig") or "").endswith("()"):
        kind = "erc721"
    elif mint.get("mint_sig"):
        kind = "erc721a"
    else:
        return STATIC_GAS_UNKNOWN
    base, extra = STATIC_GAS[kind]
    return base + extra * (qty - 1)


# key: (label, priority-fee multiplier on market tip, base-fee multiplier for max fee)
# max fee is only the protocol's authorization ceiling (EIP-1559 needs maxFee >= base fee to land).
# you pay base + priority actually used, never the max. multipliers are high so the max is never the limit.
SPEEDS = {
    "slow": ("Slow", 1.0, 4.0),
    "medium": ("Medium", 1.5, 6.0),
    "fast": ("Fast", 2.5, 8.0),
    "superfast": ("Super Fast", 6.0, 12.0),
}


def encode_mint(sig, qty):
    if not isinstance(sig, str) or not MINT_SIGNATURE_RE.fullmatch(sig):
        raise ValueError("mint signature must take no arguments or one uint256 quantity")
    if isinstance(qty, bool) or not isinstance(qty, int) or not 1 <= qty < 2**256:
        raise ValueError("mint quantity must be a positive uint256")
    sel = keccak(text=sig)[:4]
    if sig.endswith("()"):
        return "0x" + sel.hex()
    return "0x" + sel.hex() + format(qty, "064x")


def encode_seadrop_mint(nft, fee_recipient, minter, qty):
    """OpenSea SeaDrop mintPublic(nft, feeRecipient, minter, quantity)."""
    sel = keccak(text="mintPublic(address,address,address,uint256)")[:4]
    return "0x" + sel.hex() + nft[2:].lower().rjust(64, "0") + fee_recipient[2:].lower().rjust(64, "0") + minter[2:].lower().rjust(64, "0") + format(qty, "064x")


def market_fees(rpc, chain):
    """(base_fee_wei, market_tip_wei, floor_tip_wei) from the chain at this moment."""
    blk = rpc.call("eth_getBlockByNumber", ["latest", False])
    base = int((blk or {}).get("baseFeePerGas") or "0x0", 16)
    if base == 0:
        base = int(rpc.call("eth_gasPrice", []), 16)
    try:
        tip = int(rpc.call("eth_maxPriorityFeePerGas", []), 16)
    except Exception:
        tip = int(chain.priority_fee_gwei * 1e9)
    floor = int(chain.priority_fee_gwei * 1e9)
    return base, tip, floor


def tier_fees(base, tip, floor, speed):
    """(priority_fee_wei, max_fee_wei) for a speed tier. No config ceiling."""
    _, tm, bm = SPEEDS[speed]
    prio = max(int(tip * tm), int(floor * tm))
    return prio, int(base * bm) + prio


def fee_table(rpc, chain, gas=STATIC_GAS_UNKNOWN):
    """Live tier table. gas: the pre-sale estimate used for the cost preview only."""
    base, tip, floor = market_fees(rpc, chain)
    rows = {}
    for k, (label, _, _) in SPEEDS.items():
        prio, mx = tier_fees(base, tip, floor, k)
        rows[k] = {"label": label, "prio_gwei": prio / 1e9, "max_gwei": mx / 1e9,
                   "cost_eth": gas * min(mx, base + prio) / 1e18}
    return {"base_gwei": base / 1e9, "tip_gwei": tip / 1e9, "rows": rows}


def resolve_gas(rpc, addr, to, value, data, setting):
    """Fire-time gas limit: live estimate x buffer. If the estimate fails, a high safe limit.
    A user-typed number is a floor (never a cap): the limit is max(that number, the estimate)."""
    floor = int(setting) if isinstance(setting, int) and setting > 0 else 0
    try:
        est = int(rpc.call("eth_estimateGas", [{"from": addr, "to": to, "value": hex(value), "data": data}]), 16)
        return max(21_000, int(est * AUTO_BUFFER), floor)
    except Exception:
        return max(AUTO_FALLBACK_GAS, floor)


BUMP = 0.12   # two-shot: the normal shot pays 12% more maxFee and priority than the pre-fire shot (same nonce)
DUAL = "dual"          # pre-fire shot + bumped normal shot, same nonce
PREFIRE = "prefire"    # only the pre-fire shot (one block before the open, ws path)
NORMAL = "normal"      # only the normal shot, at open, no early shot
MODES = (DUAL, PREFIRE, NORMAL)


def fire_mode(mint):
    """The mint's fire mode, or None for records that predate the picker (legacy path)."""
    m = (mint or {}).get("fire_mode")
    return m if m in MODES else None


def is_dual(mint):
    """Two-shot mode (pre-fire + normal, same nonce). Records without fire_mode stay single."""
    return fire_mode(mint) == DUAL


def bump_fees(prio, mx, pct=BUMP):
    """Strictly above (1+pct) so EIP-1559 replacement rules (>=10% on both fields) always accept the replacement."""
    return int(prio * (1 + pct)) + 1, int(mx * (1 + pct)) + 1


def _raw(e, chain, prio, mx, gas):
    """Local CPU only: sign one tx from an entry with explicit fees and gas. No rpc."""
    tx = {"type": 2, "chainId": chain.chain_id, "nonce": e["nonce"], "to": _cs(e["to"]),
          "value": e["value"], "data": e["data"], "gas": gas,
          "maxFeePerGas": mx, "maxPriorityFeePerGas": prio}
    raw = "0x" + Account.sign_transaction(tx, e["priv"]).raw_transaction.hex().removeprefix("0x")
    return raw, {"gas": gas, "max_fee_gwei": mx / 1e9, "prio_gwei": prio / 1e9, "prio_wei": prio, "max_wei": mx}


def _sign(e, chain, speed, base, tip, floor, gas):
    """Local CPU only: one normal-tier tx."""
    prio, mx = tier_fees(base, tip, floor, speed)
    return _raw(e, chain, prio, mx, gas)


def _sign_pair(e, chain, speed, base, tip, floor, gas):
    """Two-shot: same nonce. Returns (pre_raw, pre_info, raw, info); raw is the bumped normal shot."""
    prio, mx = tier_fees(base, tip, floor, speed)
    pre_raw, pre_info = _raw(e, chain, prio, mx, gas)
    bp, bm = bump_fees(prio, mx)
    raw, info = _raw(e, chain, bp, bm, gas)
    return pre_raw, pre_info, raw, info


def _build(e, chain, rpc, speed, gas_setting, dual=False):
    """Live fees + gas, then sign. Returns (raw, info, pre_raw, pre_info); pre_* are None unless dual."""
    base, tip, floor = market_fees(rpc, chain)
    gas = resolve_gas(rpc, e["address"], e["to"], e["value"], e["data"], gas_setting)
    # no balance gate: a short balance is sent anyway and fails on-chain, reported in results
    if dual:
        pre_raw, pre_info, raw, info = _sign_pair(e, chain, speed, base, tip, floor, gas)
        return raw, info, pre_raw, pre_info
    raw, info = _sign(e, chain, speed, base, tip, floor, gas)
    return raw, info, None, None


def lift_over_sent(t, sent, chain):
    """The normal shot must out-bid the pre-fire shot that actually went out (same nonce).

    Re-signs locally, with the same key and nonce, only when the normal fees are not already >= (1+BUMP) x sent.
    """
    if not sent or not t.get("priv") or not t.get("info"):
        return t
    cur = t["info"]
    need_p = int(sent["prio_wei"] * (1 + BUMP)) + 1
    need_m = int(sent["max_wei"] * (1 + BUMP)) + 1
    if cur["prio_wei"] >= need_p and cur["max_wei"] >= need_m:
        return t
    raw, info = _raw(t, chain, max(cur["prio_wei"], need_p), max(cur["max_wei"], need_m), cur["gas"])
    return {**t, "raw": raw, "info": info}


def presign(mint, chain, rpc, entries):
    """entries: [(label, address, priv_hex, qty)]. Returns [{label, address, raw|None, error|None, ...}].

    Entries keep the key in memory (never logged) so fire-time can re-size fees from live data.
    """
    price = mint.get("price_wei") or 0
    speed = mint.get("speed", "fast")
    gas_setting = mint.get("gas_limit")
    # Read shared fee data once; nonce and gas estimation are independent per-wallet reads.
    try:
        base, tip, floor = market_fees(rpc, chain)
    except Exception as ex:
        return [{"label": label, "address": addr, "raw": None, "priv": None,
                 "error": f"{type(ex).__name__}: {ex}"} for label, addr, _priv, _qty in entries]

    def build_one(item):
        label, addr, priv, qty = item
        qty = max(1, int(qty))
        if not mint.get("seadrop") and (mint.get("mint_sig") or "").endswith("()"):
            qty = 1
        if mint.get("per_wallet"):
            qty = min(qty, mint["per_wallet"])
        try:
            api_tx = (mint.get("_api_calldata") or {}).get(addr.lower())
            to, data, value_override = mint_engine.build_transaction(
                chain.name, mint, addr, qty)
            value = price * qty if value_override is None else value_override
            # These calls can run together, and different wallets can be prepared together too.
            with cf.ThreadPoolExecutor(max_workers=2) as pair:
                nonce_f = pair.submit(rpc.call, "eth_getTransactionCount", [addr, "pending"])
                gas_f = pair.submit(resolve_gas, rpc, addr, to, value, data, gas_setting)
                nonce = int(nonce_f.result(), 16)
                gas = gas_f.result()
            entry = {"label": label, "address": addr, "priv": priv, "nonce": nonce,
                     "to": to, "value": value, "data": data, "qty": qty}
            if isinstance(api_tx, dict):
                entry["authorization_expires_at"] = api_tx.get("authorization_expires_at")
            if is_dual(mint):
                pre_raw, pre_info, raw, info = _sign_pair(entry, chain, speed, base, tip, floor, gas)
            else:
                raw, info = _sign(entry, chain, speed, base, tip, floor, gas)
                pre_raw = pre_info = None
            entry.update({"raw": raw, "error": None, "info": info,
                          "pre_raw": pre_raw, "pre_info": pre_info})
            return entry
        except Exception as ex:
            return {"label": label, "address": addr, "raw": None, "priv": None,
                    "error": f"{type(ex).__name__}: {ex}"}

    with cf.ThreadPoolExecutor(max_workers=min(20, max(1, len(entries)))) as pool:
        return list(pool.map(build_one, entries))


def refresh(txs, mint, chain, rpc):
    """Re-sign every presigned entry with fresh fees and gas. All rpc lives here (background), never at fire.

    Market fees are read once per call; gas is re-estimated per wallet with the same auto-fallback as presign.
    """
    speed = mint.get("speed", "fast")
    gas_setting = mint.get("gas_limit")
    try:
        base, tip, floor = market_fees(rpc, chain)
    except Exception as ex:
        return [{**t, "error": f"{type(ex).__name__}: {ex}"} if t.get("priv") else t for t in txs]
    def refresh_one(t):
        if not t.get("priv"):
            return t
        try:
            gas = resolve_gas(rpc, t["address"], t["to"], t["value"], t["data"], gas_setting)
            if is_dual(mint):
                pre_raw, pre_info, raw, info = _sign_pair(t, chain, speed, base, tip, floor, gas)
                return {**t, "raw": raw, "error": None, "info": info,
                        "pre_raw": pre_raw, "pre_info": pre_info}
            raw, info = _sign(t, chain, speed, base, tip, floor, gas)
            return {**t, "raw": raw, "error": None, "info": info}
        except Exception as ex:
            return {**t, "raw": None, "error": f"{type(ex).__name__}: {ex}"}

    with cf.ThreadPoolExecutor(max_workers=min(20, max(1, len(txs)))) as pool:
        return list(pool.map(refresh_one, txs))


REFRESH_WINDOW_S = 10     # background fee/gas refresh starts this many seconds before the mint opens
REFRESH_EVERY_S = 2       # ...and repeats every this many seconds (until fire)
AUTH_RETRY_INTERVAL_S = 5


class FeeLoop:
    """Background fee+gas refresher. Holds the latest signed set; fire() reads it with zero rpc.

    Starts at (start_ts - REFRESH_WINDOW_S), re-signs every REFRESH_EVERY_S, stops when stop() is called.
    snapshot() is a lock + list read, nothing else.
    """

    def __init__(self, base_txs, mint, chain, rpc):
        import threading
        self.txs = base_txs
        self.mint, self.chain, self.rpc = mint, chain, rpc
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.ticks = 0
        self.last_error = None
        self._th = threading.Thread(target=self._loop, daemon=True, name="fee-refresh")

    def start(self):
        self._th.start(); return self

    def stop(self):
        self._stop.set()

    def snapshot(self):
        with self._lock:
            return self.txs

    def _loop(self):
        start = self.mint.get("start_ts")
        if start:
            # wait until the window opens (wake early if stopped); chunked so far-future starts never overflow
            while not self._stop.is_set():
                left = start - REFRESH_WINDOW_S - time.time()
                if left <= 0:
                    break
                self._stop.wait(min(left, 60.0))
        while not self._stop.is_set():
            try:
                new = refresh(self.snapshot(), self.mint, self.chain, self.rpc)
                with self._lock:
                    self.txs = new
                self.ticks += 1
            except Exception as ex:  # never let the refresher kill the fire path; old signed set stays valid
                self.last_error = f"{type(ex).__name__}: {ex}"
            self._stop.wait(REFRESH_EVERY_S)


def _sale_started(mint, rpc):
    if not mint.get("sale_started_fn"):
        return False
    try:
        return int(rpc.eth_call(mint.get("contract"), "0x" + keccak(text=mint["sale_started_fn"])[:4].hex()), 16) == 1
    except Exception:
        return False


def _probe_sale_started(mint, rpc, now):
    if now < mint.get("_next_sale_probe_at", 0):
        return False
    mint["_next_sale_probe_at"] = now + SALE_PROBE_INTERVAL_S
    return _sale_started(mint, rpc)


def _ready(mint, rpc, now, head=None, prefire=False):
    """Should we fire now? head: the newest header (ws path) or None (polling path).

    Normal: sale live (saleStarted() or wall clock >= start_ts).
    Pre-fire: the NEXT block is expected at/after start_ts, so a tx broadcast now lands in the block
    where the mint opens. Pre-fire is only used on the ws path (needs a live head clock).
    """
    if prefire and head is not None and mint.get("start_ts"):
        block_time = mint.get("_block_time_s") or 1.0
        return head["timestamp"] + block_time >= mint["start_ts"]
    if mint.get("authorization_at_fire") and head is None and mint.get("start_ts"):
        if now < mint.get("_next_chain_time_probe", 0):
            return mint.get("_cached_chain_time", 0) >= mint["start_ts"]
        mint["_next_chain_time_probe"] = now + SALE_PROBE_INTERVAL_S
        try:
            latest = rpc.call("eth_getBlockByNumber", ["latest", False])
            mint["_cached_chain_time"] = int(latest["timestamp"], 16)
        except Exception:
            return False
        return (mint["_cached_chain_time"] >= mint["start_ts"]
                or _probe_sale_started(mint, rpc, now))
    if head is not None:
        # An on-chain early open is authoritative, so check it on each new header before start_ts.
        if mint.get("start_ts") and head["timestamp"] < mint["start_ts"]:
            if _probe_sale_started(mint, rpc, now):
                return True
            return False
        if mint.get("start_ts"):
            return True
        return _probe_sale_started(mint, rpc, now)
    if mint.get("start_ts") and now >= mint["start_ts"]:
        return True
    return _probe_sale_started(mint, rpc, now)


def _expired(mint, now, head=None):
    end_ts = mint.get("end_ts")
    current_time = head["timestamp"] if head is not None else now
    return bool(end_ts and current_time >= end_ts)


def _entries(mint):
    """The presign entries builder the bot attached to this mint. Missing -> a readable error, not a KeyError."""
    fn = mint.get("_entries_fn")
    if fn is None:
        raise RuntimeError("mint has no wallet entries builder (watcher not wired)")
    return fn


def presign_lead(mint):
    """Gated authorizations are fetched and simulated only when the phase opens."""
    return 0 if mint.get("authorization_at_fire") else PRESIGN_LEAD_S


def _prefire_due(mint, head):
    """Two-shot trigger: the next block (head timestamp + block time) is at/after the open, so a tx sent now lands in the block the mint opens."""
    if not head or not mint.get("start_ts"):
        return False
    return head["timestamp"] + (mint.get("_block_time_s") or 1.0) >= mint["start_ts"]


def _mark_fire_started(mint, status_cb, shot):
    already_started = mint.get("fire_started", False)
    mint["fire_started"] = True
    if status_cb and not already_started:
        status_cb("firing", {"shot": shot})


def _fire_pre(txs, bcast, pre_state, mint, status_cb=None):
    """Two-shot pre-fire: broadcast the normal-tier shot (pre_raw) for every wallet, once. Records the fees it sent."""
    _mark_fire_started(mint, status_cb, "prefire")
    shots = [{**t, "raw": t["pre_raw"]} for t in txs if t.get("pre_raw")]
    pre_state["res"] = fire(shots, bcast, None, shot="prefire")
    pre_state["sent"] = {(t.get("address") or "").lower(): t["pre_info"] for t in shots}
    return pre_state["res"]


def _fire_normal(txs, bcast, chain, pre_state, status_cb=None):
    """Normal shot: bumped fees, same nonce. Merged with the pre-fire result so each wallet shows both shots."""
    sent = pre_state.get("sent") or {}
    normal = [lift_over_sent(t, sent.get((t.get("address") or "").lower()), chain) for t in txs]
    nres = fire(normal, bcast, None, shot="normal")
    pres = pre_state.get("res") or {}
    results = list(pres.get("results", [])) + list(nres.get("results", []))
    stamps = [x for x in (pres.get("fired_at"), nres.get("fired_at")) if x]
    res = {"fired_at": min(stamps) if stamps else nres.get("fired_at"), "elapsed_s": nres.get("elapsed_s"),
           "bot_ms": nres.get("bot_ms"), "results": results, "skipped": nres.get("skipped", []),
           "two_shot": bool(pre_state.get("res"))}
    if status_cb:
        status_cb("fired", res)
    return res


def _retry_failed(res, txs, rpc, feed, status_cb=None):
    """If every broadcast errored (not 'already known'), re-send the SAME signed raw txs once on the next head.
    Same nonce + same bytes, so a retry can't mint twice. Returns the retry result, or the original."""
    if not RETRY_FAILED or feed is None or not feed.available():
        return res
    results = res.get("results") or []
    if not results or any(not (r.get("rpc") or {}).get("error") for r in results):
        return res
    head = feed.wait_next(timeout=15)
    if head is None:
        return res
    again = [t for t in txs if t.get("raw") and any(r.get("label") == t.get("label") for r in results)]
    if not again:
        return res
    log.info("mint broadcast failed on all wallets, retrying once on block %s", head)
    r2 = fire(again, rpc, None, shot="retry")
    r2["retry_of"] = res.get("fired_at")
    if status_cb:
        status_cb("fired", r2)
    return r2


def _no_transaction_reason(txs, mint):
    errors = [str(tx["error"]) for tx in txs if tx.get("error")]
    return "; ".join(errors[:3]) or mint.get("_zero_reason") or "no signed transactions were ready"


def _authorization_expired(txs, now):
    return any(
        isinstance(tx.get("authorization_expires_at"), (int, float))
        and tx["authorization_expires_at"] <= now
        for tx in txs
    )


def wait_and_fire(mint, chain, rpc, presigned_box, cancel_flag, status_cb=None, bcast=None, feed=None):
    """Blocking. presigned_box: dict to fill with presign results. Fires once when live.

    Single mode: the original path (ws header or 0.2s polling; prefire flag on the ws path).
    Two-shot (fire_mode == 'dual'): on the ws path the pre-fire shot goes out when the next block is due at/after the
    open; the normal shot (bumped fees, same nonce) goes out the moment the sale is live. Without ws both go at live.
    """
    mode = fire_mode(mint)
    dual = mode == DUAL
    pre_only = mode == PREFIRE
    prefire = bool(mint.get("prefire")) and mode is None   # legacy flag; the picker modes set their own path
    pre_state = {"sent": None, "res": None}
    loop = None
    next_auth_retry = 0.0
    auth_waiting = False
    try:
        while not cancel_flag():
            now = time.time()
            ws_live = bool(feed is not None and feed.available())
            head = None
            if ws_live:
                head = feed.wait_next(timeout=0.2)   # only act on a real new header
                if head is None:
                    continue
                if feed.block_time_s:
                    mint["_block_time_s"] = feed.block_time_s
                if _expired(mint, now, head):
                    mint["expired"] = True
                    if status_cb:
                        status_cb("expired", {"end_ts": mint["end_ts"]})
                    return None
                live = _ready(mint, rpc, now, head=head, prefire=prefire)
            else:
                if _expired(mint, now):
                    mint["expired"] = True
                    if status_cb:
                        status_cb("expired", {"end_ts": mint["end_ts"]})
                    return None
                live = _ready(mint, rpc, now)
            should_prepare = (presigned_box.get("txs") is None
                              and (not auth_waiting or now >= next_auth_retry))
            prepare_early = bool(
                not mint.get("authorization_at_fire") and mint.get("start_ts")
                and now >= mint["start_ts"] - presign_lead(mint))
            if should_prepare and (live or prepare_early):
                presigned_box["txs"] = _entries(mint)()
                if status_cb and presigned_box["txs"]:
                    status_cb("presigned", presigned_box["txs"])
                if mint.get("_authorization_pending"):
                    reason = mint.get("_zero_reason") or "wallet-specific phase authorization is unavailable"
                    if status_cb:
                        status_cb("waiting_authorization", {"reason": reason})
                    presigned_box["txs"] = None
                    auth_waiting = True
                    next_auth_retry = now + AUTH_RETRY_INTERVAL_S
                    time.sleep(POLL_INTERVAL_S)
                    continue
                auth_waiting = False
            if loop is None and mint.get("start_ts") and presigned_box.get("txs") is not None:
                loop = FeeLoop(presigned_box["txs"], mint, chain, rpc).start()
            prepared = presigned_box.get("txs")
            if prepared is not None and _authorization_expired(prepared, now):
                if dual and pre_state["res"] is not None:
                    if status_cb:
                        status_cb("fired", pre_state["res"])
                    return pre_state["res"]
                if loop is not None:
                    loop.stop()
                    loop = None
                presigned_box["txs"] = None
                auth_waiting = False
                next_auth_retry = 0.0
                continue
            if dual and pre_state["res"] is None and presigned_box.get("txs") is not None \
                    and (live or _prefire_due(mint, head)):
                txs = loop.snapshot() if loop is not None else presigned_box["txs"]
                if not any(tx.get("pre_raw") for tx in txs):
                    if status_cb:
                        status_cb("no_transactions", {
                            "reason": _no_transaction_reason(txs, mint),
                        })
                    return None
                _fire_pre(txs,
                          bcast or rpc, pre_state, mint, status_cb)
            if pre_only and presigned_box.get("txs") is not None and (live or _prefire_due(mint, head)):
                # pre-fire only: one shot (normal-tier fees, same as the dual pre-fire shot), then stop. No normal shot.
                if loop is not None:
                    loop.stop()
                txs = loop.snapshot() if loop is not None else presigned_box["txs"]
                if not any(tx.get("raw") for tx in txs):
                    if status_cb:
                        status_cb("no_transactions", {
                            "reason": _no_transaction_reason(txs, mint),
                        })
                    return None
                _mark_fire_started(mint, status_cb, "prefire")
                res = fire(txs, bcast or rpc, None, shot="prefire")
                if status_cb:
                    status_cb("fired", res)
                return res
            if live and not pre_only:
                if loop is not None and loop.ticks > 0:
                    loop.stop()
                    txs = loop.snapshot()    # freshest background-signed set; zero rpc before broadcast
                else:
                    # no background refresh has landed yet (ws head beat the first tick): refresh now, rare path
                    if loop is not None:
                        loop.stop()
                    txs = refresh(presigned_box.get("txs") or [], mint, chain, rpc)
                if not dual and not any(tx.get("raw") for tx in txs):
                    if status_cb:
                        status_cb("no_transactions", {
                            "reason": _no_transaction_reason(txs, mint),
                        })
                    return None
                _mark_fire_started(mint, status_cb, "normal")
                if dual:
                    return _fire_normal(txs, bcast or rpc, chain, pre_state, status_cb)
                res = fire(txs, bcast or rpc, status_cb)
                return _retry_failed(res, txs, bcast or rpc, feed, status_cb)
            if not ws_live:
                time.sleep(POLL_INTERVAL_S)
    finally:
        if loop is not None:
            loop.stop()
    return None


STAGGER_MS = 0          # per-wallet delay between broadcasts (0 = all at once, the original behavior)
RETRY_FAILED = True     # re-broadcast failed raw txs once on the next block


def fire(txs, rpc, status_cb=None, key="raw", shot=None, stagger_ms=None):
    """rpc: an RPC whose .broadcast() fans the raw tx out to all broadcast endpoints. key: which signed field to send.

    Bot time = fire callback start -> each broadcast returned (local clock, no chain time included).
    sent_s is the same measurement for the whole fan-out; bot_ms is the per-wallet figure. shot tags the rows.
    """
    ready = [t for t in txs if t.get(key)]
    wall0 = time.time()
    t0 = time.perf_counter()
    with cf.ThreadPoolExecutor(max_workers=max(1, len(ready))) as ex:
        gap = (STAGGER_MS if stagger_ms is None else stagger_ms) / 1000.0

        def _send(t, i):
            if gap and i:
                time.sleep(gap * i)          # staggered burst: wallet i waits i*gap
            out = rpc.broadcast(t[key])
            return out, round((time.perf_counter() - t0) * 1000, 1)
        futs = {ex.submit(_send, t, i): t for i, t in enumerate(ready)}
        results = []
        for f in cf.as_completed(futs):
            t = futs[f]
            try:
                out, bot_ms = f.result()
                results.append({"label": t["label"], "address": t.get("address"), "rpc": out,
                                "sent_s": round(bot_ms / 1000, 3), "bot_ms": bot_ms, "shot": shot, "fire_at": wall0})
            except Exception as e:
                ms = round((time.perf_counter() - t0) * 1000, 1)
                results.append({"label": t["label"], "address": t.get("address"), "rpc": {"error": err_text(e)},
                                "sent_s": round(ms / 1000, 3), "bot_ms": ms, "shot": shot, "fire_at": wall0})
    elapsed = time.perf_counter() - t0
    res = {"fired_at": time.time() - elapsed, "elapsed_s": round(elapsed, 3), "bot_ms": round(elapsed * 1000, 1), "results": results,
           "skipped": [{"label": t["label"], "address": t.get("address"), "error": t.get("error")} for t in txs if not t.get(key)]}
    if status_cb: status_cb("fired", res)
    return res
