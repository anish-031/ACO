"""Pure builders for the wallet-fleet panels and mint records. No Telegram, no network.

Everything here is testable offline. Only masked addresses and labels are rendered.
"""
import html
from acobot import results as resmod

ICON = {"ok": "✅", "warn": "⚠️", "fail": "❌"}
MAX_FEED_LINES = 40


def esc(x):
    return html.escape(str(x), quote=False)


# ---------- design system ----------
# every panel: brand line, bold title, thin divider, then body. status glyphs: ✅ ❌ ⏳ ⚠️ 💰 🔥
BRAND = "⚡ <b>ACO SNIPER</b>"
DIVIDER = "─" * 22


def header(title):
    """Brand line + bold panel title + divider. title is plain text (escaped here)."""
    return [BRAND, f"<b>{esc(title)}</b>", DIVIDER]


def kv(label, value, width=10):
    """Aligned key/value row: monospace label padded to a fixed width, value after it."""
    return f"<code>{esc(label):<{width}}</code> {esc(value)}"


def card(title, lines, status=None):
    """Result card. status 'ok' | 'fail' | 'warn' | 'wait' picks the leading glyph on the title."""
    glyph = {"ok": "✅", "fail": "❌", "warn": "⚠️", "wait": "⏳"}.get(status, "")
    head = f"{glyph} <b>{esc(title)}</b>" if glyph else f"<b>{esc(title)}</b>"
    return [head, DIVIDER] + list(lines)


def hint_line(text):
    """One suggested next step, shown under a failure card."""
    return f"👉 {esc(text)}"


def mask(addr):
    """Display form: full public address (addresses are public; keys are never shown)."""
    return str(addr or "")


def eth(wei, places=5):
    if wei is None:
        return "?"
    return f"{wei / 1e18:.{places}f} ETH"


def label_of(s):
    """Strip the 'owner:' prefix sniper.presign adds to labels."""
    s = str(s or "")
    return s.split(":", 1)[-1] if ":" in s else s


def wallet_step_lines(d, allw):
    """allw: [(owner, label, address, qty)]. Returns HTML lines for the select panel."""
    excl = {str(a).lower() for a in (d.get("wex") or [])}
    chosen = [w for w in allw if str(w[2]).lower() not in excl]
    lines = [f"🎯 <b>{esc(d.get('name') or 'mint')}</b> on {esc(d.get('chain', 'ethereum'))}",
             f"<code>{esc(d.get('contract', ''))}</code>", ""]
    if not allw:
        lines.append("no wallets on this chain yet. add one in /wallets first.")
    else:
        n = len(chosen)
        lines.append(f"<b>minting from {n} wallet{'s' if n != 1 else ''}</b> (of {len(allw)} on this chain)")
        for owner, label, addr, qty in allw:
            mark = "✅" if str(addr).lower() not in excl else "⬜"
            lines.append(f"{mark} {esc(label)} {esc(mask(addr))}  x{qty}")
    return lines


def mint_record(mid, d, wex, owner):
    """Scheduled-mint record for MINTS. Only fields the watcher and sniper read."""
    return {
        "id": mid,
        "contract": d["contract"],
        "chain": d.get("chain", "ethereum"),
        "status": "scheduled",
        "name": d.get("name"),
        "start_ts": d.get("start_ts"),
        "end_ts": d.get("end_ts"),
        "price_wei": d.get("price_wei"),
        "display_price_wei": d.get("display_price_wei"),
        "per_wallet": d.get("per_wallet"),
        "mint_qty": d.get("mint_qty"),
        "mint_sig": d.get("mint_sig"),
        "seadrop": bool(d.get("seadrop")),
        "mint_to": d.get("mint_to"),
        "fee_recipient": d.get("fee_recipient"),
        "speed": d.get("speed", "fast"),
        "fire_mode": d.get("fire_mode", "dual"),
        "gas_limit": d.get("gas_limit", "auto"),
        "phase": d.get("phase"),
        "phase_idx": d.get("phase_idx"),
        "phase_label": d.get("phase_label"),
        "list_mode": d.get("list_mode"),
        "stage_index": d.get("stage_index"),
        "merkle_root": d.get("merkle_root"),
        "signers": d.get("signers"),
        "mint_site": d.get("mint_site"),
        "wex": sorted({a.lower() for a in (wex or [])}),
        "owner": owner,
        "sale_started_fn": d.get("sale_started_fn"),
    }


