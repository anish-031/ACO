"""OpenSea drop schedule (stage names, windows, per-wallet limits) for display.

Public collection page, no login. OpenSea embeds the drop as JSON inside the page
("dropBySlug" -> "stages"). The contract's /assets/<chain>/<contract>/1 URL redirects to the
collection slug page. Results are cached per contract on disk.

Chain data stays the source of truth for execution. This module only enriches labels and
the schedule display. Any failure returns None and the caller keeps chain stages.
"""
import json, re, time, logging, urllib.request, urllib.error
from html import unescape as html_unescape
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger("acobot.opensea")
CACHE_FILE = Path(__file__).resolve().parent.parent / "data" / "opensea_cache.json"
GTD_RE = re.compile(r"\b(gtd|guaranteed|guarantee)\b", re.I)
FCFS_RE = re.compile(r"\b(fcfs|first[\s-]?come)\b", re.I)
_SIGNED_STAGE_TYPES = {"SIGNED_PRESALE", "SIGNED", "SIGNATURE", "SIGNED_MINT"}
_ALLOWLIST_STAGE_TYPES = {
    "ALLOWLIST", "ALLOW_LIST", "MERKLE", "MERKLE_PRESALE", "WHITELIST", "PRESALE",
}
TTL_OK = 30 * 60      # schedule rarely changes
TTL_MISS = 5 * 60     # retry sooner after a miss
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
_cache = {}
_loaded = [False]


def _load():
    if not _loaded[0]:
        _loaded[0] = True
        try:
            _cache.update(json.loads(CACHE_FILE.read_text()))
        except Exception:
            pass


def _save():
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(_cache))
    except Exception as e:
        log.warning("opensea cache write failed: %s", type(e).__name__)


