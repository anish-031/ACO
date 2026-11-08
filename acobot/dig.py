"""Read-only project digger for the ACO bot.

dig_project(name): web-search the collection name (nftcalendar, robin-xp, opensea, X via nitter),
collect candidate contract addresses and the official X handle / opensea slug, then verify any
candidate on-chain with mintread.detect. Never schedules anything. Never guesses an address:
a contract is only returned when it appears in a source page AND has code on a configured chain.
"""
import logging, re, urllib.parse, urllib.request

log = logging.getLogger("acobot.dig")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
ADDR_RE = re.compile(r"0x[a-fA-F0-9]{40}\b")
OS_RE = re.compile(r"opensea\.io/(?:collection|assets/(?P<chain>[a-z]+)/)/?(?P<slug>[A-Za-z0-9_.\-]+)")
X_RE = re.compile(r"(?:x\.com|twitter\.com|nitter\.[a-z.]+)/([A-Za-z0-9_]{1,15})(?:[/?\"'\s]|$)")
X_SKIP = {"home", "search", "explore", "i", "intent", "share", "hashtag", "status", "opensea", "nftcalendar"}
CHAIN_HINTS = {"robinhood": "robinhood", "robin": "robinhood", "ethereum": "ethereum", "eth": "ethereum"}
SEARCH_SITES = ["nftcalendar", "robin-xp", "opensea", "x.com"]


def _get(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _ddg(query):
    """DuckDuckGo html results: list of (url, snippet_text). Empty list on failure."""
    try:
        html = _get("https://html.duckduckgo.com/html/?q=" + urllib.parse.quote_plus(query))
    except Exception as e:
        log.warning("dig search failed: %s", type(e).__name__)
        return []
    out = []
    for m in re.finditer(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.S):
        href = m.group(1)
        if "uddg=" in href:
            href = urllib.parse.unquote(href.split("uddg=")[1].split("&")[0])
        out.append((href, re.sub(r"<[^>]+>", " ", m.group(2))))
    return out[:8]


def _page_text(url):
    try:
        return _get(url)
    except Exception:
        return ""


def _slug(name):
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")


def _search_hits(name):
    """Multiple query variants across nftcalendar, robin-xp, opensea and nitter mirrors of x.com."""
    queries = [f"{name} nft mint contract", f"{name} nftcalendar events", f"{name} robin-xp upcoming mints",
               f"{name} opensea collection", f"{name} x.com twitter official", f"{name} nitter",
               f"{name} nft mint robinhood chain", f"{name} digital humans nft mint",
               f"souls club {name} contract", f"{name} robinhood mint"]
    hits = []
    for q in queries:
        hits.extend(_ddg(q))
    # direct source pages + known opensea slug patterns (the page is read for the contract)
    slug = _slug(name)
    for u in ["https://nftcalendar.io/events/", "https://robin-xp.com/upcoming-mints",
              f"https://opensea.io/collection/{slug}", f"https://opensea.io/collection/{slug.replace('-', '')}",
              f"https://opensea.io/collection/souls-club-{slug}"]:
        hits.append((u, ""))
    seen, out = set(), []
    for u, t in hits:
        if u not in seen:
            seen.add(u)
            out.append((u, t))
    return out


def dig(name, chains, detect_fn, rpc_for):
    """Return a dict with what was found. chains: list of chain names enabled.
    detect_fn(contract, rpc) -> detect dict (mintread.detect). rpc_for(chain) -> RPC.
    """
    name = (name or "").strip()
    if not name:
        return {"error": "no project name given"}
    hits = _search_hits(name)
    text_blob = " ".join(f"{u} {t}" for u, t in hits)
    pages = []
    for u, _ in hits[:12]:
        pages.append(_page_text(u))
    blob = text_blob + " " + " ".join(pages)

    chain_hint = None
    low = blob.lower()
    for k, v in CHAIN_HINTS.items():
        if k in low:
            chain_hint = v
            break

    x_handles = []
    for m in X_RE.finditer(blob):
        h = m.group(1)
        if h.lower() not in X_SKIP and h not in x_handles:
            x_handles.append(h)
    os_slugs = []
    for m in OS_RE.finditer(blob):
        slug = m.group("slug")
        if slug and slug not in os_slugs:
            os_slugs.append(slug)
    cand = []
    for a in ADDR_RE.findall(blob):
        a = a.lower()
        if a not in cand and a != "0x" + "0" * 40:
            cand.append(a)

    out = {"name": name, "x_handles": x_handles[:3], "opensea_slugs": os_slugs[:3],
           "chain_hint": chain_hint, "candidates": [], "verified": None}
    for a in cand[:6]:
        for cn in ([chain_hint] if chain_hint in chains else []) + [c for c in chains if c != chain_hint]:
            try:
                d = detect_fn(a, rpc_for(cn))
            except Exception as e:
                continue  # no code on this chain or rpc error: try next chain
            out["verified"] = {
                "contract": a, "chain": cn, "chain_id": None,
                "price_wei": d.get("price_wei"), "start_ts": d.get("start_ts"),
                "per_wallet": d.get("per_wallet"), "phases": d.get("phases") or [],
                "seadrop": bool(d.get("seadrop")), "paused": d.get("paused"),
                "sale_started": d.get("sale_started"), "name": d.get("name"),
            }
            break
        if out["verified"]:
            break
    if not out["verified"]:
        out["missing"] = "no contract address found on a source page that has code on a configured chain"
    return out


def format_for_user(res):
    """Plain text for the LLM / user. Never guesses; only states what was found."""
    if res.get("error"):
        return res["error"]
    lines = []
    if res.get("x_handles"):
        lines.append("x accounts seen: " + ", ".join("@" + h for h in res["x_handles"]))
    if res.get("opensea_slugs"):
        lines.append("opensea slugs seen: " + ", ".join(res["opensea_slugs"]))
    v = res.get("verified")
    if v:
        lines.append(f"contract {v['contract']} on {v['chain']}, verified on-chain")
        lines.append("price_wei: " + str(v.get("price_wei")) + ", start_ts: " + str(v.get("start_ts")) +
                     ", per_wallet: " + str(v.get("per_wallet")) + ", phases: " + ", ".join(v.get("phases") or []))
        lines.append("seadrop: " + str(v.get("seadrop")))
    else:
        lines.append("no contract address found in the sources I could read. " + str(res.get("missing", "")))
    return "\n".join(lines)
