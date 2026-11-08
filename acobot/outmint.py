"""Bars (/outmint) engine: cast (mint) and melt against a bar contract.

No bar contract is live yet (verified 2026-10-30). The contract address,
chain and ABI details live in bars_config.json (or config defaults) so a
verified address can be dropped in without code changes. Preparation is
read-only; execute_cast/execute_melt broadcast only after the Telegram
final-confirm tap. Fast-mint: nonce, gas and calldata are prebuilt at
prepare time so execution is a single sign+broadcast.
"""
import json
import os
import time
from decimal import Decimal
from eth_abi import encode
from eth_account import Account
from eth_utils import keccak, to_checksum_address

from acobot.tokenbuy import _selector, _addr, _int

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bars_config.json")

# Standard ERC-721 public mint surface. If the live bar contract differs,
# update mint_signature/mint_value_fn in the config; nothing else changes.
DEFAULTS = {
    "chain": "ethereum",
    "contract": None,           # None until a verified deployment exists
    "mint_signature": "mint(uint256)",          # mint(barId)
    "mint_payment": "value",                    # payable native; 'erc20' uses approve+transfer
    "payment_token": None,                      # e.g. BALANCE 0x3620...eA07 if bars are bought with it
    "max_per_tx": 10,
    "cast_seconds": 8 * 24 * 3600,              # unverified; display-only
    "melt_signature": "melt(uint256)",
    "balance_signature": "balanceOf(address)",
}

BALANCE_TOKEN = "0x3620540c994433D7B4a628C46d2ED5F92572eA07"  # jack.art/exchange, not a bar contract


def load_config():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def bar_cost(bar_id):
    """Verified from jack: bar N holds N units, ascending. Bar N costs N."""
    n = int(bar_id)
    if n < 1:
        raise ValueError("bar id must be >= 1")
    return n


def calldata(cfg, action, bar_id):
    sig = cfg["mint_signature"] if action == "cast" else cfg["melt_signature"]
    args = encode(["uint256"], [int(bar_id)])
    return "0x" + (_selector(sig) + args).hex()


def _int_or_zero(v):
    try:
        return _int(v)
    except (ValueError, TypeError):
        return 0  # empty result (0x) means zero balance/allowance, e.g. testnet


def erc20_balance_raw(token, wallet, rpc):
    data = "0x" + (_selector("balanceOf(address)") + encode(["address"], [to_checksum_address(wallet)])).hex()
    return _int_or_zero(rpc.call("eth_call", [{"to": token, "data": data}, "latest"]))


def erc20_allowance_raw(token, wallet, spender, rpc):
    data = "0x" + (_selector("allowance(address,address)") +
                   encode(["address", "address"], [to_checksum_address(wallet), spender])).hex()
    return _int_or_zero(rpc.call("eth_call", [{"to": token, "data": data}, "latest"]))


def prepare(cfg, action, bar_id, count, wallet, rpc):
    """Prebuild everything for a fast confirm: calldata, nonce, gas. Read-only."""
    if not cfg.get("contract"):
        raise RuntimeError("no verified bar contract set yet. nothing can be minted.")
    if action not in ("cast", "melt"):
        raise ValueError("action must be cast or melt")
    n = int(bar_id)
    if n < 1:
        raise ValueError("bar id must be >= 1")
    count = int(count)
    if count < 1 or count > int(cfg.get("max_per_tx", 10)):
        raise ValueError(f"count must be 1..{cfg.get('max_per_tx', 10)}")
    token, wallet = _addr(cfg["contract"]), _addr(wallet)
    chain_id = _int(rpc.call("eth_chainId", []))
    if chain_id != int(cfg.get("chain_id", chain_id)):
        raise ValueError("RPC chain id mismatch")
    txs = []
    nonce = _int(rpc.call("eth_getTransactionCount", [wallet, "pending"]))
    try:
        gas_price = _int(rpc.call("eth_gasPrice", []))
    except Exception:
        gas_price = 0
    # one tx per bar (one contract per bar), up to count of them
    for i in range(count):
        data = calldata(cfg, action, n + i) if action == "cast" and n > 1 else calldata(cfg, action, n)
        call = {"from": wallet, "to": token, "data": data, "value": "0x0"}
        gas = _int(rpc.call("eth_estimateGas", [call, "pending"]))
        txs.append({"to": token, "data": data, "value": 0, "gas": gas * 120 // 100, "nonce": nonce + i})
    needs_approval = False
    approve_data = None
    if cfg.get("payment_token") and action == "cast":
        needs_approval = erc20_allowance_raw(cfg["payment_token"], wallet, token, rpc) < bar_cost(n) * count
        approve_data = "0x" + (_selector("approve(address,uint256)") +
                               encode(["address", "uint256"], [token, bar_cost(n) * count])).hex()
    return {"kind": f"bars_{action}", "chain_id": chain_id, "wallet": wallet, "action": action,
            "bar_id": n, "count": count, "unit_cost": bar_cost(n), "total_cost": bar_cost(n) * count,
            "txs": txs, "gas_price": gas_price, "needs_approval": needs_approval,
            "approve_data": approve_data, "cast_seconds": cfg.get("cast_seconds")}


def _sign_tx(tx, chain_id, gas_price, private_key):
    t = {"chainId": chain_id, "nonce": int(tx["nonce"]), "to": to_checksum_address(tx["to"]),
         "data": tx["data"], "value": int(tx["value"]), "gas": int(tx["gas"])}
    if int(gas_price or 0):
        t["gasPrice"] = int(gas_price)
    else:
        t["maxPriorityFeePerGas"] = 2_000_000_000
        t["maxFeePerGas"] = 50_000_000_000
    signed = Account.sign_transaction(t, private_key).raw_transaction.hex()
    return "0x" + signed


def execute_prepared(prepared, private_key, bcast):
    """Sign and broadcast every prebuilt tx back-to-back (fast path)."""
    if not prepared or not prepared.get("txs"):
        return False, "bad prepared mint"
    try:
        acct = Account.from_key(private_key)
        if acct.address.lower() != prepared["wallet"].lower():
            return False, "wallet/key mismatch"
        if prepared.get("needs_approval"):
            approval = {"to": prepared["txs"][0]["to"], "data": prepared["approve_data"],
                        "value": 0, "gas": 80000, "nonce": int(prepared["txs"][0]["nonce"]) - 1}
            out = bcast.broadcast(_sign_tx(approval, prepared["chain_id"], prepared.get("gas_price"), private_key))
            atx = next((v for v in out.values() if isinstance(v, str) and v.startswith("0x") and len(v) == 66), None)
            if not atx:
                return False, "approval broadcast failed"
            deadline = time.time() + 45
            while time.time() < deadline:
                try:
                    rec = bcast.call("eth_getTransactionReceipt", [atx])
                    if rec and rec.get("status") == "0x1":
                        break
                except Exception:
                    pass
                time.sleep(3)
        txids = []
        for tx in prepared["txs"]:
            out = bcast.broadcast(_sign_tx(tx, prepared["chain_id"], prepared.get("gas_price"), private_key))
            txid = next((v for v in out.values() if isinstance(v, str) and v.startswith("0x") and len(v) == 66), None)
            if not txid:
                return (True, txids) if txids else (False, "RPC did not accept transaction")
            txids.append(txid)
        return True, txids
    except Exception as exc:
        return False, f"transaction failed: {type(exc).__name__}"
