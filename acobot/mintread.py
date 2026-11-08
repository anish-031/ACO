"""Detect mint parameters on a contract: price, start time, per-wallet cap, phases.

Order of checks:
1. EIP-1167 minimal proxy -> resolve implementation and analyze that too.
2. OpenSea SeaDrop: if the NFT is registered on SeaDrop (0x00005ea0...) with a public
   drop, read getPublicDrop(nft) for price, start, end, per-wallet cap. Mint goes through
   SeaDrop.mintPublic(nft, feeRecipient, minter, qty).
3. Plain contract getters (price()/startTime()/maxPerWallet()/paused() ...), only called
   when the selector really exists in the dispatcher.
Verified ABI (Sourcify, no key) is used for names when available.
"""
import concurrent.futures as cf, json, re, urllib.error, urllib.request
from acobot import abi
from acobot import provider_manager

TS_MIN, TS_MAX = 1_600_000_000, 4_102_444_800
SEADROP = "0x00005ea00ac477b1030ce78506496e8c2de24bf5"
SEADROP_MINT_SIG = "mintPublic(address,address,address,uint256)"
PHASE_RE = re.compile(r"(presale|whitelist|allowlist|waitlist|public|phase|airdrop)", re.I)
MINT_SIG_RE = re.compile(r"^(mint|publicMint|claim|purchase|buy|mintPublic|presaleMint|whitelistMint|allowlistMint|mintTo)\((uint256)?\)$")


class NoContract(Exception):
    """No contract bytecode at the address on the chosen chain."""


def _words(hex_result):
    h = hex_result[2:] if hex_result and hex_result.startswith("0x") else (hex_result or "")
    return [int(h[i:i + 64], 16) for i in range(0, len(h) - 63, 64)]


def _word(hex_result):
    w = _words(hex_result)
    return w[0] if w else None


def _addr_word(a):
    return a[2:].lower().rjust(64, "0")


def resolve_eip1167(code):
    """Implementation address if code is an EIP-1167 minimal proxy, else None."""
    c = code[2:].lower() if code.startswith("0x") else code.lower()
    if c.startswith("363d3d373d3d3d363d73") and len(c) >= 60 + 22:
        return "0x" + c[22:62]
    return None


def seadrop_public(contract, rpc):
    """Read SeaDrop's public drop config for an NFT. Returns dict or None."""
    try:
        raw = rpc.eth_call(SEADROP, "0x" + abi.selector_of("getPublicDrop(address)") + _addr_word(contract))
    except Exception:
        return None
    w = _words(raw)
    if len(w) < 4:
        return None
    price, start, end, maxw = w[0], w[1], w[2], w[3]
    if start == 0 and end == 0 and price == 0:
        return None  # no public drop configured for this NFT on SeaDrop
    fee_recipient = None
    try:
        r = rpc.eth_call(SEADROP, "0x" + abi.selector_of("getAllowedFeeRecipients(address)") + _addr_word(contract))
        arr = _words(r)
        if len(arr) >= 3 and arr[1] > 0:
            cnt = arr[1]
            cands = [x for x in arr[2:2 + cnt] if x]
            if cands:
                fee_recipient = "0x" + format(cands[0], "040x")
    except Exception:
        pass
    return {"price_wei": price, "start_ts": start or None, "end_ts": end or None,
            "per_wallet": maxw or None, "fee_recipient": fee_recipient}


ZERO_ROOT = "0x" + "00" * 32


