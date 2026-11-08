"""Mint watcher: decides when a scheduled mint is live and fires once.

Live = block >= start_block_time (timestamp) OR saleStarted() true, and not paused.
Fire ONCE per mint; the signed raw tx is prepared ahead of time and broadcast.
"""
from dataclasses import dataclass, field
import time

@dataclass
class ScheduledMint:
    contract: str
    chain: str
    start_ts: int | None = None          # unix seconds, admin-provided or detected
    price_wei: int | None = None
    per_wallet: int | None = None
    sale_started_fn: str | None = None   # e.g. "saleStarted()" if detected
    signed_txs: list = field(default_factory=list)  # [(wallet_label, raw_tx_hex)]
    fired: bool = False

def is_live(mint, head_ts, sale_started_value=None):
    if mint.fired:
        return False
    if sale_started_value is True:
        return True
    if mint.start_ts is not None and head_ts >= mint.start_ts:
        return True
    return False

def poll_once(mint, rpc, broadcaster_rpc=None):
    """One poll tick. Returns list of broadcast results if fired, else []."""
    blk = rpc.call("eth_getBlockByNumber", ["latest", False])
    head_ts = int(blk["timestamp"], 16)
    started = None
    if mint.sale_started_fn:
        sel = mint.sale_started_fn
        try:
            started = int(rpc.eth_call(mint.contract, sel), 16) == 1
        except Exception:
            started = None
    if not is_live(mint, head_ts, started):
        return []
    mint.fired = True
    out = []
    for label, raw in mint.signed_txs:
        out.append((label, (broadcaster_rpc or rpc).broadcast(raw)))
    return out

def run(mint, rpc, interval=0.25, max_seconds=None):
    t0 = time.time()
    while not mint.fired:
        res = poll_once(mint, rpc)
        if res:
            return res
        if max_seconds and time.time() - t0 > max_seconds:
            return []
        time.sleep(interval)