def check_lines(verdicts, price_wei=0):
    verdicts = verdicts or []
    counts = {"ok": 0, "warn": 0, "fail": 0}
    out = ["<b>🔎 wallet eligibility and balance check</b>", ""]
    if not verdicts:
        return out + ["⚠️ no wallet verdicts returned", "check the selected wallets and chain rpc"]
    def _group(verds, title, icon):
        if not verds:
            return
        out.append(f"<b>{icon} {title} ({len(verds)})</b>")
        for v in verds:
            who = esc(v.get("label") or mask(v.get("address") or ""))
            bal = eth(v.get("balance_wei")) if v.get("balance_wei") is not None else "unknown"
            cost = eth(v.get("cost_wei")) if v.get("cost_wei") is not None else "unknown"
            notes = v.get("reasons") or ([v.get("reason")] if v.get("reason") else [])
            if v.get("eligibility") or (not notes and v.get("verdict") == "ok"):
                notes = ["eligible and funded"]
            out.append(f"  {icon} <b>{who}</b> — bal {esc(bal)} · need ~{esc(cost)}")
            for r in notes:
                out.append(f"      {esc(r)}")
        out.append("")

    ready = [v for v in verdicts if v.get("verdict") == "ok"]
    warn = [v for v in verdicts if v.get("verdict") == "warn"]
    fail = [v for v in verdicts if v.get("verdict") == "fail"]
    counts = {"ok": len(ready), "warn": len(warn), "fail": len(fail)}
    _group(ready, "ready to mint", "✅")
    _group(warn, "need attention", "⚠️")
    _group(fail, "cannot mint", "❌")
    out += [f"<b>total: {counts['ok']} ready · {counts['warn']} warning · {counts['fail']} failed</b>",
            "checks are read-only. force mint needs an explicit tap."]
    return out


def fund_lines(main_label, main_addr, plan, amount_s, chain):
    """plan from fleet.fund_plan. Nothing is sent until the admin taps confirm."""
    out = [f"<b>fund {esc(amount_s)} ETH each</b> on {esc(chain)}",
           f"from main {esc(main_label)} {esc(mask(main_addr))}", ""]
    for label, addr, amt in plan["rows"]:
        out.append(f"→ {esc(label)} {esc(mask(addr))}  {esc(eth(amt, 6))}")
    out += ["",
            f"total sent: {esc(eth(plan['total_wei'], 6))}",
            f"est gas: {esc(eth(plan['fee_wei'], 6))}",
            f"main wallet needs: {esc(eth(plan['need_wei'], 6))}"]
    if not plan["ok"]:
        out.append("❌ " + esc(plan["why"]))
    return out


def sweep_lines(main_label, main_addr, rows, notes, contract):
    out = [f"<b>sweep to main</b> {esc(main_label)} {esc(mask(main_addr))}",
           f"nft <code>{esc(contract)}</code>", ""]
    total = 0
    for label, addr, ids in rows:
        total += len(ids)
        shown = ", ".join(str(i) for i in ids[:10]) + (" ..." if len(ids) > 10 else "")
        out.append(f"→ {esc(label)} {esc(mask(addr))}: {len(ids)} nft (#{esc(shown)})")
    out += ["", f"total: {total} nft"]
    for n in notes:
        out.append("⚠️ " + esc(n))
    if not rows:
        out.append("nothing to sweep.")
    return out