def seadrop_stages(contract, rpc, sd=None):
    """Enumerate SeaDrop stages the chain actually holds for this NFT.

    SeaDrop keeps ONE public drop per nft (getPublicDrop) and ONE allowlist merkle root per nft
    (getAllowListMerkleRoot). Allowlist stage schedules are not stored on-chain: they are passed
    as mintParams in each mintAllowList call, so only the root and a gated flag can be read here.
    """
    sd = sd or SEADROP
    stages = []
    pub = seadrop_public(contract, rpc)
    if pub:
        stages.append({"key": "public", "gated": False, "price_wei": pub["price_wei"],
                       "start_ts": pub["start_ts"], "end_ts": pub["end_ts"], "per_wallet": pub["per_wallet"],
                       "fee_recipient": pub["fee_recipient"]})
    try:
        raw_root = rpc.eth_call(sd, "0x" + abi.selector_of("getAllowListMerkleRoot(address)") + _addr_word(contract))
        root = raw_root.lower() if isinstance(raw_root, str) and re.fullmatch(r"0x[0-9a-f]{64}", raw_root, re.I) else None
    except Exception:
        root = None
    try:
        signers = _words(rpc.eth_call(sd, "0x" + abi.selector_of("getSigners(address)") + _addr_word(contract)))
        n_signers = signers[1] if len(signers) >= 2 and len(signers) >= 2 + signers[1] else 0
    except Exception:
        n_signers = 0
    if n_signers:
        stages.append({"key": "signed", "gated": True, "label": "server-signed allowlist",
                       "signers": n_signers, "price_wei": None, "start_ts": None, "end_ts": None,
                       "per_wallet": None, "fee_recipient": None})
    if root and root != ZERO_ROOT:
        stages.append({"key": "allowlist", "gated": True, "merkle_root": root, "price_wei": None,
                       "start_ts": None, "end_ts": None, "per_wallet": None, "fee_recipient": None})
    return stages


def _call_word(rpc, contract, sig):
    try:
        return _word(rpc.eth_call(contract, "0x" + abi.selector_of(sig)))
    except Exception:
        return None


def _call_str(rpc, contract, sig):
    try:
        h = rpc.eth_call(contract, "0x" + abi.selector_of(sig))
        h = h[2:] if h.startswith("0x") else h
        if len(h) < 128:
            return None
        off = int(h[0:64], 16) * 2
        n = int(h[off:off + 64], 16)
        s = bytes.fromhex(h[off + 64: off + 64 + 2 * n]).decode("utf-8", "replace").strip()
        return s[:80] or None
    except Exception:
        return None


EIP7702_PREFIX = "0xef0100"  # EIP-7702 delegation designator: an EOA whose code points at an implementation


def resolve_eip7702(code):
    """Delegate address if code is an EIP-7702 delegation designator (EOA, not a contract), else None."""
    c = code[2:].lower() if code.startswith("0x") else code.lower()
    if c.startswith(EIP7702_PREFIX[2:]) and len(c) == 6 + 40:
        return "0x" + c[6:]
    return None


def nft_standard(contract, rpc):
    """ERC-165 check. Returns 'erc721', 'erc1155', 'erc721+erc2981' style label or None. Never raises."""
    def supports(iid):
        try:
            w = _word(rpc.eth_call(contract, "0x" + abi.selector_of("supportsInterface(bytes4)") + iid + "0" * 56))
            return w == 1
        except Exception:
            return False
    if supports("80ac58cd"):
        return "erc721"
    if supports("d9b67a26"):
        return "erc1155"
    return None


def identity(contract, rpc):
    """Name, symbol, owner, standard for a token contract. Missing values are None."""
    return {"name": _call_str(rpc, contract, "name()"),
            "symbol": _call_str(rpc, contract, "symbol()"),
            "owner": _call_addr(rpc, contract, "owner()"),
            "standard": nft_standard(contract, rpc)}


def _call_addr(rpc, contract, sig):
    try:
        w = _word(rpc.eth_call(contract, "0x" + abi.selector_of(sig)))
    except Exception:
        return None
    if not w:
        return None
    return "0x" + format(w, "040x")


