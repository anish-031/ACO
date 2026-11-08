"""Gas spike guard: compare current gas with the chain's recent baseline before a scheduled fire time.

Uses eth_feeHistory (last N blocks, 20th/50th percentile tips) through the chain's RPC list. The
baseline is the median base fee over the window. Warn only when the current base fee is clearly above
it (ratio threshold) AND the absolute cost of a typical mint is material in USD. Flat/cheap chains
(e.g. robinhood at ~0.1 gwei) are reported as normal, never as a spike.
"""
import statistics
from acobot.rpc import RPC

WINDOW_BLOCKS = 60
SPIKE_RATIO = 2.0          # current base fee / recent median
MIN_USD_SPIKE = 1.0        # ignore ratio spikes that cost less than this per mint (cheap chain noise)
TYPICAL_GAS = 110_000      # rough mint gas used for the USD estimate (warn-only)


def _h(x):
    return int(x, 16) if isinstance(x, str) else int(x)


def analyze(fee_history, current_base_wei=None):
    """Pure function. fee_history: eth_feeHistory result dict. Returns a verdict dict."""
    bases = [_h(b) for b in (fee_history.get("baseFeePerGas") or []) if b is not None]
    if len(bases) < 5:
        return {"state": "unknown", "current_gwei": None, "baseline_gwei": None, "ratio": None}
    current = current_base_wei if current_base_wei is not None else bases[-1]
    past = bases[:-1]
    baseline = statistics.median(past) or 0
    ratio = (current / baseline) if baseline else None
    return {"state": "ok", "current_gwei": current / 1e9, "baseline_gwei": baseline / 1e9,
            "ratio": ratio, "trend_up": current > bases[-2] if len(bases) > 1 else False}


def verdict(chain_name, analysis, native_usd):
    """Turn analysis into a human verdict. native_usd: USD per native token or None."""
    if analysis["state"] != "ok":
        return {"level": "unknown", "line": "gas check unavailable right now"}
    cur = analysis["current_gwei"]
    usd_per_mint = (cur * 1e-9 * TYPICAL_GAS * native_usd) if native_usd else None
    usd_s = f"~${usd_per_mint:,.2f}" if usd_per_mint is not None and usd_per_mint >= 0.01 else (
        f"~${usd_per_mint:.4f}" if usd_per_mint is not None else "usd n/a")
    ratio = analysis.get("ratio")
    if ratio is None:
        return {"level": "ok", "line": f"gas flat, one mint about {usd_s}"}
    spiking = ratio >= SPIKE_RATIO and (usd_per_mint or 0) >= MIN_USD_SPIKE
    if spiking:
        return {"level": "warn", "line": (f"gas is {ratio:.1f}x its recent baseline, one mint about {usd_s}"
                                          + (" and rising" if analysis.get("trend_up") else ""))}
    if ratio >= SPIKE_RATIO:
        return {"level": "ok", "line": f"gas up {ratio:.1f}x vs baseline but cheap ({usd_s} per mint)"}
    return {"level": "ok", "line": f"gas normal ({ratio:.2f}x baseline), one mint about {usd_s}"}


def check(rpc: RPC, chain_name, native_usd=None):
    """Live check. Never raises: returns the verdict dict with state 'unknown' on any rpc failure."""
    try:
        fh = rpc.call("eth_feeHistory", [hex(WINDOW_BLOCKS), "latest", [20, 50]])
    except Exception:
        return {"level": "unknown", "line": "gas check unavailable right now", "analysis": None}
    a = analyze(fh)
    v = verdict(chain_name, a, native_usd)
    v["analysis"] = a
    return v


def warning_text(mint_name, fire_ts_text, v):
    """Admin warning panel ahead of a drop. Plain, USD and gwei-free per panel rules."""
    return (f"<b>gas warning: {mint_name}</b>\n"
            f"opens {fire_ts_text}\n"
            f"{v['line']}\n"
            "the bot still pays whatever gas the chain asks at fire time.")