def _iso_ts(s):
    from datetime import datetime
    if not isinstance(s, str) or not s:
        return None
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _optional_int(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


_WALLET_LIMIT_KEYS = (
    "perWalletLimit", "maxTotalMintableByWallet", "maxPerWallet", "maxMintsPerWallet",
    "per_wallet_limit", "max_total_mintable_by_wallet", "max_per_wallet",
)


def _wallet_limit(stage):
    """Wallet cap from whichever field this OpenSea payload uses."""
    if not isinstance(stage, dict):
        return None
    for key in _WALLET_LIMIT_KEYS:
        limit = _optional_int(stage.get(key))
        if limit is not None and limit > 0:
            return limit
    return None


def _nearby_drop_counts(html, array_start):
    window = html[max(0, array_start - 500):array_start]
    supply = re.search(r'"maxSupply"\s*:\s*(\d+)', window)
    minted = re.search(r'"totalSupply"\s*:\s*(\d+)', window)
    return (int(supply.group(1)) if supply else None,
            int(minted.group(1)) if minted else None)


def visible_wallet_limits(html):
    """Limits printed on the mint schedule when the JSON stage omits them."""
    found = {}
    for match in re.finditer(r'font-medium">([^<]{1,80})</span>', html or ""):
        label = html_unescape(match.group(1)).strip()
        if not label or label.lower() in {"allowlist", "public", "presale"} or "limit" in label.lower():
            continue
        window = html[match.end():match.end() + 900]
        limit = re.search(r"Limit\s+(\d+)\s+per wallet", window, re.I)
        if limit:
            found[label] = int(limit.group(1))
    return found


def classify_list_mode(stage):
    """Classify a drop stage as gtd, fcfs, public, or presale.

    Public sales labeled FCFS stay public races. GTD is a reserved allowlist.
    An allowlist labeled FCFS is still gated; the label only changes how it is fired.
    """
    stage = stage or {}
    label = str(stage.get("label") or "")
    stage_type = str(stage.get("type") or stage.get("stageType") or "").upper()
    if stage_type == "PUBLIC_SALE":
        return "fcfs" if FCFS_RE.search(label) else "public"
    if GTD_RE.search(label):
        return "gtd"
    if FCFS_RE.search(label):
        return "fcfs"
    members = stage.get("allowlist_members")
    allocation = stage.get("allowlist_allocation")
    per_wallet = stage.get("per_wallet")
    if (isinstance(members, int) and isinstance(allocation, int) and isinstance(per_wallet, int)
            and members > 0 and per_wallet > 0 and allocation == members * per_wallet):
        return "gtd"
    return "presale"


def _chain_list_mode(stage):
    key = stage.get("key") or stage.get("chain_key")
    if key == "public" or not stage.get("gated"):
        return "public"
    return "presale"


def execution_key(os_stage, chain_by_key):
    """Map one OpenSea stage onto the chain method that can execute it."""
    stage_type = str((os_stage or {}).get("type") or "").upper()
    has_signed = "signed" in chain_by_key
    has_allow = "allowlist" in chain_by_key
    if stage_type == "PUBLIC_SALE":
        return "public" if "public" in chain_by_key else None
    if stage_type in _SIGNED_STAGE_TYPES:
        if has_signed:
            return "signed"
        return "allowlist" if has_allow else None
    if has_allow and (stage_type in _ALLOWLIST_STAGE_TYPES or not has_signed):
        return "allowlist"
    if has_signed:
        return "signed"
    if has_allow:
        return "allowlist"
    return None


def parse_stages(html):
    """Return the drop stages from an OpenSea collection page, or [] if none."""
    decoder = json.JSONDecoder()
    out = []
    stage_arrays = 0
    for match in re.finditer(r'"stages"\s*:', html):
        array_start = html.find("[", match.end())
        if array_start < 0 or array_start - match.end() > 16:
            continue
        try:
            stages, _ = decoder.raw_decode(html, array_start)
        except json.JSONDecodeError:
            continue
        if not isinstance(stages, list):
            continue
        stage_arrays += 1
        drop_supply, drop_minted = _nearby_drop_counts(html, array_start)
        found = []
        for stage in stages:
            label = stage.get("label") or stage.get("name") if isinstance(stage, dict) else None
            stage_type = stage.get("stageType") or stage.get("type") if isinstance(stage, dict) else None
            if not isinstance(stage, dict) or not label or not stage_type:
                continue
            start_ts = _iso_ts(stage.get("startTime") or stage.get("start_time"))
            end_ts = _iso_ts(stage.get("endTime") or stage.get("end_time"))
            if start_ts is None:
                start_ts = _optional_int(stage.get("startTime") or stage.get("start_time"))
            if end_ts is None:
                end_ts = _optional_int(stage.get("endTime") or stage.get("end_time"))
            price = stage.get("price")
            token = price.get("token") if isinstance(price, dict) else None
            unit = token.get("unit") if isinstance(token, dict) else None
            try:
                price_eth = float(unit) if unit is not None else _drop_price_eth(price)
                stage_index = int(stage["stageIndex"]) if stage.get("stageIndex") is not None else (
                    int(stage["index"]) if stage.get("index") is not None else None)
            except (TypeError, ValueError):
                log.warning("opensea stage has invalid numeric fields")
                continue
            parsed = {
                "label": label,
                "type": stage_type,
                "index": stage_index,
                "start_ts": start_ts,
                "end_ts": end_ts,
                "per_wallet": _wallet_limit(stage),
                "price_eth": price_eth,
                "drop_supply": drop_supply,
                "drop_minted": drop_minted,
                "allowlist_members": _optional_int(stage.get("allowlistMemberCount")),
                "allowlist_allocation": _optional_int(stage.get("allowlistMintAllocation")),
            }
            parsed["list_mode"] = classify_list_mode(parsed)
            found.append(parsed)
        if _stage_list_score(found) > _stage_list_score(out):
            out = found
    for label, limit in visible_wallet_limits(html).items():
        for stage in out:
            if stage.get("per_wallet") is None and stage.get("label") == label:
                stage["per_wallet"] = limit
    marker_found = '"stages":[{"label"' in html
    log.info("opensea stages parse html_chars=%d marker_found=%s arrays=%d matched=%d",
             len(html), marker_found, stage_arrays, len(out))
    out.sort(key=lambda s: s["start_ts"] or 0)
    return out


def _stage_list_score(stages):
    """Prefer the stage list that actually carries wallet limits and windows."""
    score = 0
    for stage in stages or []:
        score += 1
        if stage.get("per_wallet") is not None:
            score += 5
        if stage.get("start_ts"):
            score += 1
        if stage.get("price_eth") is not None:
            score += 1
    return score


def _slug_for(chain, contract):
    """Collection slug for a contract via OpenSea's keyless contract lookup. None on failure.
    Falls back to the public asset-page redirect and remembers the last good slug."""
    import httpx
    key = f"{chain}:{contract.lower()}"
    headers = {"User-Agent": UA, "Accept": "application/json"}
    configured_key = api_key()
    if configured_key:
        headers["X-API-KEY"] = configured_key
    try:
        r = httpx.get(f"{API_BASE}/chain/{chain}/contract/{contract}",
                      headers=headers, timeout=12)
        log.info("opensea contract lookup chain=%s status=%d", chain, r.status_code)
        if r.status_code == 200:
            slug = r.json().get("collection") or None
            log.info("opensea contract lookup slug_found=%s", bool(slug))
            if slug:
                _load()
                _cache[f"slug:{key}"] = {"slug": slug, "ts": time.time()}
                _save()
                return slug
            log.warning("opensea contract lookup returned no collection slug")
        else:
            log.warning("opensea contract lookup http %s", r.status_code)
    except Exception as e:
        log.warning("opensea contract lookup failed: %s", type(e).__name__)

    try:
        asset_url = f"https://opensea.io/assets/{chain}/{contract}/1"
        r = httpx.get(asset_url, headers={"User-Agent": UA}, timeout=15, follow_redirects=True)
        final_path = urlparse(str(r.url)).path
        match = re.match(r"^/collection/([a-z0-9][a-z0-9-]*)/?$", final_path, re.I)
        slug = match.group(1) if r.status_code == 200 and match else None
        log.info("opensea asset fallback status=%d slug_found=%s", r.status_code, bool(slug))
        if slug:
            _load()
            _cache[f"slug:{key}"] = {"slug": slug, "ts": time.time()}
            _save()
            return slug
    except Exception as e:
        log.warning("opensea asset fallback failed: %s", type(e).__name__)

    _load()
    remembered = _cache.get(f"slug:{key}")
    if remembered and remembered.get("slug"):
        log.info("opensea lookup unavailable, using remembered slug for %s", key)
        return remembered["slug"]
    log.warning("opensea contract lookup has no slug or cached collection")
    return None


def _fetch(chain, contract):
    """Collection page (has the drop JSON). /assets/... redirects to /item/... which has no stages."""
    import httpx
    slug = _slug_for(chain, contract)
    if not slug:
        return None, ""
    url = f"https://opensea.io/collection/{slug}"
    r = httpx.get(url, headers={"User-Agent": UA}, timeout=15, follow_redirects=True)
    log.info("opensea collection page status=%d html_chars=%d", r.status_code, len(r.text))
    r.raise_for_status()
    html = r.text
    return slug, html


def _combine_stage_lists(page_stages, api_stages):
    """Keep wallet limits and windows when one source has the names and the other has the numbers."""
    if not api_stages:
        return page_stages
    if not page_stages:
        return api_stages
    page_by_index = {stage.get("index"): stage for stage in page_stages if stage.get("index") is not None}
    page_by_label = {stage.get("label"): stage for stage in page_stages if stage.get("label")}
    combined = []
    for stage in api_stages:
        previous = page_by_index.get(stage.get("index")) or page_by_label.get(stage.get("label")) or {}
        merged = dict(previous)
        for key, value in stage.items():
            if value is not None:
                merged[key] = value
        for key in ("per_wallet", "start_ts", "end_ts", "price_eth", "drop_supply", "drop_minted"):
            if merged.get(key) is None and previous.get(key) is not None:
                merged[key] = previous[key]
        combined.append(merged)
    return combined or page_stages


def fetch_schedule(chain, contract, *, force=False):
    """Cached OpenSea stage list for a SeaDrop collection. None when unavailable."""
    _load()
    key = f"{chain}:{contract.lower()}"
    hit = _cache.get(key)
    now = time.time()
    if not force and hit and now - hit.get("ts", 0) < (TTL_OK if hit.get("stages") else TTL_MISS):
        log.info("opensea schedule cache hit stages=%d", len(hit.get("stages") or []))
        return hit.get("stages") or None
    stages, slug = None, None
    try:
        slug, html = _fetch(chain, contract)
        stages = parse_stages(html) or None
        api_stages = drop_stages(slug) if slug else None
        stages = _combine_stage_lists(stages, api_stages)
        if slug and not stages:
            log.warning("opensea page had no drop stages for %s", slug)
    except Exception as e:
        log.warning("opensea fetch failed: %s", type(e).__name__)
    if stages is None and hit and hit.get("stages"):
        log.info("opensea fetch failed, serving stale stages for %s", key)
        return hit["stages"]
    _cache[key] = {"ts": now, "slug": slug, "stages": stages}
    if slug:
        _cache[f"slug:{key}"] = {"slug": slug, "ts": now}
    _save()
    return stages


def merge(chain_stages, os_stages):
    """Keep execution terms from chain data and attach OpenSea terms for display only.

    Each entry: label (OpenSea name or chain key), chain_key (the phase execution uses: public,
    signed or allowlist; None when the chain has no such stage), gated, snipeable, list_mode
    (gtd, fcfs, public, or presale), start_ts, end_ts, per_wallet, price_wei, display_* schedule
    fields, source. Chain stages OpenSea does not list are kept. Snipe gating always comes from
    the chain method, never from the OpenSea label. GTD and FCFS stay separate rows.
    """
    chain = {s["key"]: s for s in (chain_stages or [])}
    if not os_stages:
        return [dict(s, label=s.get("label"), chain_key=s["key"], source="chain",
                     list_mode=s.get("list_mode") or _chain_list_mode(s))
                for s in (chain_stages or [])]
    out = []
    for o in os_stages:
        price = None if o["price_eth"] is None else int(round(o["price_eth"] * 1e18))
        display = {"display_start_ts": o["start_ts"], "display_end_ts": o["end_ts"],
                   "display_per_wallet": o["per_wallet"], "display_price_wei": price}
        key = execution_key(o, chain)
        matched = chain.get(key, {}) if key else {}
        list_mode = o.get("list_mode") or classify_list_mode(o)
        public = o.get("type") == "PUBLIC_SALE"
        out.append(dict(
            matched, **display, label=o["label"], chain_key=key,
            gated=not public, snipeable=public and key == "public",
            list_mode=list_mode, index=o.get("index"),
            allowlist_members=o.get("allowlist_members"),
            allowlist_allocation=o.get("allowlist_allocation"),
            drop_supply=o.get("drop_supply"),
            drop_minted=o.get("drop_minted"),
            source="chain+opensea" if matched else "opensea",
        ))
    used = {row.get("chain_key") for row in out if row.get("chain_key")}
    public_listed = any(o.get("type") == "PUBLIC_SALE" for o in os_stages)
    for key, stage in chain.items():
        if key in used or (key == "public" and public_listed):
            continue
        out.append(dict(
            stage, label=stage.get("label"), chain_key=key, source="chain",
            gated=bool(stage.get("gated")),
            snipeable=key == "public" and not stage.get("gated"),
            list_mode=stage.get("list_mode") or _chain_list_mode(stage),
        ))
    out.sort(key=lambda x: (x.get("start_ts") or 0))
    return out


# ---------- official OpenSea API (api-key mode) ----------
API_BASE = "https://api.opensea.io/api/v2"


def fulfill_listing(listing, buyer, *, key=None, opener=None, timeout=12):
    """Return the OpenSea fulfillment transaction for one exact listing.

    Deliberately only prepares unsigned tx data. Caller must gate signing behind its confirm callback.
    """
    key = key if key is not None else api_key()
    if not key:
        raise RuntimeError("OpenSea API key is required for listing fulfillment")
    order_hash = listing.get("order_hash") or listing.get("id") or listing.get("orderHash")
    chain = listing.get("chain") or ("ethereum" if listing.get("chain_id") == 1 or listing.get("fetched_via") == "reservoir" else "robinhood")
    protocol = listing.get("protocol_address") or listing.get("protocolAddress")
    if isinstance(protocol, dict):
        protocol = protocol.get("address")
    if not protocol:
        protocol = (listing.get("protocol") or {}).get("address") if isinstance(listing.get("protocol"), dict) else listing.get("protocol")
    if not order_hash or not protocol:
        raise ValueError("listing must include order hash and protocol address")
    if not listing.get("contract") or listing.get("token_id") is None:
        raise ValueError("listing must include exact contract and token id")
    payload = {"listing": {"hash": order_hash, "chain": chain, "protocol_address": protocol},
               "fulfiller": {"address": buyer},
               "consideration": {"asset_contract_address": listing["contract"],
                                 "token_id": str(listing["token_id"])},
               "units_to_fill": 1}
    import httpx
    req = urllib.request.Request(API_BASE + "/listings/fulfillment_data", data=json.dumps(payload).encode(),
        headers={"X-API-KEY": key, "Accept": "application/json", "Content-Type": "application/json",
                 "User-Agent": UA}, method="POST")
    open_url = opener or urllib.request.urlopen
    with open_url(req, timeout=timeout) as response:
        data = json.loads(response.read())
    fd = data.get("fulfillment_data") or {}
    tx = fd.get("transaction") or data.get("transaction") or {}
    if not tx.get("to") or not (tx.get("input_data") or tx.get("data")):
        raise RuntimeError("OpenSea did not return a usable fulfillment transaction")
    value = tx.get("value", "0")
    if isinstance(value, str) and value.startswith("0x"):
        value = int(value, 16)
    else:
        value = int(value)
    tx_chain = tx.get("chain")
    if isinstance(tx_chain, dict):
        tx_chain = tx_chain.get("identifier") or tx_chain.get("chain")
    chain_id = tx.get("chain_id") or listing.get("chain_id")
    if isinstance(chain_id, str) and chain_id.isdigit():
        chain_id = int(chain_id)
    return {"to": tx["to"], "data": tx.get("data") or tx.get("input_data"),
            "value": value, "chainId": chain_id, "chain": tx_chain}


def fulfill_offer(offer, seller, *, key=None, opener=None, timeout=12):
    """Return the unsigned OpenSea transaction that accepts one exact offer."""
    key = key if key is not None else api_key()
    if not key:
        raise RuntimeError("OpenSea API key is required for offer fulfillment")
    order_hash = offer.get("order_hash") or offer.get("orderHash") or offer.get("id")
    chain = offer.get("chain") or "ethereum"
    protocol = offer.get("protocol_address") or offer.get("protocolAddress")
    if isinstance(protocol, dict):
        protocol = protocol.get("address")
    if not protocol:
        protocol = (offer.get("protocol") or {}).get("address") if isinstance(offer.get("protocol"), dict) else offer.get("protocol")
    if not order_hash or not protocol:
        raise ValueError("offer must include order hash and protocol address")
    if not offer.get("contract") or offer.get("token_id") is None:
        raise ValueError("offer must include exact contract and token id")
    payload = {"offer": {"hash": order_hash, "chain": chain, "protocol_address": protocol},
               "fulfiller": {"address": seller},
               "consideration": {"asset_contract_address": offer["contract"],
                                 "token_id": str(offer["token_id"])},
               "units_to_fill": 1}
    req = urllib.request.Request(API_BASE + "/offers/fulfillment_data", data=json.dumps(payload).encode(),
        headers={"X-API-KEY": key, "Accept": "application/json", "Content-Type": "application/json",
                 "User-Agent": UA}, method="POST")
    with (opener or urllib.request.urlopen)(req, timeout=timeout) as response:
        data = json.loads(response.read())
    fd = data.get("fulfillment_data") or {}
    tx = fd.get("transaction") or data.get("transaction") or {}
    if not tx.get("to") or not (tx.get("input_data") or tx.get("data")):
        raise RuntimeError("OpenSea did not return a usable offer fulfillment transaction")
    value = tx.get("value", "0")
    value = int(value, 16) if isinstance(value, str) and value.startswith("0x") else int(value)
    chain_id = tx.get("chain_id") or offer.get("chain_id")
    if isinstance(chain_id, str) and chain_id.isdigit():
        chain_id = int(chain_id)
    return {"to": tx["to"], "data": tx.get("data") or tx.get("input_data"),
            "value": value, "chainId": chain_id}


def drop_stages(slug, *, get=None):
    """Official drop stages when OPENSEA_API_KEY is set. None when the key or call is unavailable."""
    key = api_key()
    if not key or not slug:
        return None
    try:
        import httpx
        response = (get or httpx.get)(
            f"{API_BASE}/drops/{slug}",
            headers={"X-API-KEY": key, "Accept": "application/json", "User-Agent": UA},
            timeout=8,
        )
        if getattr(response, "status_code", 0) != 200:
            return None
        return stages_from_drop(response.json())
    except Exception as exc:
        log.info("opensea drop stages failed: %s", type(exc).__name__)
        return None


def stages_from_drop(body):
    """Map an OpenSea drop payload onto the same stage records as the collection page."""
    drop = body.get("drop") if isinstance(body, dict) and isinstance(body.get("drop"), dict) else body
    raw_stages = drop.get("stages") if isinstance(drop, dict) else None
    if not isinstance(raw_stages, list):
        return None
    found = []
    for stage in raw_stages:
        parsed = _drop_stage(stage)
        if parsed:
            found.append(parsed)
    found.sort(key=lambda item: item["start_ts"] or 0)
    return found or None


def _drop_price_eth(price):
    if isinstance(price, (int, float)) and not isinstance(price, bool):
        return float(price)
    if isinstance(price, str):
        try:
            return float(price)
        except ValueError:
            return None
    if not isinstance(price, dict):
        return None
    token = price.get("token") if isinstance(price.get("token"), dict) else price
    for key in ("unit", "amount", "eth", "value"):
        raw = token.get(key) if isinstance(token, dict) else None
        if raw is None:
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def _drop_stage(stage):
    if not isinstance(stage, dict):
        return None
    label = stage.get("label") or stage.get("name")
    stage_type = stage.get("stage_type") or stage.get("stageType") or stage.get("type")
    if not isinstance(label, str) or not label or not isinstance(stage_type, str) or not stage_type:
        return None
    try:
        stage_index = int(stage["stageIndex"]) if stage.get("stageIndex") is not None else (
            int(stage["index"]) if stage.get("index") is not None else None)
    except (TypeError, ValueError):
        return None
    per_wallet = _wallet_limit(stage)
    parsed = {
        "label": label,
        "type": stage_type,
        "index": stage_index,
        "start_ts": _iso_ts(stage.get("startTime") or stage.get("start_time")),
        "end_ts": _iso_ts(stage.get("endTime") or stage.get("end_time")),
        "per_wallet": per_wallet,
        "price_eth": _drop_price_eth(stage.get("price")),
        "allowlist_members": _optional_int(stage.get("allowlistMemberCount")),
        "allowlist_allocation": _optional_int(stage.get("allowlistMintAllocation")),
    }
    parsed["list_mode"] = classify_list_mode(parsed)
    return parsed


def api_key():
    """OPENSEA_API_KEY env first, then config.toml [opensea] api_key. Empty string when none."""
    import os
    k = os.environ.get("OPENSEA_API_KEY", "").strip()
    if k:
        return k
    try:
        import tomllib
        cfg = Path(__file__).resolve().parent.parent / "config.toml"
        with open(cfg, "rb") as f:
            return (tomllib.load(f).get("opensea", {}) or {}).get("api_key", "").strip()
    except Exception:
        return ""


_AGENT_KEY = {"value": "", "expires": 0.0, "cooldown_until": 0.0}
_AGENT_LOCK = __import__("threading").Lock()


def ensure_api_key(*, post=None, now=None):
    """Configured key, otherwise OpenSea's official free agent key.

    POST /api/v2/auth/keys needs no signup. The key stays in memory until it
    expires and is never written to disk or logs. A 429 backs off instead of
    minting more keys.
    """
    configured = api_key()
    if configured:
        return configured
    clock = now or time.time
    current = clock()
    with _AGENT_LOCK:
        if _AGENT_KEY["value"] and _AGENT_KEY["expires"] > current + 30:
            return _AGENT_KEY["value"]
        if _AGENT_KEY["cooldown_until"] > current:
            return ""
    try:
        import httpx
        sender = post or httpx.post
        response = sender(
            f"{API_BASE}/auth/keys",
            headers={"Accept": "application/json", "User-Agent": UA},
            timeout=8,
        )
    except Exception as exc:
        log.info("opensea agent key request failed: %s", type(exc).__name__)
        with _AGENT_LOCK:
            _AGENT_KEY["cooldown_until"] = clock() + 60
        return ""
    status = getattr(response, "status_code", 0)
    if status == 429:
        retry = 0
        try:
            retry = int(response.headers.get("Retry-After") or 0)
        except (TypeError, ValueError, AttributeError):
            retry = 0
        wait = min(3600, retry or 900)
        with _AGENT_LOCK:
            _AGENT_KEY["cooldown_until"] = clock() + wait
        log.info("opensea agent key rate limited for %ss", wait)
        return ""
    if status != 200:
        with _AGENT_LOCK:
            _AGENT_KEY["cooldown_until"] = clock() + 120
        log.info("opensea agent key http %s", status)
        return ""
    try:
        body = response.json()
    except Exception:
        return ""
    key = str((body or {}).get("api_key") or "").strip()
    if not key:
        return ""
    expires = clock() + 6 * 24 * 3600
    raw_expiry = (body or {}).get("expires_at")
    if isinstance(raw_expiry, str):
        try:
            from datetime import datetime, timezone
            parsed = datetime.fromisoformat(raw_expiry.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            expires = parsed.timestamp()
        except ValueError:
            pass
    with _AGENT_LOCK:
        _AGENT_KEY["value"] = key
        _AGENT_KEY["expires"] = expires
        _AGENT_KEY["cooldown_until"] = 0.0
    log.info("opensea agent key cached until %s", int(expires))
    return key


def mode():
    """'api key' when an official key is configured, else 'keyless (limited)'."""
    return "api key" if api_key() else "keyless mode, limited"


def fetch_api(slug, key=None, timeout=10.0):
    """Official collection + recent events. Returns dict or None. Never raises."""
    import httpx
    key = key if key is not None else api_key()
    if not key or not slug:
        return None
    hdr = {"X-API-KEY": key, "Accept": "application/json", "User-Agent": UA}
    try:
        c = httpx.get(f"{API_BASE}/collections/{slug}", headers=hdr, timeout=timeout)
        if c.status_code != 200:
            log.warning("opensea api collection http %s", c.status_code)
            return None
        col = c.json()
        ev = httpx.get(f"{API_BASE}/events/collection/{slug}", params={"event_type": "mint", "limit": 10},
                       headers=hdr, timeout=timeout)
        events = ev.json().get("asset_events", []) if ev.status_code == 200 else []
        return {"collection": col, "mint_events": len(events), "mode": "api key"}
    except Exception as e:
        log.warning("opensea api failed: %s", type(e).__name__)
        return None


def project_site(chain, contract):
    """Return a collection's HTTPS website from OpenSea metadata when available."""
    import httpx

    slug = cached_slug(chain, contract) or _slug_for(chain, contract)
    if not slug:
        return None
    key = api_key()
    if not key:
        return None
    try:
        response = httpx.get(
            f"{API_BASE}/collections/{slug}",
            headers={"X-API-KEY": key, "Accept": "application/json", "User-Agent": UA},
            timeout=8,
        )
        if response.status_code != 200:
            return None
        data = response.json()
        collection = data.get("collection", data)
        if not isinstance(collection, dict):
            return None
        candidates = [
            collection.get("external_url"),
            collection.get("project_url"),
            collection.get("website"),
        ]
        for value in candidates:
            if not isinstance(value, str):
                continue
            from urllib.parse import urlsplit
            parsed = urlsplit(value)
            if parsed.scheme == "https" and parsed.hostname and parsed.hostname.lower() != "opensea.io":
                return value
    except Exception as exc:
        log.info("opensea project site lookup failed: %s", type(exc).__name__)
    return None


SITE_MINT_HINT = re.compile(r"https?://(?!opensea\.io|etherscan|basescan)[^\s\"']+", re.I)


def site_mint_note(drop_url):
    """If a drop info url is a website mint (not a contract call), return the admin note; else None."""
    if not drop_url or "opensea.io" in drop_url or "etherscan" in drop_url:
        return None
    return ("site mint — needs the mint call or the site's API. use ⚙️ set mint call, "
            "or configure /mintapiset with the official API's {wallet}, {phase}, {stage_index}, and {qty} fields "
            "when the project exposes phase-specific calldata and authorization.")


def remember_slug(chain, contract, slug):
    """Cache a contract -> slug mapping (called when a slug is discovered from a deep link, watchlist add or addmint)."""
    if not (chain and contract and slug):
        return
    _load()
    _cache[f"slug:{chain}:{contract.lower()}"] = {"slug": slug, "ts": time.time()}
    _save()


def cached_slug(chain, contract):
    _load()
    hit = _cache.get(f"slug:{chain}:{(contract or '').lower()}")
    return hit.get("slug") if hit else None


def floor_stats(slug, timeout=8.0):
    """Keyless floor (native units) from OpenSea's slug stats endpoint. None when unavailable."""
    import httpx
    if not slug:
        return None
    try:
        r = httpx.get(f"{API_BASE}/collections/{slug}/stats", headers={"User-Agent": UA}, timeout=timeout)
        if r.status_code != 200:
            return None
        v = r.json().get("total", {}).get("floor_price")
        return float(v) if v is not None else None
    except Exception as e:
        log.warning("opensea floor stats failed: %s", type(e).__name__)
        return None