def fetch_verified_abi(chain_id, contract, opener=None):
    """Fetch a verified ABI from Sourcify without requiring an API key."""
    if opener is None:
        abi_items, _source, errors = provider_manager.verified_abi(chain_id, contract)
        if abi_items:
            return abi_items, None
        detail = "; ".join(errors[:3])
        return [], f"verified ABI lookup failed ({detail})" if detail else None

    for match in ("full_match", "partial_match"):
        url = f"https://repo.sourcify.dev/contracts/{match}/{int(chain_id)}/{contract}/metadata.json"
        request = urllib.request.Request(url, headers={"User-Agent": "acobot/0.1"})
        try:
            with opener(request, timeout=3) as response:
                metadata = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                continue
            return [], f"verified ABI lookup failed (HTTP {exc.code})"
        except Exception as exc:
            detail = str(exc).replace("\n", " ")[:200]
            suffix = f": {detail}" if detail else ""
            return [], f"verified ABI lookup failed ({type(exc).__name__}{suffix})"
        if not isinstance(metadata, dict):
            return [], "verified ABI lookup returned malformed metadata"
        output = metadata.get("output")
        abi_items = (output.get("abi") if isinstance(output, dict) else None) or metadata.get("abi")
        if isinstance(abi_items, list):
            return [item for item in abi_items if isinstance(item, dict)], None
    return [], None


def _abi_signature(item):
    if item.get("type") != "function" or not item.get("name"):
        return None
    inputs = item.get("inputs", [])
    types = [arg.get("type") for arg in inputs]
    if any(not typ or typ.startswith("tuple") for typ in types):
        return None
    return f"{item['name']}({','.join(types)})"