def _reason(rpc):
    if not isinstance(rpc, dict):
        return "unknown"
    e = rpc.get("error")
    if e:
        return str(e)
    for v in rpc.values():
        if isinstance(v, str) and v.startswith("ERR"):
            return "rpc rejected it (" + v[4:].strip() + ")"
    return "no rpc accepted it"


def feed_lines(mid, payload, receipts=None):
    """Per-wallet result feed after a mint fires.

    receipts: optional {tx_hash: 'ok'|'reverted'|'pending'} (from a receipt poll). Without a
    receipt we only know an rpc accepted the tx, so we say 'accepted', never 'minted'.
    """
    receipts = receipts or {}
    lines = [f"🚀 mint #{mid} fired in {payload.get('elapsed_s', '?')}s", ""]
    minted = accepted = failed = 0
    for r in payload.get("results", []):
        who = esc(label_of(r.get("label")) or mask(r.get("address") or ""))
        rpc = r.get("rpc") or {}
        hashes = [v for v in rpc.values() if isinstance(v, str) and v.startswith("0x")]
        if not hashes:
            failed += 1
            lines.append(f"❌ {who}: failed, {esc(_reason(rpc))}")
            hint = resmod.hint_for(_reason(rpc))
            if hint:
                lines.append(esc(f"    hint: {hint}"))
            continue
        rc = next((receipts[h] for h in hashes if h in receipts), None)
        if rc == "ok":
            minted += 1
            lines.append(f"✅ {who}: minted")
        elif rc == "reverted":
            failed += 1
            lines.append(f"❌ {who}: failed, the mint tx reverted on-chain")
        else:
            accepted += 1
            lines.append(f"⏳ {who}: accepted by rpc, receipt not checked")
    for sk in payload.get("skipped", []):
        failed += 1
        lab = esc(label_of(sk.get("label")) or mask(sk.get("address") or ""))
        lines.append(f"❌ {lab}: skipped, {esc(sk.get('error') or 'skipped')}")
    total = len(payload.get("results", [])) + len(payload.get("skipped", []))
    lines.append("")
    lines.append(f"summary: {minted} minted, {accepted} accepted (unconfirmed), {failed} failed of {total} wallets")
    return lines[:MAX_FEED_LINES + 2]


def schedule_check_lines(verdicts):
    """Compatibility renderer for per-wallet preflight verdicts."""
    return preflight_lines(verdicts)


def preflight_lines(verdicts, gated=False):
    """Schedule preflight: every selected wallet and explicit blockers/warnings."""
    out = ["<b>⚡ schedule pre-flight</b>", "balance is checked against mint price + estimated gas", ""]
    if not verdicts:
        return out + ["⚠️ no wallets selected on this chain"]
    for v in verdicts:
        lvl = v.get("verdict", "warn")
        who = f"<b>{esc(v.get('label') or 'wallet')}</b> {esc(v.get('masked', ''))}"
        bal = eth(v.get("balance_wei")) if v.get("balance_wei") is not None else "unknown"
        cost = eth(v.get("cost_wei")) if v.get("cost_wei") is not None else "unknown"
        out.append(f"{ICON.get(lvl, '⚠️')} {who} · balance {esc(bal)} · price+gas ~{esc(cost)}")
        reasons = v.get("reasons") or ([v.get("reason")] if v.get("reason") else [])
        if not reasons:
            out.append("    • ready for this stage")
        for reason in reasons:
            if reason.startswith("not enough for the mint fee") or reason.startswith("not enough gas"):
                reason = "insufficient balance for mint price + gas: " + reason
            elif reason.startswith("already minted max"):
                reason = "not eligible: per-wallet mint limit reached"
            out.append(f"    • ⚠️ {esc(reason)}")
    failures = [v for v in verdicts if v.get("reasons") or v.get("reason") not in (None, "ready") or v.get("verdict") in ("warn", "fail")]
    if failures:
        out += ["", "⚠️ review the listed wallets. force mint is required to proceed with any warning or failed check."]
    elif gated:
        out += ["", "⚠️ gated stage: eligibility must be verified before scheduling"]
    return out


