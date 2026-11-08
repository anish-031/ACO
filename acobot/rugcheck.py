"""Rug check for /addmint: a short safety readout with plain lines, no raw error dumps.

Checks (all read-only eth_call, nothing signed or sent):
- owner: has an owner, renounced (zero address), or unknown
- proxy: upgradeable proxy (EIP-1167 minimal or otherwise upgradable by admin)
- admin mint roles: AccessControl DEFAULT_ADMIN_ROLE or MINTER_ROLE held by a non-zero address
- forcing functions: blacklist / blocklist / freeze / whitelist-gated transfer selectors in the bytecode
- honeypot: static only. A live simulated mint needs a funded burner, so it is reported as not run.

risk: low / medium / high with a short reason list. Never raises.
"""
import logging, time
from eth_utils import keccak
from acobot import abi, gasguard, opensea

log = logging.getLogger("acobot.rugcheck")
ZERO = "0x" + "00" * 32
MINTER_ROLE = "0x" + keccak(text="MINTER_ROLE").hex()
DEFAULT_ADMIN_ROLE = ZERO
FORCING_HINTS = ("blacklist", "blocklist", "freeze", "frozen", "whitelist", "allowlist", "setblocked", "banned")


def _word_addr(hexword):
    if not hexword or len(hexword) < 66:
        return None
    return "0x" + hexword[-40:]


def _is_zero(addr):
    return not addr or int(addr, 16) == 0


def _call(rpc, contract, sig, args_hex=""):
    try:
        return rpc.eth_call(contract, "0x" + abi.selector_of(sig) + args_hex)
    except Exception:
        return None


def _has_role(rpc, contract, role, holder_hex):
    res = _call(rpc, contract, "hasRole(bytes32,address)", role[2:] + holder_hex[2:].rjust(64, "0"))
    if not res or len(res) < 66:
        return None
    return int(res[-1], 16) == 1


def scan(rpc, contract, d):
    """d: the analysis dict from mintread.detect (sels/proxy/owner/names). Returns a readout dict."""
    reasons, flags = [], {}
    owner_raw = d.get("owner")
    owner = owner_raw if owner_raw else None
    if owner is None:
        flags["owner"] = "unknown"
    elif _is_zero(owner):
        flags["owner"] = "renounced"
    else:
        flags["owner"] = "held"
        reasons.append("owner not renounced: one address can change the contract")

    proxy = bool(d.get("proxy_impl"))
    flags["proxy"] = "upgradeable proxy" if proxy else "no"
    if proxy:
        reasons.append("proxy contract: implementation can be swapped")

    if owner and not _is_zero(owner):
        mint_admin = _has_role(rpc, contract, MINTER_ROLE, owner)
        admin = _has_role(rpc, contract, DEFAULT_ADMIN_ROLE, owner)
        if mint_admin or admin:
            flags["admin_roles"] = "owner holds admin or minter role"
            reasons.append("owner can mint or grant roles")
        else:
            flags["admin_roles"] = "none held by owner"
    else:
        flags["admin_roles"] = "unknown"

    names = " ".join(n.lower() for n in (d.get("selector_names") or []))
    hits = [h for h in FORCING_HINTS if h in names]
    flags["forcing"] = ("found: " + ", ".join(sorted(set(hits)))) if hits else "none found"
    if hits:
        reasons.append("contract can block or freeze wallets")

    flags["honeypot"] = "static check only (live simulation not run)"

    # Live SeaDrop sanity. Values are explicitly unknown when the chain read cannot establish them.
    cfg = {}
    if d.get("seadrop"):
        price = d.get("price_wei")
        start = d.get("start_ts")
        end = d.get("end_ts")
        per_wallet = d.get("per_wallet")
        supply, minted = d.get("max_supply"), d.get("minted")
        cfg = {"price": price, "start_ts": start, "end_ts": end, "per_wallet": per_wallet,
               "max_supply": supply, "minted": minted, "fee_recipient": d.get("fee_recipient")}
        if price is None:
            reasons.append("SeaDrop price unavailable from live read")
        if supply is None or minted is None:
            reasons.append("supply or minted count unavailable from collection getters")
        elif supply and minted > supply:
            reasons.append("minted count exceeds reported max supply")
        if start and end and end < start:
            reasons.append("SeaDrop end time precedes start time")
        if per_wallet is None:
            reasons.append("SeaDrop per-wallet limit unavailable")
        if not d.get("fee_recipient"):
            reasons.append("SeaDrop fee recipient unavailable or not allowlisted")
    else:
        reasons.append("no live SeaDrop public config detected; other phases may be custom")

    floor = None
    # Resolve a known or keyless slug using the collection contract, then fetch a current market floor.
    slug = d.get("slug") or opensea.cached_slug(d.get("chain", "robinhood"), contract)
    if not slug:
        try:
            slug = opensea._slug_for(d.get("chain", "robinhood"), contract)
        except Exception:
            slug = None
    if slug:
        floor = opensea.floor_stats(slug, timeout=3.0)
    flags["holder_context"] = (f"collection floor {floor:g} native (market snapshot)" if floor is not None
                               else "holder/floor context unavailable")
    try:
        gas = gasguard.check(rpc, d.get("chain", "robinhood"))
        flags["gas"] = gas.get("line", "gas outlook unavailable")
    except Exception:
        flags["gas"] = "gas outlook unavailable"

    score = 0
    score += 2 if flags["owner"] == "held" else 0
    score += 1 if proxy else 0
    score += 2 if flags.get("admin_roles") == "owner holds admin or minter role" else 0
    score += 2 if hits else 0
    score += 2 if d.get("seadrop") and any(x in reasons for x in
              ("SeaDrop price unavailable from live read", "SeaDrop end time precedes start time",
               "minted count exceeds reported max supply")) else 0
    level = "risky" if score >= 4 else ("caution" if score >= 1 else "safe")
    if flags["owner"] == "unknown":
        reasons.append("owner could not be read")
        level = "caution" if level == "safe" else level
    return {"level": level, "flags": flags, "reasons": reasons, "seadrop": cfg,
            "floor": floor, "checked_at": int(time.time())}


def panel_lines(readout):
    fl = readout["flags"]
    level = {"low": "safe", "medium": "caution", "high": "risky"}.get(readout["level"], readout["level"])
    sd = readout.get("seadrop") or {}
    lines = [f"risk: {level}",
             f"owner: {fl['owner']}",
             f"proxy: {fl['proxy']}",
             f"admin mint roles: {fl.get('admin_roles', 'unknown')}",
             f"blacklist/whitelist: {fl['forcing']}",
             f"honeypot: {fl['honeypot']}",
             f"SeaDrop: {'live config' if sd else 'not detected / unavailable'}",
             f"supply: {sd.get('minted', 'unknown')} minted / {sd.get('max_supply', 'unknown')} max",
             f"per-wallet: {sd.get('per_wallet', 'unknown')}",
             f"fee recipient: {sd.get('fee_recipient') or 'unknown'}",
             f"market: {fl.get('holder_context', 'unavailable')}",
             f"Robinhood gas: {fl.get('gas', 'unavailable')}"]
    if readout["reasons"]:
        lines.append("")
        lines += [f"- {r}" for r in readout["reasons"]]
    return lines
