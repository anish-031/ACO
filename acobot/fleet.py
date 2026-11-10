"""Fleet ops: per-wallet on-chain eligibility, fund (native batch) and sweep (ERC-721 batch).

Read-only functions here never sign. Signing happens only in execute_* after the admin confirms.
Keys are decrypted just before signing and never returned or logged.
"""
import concurrent.futures as cf
import time
from acobot.rpc import RPC
from eth_account import Account
from eth_utils import keccak, to_checksum_address as cs
from acobot import abi, mintread, sniper, wallets

GAS_TRANSFER = 21_000
GAS_NFT_TRANSFER = 120_000


def _word_addr(a):
    return a[2:].lower().rjust(64, "0")


def _int(h):
    h = h[2:] if h and h.startswith("0x") else (h or "0")
    return int(h or "0", 16)


# ---------- eligibility ----------

def minted_so_far(rpc, contract, addr, seadrop=False):
    """Tokens this address has minted. SeaDrop: getMintStats(nft, minter) on the SeaDrop contract
    returns (minterNumMinted, currentTotalSupply, maxSupply). Plain NFT: balanceOf as a proxy.
    None if unknown."""
    if seadrop:
        try:
            raw = rpc.eth_call(mintread.SEADROP, "0x" + abi.selector_of("getMintStats(address,address)")
                               + _word_addr(contract) + _word_addr(addr))
            return _int(raw[2:66])
        except Exception:
            return None
    try:
        return _int(rpc.eth_call(contract, "0x" + abi.selector_of("balanceOf(address)") + _word_addr(addr)))
    except Exception:
        return None


def check_wallet(rpc, chain, mint, addr, label, qty, fees=None):
    """One wallet's on-chain verdict for a draft/mint dict. Never raises.

    Returns dict: label, address, masked, balance_wei, cost_wei, fee_wei, gas, minted, per_wallet,
    qty, verdict (ok|warn|fail), reason, reasons (list of plain strings).
    """
    price = mint.get("price_wei") or 0
    per = mint.get("per_wallet")
    out = {"label": label, "address": addr, "masked": wallets.mask(addr), "qty": qty,
           "balance_wei": None, "cost_wei": None, "fee_wei": None, "gas": None,
           "minted": None, "per_wallet": per, "verdict": "ok", "reasons": []}
    issues = []  # (level, text)

    # 1. allowlist stages: checked properly with a real simulation below. The estimate either
    # goes through (wallet can mint on this stage) or reverts (wallet is not on the allowlist).
    phase = mint.get("phase")
    gated = phase in ("allowlist", "signed") or mint.get("gated")

    # 2. balance
    try:
        bal = int(rpc.call("eth_getBalance", [addr, "latest"]), 16)
        out["balance_wei"] = bal
    except Exception:
        issues.append(("warn", "balance could not be read right now"))
        bal = None

    # 3. gas + fee estimate (live market, the speed tier chosen for the mint)
    try:
        base, tip, floor = sniper.market_fees(rpc, chain)
        prio, mx = sniper.tier_fees(base, tip, floor, mint.get("speed", "fast"))
    except Exception:
        base = prio = mx = None
    gas = None
    estimated = False
    try:
        value = price * qty
        if mint.get("seadrop"):
            to = mint["mint_to"]; data = sniper.encode_seadrop_mint(mint["contract"], mint["fee_recipient"], addr, qty)
        else:
            to = mint["contract"]; data = sniper.encode_mint(mint["mint_sig"], qty)
        est = int(rpc.call("eth_estimateGas", [{"from": addr, "to": to, "value": hex(value), "data": data}]), 16)
        gas = max(21_000, int(est * sniper.AUTO_BUFFER))
        estimated = True
        if gated:
            out["eligibility"] = True   # the chain simulated THIS wallet minting THIS stage: allowlist ok
    except Exception as exc:
        # pre-sale: eth_estimateGas reverts. use the research-based per-contract estimate (preview only)
        gas = sniper.static_gas(mint, qty)
        phase_open = True
        try:
            start_ts = int(mint.get("start_ts") or 0)
            phase_open = not start_ts or int(time.time()) >= start_ts
        except (TypeError, ValueError, OverflowError):
            phase_open = True
        if gated and phase_open:
            out["eligibility"] = False
            issues.append(("fail", "allowlist check: the chain rejected this wallet's mint (not on the allowlist)"))
        elif gated:
            out["eligibility"] = None
    if isinstance(mint.get("gas_limit"), int) and mint["gas_limit"] > 0:
        gas = max(gas, int(mint["gas_limit"]))
    if not estimated:
        issues.append(("info", "estimate from similar mints, exact at fire time"))
    out["gas"] = gas
    gas_price = min(mx, base + prio) if mx else None
    fee_wei = (gas * gas_price) if gas_price else None
    out["fee_wei"] = fee_wei
    cost = price * qty
    out["cost_wei"] = cost + (fee_wei or 0)

    # 4. mint fee + gas vs balance
    if bal is not None:
        if bal < price * qty:
            issues.append(("warn", f"not enough for the mint fee ({price * qty / 1e18:.5f} ETH needed, {bal / 1e18:.5f} ETH here)"))
        elif fee_wei is not None and bal < cost + (fee_wei or 0):
            issues.append(("warn", f"not enough gas for fee+gas (needs ~{out['cost_wei'] / 1e18:.5f} ETH, has {bal / 1e18:.5f} ETH)"))

    # 5. already minted vs per-wallet limit
    minted = minted_so_far(rpc, mint["contract"], addr, seadrop=bool(mint.get("seadrop")))
    out["minted"] = minted
    if per and minted is not None:
        left = per - minted
        if left <= 0:
            issues.append(("fail", f"already minted max ({minted}/{per})"))
        elif qty > left:
            issues.append(("warn", f"qty {qty} is above the {left} left for this wallet, will mint {left}"))
    elif per and qty > per:
        issues.append(("warn", f"qty {qty} is above the per-wallet limit {per}, will mint {per}"))

    fails = [t for lvl, t in issues if lvl == "fail"]
    warns = [t for lvl, t in issues if lvl == "warn"]  # info never changes the verdict
    out["reasons"] = fails + warns
    out["verdict"] = "fail" if fails else ("warn" if warns else "ok")
    out["reason"] = out["reasons"][0] if out["reasons"] else "ready"
    return out


