"""Shared crew watchlist: tracked mints visible to everyone the bot talks to.

Stored in data/watchlist.json. Admin adds (DM or group). Anyone allowed sees the list.
Each entry: contract, chain, opens_ts (UTC unix seconds or None), phase label, who scheduled it,
status (watching / scheduled / live / done / cancelled), added_by, added_at.
"""
import json, time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_FILE = Path(__file__).resolve().parent.parent / "data" / "watchlist.json"
STATUSES = ("watching", "scheduled", "live", "done", "cancelled")


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


def add(rows, chain, contract, by, name=None, opens_ts=None, phase=None, scheduled_by=None):
    """Add or update an entry for (chain, contract). Returns (rows, entry)."""
    contract = contract.lower()
    for r in rows:
        if r["chain"] == chain and r["contract"] == contract:
            if name:
                r["name"] = name
            if opens_ts:
                r["opens_ts"] = opens_ts
            if phase:
                r["phase"] = phase
            if scheduled_by:
                r["scheduled_by"] = scheduled_by
            return rows, r
    entry = {"chain": chain, "contract": contract, "name": name, "opens_ts": opens_ts, "phase": phase,
             "scheduled_by": scheduled_by, "status": "watching", "added_by": by,
             "added_at": int(time.time())}
    rows.append(entry)
    return rows, entry


def set_status(rows, chain, contract, status):
    if status not in STATUSES:
        raise ValueError("bad status")
    for r in rows:
        if r["chain"] == chain and r["contract"] == contract.lower():
            r["status"] = status
            return True
    return False


def _when(ts):
    if not ts:
        return "time tbd"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%a %d %b %H:%M UTC")


def panel_lines(rows, now=None):
    """Watchlist panel. Sorted by open time (unknown last). Plain text, no raw ids beyond contract tail."""
    if not rows:
        return ["the watchlist is empty. admin can add a mint with /watchlist add <contract>."]
    now = now or time.time()
    order = sorted(rows, key=lambda r: (r.get("opens_ts") is None, r.get("opens_ts") or 0))
    out = []
    for r in order:
        head = r.get("name") or (r["contract"][:10] + "...")
        status = r.get("status", "watching")
        if r.get("opens_ts") and r["opens_ts"] < now and status in ("scheduled", "watching"):
            status = "open now" if status == "watching" else status
        out.append(f"{head} ({r['chain']})")
        out.append(f"  opens {_when(r.get('opens_ts'))} · phase {r.get('phase') or 'not set'}")
        out.append(f"  scheduled by {r.get('scheduled_by') or 'not scheduled'} · {status}")
    return out
