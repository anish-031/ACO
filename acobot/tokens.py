"""ERC-20 reads, token resolution and balance snapshots. Read-only: never signs, never holds keys.

Token discovery: public RPCs have no 'list my tokens' call, so holdings are checked for
(a) the built-in list below per chain and (b) any contract the admin added with a CA in
/send or /sweep, saved in data/tokens.json. Other tokens need their contract address.
"""
import json, os, re
from decimal import Decimal
from pathlib import Path
from acobot import abi
from eth_utils import to_checksum_address as cs

DATA = Path(os.path.expanduser("~/.folk/acobot/data"))
TOKENS_FILE = DATA / "tokens.json"
ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

# verified mainnet addresses only
KNOWN = {
    "ethereum": {
        "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48": "USDC",
        "0xdAC17F958D2ee523a2206206994597C13D831ec7": "USDT",
        "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2": "WETH",
        "0x6B175474E89094C44Da98b954EedeAC495271d0F": "DAI",
        "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599": "WBTC",
    },
    "robinhood": {},
}
NATIVE_SYMBOL = {"ethereum": "ETH", "robinhood": "ETH", "ink": "ETH"}


def _load_added():
    return json.loads(TOKENS_FILE.read_text()) if TOKENS_FILE.exists() else {}


def _save_added(d):
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = TOKENS_FILE.with_suffix(".tmp"); tmp.write_text(json.dumps(d)); os.replace(tmp, TOKENS_FILE)


def _word(a):
    return a[2:].lower().rjust(64, "0")


def _decode_str(hexdata):
    b = bytes.fromhex(hexdata[2:] if hexdata.startswith("0x") else hexdata)
    if len(b) >= 64 and int.from_bytes(b[32:64], "big") < 1024:
        n = int.from_bytes(b[32:64], "big")
        return b[64:64 + n].decode("utf-8", "ignore").strip("\x00 ")
    return b[:32].rstrip(b"\x00").decode("utf-8", "ignore").strip()


def read_erc20(rpc, contract):
    """(symbol, decimals) read from the contract. Raises if it is not an ERC-20."""
    sym = _decode_str(rpc.eth_call(contract, "0x" + abi.selector_of("symbol()")))
    dec = int(rpc.eth_call(contract, "0x" + abi.selector_of("decimals()")), 16)
    return sym or "TOKEN", dec


def balance_of(rpc, contract, addr):
    return int(rpc.eth_call(contract, "0x" + abi.selector_of("balanceOf(address)") + _word(addr)), 16)


def tracked(chain):
    """[{contract, symbol, decimals}] to check on this chain (built-in + admin-added)."""
    out = [{"contract": ca, "symbol": sym, "decimals": None} for ca, sym in KNOWN.get(chain, {}).items()]
    for ca, meta in _load_added().get(chain, {}).items():
        if not any(o["contract"].lower() == ca.lower() for o in out):
            out.append({"contract": ca, "symbol": meta["symbol"], "decimals": meta["decimals"]})
    return out


def remember(chain, contract, symbol, decimals):
    d = _load_added()
    d.setdefault(chain, {})[cs(contract)] = {"symbol": symbol, "decimals": decimals}
    _save_added(d)


def resolve(rpc, chain, text):
    """Map user input to a token: {kind:'native'|'erc20', symbol, decimals, contract}.
    Raises ValueError with a plain message."""
    t = (text or "").strip()
    if not t:
        raise ValueError("say a token: native, a symbol like USDC, or a contract address")
    if t.lower() in ("native", NATIVE_SYMBOL.get(chain, "").lower()):
        return {"kind": "native", "symbol": NATIVE_SYMBOL.get(chain, "ETH"), "decimals": 18, "contract": None}
    if ADDR_RE.match(t):
        try:
            sym, dec = read_erc20(rpc, cs(t))
        except Exception:
            raise ValueError("that contract is not a readable token on this chain")
        remember(chain, t, sym, dec)
        return {"kind": "erc20", "symbol": sym, "decimals": dec, "contract": cs(t)}
    for tk in tracked(chain):
        if tk["symbol"].lower() == t.lower():
            dec = tk["decimals"]
            if dec is None:
                _, dec = read_erc20(rpc, tk["contract"])
            return {"kind": "erc20", "symbol": tk["symbol"], "decimals": int(dec), "contract": tk["contract"]}
    raise ValueError(f"unknown token '{t}' on {chain}. use its contract address (0x...)")


def fmt_amount(raw, decimals, places=6):
    v = Decimal(raw) / (Decimal(10) ** decimals)
    s = f"{v:,.{places}f}".rstrip("0").rstrip(".")
    return s or "0"


def parse_amount(text, decimals):
    """'1.5' -> raw int in token base units. Raises ValueError."""
    try:
        d = Decimal(text)
    except Exception:
        raise ValueError("amount must be a number, or 'all'")
    if d <= 0:
        raise ValueError("amount must be above zero")
    return int(d * (Decimal(10) ** decimals))


def snapshot(rpc, chain, addr, usd_for):
    """One wallet on one chain: native + non-zero tracked tokens.
    usd_for(contract_or_None, raw_amount, decimals) -> usd float or None (never raises)."""
    out = {"native_wei": None, "native_usd": None, "tokens": [], "errors": []}
    try:
        bal = int(rpc.call("eth_getBalance", [addr, "latest"]), 16)
        out["native_wei"] = bal
        out["native_usd"] = usd_for(None, bal, 18)
    except Exception:
        out["errors"].append("native balance unreadable")
    for tk in tracked(chain):
        try:
            raw = balance_of(rpc, tk["contract"], addr)
            dec = tk["decimals"]
            if dec is None:
                _, dec = read_erc20(rpc, tk["contract"])
        except Exception:
            out["errors"].append(f"{tk['symbol']} unreadable")
            continue
        if raw == 0:
            continue
        out["tokens"].append({"symbol": tk["symbol"], "contract": tk["contract"], "raw": raw,
                              "decimals": int(dec), "usd": usd_for(tk["contract"], raw, int(dec))})
    return out