def check_all(chain, mint, entries):
    """chain: config.Chain. Entries are (label,address,qty) or (owner,label,address,qty).
    Runs in parallel and always returns one verdict per wallet."""
    rpc = RPC(chain)

    def one(e):
        label, addr, qty = e[-3:]
        try:
            return check_wallet(rpc, chain, mint, addr, label, qty)
        except Exception as ex:
            return {"label": label, "address": addr, "masked": wallets.mask(addr), "verdict": "warn",
                    "reason": f"check failed: {type(ex).__name__}", "reasons": [f"check failed: {type(ex).__name__}"]}

    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        return list(ex.map(one, entries))


# ---------- fund ----------

def fund_plan(main_addr, main_balance_wei, targets, amount_wei, chain, rpc):
    """targets: [(label, address)]. Returns {'rows', 'total_wei', 'gas_each', 'fee_wei', 'ok', 'why'}."""
    base, tip, floor = sniper.market_fees(rpc, chain)
    _, mx = sniper.tier_fees(base, tip, floor, "fast")
    fee_each = GAS_TRANSFER * min(mx, base + tip)
    total = amount_wei * len(targets)
    need = total + fee_each * len(targets)
    ok = main_balance_wei >= need
    return {"rows": [(l, a, amount_wei) for l, a in targets], "total_wei": total,
            "fee_wei": fee_each * len(targets), "need_wei": need, "ok": ok,
            "why": "" if ok else f"main wallet short: needs ~{need / 1e18:.5f} ETH, has {main_balance_wei / 1e18:.5f} ETH"}


