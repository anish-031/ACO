"""Read-only balance aggregation and guarded sweep planning helpers."""
from decimal import Decimal
from acobot import tokens as tkn, wallets


def filter_selected_wallets(wallet_rows, selected_addresses=None):
    """Filter wallet dicts by selected address; None means all wallets."""
    if selected_addresses is None:
        return list(wallet_rows)
    selected = {str(address).lower() for address in selected_addresses}
    return [w for w in wallet_rows if w["address"].lower() in selected]


def toggle_wallet_selection(selected_addresses, address):
    """Toggle one address; selecting from all starts a specific-wallet selection."""
    selected = set() if selected_addresses is None else {str(a).lower() for a in selected_addresses}
    address = str(address).lower()
    if address in selected:
        selected.remove(address)
    else:
        selected.add(address)
    return sorted(selected)


def aggregate_balances(rows, reader):
    """rows are (label,address); reader(address) returns {amount, usd, symbol}.
    Sum only successful reads; preserve per-wallet errors as terse exception names.
    """
    out, total, usd_total, usd_complete = [], Decimal(0), Decimal(0), True
    successes = 0
    for label, address in rows:
        try:
            snap = reader(address)
            amount = Decimal(str(snap["amount"]))
            usd = snap.get("usd")
            usd = None if usd is None else Decimal(str(usd))
            out.append({"label": label, "address": address, "amount": amount, "usd": usd,
                        "symbol": snap["symbol"], "error": None})
            total += amount
            successes += 1
            if usd is None:
                usd_complete = False
            else:
                usd_total += usd
        except Exception as exc:
            out.append({"label": label, "address": address, "amount": None, "usd": None,
                        "symbol": None, "error": type(exc).__name__})
    return {"rows": out, "total": total,
            "usd_total": usd_total if usd_complete and successes else None}


def plan_sweep(rpc, chain, token, senders, destination):
    """Read-only all-balance plan through the fleet transfer path."""
    from acobot import fleet
    rows = fleet.plan_transfers(rpc, chain, token, senders, destination, "all")
    _, max_fee = fleet._send_fee(chain, rpc)
    gas_limit = fleet.GAS_TRANSFER if token["kind"] == "native" else fleet.GAS_ERC20
    fee_each = gas_limit * max_fee
    for row in rows:
        row["gas_wei"] = fee_each if row.get("ok") else 0
    good = [r for r in rows if r.get("ok")]
    return {"rows": rows, "total_raw": sum(int(r["raw"]) for r in good),
            "wallet_count": len(good), "gas_wei": fee_each * len(good),
            "destination": destination, "skipped": [r for r in rows if not r.get("ok")]}


def execute_sweep(owner_id, plan, token, chain, read_rpc, bcast, *, confirmed=False):
    """Execution is impossible unless caller supplies explicit final confirmation."""
    if not confirmed:
        raise PermissionError("final confirmation required")
    from acobot import fleet
    return fleet.execute_transfers(owner_id, plan["rows"], token, chain, read_rpc, bcast)


def evm_token_snapshot(rpc, chain_name, address, token, usd_for):
    if token["kind"] == "native":
        raw = int(rpc.call("eth_getBalance", [address, "latest"]), 16)
        return {"amount": Decimal(raw) / (Decimal(10) ** token["decimals"]),
                "usd": usd_for(None, raw, token["decimals"]), "symbol": token["symbol"]}
    raw = tkn.balance_of(rpc, token["contract"], address)
    return {"amount": Decimal(raw) / (Decimal(10) ** token["decimals"]),
            "usd": usd_for(token["contract"], raw, token["decimals"]), "symbol": token["symbol"]}


def plan_solana_sweep(senders, destination, asset, *, get_balance, get_token_balance, rent_exempt, fee_lamports=5000):
    """Plan SOL/SPL sweeps, preserving rent-exempt reserve and fee in each sender."""
    rows, total, gas_total = [], 0, 0
    for label, address in senders:
        row = {"label": label, "address": address, "to": destination, "ok": True, "why": "",
               "symbol": asset["symbol"], "decimals": asset["decimals"], "kind": asset["kind"],
               "mint": asset.get("mint"), "raw": 0}
        try:
            lamports = int(get_balance(address))
            if asset["kind"] == "native":
                reserve = int(rent_exempt(address))
                row["raw"] = max(0, lamports - reserve - int(fee_lamports))
                row["rent_reserve"] = reserve
                row["fee_lamports"] = int(fee_lamports)
                if row["raw"] <= 0:
                    row.update(ok=False, why="insufficient SOL after fee and rent reserve")
            elif lamports < int(fee_lamports):
                row.update(ok=False, why="no SOL for gas")
            else:
                snap = get_token_balance(address, asset["mint"])
                row["raw"] = int(snap["raw"])
                row["token_account"] = snap.get("token_account")
                if row["raw"] <= 0:
                    row.update(ok=False, why="no token balance")
            if row["ok"]:
                total += row["raw"]
                gas_total += int(fee_lamports)
        except Exception as exc:
            row.update(ok=False, why=f"read failed ({type(exc).__name__})")
        rows.append(row)
    good = [r for r in rows if r["ok"]]
    return {"rows": rows, "total_raw": total, "wallet_count": len(good), "gas_lamports": gas_total,
            "gas_wei": gas_total, "destination": destination, "skipped": [r for r in rows if not r["ok"]]}


def execute_solana_sweep(owner_id, plan, asset, destination, *, send_native, send_token, confirmed=False):
    if not confirmed:
        raise PermissionError("final confirmation required")
    results = []
    for row in plan["rows"]:
        if not row.get("ok"):
            results.append({"label": row["label"], "ok": False, "why": row.get("why", "skipped")})
            continue
        try:
            if asset["kind"] == "native":
                tx = send_native(row["address"], destination, row["raw"])
            else:
                tx = send_token(row["address"], row["token_account"], destination, asset["mint"], row["raw"])
            results.append({"label": row["label"], "ok": True, "tx": tx, "raw": row["raw"]})
        except Exception as exc:
            results.append({"label": row["label"], "ok": False, "why": type(exc).__name__})
    return results
