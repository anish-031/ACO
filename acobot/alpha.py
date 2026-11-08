"""Copy alpha: recent trades by tracked wallets, read from the sagetracer tracker db.

The wallet tracker (sagetracer) writes every alert into its `events` table. This module only READS that
file: sqlite opened read-only (mode=ro) with a busy timeout, so acobot never writes into the tracker db.
Pure helpers (cards, pnl, dedupe) take plain dicts so they can be unit tested without telegram or rpc.
"""
import html
import json
import re
import sqlite3
import time
import urllib.request
from pathlib import Path

TRACK_DB = Path.home() / ".folk" / "sagetracer" / "data" / "tracker.db"
RESERVOIR = {"ethereum": "https://api.reservoir.tools"}
UA = "acobot/0.1"
FLOOR_TTL = 300
LIST_LIMIT = 50
ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
ICON = {"MINTED": "🆕", "BOUGHT": "🟢", "SOLD": "🔴", "SENT": "📤", "RECEIVED": "📥"}
CHAIN_TAG = {"ethereum": "ETH", "robinhood": "ROBIN"}
EXPLORER_TX = {"ethereum": "https://etherscan.io/tx/{}", "robinhood": "https://robinhoodchain.blockscout.com/tx/{}"}
OPENSEA_ASSET = "https://opensea.io/assets/ethereum/{}/{}"
BLOCKSCOUT = "https://robinhoodchain.blockscout.com"
DIVIDER = "─" * 22

_floor_cache = {}     # (chain, ca lower) -> (ts, floor_native)


def read_events(limit=LIST_LIMIT, path=TRACK_DB):
    """Newest tracker events, read-only. Returns [] when the db is missing or unreadable (never raises)."""
    if not Path(path).exists():
        return []
    try:
        con = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        finally:
            con.close()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


def get_event(eid, path=TRACK_DB):
    if not str(eid).isdigit() or not Path(path).exists():
        return None
    try:
        con = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        try:
            row = con.execute("SELECT * FROM events WHERE id=?", (int(eid),)).fetchone()
        finally:
            con.close()
        return dict(row) if row else None
    except sqlite3.Error:
        return None


def valid_ca(ca):
    return isinstance(ca, str) and bool(ADDR_RE.match(ca))


def draft_contract(ca, chain, enabled_chains):
    """CA for the native addmint draft. None when the CA is invalid or the chain is not one this bot analyzes."""
    if chain not in enabled_chains or not valid_ca(ca):
        return None
    return ca


def _ids(e):
    return [x for x in (e.get("ids") or "").split(",") if x]


def eth_price(e):
    if not e.get("price_known") or e.get("price_wei") in (None, ""):
        return None
    return int(e["price_wei"]) / 1e18


def short(a):
    return f"{a[:6]}…{a[-4:]}" if a and len(a) > 12 else (a or "")


def utc_line(ts):
    """'2026-10-28 12:30 UTC (18:45 NPT)'. UTC first, Kathmandu alongside."""
    from datetime import datetime, timezone, timedelta
    t = datetime.fromtimestamp(int(ts), timezone.utc)
    npt = t + timedelta(hours=5, minutes=45)
    return f"{t:%Y-%m-%d %H:%M} UTC ({npt:%H:%M} NPT)"


def entry_label(e):
    """One tappable row: 🟢 bought craz · Vaultlings #483 · 0.0440 ETH"""
    coll = (e.get("coll") or short(e.get("ca") or ""))[:20]
    ids = _ids(e)
    tid = f"#{ids[0]}" if ids else ""
    p = eth_price(e)
    ps = f" · {p:.4f} ETH" if p is not None else ""
    return f"{ICON.get(e['kind'], '•')} {e['kind'].lower()} {e.get('wallet_name') or ''} · {coll} {tid}{ps}".strip()


