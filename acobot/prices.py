"""Native token USD price for gas estimates. Free CoinGecko endpoint, no key. Cached.

Chain gas tokens: ethereum and robinhood both use ETH as the gas token.
Returns None when the price is unavailable (caller falls back to the ETH amount).
"""
import json, time, urllib.request

CHAIN_TOKEN_ID = {"ethereum": "ethereum", "robinhood": "ethereum"}
TTL_OK = 300        # seconds a good price is reused
TTL_STALE = 3600    # a failed lookup falls back to a last-known price this old at most
_cache = {}         # token id -> (ts, usd)


def _fetch(token_id):
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={token_id}&vs_currencies=usd"
    req = urllib.request.Request(url, headers={"User-Agent": "acobot/0.1", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        data = json.loads(r.read())
    return float(data[token_id]["usd"])


def native_usd(chain_name):
    tid = CHAIN_TOKEN_ID.get(chain_name)
    if not tid:
        return None
    now = time.time()
    hit = _cache.get(tid)
    if hit and now - hit[0] < TTL_OK:
        return hit[1]
    try:
        usd = _fetch(tid)
        _cache[tid] = (now, usd)
        return usd
    except Exception:
        if hit and now - hit[0] < TTL_STALE:
            return hit[1]
        return None


def fmt_usd(x):
    if x is None:
        return None
    if x >= 0.01:
        return f"~${x:.2f}"
    return f"~${x:.4f}"


# ---- ERC-20 USD prices by contract (CoinGecko token_price, same free no-key endpoint) ----
TOKEN_PLATFORM = {"ethereum": "ethereum", "robinhood": None, "ink": None}
STABLE = {"USDC", "USDT", "DAI"}   # priced at $1 when the lookup fails
_tcache = {}                        # (platform, contract lower) -> (ts, usd)


def _fetch_token(platform, contract):
    url = (f"https://api.coingecko.com/api/v3/simple/token_price/{platform}"
           f"?contract_addresses={contract}&vs_currencies=usd")
    req = urllib.request.Request(url, headers={"User-Agent": "acobot/0.1", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        data = json.loads(r.read())
    return float(data[contract.lower()]["usd"])


def token_usd(chain_name, contract):
    """USD per whole token for an ERC-20 contract. None if unknown. Cached like native_usd."""
    platform = TOKEN_PLATFORM.get(chain_name)
    if not platform or not contract:
        return None
    key = (platform, contract.lower())
    now = time.time()
    hit = _tcache.get(key)
    if hit and now - hit[0] < TTL_OK:
        return hit[1]
    try:
        usd = _fetch_token(platform, contract)
        _tcache[key] = (now, usd)
        return usd
    except Exception:
        if hit and now - hit[0] < TTL_STALE:
            return hit[1]
        return None
