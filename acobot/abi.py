"""ABI / function detection from bytecode.

Strategy: extract PUSH4 selectors from the runtime dispatcher, resolve names via
4byte.directory (signature database), then check which known mint/price/start
getters exist and read their values with eth_call.
"""
import json, urllib.request

KNOWN_READS = {
    "price": "price()", "cost": "cost()", "mintPrice": "mintPrice()", "PRICE": "PRICE()",
    "startTime": "startTime()", "saleStart": "saleStart()", "mintStart": "mintStart()",
    "maxPerWallet": "maxPerWallet()", "maxMintPerWallet": "maxMintPerWallet()",
    "maxPerTx": "maxPerTx()", "saleStarted": "saleStarted()", "paused": "paused()",
    "totalSupply": "totalSupply()", "maxSupply": "maxSupply()",
    "whitelistStartTime": "whitelistStartTime()", "publicStartTime": "publicStartTime()",
    "MAX_PER_WALLET": "MAX_PER_WALLET()", "MAX_MINT": "MAX_MINT()",
}
MINT_HINTS = ("mint", "claim", "purchase", "buy")

def selectors_from_bytecode(code_hex):
    b = bytes.fromhex(code_hex[2:] if code_hex.startswith("0x") else code_hex)
    sels, i = set(), 0
    while i < len(b):
        op = b[i]
        if op == 0x63 and i + 4 < len(b):
            sels.add(b[i + 1:i + 5].hex())
        if 0x60 <= op <= 0x7f:
            i += op - 0x5f
        i += 1
    return sels

def selector_of(sig):
    """4-byte selector for a text signature, e.g. 'price()' -> 'a035b1fe'."""
    from eth_utils import keccak
    return keccak(text=sig)[:4].hex()

def lookup_4byte(selector):
    """Return the most common text signature for a 4-byte selector, or None."""
    url = f"https://www.4byte.directory/api/v1/signatures/?hex_signature=0x{selector}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 acobot/0.1"})
        with urllib.request.urlopen(req, timeout=8) as r:
            res = json.loads(r.read()).get("results", [])
    except Exception:
        return None
    if not res:
        return None
    # several signatures can share a selector; keep all, callers pick by name
    return " | ".join(x["text_signature"] for x in sorted(res, key=lambda x: x["id"]))

def analyze(code_hex, resolve=lookup_4byte):
    """Return dict: selectors count, named mint-related functions found."""
    sels = selectors_from_bytecode(code_hex)
    names = {}
    for s in sels:
        n = resolve(s)
        if n:
            names[s] = n
    mint_fns = sorted({c for n in names.values() for c in n.split(" | ") if any(h in c.lower() for h in MINT_HINTS)})
    return {"selector_count": len(sels), "named": names, "mint_functions": mint_fns}