def execute_fund(owner_id, main_label, main_addr, rows, chain, read_rpc, bcast):
    """Sign and broadcast native transfers from the main wallet. Returns list of result dicts."""
    ents = wallets.signing_entries([owner_id], only={main_addr.lower()})
    if not ents:
        raise RuntimeError("main wallet key not found")
    _, _, _, priv, _ = ents[0]
    nonce = int(read_rpc.call("eth_getTransactionCount", [main_addr, "pending"]), 16)
    base, tip, floor = sniper.market_fees(read_rpc, chain)
    prio, mx = sniper.tier_fees(base, tip, floor, "fast")
    out = []
    for i, (label, addr, amount) in enumerate(rows):
        try:
            tx = {"type": 2, "chainId": chain.chain_id, "nonce": nonce + i, "to": cs(addr), "value": amount,
                  "data": "0x", "gas": GAS_TRANSFER, "maxFeePerGas": mx, "maxPriorityFeePerGas": prio}
            raw = Account.sign_transaction(tx, priv).raw_transaction.hex()
            res = bcast.broadcast(raw)
            ok = any(isinstance(v, str) and v.startswith("0x") for v in res.values())
            out.append({"label": label, "address": addr, "ok": ok, "why": "" if ok else "no rpc accepted it"})
        except Exception as ex:
            out.append({"label": label, "address": addr, "ok": False, "why": type(ex).__name__})
    return out


# ---------- sweep (ERC-721 enumerable) ----------

def nft_holdings(rpc, contract, addr):
    """Token ids held by addr via ERC721Enumerable. Returns (ids, status) where status is 'ok', 'not_enumerable', 'error'."""
    try:
        n = _int(rpc.eth_call(contract, "0x" + abi.selector_of("balanceOf(address)") + _word_addr(addr)))
    except Exception:
        return [], "error"
    if n == 0:
        return [], "ok"
    ids = []
    for i in range(n):
        try:
            data = "0x" + abi.selector_of("tokenOfOwnerByIndex(address,uint256)") + _word_addr(addr) + format(i, "064x")
            ids.append(_int(rpc.eth_call(contract, data)))
        except Exception:
            return ids, "not_enumerable"
    return ids, "ok"


TOPIC_TRANSFER = "0x" + keccak(text="Transfer(address,address,uint256)").hex()

def pad_topic(addr):
    return "0x" + "0" * 24 + addr.lower().replace("0x", "")

def discover_nft_contracts(rpc, addresses, head=-20000):
    """Recent Transfer logs touching fleet wallets -> distinct NFT contract addresses."""
    pads = [pad_topic(a) for a in addresses]
    topics = [[TOPIC_TRANSFER], pads, None]
    try:
        endn = _int(rpc.call("eth_blockNumber", []))
        logs = rpc.call("eth_getLogs", [{"fromBlock": hex(max(0, endn + head)), "toBlock": hex(endn), "topics": topics}]) or []
    except Exception:
        return []
    out = []
    for lg in logs:
        ca = (lg.get("address") or "").lower()
        tps = lg.get("topics") or []
        if ca not in out and len(tps) >= 4:
            out.append(ca)
    return out[:20]


def sweep_plan(rpc, contract, holders):
    """holders: [(label, address)]. Returns rows of (label, address, [token ids]) plus notes."""
    rows, notes = [], []
    for label, addr in holders:
        ids, st = nft_holdings(rpc, contract, addr)
        if st == "not_enumerable":
            notes.append(f"{label}: contract doesn't expose token ids, skipped")
        elif st == "error":
            notes.append(f"{label}: holdings could not be read right now")
        elif ids:
            rows.append((label, addr, ids))
    return rows, notes