_RES_ICON = {"minted": "✅", "failed": "❌", "pending": "⏳"}


def _failure_lines(r):
    """Full failure detail for one failed wallet: reason, hint, rpc text, revert data, tx status, block."""
    out = []
    reason = r.get("reason") or r.get("revert") or r.get("error") or r.get("rpc_error") or "no reason recorded (send record incomplete)"
    out.append("❌ <b>reason:</b> " + esc(reason))
    if r.get("hint"):
        out.append(esc(f"    hint: {r['hint']}"))
    if r.get("rpc_error") and r.get("rpc_error") != reason:
        out.append(esc(f"    rpc: {r['rpc_error']}"))
    if r.get("revert_raw"):
        raw = r["revert_raw"]
        out.append(esc(f"    revert data: {raw[:138]}{'...' if len(raw) > 138 else ''}"))
    if r.get("tx_status") is not None:
        out.append(esc(f"    tx status: {r['tx_status']} ({'reverted' if r['tx_status'] == '0x0' else 'success'})"))
    if r.get("block") is not None:
        out.append(esc(f"    block: {r['block']}"))
    if r.get("replay"):
        out.append(esc(f"    replay: {r['replay']}"))
    return out


_SHOT_LABEL = {"prefire": "pre-fire", "normal": "normal"}


def _ms(x):
    return f"{x:.0f}ms" if x is not None else "-"


def timing_lines(w):
    """Per-wallet timing block in ms. All three parts add up to the total.
    submit  = fire start -> that wallet's broadcast returned (perf_counter, bot_ms).
    chain   = broadcast -> block inclusion. Inclusion comes from the block timestamp, which is whole
              seconds, so this is approximate to about 1s.
    receipt = inclusion -> receipt seen (the rest of receipt_s).
    Parts that cannot be computed (no receipt yet, no block time, pre-fire early shot) show '-'."""
    bot = w.get("bot_ms")
    rc = w.get("receipt_s")
    fire_at = w.get("fire_at")
    bts = w.get("block_ts")
    total = round(rc * 1000) if rc is not None else None
    chain = receipt = None
    if bot is not None and fire_at and bts and total is not None:
        incl_ms = bts * 1000 - fire_at * 1000
        chain = max(0.0, incl_ms - bot)
        receipt = max(0.0, total - bot - chain)
    return [
        f"⚡ submit: {_ms(bot)} (broadcast launch → RPC response)",
        f"⛓ chain: {_ms(chain)} (broadcast → included in block)",
        f"📥 receipt: {_ms(receipt)} (inclusion → receipt confirmed)",
        f"total: {_ms(total)}",
    ]


def avg_bot_ms(rows):
    vals = [r["bot_ms"] for r in rows if r.get("bot_ms") is not None]
    return round(sum(vals) / len(vals), 1) if vals else None


