"""Mint cost ledger and PnL vs current floor.

Every successful mint (a 'minted' receipt row) is recorded once: wallet, collection, qty, mint price
and the gas actually paid, both in ETH and USD at fire time. Stored in data/portfolio.json.

Unrealized PnL = (held tokens x current floor in native token x native USD now) - cost in USD (price + gas).
Floor comes from the Reservoir collections API (no key for the basic call). When unavailable the panel
says 'floor n/a' and shows cost only. Chains without a floor source are shown the same way.
"""
import json, time, urllib.request, logging
from pathlib import Path

log = logging.getLogger("acobot.portfolio")
DEFAULT_FILE = Path(__file__).resolve().parent.parent / "data" / "portfolio.json"
RESERVOIR_HOST = {"ethereum": "https://api.reservoir.tools"}   # robinhood: no floor source wired yet
UA = "acobot/0.1"
FLOOR_TTL = 300
_floor_cache = {}   # (chain, contract lower) -> (ts, floor_native)


def load(path=DEFAULT_FILE):
    p = Path(path)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text())
    except Exception:
        return []


def _save(path, rows):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, indent=1))
    tmp.replace(p)


def record_result(rec, price_wei, usd_per_eth, path=DEFAULT_FILE):
    """Append one ledger entry per minted wallet row of a saved result record. Idempotent per tx hash.

    rec: saved result record (id, contract, chain, name, qty, saved_at, rows).
    Returns the number of new entries written.
    """
    rows_in = load(path)
    seen = {e.get("tx") for e in rows_in}
    qty = int(rec.get("qty") or 1)
    added = 0
    for r in rec.get("rows") or []:
        if r.get("status") != "minted" or not r.get("tx_hash") or r["tx_hash"] in seen:
            continue
        price_eth = (price_wei or 0) * qty / 1e18
        gas_eth = float(r.get("gas_eth") or 0)
        usd = usd_per_eth or 0
        rows_in.append({
            "mint_id": rec.get("id"),
            "chain": rec.get("chain"),
            "contract": (rec.get("contract") or "").lower(),
            "name": rec.get("name"),
            "wallet": r.get("label") or "",
            "address": (r.get("address") or "").lower(),
            "qty": qty,
            "tx": r["tx_hash"],
            "price_eth": price_eth,
            "gas_eth": gas_eth,
            "cost_usd": round((price_eth + gas_eth) * usd, 4) if usd else None,
            "eth_usd_at_mint": usd or None,
            "ts": int(rec.get("saved_at") or time.time()),
        })
        seen.add(r["tx_hash"])
        added += 1
    if added:
        _save(path, rows_in)
    return added


def fetch_floor(chain, contract):
    """Current floor in native token for a collection. None when no source or the call fails."""
    if not contract:
        return None
    key = (chain, contract.lower())
    now = time.time()
    # keyless path first: cached contract->slug, then OpenSea slug stats (works without an API key)
    from acobot import opensea
    slug = opensea.cached_slug(chain, contract)
    if slug:
        floor = opensea.floor_stats(slug)
        if floor is not None:
            _floor_cache[key] = (now, floor)
            return floor
    host = RESERVOIR_HOST.get(chain)
    if not host:
        return None
    try:
        req = urllib.request.Request(f"{host}/collections/v7?contract={contract}",
                                     headers={"User-Agent": UA, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
        floor = float(data["collections"][0]["floorAsk"]["price"]["amount"]["native"])
    except Exception as e:
        log.warning("floor lookup failed: %s", type(e).__name__)
        return None
    _floor_cache[key] = (now, floor)
    return floor


def build_report(entries, floor_fn, usd_fn):
    """Group ledger entries by (wallet address, chain, contract) and value them.

    floor_fn(chain, contract) -> floor in native token or None. usd_fn(chain) -> native USD or None.
    Returns {"positions": [...], "total": {...}}. All money fields are USD (None when unknown).
    """
    groups = {}
    for e in entries:
        k = (e.get("address") or "", e.get("chain") or "", e.get("contract") or "")
        g = groups.setdefault(k, {"wallet": e.get("wallet") or "", "address": e.get("address") or "",
                                  "chain": e.get("chain"), "contract": e.get("contract"),
                                  "name": e.get("name"), "held": 0, "mints": 0, "cost_usd": 0.0,
                                  "cost_missing": False})
        g["held"] += int(e.get("qty") or 1)
        g["mints"] += 1
        if e.get("cost_usd") is None:
            g["cost_missing"] = True
        else:
            g["cost_usd"] += e["cost_usd"]
    usd_cache, floor_cache = {}, {}
    positions = []
    for g in groups.values():
        ch, ca = g["chain"], g["contract"]
        if ch not in usd_cache:
            usd_cache[ch] = usd_fn(ch)
        if (ch, ca) not in floor_cache:
            floor_cache[(ch, ca)] = floor_fn(ch, ca)
        usd, floor = usd_cache[ch], floor_cache[(ch, ca)]
        value = g["held"] * floor * usd if (floor is not None and usd) else None
        unreal = None if (value is None or g["cost_missing"]) else value - g["cost_usd"]
        g.update({"floor_eth": floor, "value_usd": value, "unrealized_usd": unreal})
        positions.append(g)
    total_cost = sum(p["cost_usd"] for p in positions)
    complete = bool(positions) and all(p["value_usd"] is not None and not p["cost_missing"] for p in positions)
    total_value = sum(p["value_usd"] for p in positions if p["value_usd"] is not None)
    total = {"cost_usd": total_cost, "value_usd": total_value,
             "unrealized_usd": (total_value - total_cost) if complete else None, "complete": complete}
    return {"positions": positions, "total": total}


def fmt_usd_signed(x):
    if x is None:
        return "n/a"
    sign = "+" if x >= 0 else "-"
    return f"{sign}${abs(x):,.2f}"


def panel_lines(report, mask):
    """Plain panel lines. mask(address) -> short address for display when no wallet label."""
    if not report["positions"]:
        return ["no mints logged yet. a successful mint is recorded here automatically."]
    lines = []
    for p in sorted(report["positions"], key=lambda x: (x["wallet"], x["name"] or "")):
        head = p["name"] or (p["contract"] or "")[:10]
        floor = f"{p['floor_eth']:.4f} ETH" if p["floor_eth"] is not None else "floor n/a"
        lines.append(f"{p['wallet'] or mask(p['address'])} · {head} ({p['chain']})")
        cost = "cost n/a" if p["cost_missing"] else f"cost ${p['cost_usd']:,.2f}"
        lines.append(f"  held {p['held']} · {cost} · floor {floor}")
        lines.append(f"  unrealized {fmt_usd_signed(p['unrealized_usd'])}")
    t = report["total"]
    lines.append("")
    lines.append(f"total cost ${t['cost_usd']:,.2f} · value ${t['value_usd']:,.2f}")
    lines.append(f"total unrealized {fmt_usd_signed(t['unrealized_usd'])}"
                 + ("" if t["complete"] else " (partial: some floors or costs missing)"))
    return lines