def detect(contract, rpc, log=None):
    code = rpc.get_code(contract)
    if not code or code in ("0x", "0x0"):
        raise NoContract("no contract code at that address on this chain")

    deleg = resolve_eip7702(code)
    if deleg:
        # an EOA with a 7702 delegation: a wallet, not a mint contract. report it, do not pretend it is an NFT
        out = {"contract": contract, "source": "EIP-7702 delegated wallet", "delegate": deleg,
               "selector_count": 0, "proxy_impl": None, "price_wei": None, "start_ts": None, "end_ts": None,
               "per_wallet": None, "sale_started": None, "paused": None, "mint_sig": None, "mint_to": None,
               "fee_recipient": None, "seadrop": False, "phases": [], "notes": [], "kind": "eoa_7702",
               "name": None, "symbol": None, "owner": None, "standard": None}
        out["notes"].append("this address is an EOA (wallet) with an EIP-7702 delegation, not an NFT contract")
        import time
        out["onchain_read_ts"] = int(time.time())
        return out

    impl = resolve_eip1167(code)
    impl_code = rpc.get_code(impl) if impl else None
    analyze_code = impl_code if impl_code and impl_code not in ("0x", "0x0") else code
    sels = abi.selectors_from_bytecode(analyze_code)
    abi_contract = impl or contract
    chain_id = getattr(rpc, "expected_chain_id", None)
    verified_abi, abi_error = (fetch_verified_abi(chain_id, abi_contract)
                               if chain_id is not None else ([], None))
    sourcify_names = {}
    for item in verified_abi:
        signature = _abi_signature(item)
        if signature:
            selector = abi.selector_of(signature)
            if selector in sels:
                sourcify_names[selector] = signature
    missing = [selector for selector in sels if selector not in sourcify_names]
    with cf.ThreadPoolExecutor(max_workers=12) as ex:
        resolved = dict(sourcify_names)
        resolved.update(zip(missing, ex.map(abi.lookup_4byte, missing)))
    named = abi.analyze(analyze_code, resolve=lambda s: resolved.get(s))
    names = [c for v in named["named"].values() for c in v.split(" | ")]
    out_names = sorted(set(names))

    out = {"contract": contract, "selector_count": named["selector_count"],
           "proxy_impl": impl, "source": "bytecode",
           "price_wei": None, "start_ts": None, "end_ts": None, "per_wallet": None,
           "sale_started": None, "paused": None, "mint_sig": None, "mint_to": None,
           "fee_recipient": None, "seadrop": False,
           "phases": sorted({n for n in names if PHASE_RE.search(n)}),
           "notes": [], "selector_names": out_names}
    out["abi_source"] = "Sourcify" if sourcify_names else None
    if abi_error:
        out["notes"].append(abi_error)

    # Independent collection reads are parallelized, but every value is from this detect run.
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        futures = {
            "name": ex.submit(_call_str, rpc, contract, "name()"),
            "max_supply": ex.submit(_call_word, rpc, contract, "maxSupply()"),
            "minted": ex.submit(_call_word, rpc, contract, "totalSupply()"),
            "symbol": ex.submit(_call_str, rpc, contract, "symbol()"),
            "owner": ex.submit(_call_addr, rpc, contract, "owner()"),
            "standard": ex.submit(nft_standard, contract, rpc),
        }
        ident = {k: f.result() for k, f in futures.items()}
    out.update({k: ident[k] for k in ("name", "max_supply", "minted", "symbol", "owner", "standard")})
    out["kind"] = "nft" if ident["standard"] else "contract"
    stages = seadrop_stages(contract, rpc)
    if stages:
        public_stage = next((stage for stage in stages if stage["key"] == "public"), None)
        out["stages"] = stages
        out["phases"] = sorted({stage["key"] for stage in stages})
        out.update({"seadrop": True, "source": "OpenSea SeaDrop",
                    "price_wei": public_stage["price_wei"] if public_stage else None,
                    "start_ts": public_stage["start_ts"] if public_stage else None,
                    "end_ts": public_stage["end_ts"] if public_stage else None,
                    "per_wallet": public_stage["per_wallet"] if public_stage else None,
                    "fee_recipient": public_stage["fee_recipient"] if public_stage else None,
                    "mint_sig": SEADROP_MINT_SIG, "mint_to": SEADROP})
        if public_stage and public_stage["end_ts"] and public_stage["start_ts"] and public_stage["end_ts"] < public_stage["start_ts"]:
            out["notes"].append("drop end is before start")
        import time
        out["onchain_read_ts"] = int(time.time())
        return out

    def read(sig):
        sel = abi.selector_of(sig)
        if sel not in sels:
            return None
        try:
            return _word(rpc.eth_call(contract, "0x" + sel))
        except Exception:
            return None

    for k in ("price", "cost", "mintPrice", "PRICE"):
        v = read(abi.KNOWN_READS[k])
        if v is not None:
            out["price_wei"] = v; out["price_fn"] = abi.KNOWN_READS[k]; break
    for k in ("startTime", "saleStart", "mintStart", "whitelistStartTime", "publicStartTime"):
        sig = abi.KNOWN_READS.get(k)
        if not sig:
            continue
        v = read(sig)
        if v is not None:
            if TS_MIN <= v <= TS_MAX:
                out["start_ts"] = v; out["start_fn"] = sig
            else:
                out["notes"].append(f"{sig} returned {v}, not a unix timestamp")
            break
    for k in ("maxPerWallet", "maxMintPerWallet", "maxPerTx", "MAX_PER_WALLET", "MAX_MINT"):
        sig = abi.KNOWN_READS.get(k)
        if not sig:
            continue
        v = read(sig)
        if v is not None and v > 0:
            out["per_wallet"] = v; break
    v = read(abi.KNOWN_READS["saleStarted"])
    if v is not None:
        out["sale_started"] = bool(v); out["sale_started_fn"] = abi.KNOWN_READS["saleStarted"]
    v = read(abi.KNOWN_READS["paused"])
    if v is not None:
        out["paused"] = bool(v)

    mint = [n for n in names if MINT_SIG_RE.match(n)]
    if mint:
        out["mint_sig"] = sorted(mint, key=lambda s: ("(uint256)" not in s, s))[0]
    elif named["mint_functions"]:
        out["notes"].append("mint-like functions found but none with a simple (uint256) or () signature: " + ", ".join(named["mint_functions"]))
    else:
        out["notes"].append("no mint-like function found in the dispatcher")
    if out["price_wei"] is None:
        out["notes"].append("price getter not found")
    if out["start_ts"] is None and not out.get("sale_started_fn"):
        out["notes"].append("start time not detected")
    import time
    out["onchain_read_ts"] = int(time.time())
    return out