def _dual_wallet_lines(mid, rec, qty):
    """Two-shot wallet block: which shot landed, the other shown as replaced (same nonce, fee not spent)."""
    lines = []
    from acobot import results as resmod
    for w in resmod.collapse_wallets(rec.get("rows", [])):
        st = w.get("status") or "failed"
        who = esc(label_of(w.get("label")) or "wallet")
        lines.append(f"<b>{_RES_ICON.get(st, '⚠️')} {who} {esc(mask(w.get('address') or ''))}</b>")
        shots = w.get("shots") or {}
        for key in ("prefire", "normal"):
            s = shots.get(key)
            if not s:
                continue
            name = _SHOT_LABEL[key]
            if s.get("status") == "minted":
                lines.append(esc(f"✅ {name} landed"))
            elif s.get("status") == "superseded":
                lines.append(esc(f"↩ {name} replaced (same nonce), fee not spent"))
            elif s.get("status") == "pending":
                lines.append(esc(f"⏳ {name} waiting for a receipt"))
            else:
                lines.append(esc(f"✖ {name} not included"))
        lines.extend(esc(t) for t in timing_lines(w))
        n_mint = qty if st == "minted" else 0
        lines.append(esc(f"minted: {n_mint} NFT{'s' if n_mint != 1 else ''}"))
        h = w.get("tx_hash")
        if h:
            short = esc(f"{h[:10]}...{h[-6:]}")
            if w.get("explorer"):
                lines.append(f'tx: <a href="{html.escape(w["explorer"], quote=True)}">{short}</a>')
            else:
                lines.append(f"tx: <code>{short}</code>")
        # gas counts only the included tx (superseded/failed shots never spent gas)
        gas = f"{w['gas_eth']:.6f} ETH" if w.get("gas_eth") is not None else "n/a"
        if w.get("gas_usd") is not None:
            gas += f" ({resmod.fmt_usd_val(w['gas_usd'])})"
        lines.append(esc(f"gas: {gas}"))
        lines.append(esc(f"status: {st}"))
        if st == "failed":
            lines.extend(_failure_lines(w))
        lines.append("")
    return lines


_MODE_RESULT = {"prefire": "pre-fire only", "normal": "normal only", "dual": "both: pre-fire + normal"}


def result_mode_label(rec):
    """Which fire mode ran for this mint. None for records saved before the picker existed."""
    return _MODE_RESULT.get(rec.get("fire_mode"))


def result_lines(mid, rec):
    """Per-wallet result panel (HTML lines). rec is the saved result record for one mint."""
    summ = rec.get("summary") or {}
    mode = result_mode_label(rec)
    if any(r.get("shot") in ("prefire", "normal") for r in rec.get("rows", [])):
        try:
            qty_d = int(rec.get("qty") or 1)
        except (TypeError, ValueError):
            qty_d = 1
        from acobot import results as resmod2
        minted_w = [w for w in resmod2.collapse_wallets(rec.get("rows", [])) if w.get("status") == "minted"]
        total = qty_d * len(minted_w)
        gas_total = resmod2.fmt_usd_val(summ.get("total_gas_usd")) if summ.get("total_gas_usd") is not None else "n/a"
        head = [f"<b>🚀 mint #{mid} results ({esc(mode or 'two shots')})</b>",
                f"<b>🎟 total minted: {total} NFT{'s' if total != 1 else ''}</b>",
                f"<b>💰 total gas: {esc(gas_total)}</b>",
                 f"<b>⚡ avg submit time: {_ms(avg_bot_ms(rec.get('rows', [])))}</b>", esc(resmod2.summary_line(summ)), ""]
        if rec.get("note") and not rec.get("rows"):
            head.append("⚠️ " + esc(rec["note"]))
        return head + _dual_wallet_lines(mid, rec, qty_d)
    gas_total = resmod.fmt_usd_val(summ.get("total_gas_usd")) if summ.get("total_gas_usd") is not None else "n/a"
    try:
        qty = int(rec.get("qty") or 1)
    except (TypeError, ValueError):
        qty = 1
    rows = rec.get("rows", [])
    minted_total = sum(qty for r in rows if (r.get("status") or "failed") == "minted")
    lines = [BRAND, DIVIDER, f"<b>🚀 mint #{mid} results" + (f" ({esc(mode)})" if mode else "") + "</b>",
             f"<b>🎟 total minted: {minted_total} NFT{'s' if minted_total != 1 else ''}</b>",
             f"<b>💰 total gas: {esc(gas_total)}</b>",
             f"<b>⚡ avg submit time: {_ms(avg_bot_ms(rows))}</b>",
             esc(resmod.summary_line(summ)), ""]
    if rec.get("note") and not rows:
        lines.append("⚠️ " + esc(rec["note"]))
    for r in rows:
        st = r.get("status") or "failed"
        who = esc(label_of(r.get("label")) or "wallet")
        n_mint = qty if st == "minted" else 0
        lines.append(f"<b>{_RES_ICON.get(st, '⚠️')} {who} {esc(mask(r.get('address') or ''))}</b>")
        lines.extend(esc(t) for t in timing_lines(r))
        lines.append(esc(f"minted: {n_mint} NFT{'s' if n_mint != 1 else ''}"))
        h = r.get("tx_hash")
        if h:
            short = esc(f"{h[:10]}...{h[-6:]}")
            if r.get("explorer"):
                lines.append(f'tx: <a href="{html.escape(r["explorer"], quote=True)}">{short}</a>')
            else:
                lines.append(f"tx: <code>{short}</code>")
        gas = f"{r['gas_eth']:.6f} ETH" if r.get("gas_eth") is not None else "n/a"
        if r.get("gas_usd") is not None:
            gas += f" ({resmod.fmt_usd_val(r['gas_usd'])})"
        lines.append(esc(f"gas: {gas}"))
        lines.append(esc(f"status: {st}"))
        if st == "failed":
            lines.extend(_failure_lines(r))
        elif st == "pending":
            lines.append("reason: " + esc(r.get("error") or "no receipt yet"))
        lines.append("")
    return lines