def execute_sweep(owner_id, contract, rows, main_addr, chain, read_rpc, bcast):
    """rows: [(label, from_addr, [ids])]. Signs each wallet's transfers with its own key."""
    addrs = {r[1].lower() for r in rows}
    keys = {e[2].lower(): e[3] for e in wallets.signing_entries([owner_id], only=addrs)}
    base, tip, floor = sniper.market_fees(read_rpc, chain)
    prio, mx = sniper.tier_fees(base, tip, floor, "fast")
    out = []
    for label, frm, ids in rows:
        priv = keys.get(frm.lower())
        if not priv:
            out.append({"label": label, "ok": False, "why": "key not found", "moved": 0}); continue
        nonce = int(read_rpc.call("eth_getTransactionCount", [frm, "pending"]), 16)
        moved = 0
        for j, tid in enumerate(ids):
            try:
                data = ("0x" + abi.selector_of("safeTransferFrom(address,address,uint256)")
                        + _word_addr(frm) + _word_addr(main_addr) + format(tid, "064x"))
                tx = {"type": 2, "chainId": chain.chain_id, "nonce": nonce + j, "to": cs(contract),
                      "value": 0, "data": data, "gas": GAS_NFT_TRANSFER, "maxFeePerGas": mx, "maxPriorityFeePerGas": prio}
                raw = Account.sign_transaction(tx, priv).raw_transaction.hex()
                res = bcast.broadcast(raw)
                if any(isinstance(v, str) and v.startswith("0x") for v in res.values()):
                    moved += 1
            except Exception as ex:
                out.append({"label": label, "ok": False, "why": type(ex).__name__, "moved": moved})
                break
        else:
            out.append({"label": label, "ok": moved == len(ids), "why": "" if moved == len(ids) else "some transfers rejected",
                        "moved": moved})
    return out


# ---------- token transfers (native or ERC-20) and token sweep ----------
from acobot import tokens as tkn

GAS_ERC20 = 100_000


def _send_fee(chain, rpc, speed="fast"):
    base, tip, floor = sniper.market_fees(rpc, chain)
    prio, mx = sniper.tier_fees(base, tip, floor, speed)
    return prio, mx


def plan_transfers(rpc, chain, token, senders, to_addr, amount_text, speed="fast"):
    """senders: [(label, address)]. amount_text: a number or 'all'.
    Returns rows: {label, address, masked, symbol, raw, decimals, to, ok, why}. Read-only."""
    prio, mx = _send_fee(chain, rpc, speed)
    gas = GAS_TRANSFER if token["kind"] == "native" else GAS_ERC20
    fee_each = gas * mx
    rows = []
    for label, addr in senders:
        row = {"label": label, "address": addr, "masked": wallets.mask(addr), "symbol": token["symbol"],
               "decimals": token["decimals"], "raw": 0, "to": to_addr, "ok": True, "why": ""}
        try:
            if token["kind"] == "native":
                bal = int(rpc.call("eth_getBalance", [addr, "latest"]), 16)
                spendable = bal - fee_each
            else:
                bal = tkn.balance_of(rpc, token["contract"], addr)
                spendable = bal
                gas_bal = int(rpc.call("eth_getBalance", [addr, "latest"]), 16)
                if gas_bal < fee_each:
                    row["ok"] = False; row["why"] = "no ETH for gas"
            if amount_text.lower() == "all":
                raw = max(spendable, 0)
            else:
                raw = tkn.parse_amount(amount_text, token["decimals"])
                if raw > spendable:
                    row["ok"] = False
                    need = tkn.fmt_amount(raw + fee_each, token["decimals"])
                    have = tkn.fmt_amount(max(bal, 0), token["decimals"])
                    row["why"] = f"insufficient funds — needs ~{need} {token['symbol']} (incl gas), wallet has {have} {token['symbol']}"
            if raw <= 0:
                row["ok"] = False
                if not row["why"]:
                    have = tkn.fmt_amount(max(bal, 0), token["decimals"])
                    row["why"] = f"insufficient funds — wallet has {have} {token['symbol']}, not enough for gas"
            row["raw"] = raw
        except Exception as ex:
            row["ok"] = False; row["why"] = f"read failed ({type(ex).__name__})"
        rows.append(row)
    return rows


def _transfer_tx(chain, token, frm, to, raw, nonce, prio, mx, priv):
    if token["kind"] == "native":
        tx = {"type": 2, "chainId": chain.chain_id, "nonce": nonce, "to": cs(to), "value": raw,
              "data": "0x", "gas": GAS_TRANSFER, "maxFeePerGas": mx, "maxPriorityFeePerGas": prio}
    else:
        data = "0x" + abi.selector_of("transfer(address,uint256)") + _word_addr(to) + format(raw, "064x")
        tx = {"type": 2, "chainId": chain.chain_id, "nonce": nonce, "to": cs(token["contract"]),
              "value": 0, "data": data, "gas": GAS_ERC20, "maxFeePerGas": mx, "maxPriorityFeePerGas": prio}
    return Account.sign_transaction(tx, priv).raw_transaction.hex()


