"""Read-only intel for the ACO brain: last-fire speed stats and public mint listings.

speed_stats(data_dir): averages from saved results (bot_ms, fire->broadcast, fire->receipt).
public_mints(fetch): current/upcoming public mints from nftcalendar, robin-xp, opensea, with price/window/limit.
Never guesses: fields that a source did not give are None.
"""
import json, re, statistics
from pathlib import Path

from acobot import results as resmod


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def speed_stats(data_dir, limit=20):
    """Per-recent-mint timing from results records. Returns {mints:[...], averages:{...}}."""
    ids = resmod.list_result_ids(data_dir)[-limit:]
    per_mint, bot_ms, sent, receipt = [], [], [], []
    for mid in ids:
        rec = resmod.load_result(data_dir, mid) or {}
        rows = rec.get("rows") or []
        b = [_num(r.get("bot_ms")) for r in rows]
        s = [_num(r.get("sent_s")) for r in rows]
        rc = [_num(r.get("receipt_s")) for r in rows]
        b = [x for x in b if x is not None]
        s = [x for x in s if x is not None]
        rc = [x for x in rc if x is not None]
        bot_ms += b
        sent += s
        receipt += rc
        per_mint.append({
            "id": mid, "chain": rec.get("chain"),
            "bot_ms_avg": round(statistics.mean(b), 1) if b else None,
            "fire_to_broadcast_avg_s": round(statistics.mean(s), 3) if s else None,
            "fire_to_receipt_avg_s": round(statistics.mean(rc), 2) if rc else None,
            "fire_to_receipt_min_s": round(min(rc), 2) if rc else None,
            "wallets": len({(r.get("address") or r.get("label") or "") for r in rows}),
        })
    def avg(xs, nd):
        return round(statistics.mean(xs), nd) if xs else None
    return {
        "mints": per_mint,
        "averages": {"bot_ms": avg(bot_ms, 1), "fire_to_broadcast_s": avg(sent, 3),
                     "fire_to_receipt_s": avg(receipt, 2)},
        "samples": {"bot_ms": len(bot_ms), "fire_to_broadcast_s": len(sent), "fire_to_receipt_s": len(receipt)},
    }


def format_speed(stats):
    if not stats.get("mints"):
        return "no fire results saved yet, so no speed numbers to show."
    a = stats["averages"]
    lines = []
    if a["bot_ms"] is not None:
        lines.append(f"bot time avg {a['bot_ms']} ms over {stats['samples']['bot_ms']} sends")
    if a["fire_to_broadcast_s"] is not None:
        lines.append(f"fire to broadcast avg {a['fire_to_broadcast_s']} s")
    if a["fire_to_receipt_s"] is not None:
        lines.append(f"fire to receipt avg {a['fire_to_receipt_s']} s (chain block time is the floor)")
    return "; ".join(lines) or "results saved but no timing fields recorded."


# ---- public mints -----------------------------------------------------------

PUBLIC_SOURCES = [
    ("nftcalendar", "https://nftcalendar.io/events/"),
    ("robin-xp", "https://robin-xp.com/upcoming-mints"),
]


def parse_listing(html, source):
    """Pull (name, price, window, limit, url) rows from a listing page. Missing fields stay None."""
    out = []
    for m in re.finditer(r'<a[^>]+href="(https?://[^"]*|/[^"]*)"[^>]*>([^<]{3,80})</a>', html or ""):
        href, title = m.group(1), m.group(2).strip()
        if "mint" in href.lower() or "event" in href.lower() or "nft" in title.lower():
            out.append({"name": title, "url": href if href.startswith("http") else None,
                        "source": source, "price": None, "window": None, "limit": None})
    return out


def public_mints(fetch):
    """fetch(url)->html str or raises. Returns {mints:[...], errors:[...]}. Never invents fields."""
    mints, errors = [], []
    for source, url in PUBLIC_SOURCES:
        try:
            html = fetch(url)
        except Exception as e:
            errors.append(f"{source}: {type(e).__name__}")
            continue
        mints.extend(parse_listing(html, source))
    seen, uniq = set(), []
    for m in mints:
        k = m["name"].lower()
        if k not in seen:
            seen.add(k)
            uniq.append(m)
    return {"mints": uniq[:10], "errors": errors}


def format_mints(res):
    if not res.get("mints"):
        extra = (" sources failing: " + ", ".join(res["errors"])) if res.get("errors") else ""
        return "no public mints found on the sources i could read." + extra
    lines = []
    for m in res["mints"]:
        lines.append(f"{m['name']} ({m['source']})"
                     f"{' price ' + m['price'] if m.get('price') else ''}"
                     f"{' window ' + m['window'] if m.get('window') else ''}"
                     f"{' limit ' + str(m['limit']) if m.get('limit') else ''}"
                     f"{' ' + m['url'] if m.get('url') else ''}")
    if res.get("errors"):
        lines.append("sources failing: " + ", ".join(res["errors"]))
    return "\n".join(lines)