# ---------- balances / token transfers ----------
def usd_txt(x):
    if x is None:
        return "usd ?"
    return f"~${x:,.2f}" if x >= 0.01 else f"~${x:.4f}"


def balance_block(label, masked, chain, snap, native_sym):
    head = f"<b>{esc(label)}</b> {esc(masked)} · {esc(chain)}"
    nat = snap.get("native_wei")
    native = (f"{esc(native_sym)} {esc(eth(nat, 6).replace(' ETH', ''))}  {esc(usd_txt(snap.get('native_usd')))}"
              if nat is not None else f"{esc(native_sym)} ?")
    lines = [head, "  " + native]
    for t in snap.get("tokens", []):
        amt = fmt_amount_safe(t["raw"], t["decimals"])
        lines.append(f"  {esc(t['symbol'])} {esc(amt)}  {esc(usd_txt(t.get('usd')))}")
    if snap.get("errors"):
        lines.append("  ⚠️ " + esc(", ".join(snap["errors"])))
    return "\n".join(lines)


def fmt_amount_safe(raw, decimals):
    from decimal import Decimal
    v = Decimal(raw) / (Decimal(10) ** decimals)
    s = f"{v:,.4f}".rstrip("0").rstrip(".")
    return s or "0"


def transfer_lines(title, to_label, rows, notes=()):
    out = [f"<b>{esc(title)}</b>", ""]
    for r in rows:
        if r.get("ok"):
            amt = fmt_amount_safe(r["raw"], r["decimals"])
            out.append(f"→ {esc(r['label'])} {esc(r['masked'])}  {esc(amt)} {esc(r['symbol'])}  to {esc(to_label)}")
        else:
            out.append(f"✖ {esc(r['label'])} {esc(r['masked'])}  skipped: {esc(r.get('why') or 'no balance')}")
    ok = [r for r in rows if r.get("ok")]
    out += ["", f"transfers: {len(ok)}"]
    for n in notes:
        out.append("⚠️ " + esc(n))
    if not ok:
        out.append("nothing to send.")
    return out


def receipt_lines(res):
    out = []
    for r in res:
        if r.get("sent"):
            amt = fmt_amount_safe(r["raw"], r["decimals"])
            out.append(f"✅ {esc(r['label'])} {esc(r['masked'])}  {esc(amt)} {esc(r['symbol'])}")
            out.append(f"   tx <code>{esc(r['tx'])}</code>")
        else:
            out.append(f"❌ {esc(r['label'])} {esc(r['masked'])}  failed: {esc(r.get('why') or 'unknown')}")
    ok = sum(1 for r in res if r.get("sent"))
    out += ["", f"summary: {ok} sent, {len(res) - ok} failed"]
    return out