def execute_transfers(owner_id, rows, token, chain, read_rpc, bcast, speed="fast"):
    """Sign and broadcast each ok row from its own wallet. Returns per-row dicts with tx hash.
    Hash is the local keccak of the signed tx (what the RPC accepted), plus the RPC result."""
    live = [r for r in rows if r.get("ok")]
    addrs = {r["address"].lower() for r in live}
    keys = {e[2].lower(): e[3] for e in wallets.signing_entries([owner_id], only=addrs)} if addrs else {}
    prio, mx = _send_fee(chain, read_rpc, speed)
    out = []
    for r in rows:
        if not r.get("ok"):
            out.append({**r, "tx": None, "sent": False}); continue
        priv = keys.get(r["address"].lower())
        if not priv:
            out.append({**r, "ok": False, "why": "key not found", "tx": None, "sent": False}); continue
        try:
            nonce = int(read_rpc.call("eth_getTransactionCount", [r["address"], "pending"]), 16)
            raw_tx = _transfer_tx(chain, token, r["address"], r["to"], r["raw"], nonce, prio, mx, priv)
            res = bcast.broadcast(raw_tx)
            acc = [v for v in res.values() if isinstance(v, str) and v.startswith("0x")]
            if acc:
                out.append({**r, "tx": acc[0], "sent": True, "why": ""})
            else:
                out.append({**r, "ok": False, "why": "no rpc accepted it", "tx": None, "sent": False})
        except Exception as ex:
            out.append({**r, "ok": False, "why": type(ex).__name__, "tx": None, "sent": False})
    return out


def plan_token_sweep(rpc, chain, contracts_meta, holders, to_addr):
    """All tracked ERC-20s (or one) from every holder to to_addr. Returns list of token-level rows
    and notes. contracts_meta: [{contract, symbol, decimals}]."""
    rows, notes = [], []
    for tk in contracts_meta:
        token = {"kind": "erc20", "symbol": tk["symbol"], "decimals": int(tk["decimals"]), "contract": tk["contract"]}
        for label, addr in holders:
            try:
                raw = tkn.balance_of(rpc, tk["contract"], addr)
            except Exception:
                notes.append(f"{label} {tk['symbol']}: balance unreadable")
                continue
            if raw > 0:
                rows.append({"label": label, "address": addr, "masked": wallets.mask(addr), "symbol": tk["symbol"],
                             "decimals": token["decimals"], "raw": raw, "to": to_addr, "ok": True, "why": "",
                             "token": token})
    return rows, notes


def execute_token_sweep(owner_id, rows, chain, read_rpc, bcast):
    """rows from plan_token_sweep; each row carries its own token dict."""
    out = []
    keys_cache = {}
    prio, mx = _send_fee(chain, read_rpc)
    addrs = {r["address"].lower() for r in rows}
    keys = {e[2].lower(): e[3] for e in wallets.signing_entries([owner_id], only=addrs)} if addrs else {}
    for r in rows:
        priv = keys.get(r["address"].lower())
        if not priv:
            out.append({**r, "ok": False, "why": "key not found", "tx": None, "sent": False}); continue
        try:
            nonce = int(read_rpc.call("eth_getTransactionCount", [r["address"], "pending"]), 16)
            raw_tx = _transfer_tx(chain, r["token"], r["address"], r["to"], r["raw"], nonce, prio, mx, priv)
            res = bcast.broadcast(raw_tx)
            acc = [v for v in res.values() if isinstance(v, str) and v.startswith("0x")]
            if acc:
                out.append({**r, "tx": acc[0], "sent": True, "why": ""})
            else:
                out.append({**r, "ok": False, "why": "no rpc accepted it", "tx": None, "sent": False})
        except Exception as ex:
            out.append({**r, "ok": False, "why": type(ex).__name__, "tx": None, "sent": False})
    return out