def card(e, floor_native=None, usd=None):
    """Full card for one tracked trade: collection, copyable CA, token id, price ETH + USD, tx and marketplace links."""
    ids = _ids(e)
    tid = ids[0] if ids else ""
    coll = e.get("coll") or short(e.get("ca") or "")
    lines = [
        f"{ICON.get(e['kind'], '•')} <b>{html.escape(e['kind'])}</b> · {html.escape(e.get('wallet_name') or '')} · "
        f"<code>{CHAIN_TAG.get(e['chain'], e['chain'].upper())}</code>",
        DIVIDER,
        f"🖼 <b>{html.escape(coll)}</b>",
        f"🎫 token #{tid}" if tid else None,
    ]
    p = eth_price(e)
    if p is not None:
        usd_s = f"  ≈ <b>${p * usd:,.2f}</b> USD" if usd else ""
        lines.append(f"💰 <b>{p:.4f} ETH</b>{usd_s}")
    elif e["kind"] in ("MINTED", "BOUGHT", "SOLD"):
        lines.append("💰 free mint (0 ETH)" if e["kind"] == "MINTED" else "💰 price not visible on-chain")
    if floor_native is not None:
        lines.append(f"📈 floor now {floor_native:.4f} ETH")
    lines.append(f"🕒 {utc_line(e['ts'])}")
    ca = e.get("ca") or ""
    if ca:
        lines += ["📍 CA", f"<code>{ca}</code>", "<i>tap the address to copy</i>"]
    links = []
    if e["chain"] == "ethereum" and ca and tid:
        links.append(f'<a href="{OPENSEA_ASSET.format(ca, tid)}">opensea</a>')
    elif e["chain"] == "robinhood" and ca:
        links.append(f'<a href="{BLOCKSCOUT}/token/{ca}">blockscout</a>')
    if e.get("tx"):
        links.append(f'<a href="{EXPLORER_TX.get(e["chain"], "{}").format(e["tx"])}">tx</a>')
    if links:
        lines.append("🔗 " + " · ".join(links))
    return "\n".join(x for x in lines if x is not None)


def fetch_floor(chain, ca):
    """Current floor (native) from reservoir, ethereum only. None on any failure. Cached FLOOR_TTL seconds."""
    if not valid_ca(ca):
        return None
    key = (chain, ca.lower())
    hit = _floor_cache.get(key)
    now = time.time()
    if hit and now - hit[0] < FLOOR_TTL:
        return hit[1]
    try:
        from acobot import opensea
        slug = opensea.cached_slug(chain, ca)
        if slug:
            floor = opensea.floor_stats(slug)
            if floor is not None:
                _floor_cache[key] = (now, floor)
                return floor
    except Exception:
        pass
    if chain not in RESERVOIR:
        return hit[1] if hit else None
    try:
        req = urllib.request.Request(f"{RESERVOIR[chain]}/collections/v7?contract={ca}",
                                     headers={"User-Agent": UA, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
        floor = float(data["collections"][0]["floorAsk"]["price"]["amount"]["native"])
    except Exception:
        return hit[1] if hit else None
    _floor_cache[key] = (now, floor)
    return floor


def best_buys(events, floor_fn):
    """BOUGHT trades with a known price, pnl vs current floor, best first. Floor-less rows sort last.
    Only ethereum has a floor source (reservoir), so robinhood rows never get pnl."""
    out = []
    for e in events:
        if e["kind"] != "BOUGHT":
            continue
        p = eth_price(e)
        if p is None or p <= 0:
            continue
        floor = floor_fn(e["chain"], e["ca"]) if e["chain"] in RESERVOIR else None
        pnl = None if floor is None else (floor - p) / p * 100
        out.append((pnl if pnl is not None else float("-inf"), e, floor))
    out.sort(key=lambda x: x[0], reverse=True)
    return out


def upcoming_mints(events):
    """MINTED events, one per (chain, contract), newest first. Each is a contract the crew just minted."""
    out, seen = [], set()
    for e in events:
        if e["kind"] != "MINTED":
            continue
        k = (e["chain"], (e.get("ca") or "").lower())
        if k in seen:
            continue
        seen.add(k)
        out.append(e)
    return out
