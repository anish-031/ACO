"""CA watcher: track X accounts and Telegram groups, extract contract addresses
from new posts/messages, and act on them: notify (manual) or auto-buy (auto).

Auto mode ALWAYS requires an explicit spend cap (in ETH) set with the target;
no cap, no auto-buy. Manual mode just pings the admin with the CA and a quick
/trade hint. Auto mode reuses the guarded buy flow: quote is still built by
tokenbuy.prepare_buy and broadcast through the same confirm-free-but-capped
path only for EVM buys; SOL auto is unsupported (jupiter quote needs fresh
keypair handling) and falls back to manual.
"""
import json
import os
import re
import threading
import time
import urllib.request

CONFIG_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ca_watchers.json")
EVM_RE = re.compile(r"0x[a-fA-Fa-f0-9]{40}")
SOL_RE = re.compile(r"(?<![0-9A-Za-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![0-9A-Za-z])")
MAX_AUTO_ETH = 0.05  # hard ceiling; config cap cannot exceed this


def load():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {"sources": [], "seen": []}


def save(data):
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)


KINDS = ("x", "dc", "tg", "group")  # x account, discord server/invite, telegram group/channel, group (legacy)


def add_source(kind, name, mode, cap_eth=0.0, chain="robinhood"):
    if kind not in KINDS:
        raise ValueError("kind must be x, dc (discord), tg (telegram group/channel) or group")
    if mode not in ("manual", "auto"):
        raise ValueError("mode must be manual or auto")
    if mode == "auto" and not (0 < float(cap_eth) <= MAX_AUTO_ETH):
        raise ValueError(f"auto needs a fund amount in eth between 0 and {MAX_AUTO_ETH}")
    data = load()
    for s in data["sources"]:
        if s["kind"] == kind and s["name"].lower() == name.lower():
            s["mode"], s["cap_eth"], s["chain"] = mode, float(cap_eth), chain
            save(data)
            return s
    src = {"kind": kind, "name": name, "mode": mode, "cap_eth": float(cap_eth), "chain": chain}
    data["sources"].append(src)
    save(data)
    return src


def remove_source(name):
    data = load()
    before = len(data["sources"])
    data["sources"] = [s for s in data["sources"] if s["name"].lower() != name.lower()]
    save(data)
    return len(data["sources"]) < before


def list_sources():
    return load()["sources"]


def extract_cas(text):
    """Contract addresses in a post/message, cleaned of casework noise."""
    if not text:
        return [], []
    evm = []
    for m in EVM_RE.findall(text):
        if m != m.lower() or True:  # keep original casing; checksum not required for matching
            evm.append(m)
    sol = [m for m in SOL_RE.findall(text)
           if not m.startswith("0x") and not EVM_RE.fullmatch(m)
           and "http" not in m.lower() and "." not in m]
    return list(dict.fromkeys(evm)), list(dict.fromkeys(sol))


def seen_key(kind, name, ca):
    return f"{kind}:{name.lower()}:{ca.lower()}"


def mark_seen(kind, name, ca):
    data = load()
    key = seen_key(kind, name, ca)
    if key not in data["seen"]:
        data["seen"].append(key)
        data["seen"] = data["seen"][-500:]
        save(data)
        return True
    return False


def is_new(kind, name, ca):
    return seen_key(kind, name, ca) not in load()["seen"]


def cap_ok(src, amount_eth):
    cap = float(src.get("cap_eth") or 0)
    return 0 < cap <= MAX_AUTO_ETH and 0 < float(amount_eth) <= cap
