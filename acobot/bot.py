"""ACO mint sniper Telegram bot. Button-driven panels that edit in place.

Admin (ADMIN_ID) creates mints; whitelisted users (added via /allow) can be signers.
Wallet keys are only accepted in private DMs. Keys are never logged or displayed.
"""
import asyncio, json, logging, os, re, shlex, sys, time, threading
import html
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from telegram import Update, BotCommand, InlineKeyboardButton as B, InlineKeyboardMarkup as M
from telegram.constants import ChatType
from telegram.error import BadRequest
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, Defaults, filters

from acobot import config as cfgmod, mintread, sniper, wallets, walletops
from acobot import authorization_resolver as auth_resolver
from acobot.rpc import RPC, err_text
from acobot.wsfeed import HeadFeed
from acobot import prices, fleet, panels, intel, results as resmod
from acobot import portfolio, snipeprice, gasguard, watchlist, rugcheck, alpha as alp
from decimal import Decimal, InvalidOperation
from eth_utils import to_checksum_address as cs
from acobot.mintread import NoContract
from acobot import sniper as sn
import concurrent.futures as cf
from acobot import mintapi
from acobot import mint_router
from acobot import solana
from acobot import mint_engine

ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
DIVIDER_LINE = "─" * 22
BOT_COMMANDS = [
    BotCommand("start", "open the main panel"),
    BotCommand("addmint", "add a mint (pick chain, send address)"),
    BotCommand("mymints", "scheduled mints"),
    BotCommand("wallets", "manage your wallets (dm)"),
    BotCommand("cancelmint", "cancel a mint by id"),
    BotCommand("balance", "wallet balances by chain (dm)"),
    BotCommand("sweepall", "sweep an asset from every wallet (dm)"),
    BotCommand("send", "send native or token (dm)"),
    BotCommand("cancel", "stop the current step"),
    BotCommand("portfolio", "mint pnl vs floor in usd"),
    BotCommand("watchlist", "crew watchlist of tracked mints"),
    BotCommand("snipeprice", "alert on listings under a max eth price (admin)"),
        BotCommand("trade", "unified token buy and sell (confirm required)"),
        BotCommand("outmint", "bars cast and melt (confirm required)"),
        BotCommand("cawatch", "watch X accounts / groups for CAs (manual or auto)"),
    BotCommand("alpha", "copy alpha: recent trades by tracked wallets"),
]

HOME = Path(os.path.expanduser("~/.folk/acobot"))
DATA = HOME / "data"; DATA.mkdir(parents=True, exist_ok=True)
MINTS_FILE = DATA / "mints.json"
ADMIN_ID = 2078639025
UTC = timezone.utc
SPEED_LABELS = {k: v[0] for k, v in sn.SPEEDS.items()}
GAS_LABEL = {"auto": "Auto (recommended)", "high": "High (fixed ceiling)", "custom": "Custom"}
GAS_HINT = {
    "auto": "auto = bot works out the gas at mint time, with a safety buffer. you only pay what is used.",
    "high": "high = a 500,000 gas floor. the bot still estimates at fire time and uses the larger number.",
    "custom": "custom = a gas number you typed in.",
}
SPEED_EMOJI = {"slow": "🐢", "medium": "🚶", "fast": "⚡", "superfast": "🚀"}
CHAIN_NAME = "ethereum"

_FEEDS = {}
BOT_STARTED = time.time()


def head_feed(chain_name):
    """Shared hot newHeads feed for a chain (started once). None when the chain has no ws_rpc."""
    c = CFG.chains.get(chain_name)
    if not c or not getattr(c, "ws_rpc", None):
        return None
    if chain_name not in _FEEDS:
        _FEEDS[chain_name] = HeadFeed(
            c.ws_rpc, name=chain_name, expected_chain_id=c.chain_id).start()
        from acobot.rpc import start_warmers
        start_warmers(list(c.broadcast_rpc or []) + list(c.rpc or []),
                      expected_chain_id=c.chain_id)   # keep verified broadcast sockets hot
    return _FEEDS[chain_name]


def enabled_chains():
    return [name for name, chain in CFG.chains.items()
            if name in cfgmod.SUPPORTED_EVM_CHAIN_IDS
            and chain.rpc
            and chain.chain_id == cfgmod.SUPPORTED_EVM_CHAIN_IDS[name]]


def enabled_mint_chains():
    chains = enabled_chains()
    if CFG and getattr(CFG, "solana_rpc_urls", None):
        chains.append("solana")
    return chains


def chain_label(name):
    return "Solana Mainnet" if name == "solana" else name

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("acobot")
# httpx logs full request URLs, which contain the bot token. keep it at WARNING.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.INFO)

CFG = None
PENDING = {}   # token -> fund/sweep plan, owner-bound
TOKEN_PENDING = {}  # key -> read-only quote and transaction; signing only after final callback
_tok = [1]
MINTS = {}          # id -> dict (persisted)
DRAFTS = {}         # draft id -> dict (admin only; persisted so panels survive restarts)
DRAFTS_FILE = HOME / "drafts.json"
THREADS = {}        # mint id -> thread
CANCEL = {}         # mint id -> threading.Event
MAIN_LOOP = [None]  # event loop used to message admin from watcher threads
_next_id = [1]


def load_env():
    p = HOME / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


def allowed(uid):
    return uid == ADMIN_ID or uid in CFG.whitelist_ids or uid in CFG.admin_ids


def username_allowed(username):
    u = ("@" + (username or "").lstrip("@")).lower()
    return u != "@" and u in {x.lower() for x in getattr(CFG, "whitelist_usernames", [])}


def is_dm(update):
    return update.effective_chat and update.effective_chat.type == ChatType.PRIVATE


def save_drafts():
    DRAFTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DRAFTS_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(DRAFTS))
        os.replace(tmp, DRAFTS_FILE)
    except OSError:
        log.exception("could not save mint drafts to %s", DRAFTS_FILE)
        raise


def load_drafts():
    if not DRAFTS_FILE.exists():
        return
    try:
        raw = json.loads(DRAFTS_FILE.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError):
        log.exception("could not load mint drafts from %s", DRAFTS_FILE)
        return
    if not isinstance(raw, dict):
        log.error("mint draft file %s must contain a JSON object", DRAFTS_FILE)
        return
    consumed = {m.get("source_draft_id") for m in MINTS.values() if m.get("source_draft_id") is not None}
    for key, draft in raw.items():
        try:
            did = int(key)
        except ValueError:
            log.warning("skipping mint draft with invalid id %r", key)
            continue
        if not isinstance(draft, dict):
            log.warning("skipping mint draft %s with invalid data", key)
            continue
        if did in consumed:
            continue
        DRAFTS[did] = draft


def parse_mint_price_wei(text):
    try:
        eth_amount = Decimal(text.strip())
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError("enter a valid ETH amount") from exc
    if not eth_amount.is_finite() or eth_amount < 0:
        raise ValueError("mint price must be a non-negative ETH amount")
    sign, digits, exponent = eth_amount.as_tuple()
    coefficient = int("".join(str(digit) for digit in digits) or "0")
    if coefficient == 0:
        return 0
    if sign and coefficient:
        raise ValueError("mint price must be a non-negative ETH amount")
    scaled_exponent = exponent + 18
    if scaled_exponent < 0:
        places = -scaled_exponent
        if coefficient and (places > len(str(coefficient)) or coefficient % 10**places):
            raise ValueError("mint price supports at most 18 decimal places")
        wei = coefficient // 10**places if coefficient else 0
    else:
        if coefficient and len(str(coefficient)) + scaled_exponent > 78:
            raise ValueError("mint price exceeds the supported uint256 range")
        wei = coefficient * 10**scaled_exponent
    if wei >= 2**256:
        raise ValueError("mint price exceeds the supported uint256 range")
    return wei


def apply_live_draft_snapshot(draft, live):
    """Refresh detected values without erasing manual fallbacks the chain cannot expose."""
    fallback_fields = {"price_wei", "start_ts", "end_ts", "per_wallet", "fee_recipient", "mint_sig", "mint_to"}
    for key in ("name", "symbol", "owner", "standard", "max_supply", "minted", "proxy_impl",
                "selector_count", "seadrop", "stages", "phases", "paused", "sale_started",
                "price_wei", "start_ts", "end_ts", "per_wallet", "fee_recipient", "mint_sig", "mint_to",
                "schedule", "schedule_unavailable"):
        value = live.get(key)
        if key in fallback_fields and (value is None or (key == "mint_sig" and draft.get("mint_sig_manual"))):
            continue
        draft[key] = value


def persist():
    tmp = MINTS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"next_id": _next_id[0], "mints": MINTS}, default=str))
    os.replace(tmp, MINTS_FILE)


def load_mints():
    if MINTS_FILE.exists():
        d = json.loads(MINTS_FILE.read_text())
        MINTS.update({int(k): v for k, v in d.get("mints", {}).items()})
        _next_id[0] = d.get("next_id", 1)


def fmt_eth(wei):
    return "unknown" if wei is None else f"{wei / 1e18:.6f} ETH"


PHASE_PLAIN = {
    "public": ("public", "open to all"),
    "whitelist": ("allowlist", "only whitelisted wallets"),
    "allowlist": ("allowlist", "only whitelisted wallets"),
    "presale": ("presale", "early access for a select group"),
    "airdrop": ("airdrop", "claim for eligible holders"),
    "signed": ("allowlist", "server-signed mints, only wallets the signer approves"),
}
LIST_MODE_LINE = {
    "gtd": "GTD: guaranteed allowlist spot",
    "fcfs": "FCFS: first come, first served",
    "public": "public mint",
    "presale": "presale allowlist",
}


def _phase_label(p):
    name, _ = PHASE_PLAIN.get(p, (p, ""))
    return name


def _phase_explain(p):
    name, plain = PHASE_PLAIN.get(p, (p, "phase defined by the contract"))
    return f"{name}: {plain}"


def _stage_line(st):
    if st.get("gated"):
        warn = "⚠️ allowlist stage: needs allowlist spot, mint will likely fail unless wallet is whitelisted"
        if st.get("merkle_root"):
            return "allowlist (need allowlist spot): merkle root set, schedule and price are in each mint call, not readable on-chain. " + warn
        return f"allowlist (need allowlist spot): server-signed mints, {st.get('signers', 0)} signer(s). " + warn
    p = st.get("price_wei")
    price = "free" if p == 0 else (f"{p / 1e18:g} ETH" if p is not None else "not detected")
    w = st.get("per_wallet")
    return (f"public: {price}, {w or '?'} per wallet, opens {fmt_ts(st.get('start_ts'))}, "
            f"closes {fmt_ts(st.get('end_ts')) if st.get('end_ts') else 'no end set'}. open to all, snipeable")


KTM = ZoneInfo("Asia/Kathmandu")


def _when(ts):
    """'2026-10-27 09:45 UTC (3:15pm GMT+5:45)' from unix seconds."""
    if not ts:
        return "unknown"
    u = datetime.fromtimestamp(int(ts), UTC)
    k = u.astimezone(KTM)
    hour = k.strftime("%I:%M%p").lstrip("0").lower()
    return f"{u.strftime('%Y-%m-%d %H:%M')} UTC ({k.strftime('%Y-%m-%d')} {hour} GMT+5:45)"


def _phase_status(start_ts, end_ts):
    if not start_ts:
        return "unknown"
    now = int(time.time())
    if end_ts and now >= int(end_ts):
        return "ended"
    if now < int(start_ts):
        return "upcoming"
    return "live"


def phase_block(st, selected):
    """One PHASE block: name, MP/Limit line, Opens line. Selected block is marked."""
    name = st.get("label") or _phase_label(st.get("chain_key") or st.get("key"))
    mark = "✅ " if selected else ""
    p = st.get("display_price_wei")
    if p is None:
        p = st.get("price_wei")
    mp = "FREE" if p == 0 else (f"{p / 1e18:.6f}".rstrip("0").rstrip(".") + " ETH" if p is not None else "not detected")
    per = st.get("display_per_wallet")
    if per is None:
        per = st.get("per_wallet")
    lim = f"{per} per wallet" if per else "not detected"
    start_ts = st.get("display_start_ts") or st.get("start_ts")
    end_ts = st.get("display_end_ts") or st.get("end_ts")
    lines = [
        f"<b>{mark}{esc(name)}</b>",
        esc(f"MP: {mp} | Limit: {lim}"),
        esc(f"Opens: {_when(start_ts)}"),
        esc(f"Ends: {_when(end_ts) if end_ts else 'no end set'}"),
        esc(f"Status: {_phase_status(start_ts, end_ts)}"),
    ]
    access = LIST_MODE_LINE.get(st.get("list_mode"))
    if st.get("index") is not None:
        lines.append(esc(f"stage index: {st.get('index')}"))
    members = st.get("allowlist_members")
    allocation = st.get("allowlist_allocation")
    if members is not None or allocation is not None:
        lines.append(esc(
            f"allowlist: {members if members is not None else 'unknown'} wallets, "
            f"{allocation if allocation is not None else 'unknown'} spots"))
    if access:
        lines.append(esc(access))
    if st.get("gated"):
        lines.append(esc("⚠️ allowlist stage: needs allowlist spot, mint will likely fail unless wallet is whitelisted"))
        if not start_ts and p is None:
            lines.append(esc("gated phase schedule unavailable"))
    elif st.get("chain_key") is None:
        lines.append(esc("not registered on this contract: can't snipe"))
    return lines


def fmt_ts(ts):
    """All mint times are UTC. Stored as unix seconds, displayed with the UTC label."""
    if not ts:
        return "unknown"
    return datetime.fromtimestamp(int(ts), UTC).strftime("%Y-%m-%d %H:%M UTC")


def esc(x):
    return html.escape(str(x))


def fmt_gwei(wei):
    return f"{wei / 1e9:.2f} gwei"


def fmt_count(n):
    if n is None:
        return "not detected"
    return f"{n:,}"


def fee_cost_s(d, eth_cost):
    """USD estimate if the price is known, else the native-token amount."""
    usd = d.get("usd_per_eth")
    if usd:
        return prices.fmt_usd(eth_cost * usd)
    return f"~{eth_cost:.5f} ETH"


def fee_block(d):
    fees = d.get("fees")
    if not fees:
        return ["fee estimates unavailable right now"]
    lines = []
    sel = d.get("speed", "fast")
    for k in ("slow", "medium", "fast", "superfast"):
        r = fees["rows"][k]
        mark = "> " if k == sel else "  "
        lines.append(f"{mark}{SPEED_EMOJI[k]} {r['label']:<11} {fee_cost_s(d, r['cost_eth'])}")
    return lines


def _selected_index(d, items):
    """Index of the single selected phase row. Selection is by row index: several rows can share
    a chain_key (e.g. two allowlist rounds), so keying on chain_key would tick them all."""
    i = d.get("phase_idx")
    if isinstance(i, int) and 0 <= i < len(items):
        return i
    cur = d.get("phase")
    for n, it in enumerate(items):
        if it[0] == cur:
            return n
    return None


def draft_phase_view(draft):
    """Return display values for the selected schedule row without changing execution data."""
    view = dict(draft)
    schedule = draft.get("schedule") or []
    if not schedule:
        return view
    selected_index = _selected_index(
        draft, [(st.get("chain_key") or st.get("key"),) for st in schedule])
    if selected_index is None:
        return view
    stage = schedule[selected_index]
    view["_selected_stage"] = stage
    view["gated"] = bool(stage.get("gated"))
    for field in ("price_wei", "start_ts", "end_ts", "per_wallet"):
        display_field = "display_" + field
        if display_field in stage:
            view[field] = stage[display_field]
        elif field in stage:
            view[field] = stage[field]
    return view


def draft_phase_snipeable(draft):
    view = draft_phase_view(draft)
    stage = view.get("_selected_stage")
    if stage is not None:
        return False if stage.get("gated") else stage.get("snipeable", True)
    if draft.get("phase") in ("allowlist", "signed"):
        return False
    return not draft.get("gated", False)


def phase_authorization_status(draft):
    """Describe authorization requirements independently from wallet eligibility."""
    view = draft_phase_view(draft)
    requirement = auth_resolver.authorization_requirement(view)
    if not requirement["required"]:
        return {**requirement, "available": True, "detail": requirement["description"]}
    available = mintapi.supports_phase(view.get("contract"))
    missing = ", ".join(requirement["missing"])
    chain_name = str(view.get("chain") or "").lower()
    if available:
        detail = "resolver will fetch and simulate wallet-specific authorization at execution time"
    elif chain_name == "ethereum":
        detail = (
            f"missing {missing}; OpenSea, Reservoir, and Magic Eden are checked for this wallet's "
            "real GTD/FCFS allowlist transaction, then simulated before signing"
        )
    else:
        detail = (
            f"missing {missing}; supported public eligibility/project endpoints will be checked automatically"
        )
    return {**requirement, "available": available, "detail": detail}


def draft_phase_authorized(draft):
    view = draft_phase_view(draft)
    if draft_phase_snipeable(view):
        return True
    return bool(view.get("start_ts") and phase_authorization_status(view)["available"])


def recommended_fire_mode(stage):
    """GTD and reserved presales take one shot at the open. FCFS races take both."""
    from acobot import opensea
    mode = (stage or {}).get("list_mode") or opensea.classify_list_mode(stage or {})
    if mode == "fcfs":
        return "dual"
    if mode in {"gtd", "presale"}:
        return "normal"
    return "dual"


def apply_mint_stage(draft, stage):
    base = draft.setdefault("_base", {"price_wei": draft.get("price_wei"), "start_ts": draft.get("start_ts"),
                                      "end_ts": draft.get("end_ts"), "per_wallet": draft.get("per_wallet")})
    draft["phase"] = stage["chain_key"]
    if stage.get("list_mode"):
        draft["list_mode"] = stage["list_mode"]
    elif stage.get("label") or stage.get("type"):
        from acobot import opensea
        draft["list_mode"] = opensea.classify_list_mode(stage)
    if not draft.get("fire_mode_picked"):
        draft["fire_mode"] = recommended_fire_mode(
            dict(stage, list_mode=draft.get("list_mode") or stage.get("list_mode")))
    if any(key in stage for key in ("display_price_wei", "display_start_ts",
                                    "display_end_ts", "display_per_wallet")):
        draft["price_wei"], draft["start_ts"], draft["end_ts"], draft["per_wallet"] = (
            base.get("price_wei"), base.get("start_ts"), base.get("end_ts"), base.get("per_wallet"))
        return
    if stage.get("gated"):
        draft["start_ts"] = stage.get("start_ts") or base.get("start_ts")
        draft["end_ts"] = stage.get("end_ts")
        draft["price_wei"] = stage.get("price_wei") if stage.get("price_wei") is not None else base.get("price_wei")
        draft["per_wallet"] = stage.get("per_wallet") if stage.get("per_wallet") is not None else base.get("per_wallet")
    else:
        draft["price_wei"], draft["start_ts"], draft["end_ts"], draft["per_wallet"] = (
            base.get("price_wei"), base.get("start_ts"), base.get("end_ts"), base.get("per_wallet"))


QTY_NO_LIMIT_CAP = 20


def _fixed_call(d):
    sig = d.get("mint_sig") or ""
    return bool(sig) and not d.get("seadrop") and sig.endswith("()")


def qty_cap(d):
    """Most NFTs one wallet mints per tx: the detected per-wallet limit, else 20. No-arg mints are fixed at 1."""
    if _fixed_call(d):
        return 1
    return int(d.get("per_wallet") or QTY_NO_LIMIT_CAP)


def qty_of(d):
    return max(1, min(int(d.get("mint_qty") or 1), qty_cap(d)))


def qty_limit_line(d):
    if _fixed_call(d):
        return "fixed amount per call, qty 1"
    cap = qty_cap(d)
    if d.get("per_wallet"):
        return f"per-wallet limit: {d['per_wallet']} (pick up to {cap})"
    return f"no limit detected (pick up to {cap})"


def cost_line(d):
    q = qty_of(d)
    price = d.get("price_wei")
    gas_s = "est gas n/a"
    fees = d.get("fees")
    if fees:
        row = fees["rows"].get(d.get("speed", "fast"))
        if row:
            gas_s = "est gas ~" + fee_cost_s(d, row["cost_eth"]).lstrip("~")
    if price is None:
        return f"cost per wallet: price not set, {gas_s}"
    mint_s = "free" if price == 0 else f"{price * q / 1e18:g} ETH ({q} × {price / 1e18:g})"
    return f"cost per wallet: {mint_s} + {gas_s}"


def qty_rows(did, d):
    cap = qty_cap(d)
    cur = qty_of(d)
    rows, row = [], []
    for n in range(1, cap + 1):
        row.append(B(("✅" if n == cur else "") + str(n), callback_data=f"dq:{did}:{n}"))
        if len(row) == 5:
            rows.append(row); row = []
    if row:
        rows.append(row)
    return rows


FIRE_MODE_LABEL = {"prefire": "⚡ Prefire", "normal": "🎯 Normal", "dual": "🔀 Both"}
FIRE_MODE_TEXT = {
    "prefire": "one shot at the opening block boundary. no normal shot.",
    "normal": "one shot at the open. no early shot.",
    "dual": "pre-fire + normal, same nonce. recommended for FCFS.",
}
FIRE_MODE_NAME = {"prefire": "Pre-fire only", "normal": "Normal only", "dual": "Both (default)"}


def draft_text(d):
    """Telegram HTML panel."""
    d = draft_phase_view(d)
    selected_stage = d.get("_selected_stage")
    chain = d.get("chain", CHAIN_NAME)
    price = d.get("price_wei")
    price_s = "free" if price == 0 else (f"{price / 1e18:g} ETH" if price is not None else "not detected")
    start, end = d.get("start_ts"), d.get("end_ts")
    opens = fmt_ts(start) if start else "not detected"
    closes = fmt_ts(end) if end else "no end set"
    per = d.get("per_wallet")
    per_s = f"{per} per wallet" if per else "not detected"
    if d.get("seadrop") and per is None:
        per_s = "unlimited / 0 from live SeaDrop config" if d.get("onchain_read_ts") else "not detected"
    phases = d.get("phases") or []
    phase_now = d.get("phase") or ("public" if d.get("seadrop") else (phases[0] if phases else "single phase"))
    gas = d.get("gas_limit", "auto")
    if gas in (None, "auto"):
        gas_s = GAS_LABEL["auto"]
    elif int(gas) == 500000:
        gas_s = GAS_LABEL["high"]
    else:
        gas_s = f"{GAS_LABEL['custom']} ({int(gas):,} gas)"
    name = esc(d.get("name") or "unnamed contract")
    status = []
    if d.get("paused") is not None:
        status.append("paused" if d["paused"] else "not paused")
    if d.get("sale_started") is not None:
        status.append("sale live now" if d["sale_started"] else "sale not live yet")
    ident_lines = []
    if d.get("symbol"):
        ident_lines.append(f"symbol: {esc(d['symbol'])}")
    if d.get("standard"):
        ident_lines.append(f"standard: {esc(d['standard'].upper())}")
    if d.get("owner"):
        ident_lines.append(f"owner: <code>{esc(d['owner'])}</code>")
    lines = [
        f"🎯 <b>{name}</b>",
        f"<code>{esc(d['contract'])}</code>",
        f"🌐 chain: {esc(chain)}   📦 source: {esc(d.get('source', 'bytecode'))}",
    ]
    if d.get("abi_source"):
        lines.append(f"verified ABI: {esc(d['abi_source'])}")
    lines += ident_lines
    read_ts = d.get("onchain_read_ts")
    read_label = datetime.fromtimestamp(read_ts, UTC).strftime("%Y-%m-%d %H:%M:%S UTC") if read_ts else "not read"
    lines += [
        "",
        "<b>💰 MINT DETAILS</b>",
        f"on-chain snapshot: {esc(read_label)}",
        "refresh before scheduling; snapshot is not a live subscription",
    ]
    if selected_stage:
        lines.append(f"selected phase: <b>{esc(selected_stage.get('label') or _phase_label(selected_stage.get('chain_key') or 'phase'))}</b>")
        lines.append("drop-page terms are display metadata; mint transaction comes from chain data or the official phase API")
    auth_status = phase_authorization_status(d)
    report = auth_resolver.project_report(d)
    lines += [
        "",
        "<b>PROJECT / AUTHORIZATION CHECK</b>",
        f"network / standard / protocol: {esc(report['network'])} / {esc(report['standard'])} / {esc(report['protocol'])}",
        f"official site: {esc(report['official_site'])}",
        f"phases: {esc(', '.join(report['phases']) if report['phases'] else 'not detected')}",
        f"selected: {esc(report['selected_phase'])} · price: {esc('unknown' if report['price_wei'] is None else price_s)} · wallet limit: {esc(report['wallet_limit'] if report['wallet_limit'] is not None else 'unknown')}",
        f"eligibility: {esc(report['eligibility'])}",
        f"authorization: {esc(report['authorization_type'])} · {esc(report['authorization_available'])}",
        f"auto retrieval: {esc(report['auto_retrieval'])}",
        f"normal schedule: {'ready' if report['schedule_possible'] else 'needs a phase opening time or on-chain live-sale signal'} · force schedule: {'available' if report['transaction_construction_possible'] or auth_status['required'] else 'needs a supported transaction route'}",
        ("Normal uses locally eligible wallets; Force bypasses local eligibility only; Both splits eligible and ineligible/unknown wallets into separate, non-overlapping schedules. "
         "All schedules wait for the configured phase time; Force Mint Now is separate."
         if auth_status["required"] else
         "Public phases have no local eligibility gate; Both avoids duplicate public mints."),
        "missing parameters: " + esc(", ".join(report["missing"]) if report["missing"] else "none detected"),
    ]
    site_inspection = d.get("site_inspection") or {}
    candidates = site_inspection.get("api_candidates") or []
    if candidates:
        lines.append("site API routes found (inspection only): " + esc(", ".join(candidates[:5])))
        lines.append("unknown routes are not called until a project-specific adapter validates their request/response format")
    if auth_status["required"]:
        lines.append("phase authorization: required — " + esc(auth_status["detail"]))
    lines += [
        f"price: <b>{esc(price_s)}</b>",
        f"per wallet: {esc(per_s)}",
        f"supply: {fmt_count(d.get('max_supply'))}",
        f"minted: {fmt_count(d.get('minted'))}",
        f"floor: {esc(str(d.get('floor_live') or 'live market data unavailable'))}",
        "",
    ]
    if draft_phase_authorized(d):
        lines += [
            "<b>🔢 QUANTITY</b>",
            esc(qty_limit_line(d)),
            f"each wallet mints: <b>{qty_of(d)}</b>",
            ("cost: supplied by the phase authorization API at fire time" if mint_router.is_gated(d)
             else esc(cost_line(d))),
        ]
    elif mint_router.is_gated(d):
        lines.append("quantity/cost: unavailable until the phase returns valid wallet authorization")
    lines += [
        "",
        "<b>🕒 WINDOW (UTC)</b>",
        f"opens: <b>{esc(opens)}</b>",
        f"closes: {esc(closes)}",
    ]
    if status:
        lines.append("status: " + esc(", ".join(status)))
    sched = d.get("schedule")
    if sched:
        lines += ["", "<b>♻️ PHASES</b>"]
        sel_i = _selected_index(d, [(st.get("chain_key"),) for st in sched])
        for n, st in enumerate(sched):
            sel = n == sel_i and st.get("chain_key") is not None
            lines += [""] + phase_block(st, sel)
    elif d.get("stages") or len(phases) > 1:
        lines += ["", "<b>♻️ PHASES</b>"]
        stage_list = d.get("stages") or [{"key": p} for p in phases]
        sel_i = _selected_index(d, [(st.get("key"),) for st in stage_list])
        for n, st in enumerate(stage_list):
            key = st.get("key")
            lines += [""] + phase_block(dict(st, chain_key=key, label=_phase_label(key)), n == sel_i)
    if d.get("schedule_unavailable"):
        lines += ["", esc("schedule unavailable (couldn't read the drop page). Showing on-chain phases only.")]
    if not sched and not (d.get("stages") or len(phases) > 1):
        lines += ["", "<b>♻️ PHASES</b>", f"single phase: {esc(_phase_label(phase_now))}"]
    if d.get("mint_sig"):
        lines += ["", "<b>⚙️ MINT CALL</b>", f"<code>{esc(d['mint_sig'])}</code>"]
    else:
        lines += [
            "",
            "<b>⚙️ MINT CALL</b>",
            "not detected. tap ⚙️ set mint call and send the function signature (e.g. mint(uint256)).",
            "or ask me to dig this project to find the real mint contract.",
        ]
    if d.get("fee_recipient"):
        lines.append(f"fee recipient: <code>{esc(d['fee_recipient'])}</code>")
    lines += ["", "<b>⛽ GAS</b>", f"limit: {esc(gas_s)}"]
    hint_key = "auto" if gas in (None, "auto") else ("high" if int(gas) == 500000 else "custom")
    lines.append("<i>" + esc(GAS_HINT[hint_key]) + "</i>")
    lines.append("<pre>" + "\n".join(fee_block(d)) + "</pre>")
    if d.get("speed") == "superfast":
        lines.append("🚀 super fast: top tier, no gas cap. it spends what it takes to land in the first block.")
    fm = d.get("fire_mode", "dual")
    lines.append("<b>🔥 mode: " + esc(FIRE_MODE_NAME.get(fm, FIRE_MODE_NAME["dual"])) + "</b>")
    if mint_router.is_gated(d):
        lines.append(
            "authorization is discovered and simulated per wallet before/at execution; firing waits for the selected phase boundary.")
    else:
        lines.append(esc(FIRE_MODE_TEXT.get(fm, FIRE_MODE_TEXT["dual"])))
    if d.get("safety"):
        lines += ["", "<b>🛡 SAFETY</b>"] + [esc(x) for x in d["safety"]]
    lines.append("~ estimate from similar mints, exact at fire time")
    for n in d.get("notes", []):
        lines.append("⚠️ " + esc(n))
    return "\n".join(lines)


def draft_kb(did, d):
    view = draft_phase_view(d)
    selected_stage = view.get("_selected_stage")
    rows = []
    sched = d.get("schedule")
    gated = [(i, st) for i, st in enumerate(sched or []) if st.get("gated")]
    if sched and len(sched) > 1:
        # every stage is a button, gated (allowlist) ones included. the phase block carries the warning.
        sel_i = _selected_index(d, [(st.get("chain_key") or st.get("key"),) for st in sched])
        prow = []
        for i, st in enumerate(sched):
            lbl = (st.get("label") or _phase_label(st.get("chain_key") or st.get("key") or "public")).replace(" (need allowlist spot)", "")
            prow.append(B(("✅ " if i == sel_i else "") + lbl, callback_data=f"dph:{did}:{i}"))
        for i in range(0, len(prow), 2):
            rows.append(prow[i:i + 2])
    elif not rows and d.get("phases") and len(d["phases"]) > 1:
        stage_list = [{"key": p} for p in d.get("phases") or []]
        sel_i = _selected_index(d, [(st.get("key"),) for st in stage_list])
        prow = [B(("✅ " if n == sel_i else "") + _phase_label(st.get("key")), callback_data=f"dph:{did}:{n}")
                for n, st in enumerate(stage_list)]
        for i in range(0, len(prow), 2):
            rows.append(prow[i:i + 2])
    rows.append([B("🔄 refresh live chain data", callback_data=f"drefresh:{did}")])
    rows.append([B("👛 pick wallets to mint", callback_data=f"wsel:{did}")])
    rows.append([B(f"🌐 chain: {d.get('chain', CHAIN_NAME)}", callback_data=f"dc:{did}")])
    # Fire mode is selected independently from phase eligibility and authorization.
    cur_mode = d.get("fire_mode", "dual")
    rows.append([B(("✅ " if cur_mode == k else "") + FIRE_MODE_LABEL[k], callback_data=f"dfm:{did}:{k}")
                 for k in ("prefire", "normal", "dual")])
    # speed tiers, 2 per row
    srow = [B(("✅ " if k == d.get("speed", "fast") else "") + SPEED_EMOJI[k] + " " + SPEED_LABELS[k],
              callback_data=f"dsp:{did}:{k}") for k in ("slow", "medium", "fast", "superfast")]
    rows += [srow[:2], srow[2:]]
    # quantity per wallet, 1..cap
    rows += qty_rows(did, view)
    # gas limit
    cur_gas = d.get("gas_limit", "auto")
    is_auto = cur_gas in (None, "auto")
    is_high = cur_gas == 500000
    is_custom = not is_auto and not is_high
    grow = [B(("✅ " if is_auto else "") + GAS_LABEL["auto"], callback_data=f"dg:{did}:auto"),
            B(("✅ " if is_high else "") + GAS_LABEL["high"], callback_data=f"dg:{did}:500000")]
    rows.append(grow)
    rows.append([B(("✅ " if is_custom else "") + GAS_LABEL["custom"], callback_data=f"dgc:{did}")])
    # fill only what the contract did not give us
    fills = []
    if selected_stage is None or not selected_stage.get("gated"):
        if d.get("price_wei") is None:
            fills.append(B("💰 set price", callback_data=f"dp:{did}"))
        if not d.get("start_ts"):
            fills.append(B("🕒 set start", callback_data=f"ds:{did}"))
        if not d.get("per_wallet"):
            fills.append(B("👛 set per-wallet", callback_data=f"dw:{did}"))
    if not d.get("mint_sig") and not d.get("seadrop"):
        fills.append(B("⚙️ set mint call", callback_data=f"dmc:{did}"))
    for i in range(0, len(fills), 2):
        rows.append(fills[i:i + 2])
    phase_view = draft_phase_view(d)
    gated = mint_router.is_gated(phase_view)
    force_ready = gated or (
        not gated and d.get("price_wei") is not None
        and (d.get("mint_sig") or d.get("seadrop"))
    )
    rows.append([
        B("📅 NORMAL SCHEDULE", callback_data=f"dsched:{did}"),
        B("🔥 FORCE SCHEDULE", callback_data=f"fsched:{did}"),
        B("🔀 Both schedules", callback_data=f"bsched:{did}"),
    ])
    if force_ready:
        rows.append([B("⚡ FORCE MINT NOW", callback_data=f"fm:{did}")])
    rows.append([B("🗑 discard", callback_data=f"dx:{did}")])
    return M(rows)


async def refresh_fees(d):
    """Live gas tiers from the chain. Never raises."""
    try:
        rpc = RPC(CFG.chains[d.get("chain", CHAIN_NAME)])
        d["fees"] = await asyncio.to_thread(sn.fee_table, rpc, CFG.chains[d.get("chain", CHAIN_NAME)], sn.static_gas(d, qty_of(d)))
    except Exception as e:
        log.info("fee estimate failed: %s", type(e).__name__)
        d["fees"] = None
    try:
        d["usd_per_eth"] = await asyncio.to_thread(prices.native_usd, d.get("chain", CHAIN_NAME))
    except Exception:
        d["usd_per_eth"] = None


async def _edit_panel(q, text, reply_markup=None):
    """Edit the tapped panel in place. Re-tapping the current selection sends the same text,
    which Telegram rejects as 'not modified'. That is not an error."""
    try:
        await q.edit_message_text(text, reply_markup=reply_markup)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def _edit_or_send(update_or_ctx, chat_id, msg_id, text, kb, ctx):
    try:
        if msg_id:
            await ctx.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text, reply_markup=kb)
            return msg_id
    except Exception as e:
        if "not modified" in str(e).lower():
            return msg_id
        log.info("edit in place failed, sending new panel: %s", type(e).__name__)
    m = await ctx.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
    return m.message_id


def main_panel():
    text = "\n".join(panels.header("home") + [
        "pick a chain, paste a contract, and i'll fire your wallets the second the sale opens.",
        "",
        "tap below, or just type it: <i>schedule robinhood mint 0x... at 20:00 utc</i>",
        "",
        "<i>every mint time is UTC, with Kathmandu time alongside.</i>",
    ])
    return text, M([[B("➕ new mint", callback_data="m:new"), B("📅 my mints", callback_data="m:list")],
                    [B("👛 wallets", callback_data="w:list"), B("💰 balances", callback_data="bal:chains")],
                    [B("📊 portfolio", callback_data="pf:open"), B("❓ help", callback_data="help")],
                    [B("🔄 restart", callback_data="rst")]])


def chain_picker_panel():
    rows = [[B(chain_label(n), callback_data=f"nc:{n}")] for n in enabled_mint_chains()]
    rows.append([B("cancel", callback_data="home")])
    return ("new mint\n\nstep 1 of 2: pick the chain the contract is on.", M(rows))


HELP_SECTIONS = {
    "mints": ("🎯 mints", [
        "<code>/addmint 0xCONTRACT</code>  analyze a contract and schedule it",
        "Public SeaDrop and supported simple EVM mints work from contract analysis. GTD, FCFS, and other gated phases need a real wallet-specific allowlist transaction from the project API or, on Ethereum, OpenSea, Reservoir, and Magic Eden. Normal Schedule uses locally eligible wallets; Force Schedule bypasses local eligibility only and still waits for the phase and valid authorization. Both splits eligible and ineligible/unknown wallets into non-overlapping schedules. Force Mint Now is separate, and never substitutes a public call.",
        "🎟 eligibility checks report allowlist membership only; they do not supply a Merkle proof or signed mint authorization.",
        "🎯 from sagetracer: the <b>snipe in ACO</b> button on a wallet alert opens this bot on that contract, straight into the mint analysis panel (admin only).",
        "<code>/mymints</code>  scheduled and past mints, tap to open",
        "<code>/cancelmint ID</code>  cancel a scheduled mint",
        "<code>/watchlist</code>  upcoming contracts you're watching",
        "<code>/snipeprice</code>  price a contract for a sniper pass",
        "<code>/solprog PROGRAM_ID</code>  inspect an executable Solana program's published Anchor IDL (read-only)",
    ]),
    "alpha": ("🎯 copy alpha", [
        "<code>/alpha</code>  last 50 trades by your tracked wallets, tap one for the card",
        "<code>/alpha up</code>  best recent buys, with pnl vs the current floor (ethereum)",
        "<code>/alpha new</code>  mints your tracked wallets just made, one tap to add to mint (admin)",
        "every card has the CA (tap to copy), price in ETH + USD, and the tx and marketplace links.",
        "the feed is read from the sagetracer tracker db, read-only.",
    ]),
    "wallets": ("👛 wallets", [
        "<code>/wallets</code>  fleet keys, main wallet, chain per wallet",
        "<code>/fund</code>  top up fleet wallets from main",
        "<code>/sweep</code>  pull funds back to main",
        "keys are loaded in dm only. never paste them in a group.",
    ]),
    "money": ("💰 money", [
        "<code>/balance</code>  live native + token balances",
        "<code>/send</code>  send from a wallet, always confirm first",
        "<code>/portfolio</code>  held mints with pnl at floor (usd)",
    ]),
    "ai": ("🧠 ai", [
        "say it plain: <i>schedule robinhood mint 0x... at 20:00 utc</i>",
        "<i>check my balances</i>, <i>send 0.1 eth from wallet 2 to main</i>",
        "<i>cancel mint 3</i>. money moves always show a confirm button first.",
        "<code>/ai</code>  check the model status",
    ]),
}


def help_text(section=None):
    if section and section in HELP_SECTIONS:
        title, body = HELP_SECTIONS[section]
        return "\n".join(panels.header(f"help · {title[2:]}") + [""] + body)
    return "\n".join(panels.header("help") + [
        "pick a section below, or type what you need.",
        "",
        "mint times are utc (kathmandu alongside). gas and fees show in usd.",
    ])


def help_kb(section=None):
    rows = [[B(v[0], callback_data=f"help:{k}") for k, v in list(HELP_SECTIONS.items())[:2]],
            [B(v[0], callback_data=f"help:{k}") for k, v in list(HELP_SECTIONS.items())[2:]]]
    rows.append([B("🏠 main", callback_data="home")])
    return M(rows)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not (allowed(update.effective_user.id) or username_allowed(update.effective_user.username)):
        await update.message.reply_text("🔒 not on the crew list yet.\n👉 ask anish to /allow you.")
        return
    await update.message.reply_text(help_text(), reply_markup=help_kb())


def start_arg_contract(args):
    """Deep-link arg from /start <arg>: the contract address when it is a valid 0x address, else None."""
    if not args:
        return None
    arg = (args[0] or "").strip()
    return arg if ADDR_RE.match(arg) and valid_address(arg) else None


async def cmd_restart(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _admin_only(update): return
    if not is_dm(update): return
    await update.message.reply_text("restarting bot. back in a few seconds.")
    os.execv(sys.executable, [sys.executable, *sys.argv])

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_dm(update):
        await update.message.reply_text("dm me to set up.")
        return
    if not (allowed(uid) or username_allowed(update.effective_user.username)):
        await update.message.reply_text("not on the crew list. ask anish to /allow you.")
        return
    # cross-bot deep link from sagetracer (t.me/sageacobot?start=0x...): admin only lands in the addmint draft
    contract = start_arg_contract(ctx.args)
    if contract and uid == ADMIN_ID:
        ctx.user_data.pop("await", None)
        await start_draft(update.effective_chat.id, ctx, contract, chain_name=CHAIN_NAME)
        return
    t, kb = main_panel()
    intro = "\n".join([
        "⚡ <b>ACO SNIPER</b>",
        "<i>mint sniper for ethereum + robinhood chain</i>",
        DIVIDER_LINE,
        "i watch a contract, pre-sign your wallets' mint txs, and fire them the second the sale opens.",
        "",
        "👉 tap <b>new mint</b> to start, or use the menu (/) for commands.",
    ])
    await ctx.bot.send_message(update.effective_chat.id, intro)
    ctx.user_data["panel"] = await _edit_or_send(update, update.effective_chat.id, None, t, kb, ctx)


async def cmd_cancel(update: Update, ctx):
    if not is_dm(update):
        return
    if ctx.user_data.pop("await", None):
        await update.message.reply_text("cancelled. tap /start for the panel.")
    else:
        await update.message.reply_text("nothing to cancel. tap /start for the panel.")


async def cmd_allow(update: Update, ctx):
    if update.effective_user.id != ADMIN_ID:
        return
    arg = (ctx.args[0] if ctx.args else "").strip()
    if not arg:
        await update.message.reply_text("usage: /allow <username|user_id>")
        return
    if arg.startswith("@") or not arg.lstrip("-").isdigit():
        uname = "@" + arg.lstrip("@")
        if uname.lower() not in {x.lower() for x in CFG.whitelist_usernames}:
            CFG.whitelist_usernames.append(uname)
            save_whitelist()
        await update.message.reply_text(f"allowed {uname} (by username).")
        return
    uid = int(arg)
    if uid not in CFG.whitelist_ids:
        CFG.whitelist_ids.append(uid)
        save_whitelist()
    await update.message.reply_text(f"allowed {uid}.")


def save_whitelist():
    p = HOME / "whitelist.json"
    p.write_text(json.dumps({"ids": CFG.whitelist_ids, "usernames": CFG.whitelist_usernames}))


def load_whitelist():
    p = HOME / "whitelist.json"
    if p.exists():
        raw = json.loads(p.read_text())
        items = raw if isinstance(raw, list) else raw.get("ids", []) + raw.get("usernames", [])
        for x in items:
            if isinstance(x, int) and x not in CFG.whitelist_ids:
                CFG.whitelist_ids.append(x)
            elif isinstance(x, str):
                u = x if x.startswith("@") else "@" + x
                if u.lower() not in {y.lower() for y in CFG.whitelist_usernames}:
                    CFG.whitelist_usernames.append(u)


async def cmd_block(update: Update, ctx):
    """Remove a user's access: same as never allowing them."""
    if update.effective_user.id != ADMIN_ID:
        return
    arg = (ctx.args[0] if ctx.args else "").strip()
    if not arg:
        await update.message.reply_text("usage: /block @username or user_id")
        return
    if arg.startswith("@") or not arg.lstrip("-").isdigit():
        uname = "@" + arg.lstrip("@")
        before = len(CFG.whitelist_usernames)
        CFG.whitelist_usernames = [x for x in CFG.whitelist_usernames if x.lower() != uname.lower()]
        removed = len(CFG.whitelist_usernames) < before
        save_whitelist()
        if removed:
            await update.message.reply_text(f"✅ {uname} removed. they no longer have access.")
        else:
            await update.message.reply_text(f"{uname} wasn't on the access list.")
    else:
        uid = int(arg)
        if uid in CFG.whitelist_ids:
            CFG.whitelist_ids.remove(uid)
            save_whitelist()
            await update.message.reply_text(f"✅ {uid} removed. they no longer have access.")
        else:
            await update.message.reply_text(f"{uid} wasn't on the access list.")


async def cmd_eligibleset(update: Update, ctx):
    if update.effective_user.id != ADMIN_ID:
        return
    from acobot import eligible as elig
    args = ctx.args or []
    if len(args) < 2:
        await update.message.reply_text("usage: /eligibleset 0xCONTRACT https://api.example.com/allow/{wallet}")
        return
    try:
        elig.save_source(args[0], args[1])
    except ValueError as e:
        await update.message.reply_text(f"not saved: {e}")
        return
    await update.message.reply_text(f"saved eligibility endpoint for {args[0].lower()}. next checks use it.")


async def cmd_mintapiset(update: Update, ctx):
    if update.effective_user.id != ADMIN_ID:
        return
    args = ctx.args or []
    if len(args) < 2:
        await update.message.reply_text("usage: /mintapiset 0xCONTRACT https://api.example.com/mint/{wallet}")
        return
    try:
        mintapi.save(args[0], args[1])
    except ValueError as e:
        await update.message.reply_text(f"not saved: {e}")
        return
    await update.message.reply_text(
        f"saved mint api for {args[0].lower()}. URL templates may use {{wallet}}, {{phase}}, "
        "{stage_index} and {qty}. Gated APIs must return JSON with transaction `to`, `data` "
        "and `value` (optional `chainId`); each wallet's exact transaction is simulated before signing.")


async def cmd_addmint(update: Update, ctx):
    if update.effective_user.id != ADMIN_ID:
        return
    if not is_dm(update):
        await update.message.reply_text("dm me to add mints.")
        return
    if not ctx.args:
        ctx.user_data.pop("await", None)
        ctx.user_data["await"] = ("chain", None)
        await ctx.bot.send_message(update.effective_chat.id, "pick the chain for this mint.",
                                   reply_markup=chain_picker_panel()[1])
        return
    ctx.user_data.pop("await", None)
    await start_draft(update.effective_chat.id, ctx, ctx.args[0].strip(), chain_name=CHAIN_NAME)


async def cmd_solprog(update: Update, ctx):
    uid = update.effective_user.id
    if not (allowed(uid) or username_allowed(update.effective_user.username)):
        await update.message.reply_text("🔒 not on the crew list yet.")
        return
    if not is_dm(update):
        await update.message.reply_text("dm me to inspect Solana programs.")
        return
    if not ctx.args:
        await update.message.reply_text("usage: /solprog PROGRAM_ID")
        return
    try:
        result = await asyncio.to_thread(auth_resolver.inspect_solana_program, ctx.args[0])
    except (ValueError, RuntimeError) as exc:
        await update.message.reply_text(f"program inspection failed: {esc(str(exc))}")
        return
    lines = [
        f"<b>Solana program: {esc(result.get('name') or 'unknown')} {esc(result.get('version') or '')}</b>",
        f"program: <code>{esc(result['program_id'])}</code>",
        "network: Solana Mainnet · protocol: " + esc(result["protocol"]),
        "authorization: " + esc(result["authorization_type"]),
        "transaction construction: not supported without a verified Solana mint adapter",
        "missing: " + esc(", ".join(result["missing"])),
        "",
        "<b>Instructions</b>",
    ]
    for instruction in result["instructions"][:40]:
        args = ", ".join(instruction["args"])
        lines.append(f"• <code>{esc(instruction['name'])}({esc(args)})</code>")
    if len(result["instructions"]) > 40:
        lines.append(f"… {len(result['instructions']) - 40} more")
    await update.message.reply_text("\n".join(lines)[:3900], parse_mode="HTML")


def valid_address(s):
    if not ADDR_RE.match(s):
        return False
    body = s[2:]
    if body == body.lower() or body == body.upper():
        return True  # no checksum to verify
    try:
        from eth_utils import is_checksum_address
        return is_checksum_address(s)
    except Exception:
        return True


def valid_mint_address(value, chain_name):
    if chain_name == "solana":
        try:
            mint_engine.validate_solana_program(value)
            return True
        except mint_engine.MintEngineError:
            return False
    try:
        mint_engine.validate_evm_address(value)
        return True
    except mint_engine.MintEngineError:
        return False


def cname_os(cname):
    """OpenSea chain slug for our chain name."""
    return {"robinhood": "robinhood", "ethereum": "ethereum"}.get(cname, cname)


def analyze_contract_sync(contract, cname):
    """Return (draft, None) or (None, friendly_error) from a fresh read. Never raises.

    Only invalid input is rejected here (no code, or an EOA wallet). A valid NFT or contract whose
    mint call is unknown is KEPT as a draft: the admin fills price/start/mint call with the buttons.
    """
    try:
        chain = CFG.chains.get(cname)
        if cname not in enabled_chains() or chain is None:
            return None, f"{cname} is not a supported EVM network."
        rpc = RPC(chain)
        try:
            rpc.assert_chain_id(chain.chain_id)
        except Exception as exc:
            log.warning("network verification failed on %s: %s", cname, type(exc).__name__)
            return None, f"couldn't verify the configured RPC is {cname}. check its endpoint configuration."
        d = mint_engine.analyze_evm(contract, chain, rpc, verify_chain=False)
        try:
            ro = rugcheck.scan(rpc, contract, d)
            d["safety"] = rugcheck.panel_lines(ro)
        except Exception:
            log.warning("rug check skipped")
        if d.get("kind") == "eoa_7702":
            return None, (f"{contract} is a wallet (EOA with an EIP-7702 delegation) on {cname}, "
                          "not a mint contract. paste the NFT contract address instead.")
        if d.get("seadrop"):
            from acobot import opensea
            d["schedule_unavailable"] = True
            try:
                os_stages = opensea.fetch_schedule(cname_os(cname), contract, force=True)
                d["schedule"] = opensea.merge(d.get("stages") or [], os_stages)
                d["schedule_unavailable"] = not os_stages
                if any(stage.get("gated") for stage in d.get("schedule") or []):
                    d["official_site"] = opensea.project_site(cname_os(cname), contract)
                    if d["official_site"]:
                        d["site_inspection"] = auth_resolver.inspect_project_site(d["official_site"])
            except Exception as e:
                log.warning("opensea enrich skipped: %s", type(e).__name__)
                d["schedule"] = opensea.merge(d.get("stages") or [], None)
        return d, None
    except NoContract:
        return None, f"no contract found on {cname}. try another chain."
    except Exception as e:
        log.warning("analyze on %s failed: %s", cname, type(e).__name__, exc_info=True)
        return None, f"couldnt read the contract on {cname} right now (rpc error). try again in a minute."


async def analyze_contract(contract, cname):
    """Async wrapper for the shared fresh contract analysis."""
    return await asyncio.to_thread(analyze_contract_sync, contract, cname)


async def start_draft(chat_id, ctx, contract, chain_name=None):
    contract = (contract or "").strip()
    cname = chain_name or CHAIN_NAME
    if not valid_mint_address(contract, cname):
        address_hint = ("a valid Solana program address" if cname == "solana"
                        else "0x followed by 40 hex characters with a valid checksum")
        await ctx.bot.send_message(chat_id, f"that address doesn't look valid; send {address_hint}.")
        ctx.user_data["await"] = ("contract", cname)
        return
    if cname == "solana":
        await ctx.bot.send_message(chat_id, "analyzing on Solana Mainnet...")
        try:
            facts = await asyncio.to_thread(
                mint_engine.analyze_solana, contract, rpc_url=CFG.solana_rpc_url)
        except Exception as exc:
            log.warning("Solana program analysis failed: %s", type(exc).__name__)
            await ctx.bot.send_message(
                chat_id, f"Solana program analysis failed: {esc(str(exc))}\n\nsend another address, or /start.")
            ctx.user_data["await"] = ("contract", cname)
            return
        ctx.user_data.pop("await", None)
        lines = [
            "<b>Solana Mainnet program analysis</b>",
            f"Program: <code>{esc(facts['program_id'])}</code>",
            f"Protocol: {esc(facts['protocol'])}",
            f"Program/IDL: {esc(facts.get('name') or 'no published Anchor IDL detected')}",
            "Authorization: " + esc(facts.get("authorization_type") or "unknown"),
            "Mint transaction construction: not supported; no Solana mint adapter is registered.",
            "Missing: " + esc(", ".join(facts.get("missing") or [
                "verified Solana mint protocol adapter and wallet-specific authorization/account mapping"
            ])),
            "No transaction was constructed or sent.",
        ]
        await ctx.bot.send_message(chat_id, "\n".join(lines), parse_mode="HTML")
        return
    await ctx.bot.send_message(chat_id, f"analyzing on {cname}...")
    d, err = await analyze_contract(contract, cname)
    if err:
        await ctx.bot.send_message(chat_id, err + "\n\nsend another address, or /start.")
        ctx.user_data["await"] = ("contract", cname)
        return
    ctx.user_data.pop("await", None)
    d["chain"] = cname
    d.setdefault("speed", "fast")
    d.setdefault("gas_limit", "auto")
    if d.get("phases"):
        d.setdefault("phase", "public" if "public" in d["phases"] else d["phases"][0])
    if not d.get("fire_mode_picked"):
        stage = draft_phase_view(d).get("_selected_stage")
        if stage:
            if stage.get("list_mode"):
                d["list_mode"] = stage["list_mode"]
            d["fire_mode"] = recommended_fire_mode(stage)
    d.setdefault("fire_mode", "dual")   # FCFS and public races use both shots; GTD uses one
    await refresh_fees(d)
    did = max(DRAFTS.keys(), default=0) + 1
    DRAFTS[did] = d
    save_drafts()
    await ctx.bot.send_message(chat_id, draft_text(d), reply_markup=draft_kb(did, d))
    if not d.get("mint_sig") and not d.get("seadrop"):
        await ctx.bot.send_message(chat_id, "no mint call found on this contract. want me to dig the web for the real mint contract? say 'dig <project name>'.")


def _safe_send(chat_id, ctx, text):
    """send with HTML parse, auto-escaping stray < > so one raw word never kills a panel"""
    import html as _h
    try:
        return ctx.bot.send_message(chat_id, text, parse_mode='HTML')
    except Exception:
        return ctx.bot.send_message(chat_id, _h.escape(text))


async def on_callback(update: Update, ctx):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data
    chat_id = q.message.chat_id
    if data.startswith("stb:"):
        await on_tokenbuy_cb(update, ctx)
        return
    if data.startswith("trd:ch:"):
        await on_trade_chain_cb(update, ctx)
        return
    if data.startswith("trd:"):
        await on_trade_mode_cb(update, ctx)
        return
    if not (allowed(uid) or username_allowed(q.from_user.username)):
        await ctx.bot.send_message(chat_id, "not on the crew list.")
        return
    if data == "m:new":
        if uid != ADMIN_ID:
            return
        ctx.user_data.pop("await", None)
        t, kb = chain_picker_panel()
        await _edit_or_send(update, chat_id, q.message.message_id, t, kb, ctx)
    elif data.startswith("dga:") or data.startswith("dgn:"):
        if uid != ADMIN_ID:
            return
        try:
            pid = int(data.split(":", 1)[1])
        except ValueError:
            return
        prop = DIG_PROPOSALS.pop(pid, None)
        if data.startswith("dgn:") or not prop:
            await ctx.bot.send_message(chat_id, "ok, not adding it." if prop else "that proposal is gone. ask me to add it again.")
            return
        await start_draft(chat_id, ctx, prop["contract"], chain_name=prop["chain"])
    elif data.startswith("sell:set:"):
        if uid != ADMIN_ID:
            return
        mid = int(data.split(":", 2)[2])
        mint = MINTS.get(mid)
        rec = resmod.get_result(DATA, mid)
        if not mint or not rec:
            await _edit_panel(q, "mint result is unavailable, so sell mode cant be armed.")
            return
        contract = mint.get("contract") or rec.get("contract")
        chain = mint.get("chain") or rec.get("chain") or CHAIN_NAME
        rows = [r for r in rec.get("rows", []) if r.get("token_id")]
        if not rows:
            await _edit_panel(q, "no minted token ids were recorded for sell mode.")
            return
        ctx.user_data["await"] = ("sell_floor", {"mid": mid, "contract": contract,
                                                  "chain": chain, "tokens": [str(r["token_id"]) for r in rows]})
        await q.message.reply_text("send the minimum offer floor in ETH. offers below it can never be accepted.")
    elif data.startswith("nc:"):
        if uid != ADMIN_ID:
            return
        cname = data[3:]
        if cname not in enabled_mint_chains():
            await ctx.bot.send_message(chat_id, "that chain isnt configured.")
            return
        ctx.user_data["await"] = ("contract", cname)
        await _edit_or_send(update, chat_id, q.message.message_id,
                            f"new mint on {chain_label(cname)}\n\nstep 2 of 2: send the EVM contract address (0x...) or Solana program address. send /cancel to stop.",
                            M([[B("change chain", callback_data="m:new"), B("cancel", callback_data="home")]]), ctx)
    elif data == "m:list":
        await show_mints(chat_id, ctx, uid, q.message.message_id)
    elif data.startswith("mc:"):
        await cancel_mint_id(chat_id, ctx, int(data[3:]), uid)
    elif data.startswith("mpick:"):
        if uid != ADMIN_ID:
            return
        parts = data.split(":")
        mid = int(parts[1])
        m = MINTS.get(mid)
        if not m:
            await _edit_panel(q, "that mint is gone.")
            return
        sel = ctx.user_data.setdefault("mintpick", {}).setdefault(str(mid), set())
        if not sel:
            sel.update(a.lower() for _o,_l,a,_q in mint_wallets(m))
        if len(parts) == 3 and parts[2] == "save":
            m["wallets"] = sorted(sel)
            persist()
            n = len(m["wallets"])
            await _edit_or_send(None, chat_id, q.message.message_id,
                f"mint #{mid} will fire from {n} selected wallet(s).", M([[B("my mints", callback_data="mymints")]]), ctx)
            return
        if len(parts) == 3:
            addr = parts[2].lower()
            if addr in sel: sel.discard(addr)
            else: sel.add(addr)
        await show_mint_wallet_picker(chat_id, ctx, mid, q.message.message_id)
    elif data.startswith(("dp:", "ds:", "dw:", "dgc:", "dmc:")):
        if uid != ADMIN_ID:
            return
        did = int(data.split(":")[1])
        field = {"dp": "price", "ds": "start", "dw": "per_wallet", "dmc": "mint_call"}.get(data[:3] if data[:3] == "dmc" else data[:2], "gas_limit")
        prompt = {"price": "send mint price in ETH, e.g. 0.05 (0 for free)",
                  "start": "send the start time in UTC, e.g. 2026-10-27 20:00, or unix seconds",
                  "per_wallet": "send the per-wallet limit as a whole number",
                  "mint_call": "send a supported mint signature, e.g. mint(uint256) or publicMint(). only fn() and fn(uint256) are supported.",
                  "gas_limit": "send a custom gas limit as a whole number, e.g. 300000, or auto"}[field]
        ctx.user_data["await"] = (field, did)
        await ctx.bot.send_message(chat_id, prompt)
    elif data.startswith("dee:"):
        if uid != ADMIN_ID:
            return
        from acobot import eligible as elig
        _, did, i = data.split(":")
        d = DRAFTS.get(int(did))
        if d is None:
            await ctx.bot.send_message(chat_id, "that draft expired. send the contract again to rebuild it.")
            return
        sched = d.get("schedule") or []
        idx = int(i)
        st = sched[idx] if 0 <= idx < len(sched) else {}
        owners = mint_wallets(d)
        wl = [{"label": lbl, "address": a, "owner": o, "qty": q} for o, lbl, a, q in owners]
        if not wl:
            await ctx.bot.send_message(chat_id, "no signer wallets on this chain to check.")
            return
        res = await elig.probe_fleet(d["contract"], d.get("chain", CHAIN_NAME), wl, st)
        panel = elig.panel_text(st.get("label") or "gated stage", [(w, r) for w, r in res])
        panel += "\n\nReport only: wallet selection is unchanged. Mint authorization is checked using the phase transaction."
        await _safe_send(chat_id, ctx, panel)
    elif data.startswith("dph:"):
        if uid != ADMIN_ID:
            return
        _, did, i = data.split(":")
        d = DRAFTS.get(int(did))
        if d is None or not d.get("phases"):
            await ctx.bot.send_message(chat_id, "that draft expired. send the contract again to rebuild it.")
            return
        sched = d.get("schedule")
        if sched:
            idx = int(i)
            if idx < 0 or idx >= len(sched):
                # stale button from an older panel (phases list changed after a re-analyze): rebuild
                await _edit_panel(q, draft_text(d), draft_kb(int(did), d))
                return
            st = sched[idx]
            if st.get("chain_key") is None:
                await ctx.bot.send_message(chat_id, "that phase isn't registered on this contract, so it can't be sniped. pick another.")
                return
            # Keep display-only OpenSea terms separate from the on-chain execution values.
            apply_mint_stage(d, st)
        else:
            phases = d.get("phases") or []
            d["phase"] = phases[int(i)] if int(i) < len(phases) else (phases[0] if phases else "public")
        d["phase_idx"] = int(i)  # single-select: this row only; all others are cleared
        save_drafts()
        # silent select: allowlist phases are selectable like any other; the warning lives in the phase block
        await _edit_panel(q, draft_text(d), draft_kb(int(did), d))
    elif data.startswith("dq:"):
        if uid != ADMIN_ID:
            return
        _, did, n = data.split(":")
        d = DRAFTS.get(int(did))
        if d is None:
            await ctx.bot.send_message(chat_id, "that draft is gone.")
            return
        d["mint_qty"] = max(1, min(int(n), qty_cap(d)))
        save_drafts()
        await _edit_panel(q, draft_text(d), draft_kb(int(did), d))
    elif data.startswith("dfm:"):
        if uid != ADMIN_ID:
            return
        _, did, mode = data.split(":")
        d = DRAFTS.get(int(did))
        if d is None or mode not in FIRE_MODE_LABEL:
            await ctx.bot.send_message(chat_id, "that draft is gone.")
            return
        d["fire_mode"] = mode
        d["fire_mode_picked"] = True
        save_drafts()
        await _edit_panel(q, draft_text(d), reply_markup=draft_kb(int(did), d))
    elif data.startswith("dfmblocked:"):
        if uid != ADMIN_ID:
            return
        d = DRAFTS.get(int(data.split(":", 1)[1]))
        if d is None:
            await ctx.bot.send_message(chat_id, "that draft is gone.")
            return
        await ctx.bot.send_message(
            chat_id,
            "The two-shot pre-fire fire mode is disabled for gated phases because authorization can be wallet- and phase-specific or expire. "
            "The separate Both schedule option remains available: locally eligible wallets use Normal, while ineligible or unknown wallets use Force. "
            "Authorization is still fetched and simulated per wallet before signing.",
        )
    elif data.startswith("dsp:"):
        if uid != ADMIN_ID:
            return
        _, did, speed = data.split(":")
        d = DRAFTS.get(int(did))
        if d is None or speed not in sn.SPEEDS:
            await ctx.bot.send_message(chat_id, "that draft is gone.")
            return
        d["speed"] = speed
        await refresh_fees(d)
        save_drafts()
        await _edit_panel(q, draft_text(d), reply_markup=draft_kb(int(did), d))
    elif data.startswith("dg:"):
        if uid != ADMIN_ID:
            return
        _, did, val = data.split(":")
        d = DRAFTS.get(int(did))
        if d is None:
            await ctx.bot.send_message(chat_id, "that draft is gone.")
            return
        d["gas_limit"] = "auto" if val == "auto" else int(val)
        save_drafts()
        await _edit_panel(q, draft_text(d), reply_markup=draft_kb(int(did), d))
    elif data.startswith("dsched:"):
        if uid != ADMIN_ID:
            return
        did = int(data.split(":")[1])
        await run_schedule_check(chat_id, ctx, did, q.message.message_id)
    elif data.startswith("fsched:"):
        if uid != ADMIN_ID:
            return
        did = int(data.split(":")[1])
        await run_schedule_check(chat_id, ctx, did, q.message.message_id, workflow="force")
    elif data.startswith("bsched:"):
        if uid != ADMIN_ID:
            return
        did = int(data.split(":")[1])
        await run_schedule_check(chat_id, ctx, did, q.message.message_id, workflow="both")
    elif data.startswith("fmconfirm:"):
        if uid != ADMIN_ID:
            return
        await confirm_force_mint(chat_id, ctx, int(data.split(":")[1]), q.message.message_id)
    elif data.startswith("fmcancel:"):
        if uid != ADMIN_ID:
            return
        did = int(data.split(":")[1])
        d = DRAFTS.get(did)
        if d is not None:
            d.pop("force_pending_until", None)
            save_drafts()
            await _edit_panel(q, draft_text(d), draft_kb(did, d))
    elif data.startswith("fm:"):
        if uid != ADMIN_ID:
            return
        await force_mint(chat_id, ctx, int(data[3:]), q.message.message_id)
    elif data.startswith("wt:"):
        if uid != ADMIN_ID:
            return
        _, did, i = data.split(":"); did = int(did); i = int(i)
        d = DRAFTS.get(did)
        if d is None: return
        allw = mint_wallets(d)
        if i < len(allw):
            ex = set(d.get("wex") or []); a = allw[i][2].lower()
            ex.symmetric_difference_update({a})
            d["wex"] = sorted(ex)
            save_drafts()
        await show_wallet_step(chat_id, ctx, did, q.message.message_id)
    elif data.startswith("wall:"):
        if uid != ADMIN_ID:
            return
        did = int(data[5:]); d = DRAFTS.get(did)
        if d is not None:
            d["wex"] = []
            save_drafts()
        await show_wallet_step(chat_id, ctx, did, q.message.message_id)
    elif data.startswith("wsel:"):
        if uid != ADMIN_ID:
            return
        await show_wallet_step(chat_id, ctx, int(data[5:]), q.message.message_id)
    elif data.startswith("wchk:"):
        if uid != ADMIN_ID:
            return
        await run_check(chat_id, ctx, int(data[5:]), q.message.message_id)
    elif data.startswith("wgo:"):
        if uid != ADMIN_ID:
            return
        await run_schedule_check(chat_id, ctx, int(data[4:]), q.message.message_id)
    elif data.startswith("wconfirm:"):
        if uid != ADMIN_ID:
            return
        parts = data.split(":")
        workflow = parts[2] if len(parts) > 2 else "normal"
        await schedule_draft(
            chat_id, ctx, int(parts[1]), q.message.message_id, workflow=workflow)
    elif data.startswith("wback:"):
        if uid != ADMIN_ID:
            return
        did = int(data[6:]); d = DRAFTS.get(did)
        if d is not None:
            await _edit_panel(q, draft_text(d), reply_markup=draft_kb(did, d))
    elif data.startswith("fc:"):
        await run_pending(chat_id, ctx, uid, int(data[3:]), q.message.message_id)
    elif data.startswith("bv:"):
        if uid != ADMIN_ID: return
        _, action, chain_name, *args = data.split(":")
        if action == "pick":
            await _edit_or_send(None, chat_id, q.message.message_id, f"<b>balances · {esc(chain_name)}</b>\npick an asset", _balance_asset_kb(chain_name), ctx)
        elif action == "paste":
            ctx.user_data["await_walletops"] = ("balance", chain_name)
            await ctx.bot.send_message(chat_id, f"send the token CA or mint for {chain_name}.")
        elif action == "show":
            asset = args[0]
            if asset == "native": token_arg = "native"
            else:
                try: token_arg = _saved_tokens(chain_name)[int(asset.replace("saved", ""))]["contract"]
                except (ValueError, IndexError, KeyError): return
                if token_arg.lower() == "native": token_arg = "native"
            ctx.user_data["balance_view"] = {"chain": chain_name, "asset": token_arg, "selected": None, "page": 0}
            await _show_balance_wallet_picker(chat_id, ctx, uid, q.message.message_id)
        elif action == "toggle":
            state = ctx.user_data.get("balance_view", {})
            if state.get("chain") != chain_name: return
            ws = _balance_wallets(uid, chain_name)
            try: wallet = ws[int(args[0])]
            except (ValueError, IndexError): return
            state["selected"] = walletops.toggle_wallet_selection(state.get("selected"), wallet["address"])
            await _show_balance_wallet_picker(chat_id, ctx, uid, q.message.message_id)
        elif action == "all":
            state = ctx.user_data.get("balance_view", {})
            if state.get("chain") != chain_name: return
            state["selected"] = None
            await _show_balance_wallet_picker(chat_id, ctx, uid, q.message.message_id)
        elif action == "page":
            state = ctx.user_data.get("balance_view", {})
            if state.get("chain") != chain_name: return
            ws = _balance_wallets(uid, chain_name)
            state["page"] = max(0, min(int(args[0]), max(0, (len(ws) - 1) // 8)))
            await _show_balance_wallet_picker(chat_id, ctx, uid, q.message.message_id)
        elif action == "balances":
            state = ctx.user_data.get("balance_view", {})
            if state.get("chain") != chain_name: return
            text, kb = await _balance_chain_text(uid, chain_name, state["asset"], state.get("selected"))
            await _edit_or_send(None, chat_id, q.message.message_id, text[:4000], kb, ctx)
    elif data.startswith("sw:"):
        if uid != ADMIN_ID: return
        p = data.split(":")
        if p[1] == "chain":
            chain_name = p[2]
            ctx.user_data["sweepall"] = {"step": "asset", "chain": chain_name}
            await _edit_or_send(None, chat_id, q.message.message_id, f"<b>sweep all · {esc(chain_name)}</b>\npick asset", _sweep_asset_kb(chain_name), ctx)
        elif p[1] == "asset":
            chain_name, val = p[2], p[3]
            if val == "nft":
                ctx.user_data["sweepall"] = {"step": "nft_ca", "chain": chain_name, "asset": "nft"}
                await ctx.bot.send_message(chat_id, "paste the NFT contract address (CA).")
                return
            if val == "native": asset = "native"
            else:
                try: asset = _saved_tokens(chain_name)[int(val.replace("saved", ""))]["contract"]
                except (ValueError, IndexError, KeyError): return
                if asset.lower() == "native": asset = "native"
            ctx.user_data["sweepall"] = {"step": "source", "chain": chain_name, "asset": asset}
            main = wallets.main_wallet(uid)
            rows = [[B("all fleet wallets", callback_data=f"sw:src:{chain_name}:all")]]
            seen = set()
            for w2 in wallets.list_wallets(uid):
                a2 = (w2["address"] or "").lower()
                if a2 in seen: continue
                seen.add(a2)
                rows.append([B(f"{w2['label']} ({wallets.mask(w2['address'])})", callback_data=f"sw:src:{chain_name}:{a2}")])
            await _edit_or_send(None, chat_id, q.message.message_id, "sweep from which wallet?", M(rows), ctx)
            return
            main = wallets.main_wallet(uid)
            rows = []
            if main and main.get("chain") == chain_name:
                rows.append([B("main wallet", callback_data=f"sw:dest:{chain_name}:main")])
            rows += [[B("paste address", callback_data=f"sw:destpaste:{chain_name}")]]
            await _edit_or_send(None, chat_id, q.message.message_id, "pick destination", M(rows), ctx)
        elif p[1] == "src":
            state = ctx.user_data.get("sweepall", {})
            if state.get("chain") != p[2]: return
            state["src"] = "all" if p[3] == "all" else p[3].lower()
            state["step"] = "destination"
            ctx.user_data["sweepall"] = state
            if state.get("asset") == "nft" and state.get("nft_ca"):
                holders = [(w["label"], w["address"]) for w in wallets.list_wallets(uid)
                           if (state["src"] == "all" or w["address"].lower() == state["src"])]
                rpc = RPC(CFG.chains[p[2]])
                r2, n2 = await asyncio.to_thread(fleet.sweep_plan, rpc, state["nft_ca"], holders)
                main = wallets.main_wallet(uid)
                if main and main.get("chain") == p[2] and not any(a.lower() == main["address"].lower() for _, a in holders):
                    r3, n3 = await asyncio.to_thread(fleet.sweep_plan, rpc, state["nft_ca"], [(main["label"], main["address"])])
                tok = _pending_token()
                PENDING[tok] = {"kind": "sweep", "owner": uid, "rows": [(l, a, i, state["nft_ca"]) for l, a, i in r2],
                                "contract": state["nft_ca"], "contracts": [state["nft_ca"]], "main": main, "chain": p[2]}
                text = "\n".join(panels.sweep_lines(main["label"], main["address"], r2, n2, state["nft_ca"]))
                await _edit_or_send(None, chat_id, q.message.message_id, text, M([[B("confirm sweep", callback_data=f"sc:{tok}"), B("cancel", callback_data=f"px:{tok}")]]), ctx)
                return
            main = wallets.main_wallet(uid)
            rows = []
            if main and main.get("chain") == p[2]:
                rows.append([B("main wallet", callback_data=f"sw:dest:{p[2]}:main")])
            rows += [[B("paste address", callback_data=f"sw:destpaste:{p[2]}")]]
            await _edit_or_send(None, chat_id, q.message.message_id, "pick destination", M(rows), ctx)
        elif p[1] == "paste":
            state = ctx.user_data.get("sweepall", {})
            if state.get("asset") == "nft":
                ctx.user_data["await_walletops"] = ("sweep_nft_ca", p[2])
            else:
                ctx.user_data["await_walletops"] = ("sweep_asset", p[2])
            await ctx.bot.send_message(chat_id, f"send token CA or mint for {p[2]}.")
        elif p[1] == "destpaste":
            ctx.user_data["await_walletops"] = ("sweep_destination", p[2])
            await ctx.bot.send_message(chat_id, "send destination address.")
        elif p[1] == "dest":
            state = ctx.user_data.get("sweepall", {})
            if state.get("chain") != p[2]: return
            main = wallets.main_wallet_for(uid, p[2])
            if p[3] == "main" and not main: return await ctx.bot.send_message(chat_id, "no main wallet on that chain.")
            dest = main["address"] if p[3] == "main" else p[3]
            if p[2] == "solana":
                if not (32 <= len(dest) <= 44 and all(c.isalnum() for c in dest)):
                    return await ctx.bot.send_message(chat_id, "invalid Solana destination.")
            elif not valid_address(dest):
                return await ctx.bot.send_message(chat_id, "invalid EVM destination.")
            await _make_sweep_preview(chat_id, ctx, uid, p[2], state["asset"], dest)
        elif p[1] == "review":
            pending = PENDING.get(int(p[2]))
            if not pending or pending.get("owner") != uid or pending.get("kind") not in ("asset_sweep", "sol_sweep"): return
            await q.edit_message_text("<b>final confirm</b>\nthis broadcasts from each eligible wallet. gas is paid by each sender. confirm only if destination and total are correct.", reply_markup=M([[B("confirm sweep", callback_data=f"swa:{p[2]}"), B("cancel", callback_data=f"px:{p[2]}")]]))
    elif data.startswith("swa:"):
        await run_pending(chat_id, ctx, uid, int(data[4:]), q.message.message_id)
    elif data.startswith("bal:"):
        if uid != ADMIN_ID:
            return
        want = data[4:]
        if want.startswith("chain:"):
            want = want[len("chain:"):]
        if want == "chains":
            kb = M([[B(chain_label(c), callback_data=f"bv:pick:{c}")]
                    for c in (*enabled_chains(), "solana") if c == "solana" or c in CFG.chains])
            await _edit_or_send(None, chat_id, q.message.message_id, "<b>balances</b>\npick chain", kb, ctx)
        elif want == "all":
            return
        else:
            await show_balance(chat_id, ctx, uid, want, q.message.message_id)
    elif data.startswith("sc:"):
        try: tok = int(data[3:])
        except ValueError: return
        p = PENDING.get(tok)
        if p is None or p.get("owner") != uid:
            await _edit_panel(q, "that confirmation expired or isnt yours. nothing was sent.")
            return
        if p.get("expires") and time.time() > p["expires"]:
            PENDING.pop(tok, None)
            await _edit_panel(q, "that confirmation expired. nothing was sent. run the command again.")
            return
        PENDING.pop(tok, None)
        await run_pending(chat_id, ctx, uid, tok, q.message.message_id)
    elif data.startswith("px:"):
        try: PENDING.pop(int(data[3:]), None)
        except ValueError: pass
        await _edit_panel(q, "cancelled.")
    elif data.startswith("drefresh:"):
        if uid != ADMIN_ID:
            return
        did = int(data[9:])
        d = DRAFTS.get(did)
        if d is None:
            await ctx.bot.send_message(chat_id, "that draft is gone.")
            return
        live, err = await analyze_contract(d["contract"], d.get("chain", CHAIN_NAME))
        if err:
            await ctx.bot.send_message(chat_id, "live chain refresh failed, kept the previous snapshot: " + err)
            return
        apply_live_draft_snapshot(d, live)
        d["live_values_only"] = True
        d["onchain_read_ts"] = live.get("onchain_read_ts")
        d["floor_live"] = None
        d["floor_read_ts"] = None
        try:
            from acobot import opensea
            slug = d.get("slug") or opensea.cached_slug(d.get("chain", CHAIN_NAME), d["contract"])
            if not slug:
                slug = await asyncio.to_thread(opensea._slug_for, d.get("chain", CHAIN_NAME), d["contract"])
            floor = await asyncio.to_thread(opensea.floor_stats, slug, 3.0) if slug else None
            if floor is not None:
                d["floor_live"] = floor
                d["floor_read_ts"] = int(time.time())
        except Exception:
            log.info("live floor read unavailable")
        if live.get("safety"):
            d["safety"] = live["safety"]
        await refresh_fees(d)
        save_drafts()
        await _edit_panel(q, draft_text(d), reply_markup=draft_kb(did, d))
    elif data.startswith("dc:"):
        if uid != ADMIN_ID:
            return
        did = int(data[3:])
        d = DRAFTS.get(did)
        if d is None:
            await ctx.bot.send_message(chat_id, "that draft is gone.")
            return
        names = enabled_chains()
        cur = d.get("chain", CHAIN_NAME)
        nxt = names[(names.index(cur) + 1) % len(names)] if cur in names and len(names) > 1 else (names[0] if names else cur)
        new, err = await analyze_contract(d["contract"], nxt)
        if err:
            await ctx.bot.send_message(chat_id, f"switching to {nxt} failed: {err} staying on {cur}.")
            return
        for k in ("price_wei", "start_ts", "per_wallet", "speed", "gas_limit", "mint_qty", "fire_mode"):
            if d.get(k) is not None:
                new[k] = d[k]
        if d.get("mint_sig_manual") and d.get("mint_sig"):
            new["mint_sig"] = d["mint_sig"]
            new["mint_sig_manual"] = True
        new["chain"] = nxt
        await refresh_fees(new)
        DRAFTS[did] = new
        save_drafts()
        await _edit_panel(q, draft_text(new), reply_markup=draft_kb(did, new))
    elif data.startswith("dn:"):
        if uid != ADMIN_ID:
            return
        d = DRAFTS.get(int(data[3:]))
        if d and mint_router.is_gated(draft_phase_view(d)):
            status = phase_authorization_status(d)
            await ctx.bot.send_message(chat_id, esc(status["detail"]))
            return
        missing = [n for n, ok in (("price", d and d.get("price_wei") is not None), ("start time", d and d.get("start_ts")), ("mint call", d and d.get("mint_sig"))) if not ok]
        await ctx.bot.send_message(chat_id, "still missing: " + (", ".join(missing) or "nothing") + ". fill those in with the buttons first.")
    elif data.startswith("dx:"):
        if uid != ADMIN_ID:
            return
        DRAFTS.pop(int(data[3:]), None)
        save_drafts()
        await _edit_panel(q, "discarded.")
    elif data == "w:list":
        await show_wallets(chat_id, ctx, uid, q.message.message_id)
    elif data == "wgen" or data.startswith("wgen:"):
        if q.message.chat.type != ChatType.PRIVATE:
            return
        if len(wallets.list_wallets(uid)) >= wallets.MAX_WALLETS:
            await ctx.bot.send_message(chat_id, f"you have {wallets.MAX_WALLETS} wallets (the cap). remove one first.")
            return
        chain_pick = data.split(":", 1)[1] if ":" in data else "ethereum"
        if chain_pick != "evm_all" and chain_pick not in wallets.CHAINS:
            return
        label_txt = "EVM (one key, all chains)" if chain_pick == "evm_all" else chain_pick
        ctx.user_data["await"] = ("wgen_qty", chain_pick)
        await ctx.bot.send_message(chat_id, f"how many {label_txt} wallets to generate? send a number (1-10). /cancel to abort.")
    elif data == "w:add":
        if q.message.chat.type != ChatType.PRIVATE:
            await ctx.bot.send_message(chat_id, "dm me to add a wallet.")
            return
        if len(wallets.list_wallets(uid)) >= wallets.MAX_WALLETS:
            await ctx.bot.send_message(chat_id, f"you have {wallets.MAX_WALLETS} wallets (the cap). remove one first.")
            return
        rows = [[B("EVM (one key, all EVM chains)", callback_data="wch:evm")]]
        rows.extend([[B(chain_label(c), callback_data=f"wch:{c}")] for c in enabled_chains()])
        rows.append([B("SOL", callback_data="wch:solana"), B("cancel", callback_data="w:list")])
        await ctx.bot.send_message(chat_id, "which chain is this wallet for?",
                                   reply_markup=M(rows))
    elif data.startswith("wch:"):
        chain_pick = data[4:]
        if chain_pick == "evm":
            await ctx.bot.send_message(chat_id, "generate a new wallet or import an existing one?",
                reply_markup=M([[B("generate", callback_data="wgen:evm_all"), B("import key", callback_data="wimp:evm_all")],
                                [B("back", callback_data="w:add")]]))
            return
        if chain_pick not in wallets.CHAINS:
            return
        await ctx.bot.send_message(chat_id, "generate a new wallet or import an existing one?",
            reply_markup=M([[B("generate", callback_data=f"wgen:{chain_pick}"), B("import key", callback_data=f"wimp:{chain_pick}")],
                            [B("back", callback_data="w:add")]]))
    elif data.startswith("wimp:"):
        chain_pick = data[5:]
        if q.message.chat.type != ChatType.PRIVATE or (chain_pick != "evm_all" and chain_pick not in wallets.CHAINS):
            return
        kb = M([[B("single wallet", callback_data=f"wimp1:{chain_pick}"),
                 B("multiple wallets", callback_data=f"wimpN:{chain_pick}")],
                [B("back", callback_data="w:add")]])
        await ctx.bot.send_message(chat_id, "add one wallet or many at once?", reply_markup=kb)
    elif data.startswith("wimp1:"):
        chain_pick = data[6:]
        if q.message.chat.type != ChatType.PRIVATE or (chain_pick != "evm_all" and chain_pick not in wallets.CHAINS):
            return
        ctx.user_data["await"] = ("wallet_key", chain_pick)
        await ctx.bot.send_message(chat_id, f"send the private key for a {chain_pick} wallet in this dm. it is stored encrypted and never shown back. /cancel to abort.")
    elif data.startswith("wimpN:"):
        chain_pick = data[6:]
        if q.message.chat.type != ChatType.PRIVATE or (chain_pick != "evm_all" and chain_pick not in wallets.CHAINS):
            return
        ctx.user_data["await"] = ("wallet_multi_count", chain_pick)
        await ctx.bot.send_message(chat_id, "how many wallets are u adding? send the number.")

    elif data.startswith("wq:"):
        addr = data[3:]
        ctx.user_data["await"] = ("wallet_qty", addr)
        await ctx.bot.send_message(chat_id, "send the mint amount for this wallet (whole number, 1 or more)")
    elif data == "wrall":
        n = len(wallets.list_wallets(uid))
        wallets.remove_all(uid)
        wallets.clear_main(uid)
        await ctx.bot.send_message(chat_id, f"removed all {n} wallets.")
        await show_wallets(chat_id, ctx, uid, q.message.message_id)
    elif data.startswith("wr:"):
        addr = data[3:]
        ok = wallets.remove_wallet(uid, addr)
        await show_wallets(chat_id, ctx, uid, q.message.message_id)
    elif data.startswith("wmain:"):
        addr = data[6:]
        wallets.set_main(uid, addr)
        await show_wallets(chat_id, ctx, uid, q.message.message_id)
    elif data == "wnop":
        return
    elif data.startswith("wcy:"):
        addr = data[4:]
        cur = next((w["chain"] for w in wallets.list_wallets(uid) if w["address"].lower() == addr.lower()), None)
        if cur:
            wallets.set_chain(uid, addr, "robinhood" if cur == "ethereum" else "ethereum")
        await show_wallets(chat_id, ctx, uid, q.message.message_id)
    elif data.startswith("vr:"):
        if uid != ADMIN_ID:
            return
        mid = int(data[3:])
        rec = resmod.load_result(DATA, mid)
        if rec is None:
            await ctx.bot.send_message(chat_id, f"no saved results for #{mid}.")
            return
        kb = M([[B("back to my mints", callback_data="m:list")], [B("home", callback_data="home")]])
        await _edit_or_send(update, chat_id, q.message.message_id, "\n".join(panels.result_lines(mid, rec)), kb, ctx)
    elif data == "home":
        t, kb = main_panel()
        await _edit_or_send(update, chat_id, q.message.message_id, t, kb, ctx)
    elif data.startswith("help:"):
        sec = data.split(":", 1)[1]
        kb = M([[B("⬅ help", callback_data="help")], [B("🏠 main", callback_data="home")]])
        await _edit_or_send(update, chat_id, q.message.message_id, help_text(sec), kb, ctx)
    elif data.startswith("cw:"):
        if uid != ADMIN_ID:
            return
        _, mflag, kind, name, chain = data.split(":", 4)
        from acobot import cawatch as cwmod
        if mflag == "a":
            # ask for the fund amount next
            ctx.user_data["await"] = ("cawatch_fund", (kind, name, chain))
            await _edit_or_send(None, chat_id, q.message.message_id,
                                "auto mode: how much eth per buy? max 0.05. send a number.", None, ctx)
            return
        try:
            s = cwmod.add_source(kind, name, "manual", chain=chain)
        except ValueError as e:
            await _edit_or_send(None, chat_id, q.message.message_id, str(e), None, ctx)
            return
        await _edit_or_send(None, chat_id, q.message.message_id,
                            f"armed: {kind} {name} = manual. CA drops -> u get pinged, u confirm every buy.", None, ctx)
    elif data.startswith("snd:src:"):
        pick = data[8:]
        ctx.user_data["send_wiz"] = {"step": "chain", "src": pick}
        chains = list(enabled_chains())
        if "solana" in wallets.CHAINS:
            chains.append("solana")
        kb = M([[B(chain_label(c), callback_data=f"snd:chn:{c}")] for c in chains])
        await _edit_or_send(None, chat_id, q.message.message_id, "which chain?", kb, ctx)
    elif data.startswith("snd:chn:"):
        chn = data[8:]
        if chn not in (*enabled_chains(), "solana"):
            await _edit_or_send(None, chat_id, q.message.message_id, "that send chain is not enabled.", None, ctx)
            return
        w = ctx.user_data.get("send_wiz") or {}
        w["chain"] = chn
        w["step"] = "token"
        ctx.user_data["send_wiz"] = w
        if chn == "solana":
            kb = M([[B("SOL (native)", callback_data="snd:tok:native")],
                    [B("paste SPL mint", callback_data="snd:tok:CA")]])
        else:
            kb = M([[B("ETH (native)", callback_data="snd:tok:native"), B("USDC", callback_data="snd:tok:usdc")],
                    [B("USDT", callback_data="snd:tok:usdt"), B("USDG", callback_data="snd:tok:usdg")],
                    [B("paste token address", callback_data="snd:tok:CA")]])
        await _edit_or_send(None, chat_id, q.message.message_id, "which token?", kb, ctx)
    elif data.startswith("snd:tok:"):
        tok = data[8:]
        w = ctx.user_data.get("send_wiz") or {}
        if tok == "CA":
            ctx.user_data["await"] = ("send_wiz_token_ca", None)
            prompt = ("paste the SPL mint address" if w.get("chain") == "solana"
                      else "paste the EVM token contract address")
            await _edit_or_send(None, chat_id, q.message.message_id, prompt, None, ctx)
            return
        w["step"] = "amount"
        w["token"] = tok
        ctx.user_data["send_wiz"] = w
        ctx.user_data["await"] = ("send_wiz_amount", None)
        await _edit_or_send(None, chat_id, q.message.message_id, "how much? send a number or 'all'", None, ctx)
    elif data.startswith("snd:dst:"):
        pick = data[8:]
        w = ctx.user_data.get("send_wiz") or {}
        if pick == "CA":
            ctx.user_data["await"] = ("send_wiz_ca", None)
            prompt = ("paste the destination Solana wallet address" if w.get("chain") == "solana"
                      else "paste the destination 0x address")
            await _edit_or_send(None, chat_id, q.message.message_id, prompt, None, ctx)
            return
        if pick == "PICK":
            w["multi"] = []
            source_addresses = {a.lower() for _, a, _ in _send_wizard_sources(
                uid, w.get("src"), w.get("chain"))}
            seen, rows = set(), []
            for w2 in wallets.list_wallets(uid):
                a = (w2["address"] or "").lower()
                chain_name = w.get("chain")
                compatible = (w2["chain"] == "solana" if chain_name == "solana"
                              else w2["chain"] in wallets.EVM_CHAINS)
                if not compatible or a in seen or a in source_addresses:
                    continue
                seen.add(a)
                rows.append([B(f"{w2['label']} ({wallets.mask(w2['address'])})", callback_data=f"snd:tgl:{w2['address']}")])
            rows.append([B("done", callback_data="snd:tgl:done"), B("cancel", callback_data="snd:tgl:cancel")])
            await _edit_or_send(None, chat_id, q.message.message_id, "tap every wallet u want to send TO, then done:", M(rows), ctx)
            return
        if pick == "all":
            w["dst"] = "all"
            w["dsts"] = ["all"]
        elif pick == "main":
            main0 = wallets.main_wallet_for(uid, w.get("chain"))
            if not main0:
                await _edit_or_send(None, chat_id, q.message.message_id, "no compatible wallet for this chain. choose a wallet or paste an address.", None, ctx)
                return
            w["dst"] = "main"
            w["dsts"] = [main0["address"]]
        else:
            w["dst"] = pick
            w["dsts"] = [pick]
        w["step"] = "gas"
        ctx.user_data["send_wiz"] = w
        if w.get("chain") == "solana":
            kb = M([[B("review SOL transfer", callback_data="snd:gas:fast")]])
            text = "SOL fees are estimated during preview; SPL sends may also need recipient token-account rent."
        else:
            kb = M([[B("slow", callback_data="snd:gas:slow"), B("medium", callback_data="snd:gas:med")],
                    [B("fast", callback_data="snd:gas:fast")]])
            text = "gas mode?"
        await _edit_or_send(None, chat_id, q.message.message_id, text, kb, ctx)
    elif data.startswith("snd:tgl:"):
        pick = data[8:]
        w = ctx.user_data.get("send_wiz") or {}
        if pick == "cancel":
            ctx.user_data["send_wiz"] = {}
            await _edit_or_send(None, chat_id, q.message.message_id, "cancelled. /send to start over.", None, ctx)
            return
        if pick == "done":
            sel = w.get("multi") or []
            if not sel:
                await _edit_or_send(None, chat_id, q.message.message_id, "no wallets selected. tap some first.", None, ctx)
                return
            w["dst"] = "multi:" + ",".join(sel)
            ctx.user_data["send_wiz"] = w
            if w.get("chain") == "solana":
                kb = M([[B("review SOL transfer", callback_data="snd:gas:fast")]])
                text = f"{len(sel)} wallets selected. review the SOL transfer."
            else:
                kb = M([[B("slow", callback_data="snd:gas:slow"), B("medium", callback_data="snd:gas:med")],
                        [B("fast", callback_data="snd:gas:fast")]])
                text = f"{len(sel)} wallets selected. gas mode?"
            await _edit_or_send(None, chat_id, q.message.message_id, text, kb, ctx)
            return
        multi = w.setdefault("multi", [])
        if pick in multi:
            multi.remove(pick)
        else:
            multi.append(pick)
        w["send_wiz"] = w
        source_addresses = {a.lower() for _, a, _ in _send_wizard_sources(
            uid, w.get("src"), w.get("chain"))}
        seen, rows = set(), []
        for w2 in wallets.list_wallets(uid):
            a = (w2["address"] or "").lower()
            chain_name = w.get("chain")
            compatible = (w2["chain"] == "solana" if chain_name == "solana"
                          else w2["chain"] in wallets.EVM_CHAINS)
            if not compatible or a in seen or a in source_addresses:
                continue
            seen.add(a)
            mark = "✅ " if a in [m.lower() for m in multi] else ""
            rows.append([B(f"{mark}{w2['label']} ({wallets.mask(w2['address'])})", callback_data=f"snd:tgl:{w2['address']}")])
        rows.append([B("done", callback_data="snd:tgl:done"), B("cancel", callback_data="snd:tgl:cancel")])
        await _edit_or_send(None, chat_id, q.message.message_id, f"{len(multi)} selected. tap to toggle, then done:", M(rows), ctx)
    elif data.startswith("snd:gas:"):
        gas = data[8:]
        w = ctx.user_data.get("send_wiz") or {}
        w["gas"] = gas
        ctx.user_data["send_wiz"] = w
        try:
            uid_ = uid
            src_pick, token_pick, amt, dst_pick = w.get("src"), w.get("token"), w.get("amount"), w.get("dst")
            if not src_pick or not token_pick or amt is None or not dst_pick:
                raise ValueError("flow got out of step. start over with /send")
            wiz_chain = w.get("chain") or "robinhood"
            senders = _send_wizard_sources(uid_, src_pick, wiz_chain)
            if not senders:
                raise ValueError(f"no compatible source wallets on {wiz_chain}.")
            source_addresses = {address.lower() for _, address, _ in senders}
            destinations = _send_wizard_destinations(uid_, dst_pick, wiz_chain, source_addresses)
            if src_pick == "all" and len(destinations) > 1:
                raise ValueError("sending from all fleet wallets to multiple destinations is ambiguous. choose one destination.")
            decimals = 9 if wiz_chain == "solana" and token_pick.lower() in ("native", "sol") else None
            if wiz_chain != "solana":
                token_text = {"native": "native", "usdc": "usdc", "usdt": "usdt",
                              "usdg": "usdg"}.get(token_pick, token_pick)
                chain = CFG.chains[wiz_chain]
                resolved_token = await asyncio.to_thread(
                    tkn.resolve, RPC(chain), wiz_chain, token_text)
                decimals = int(resolved_token["decimals"])
            elif decimals is None:
                from acobot import solana
                mint_supply = await asyncio.to_thread(
                    solana.rpc_call, "getTokenSupply",
                    [token_pick, {"commitment": "confirmed"}])
                decimals = int(mint_supply["value"]["decimals"])
            recipient_amounts = _send_amounts_per_recipient(
                amt, len(destinations), decimals)
            to_addr = destinations[0][0]
            to_label = destinations[0][1] if len(destinations) == 1 else f"{len(destinations)} wallets"
            if wiz_chain == "solana":
                from acobot import solana
                plans, skipped = [], []
                preview = ["<b>SOL send preview</b>", ""]
                for destination_index, (destination, destination_label) in enumerate(destinations):
                    for sender_label, sender_address, _ in senders:
                        try:
                            plan = await asyncio.to_thread(
                                solana.plan_transfer, sender_address, destination,
                                token_pick, recipient_amounts[destination_index])
                            plan["sender_label"] = sender_label
                            plan["destination_label"] = destination_label
                            plans.append(plan)
                            amount_display = plan["amount"] / (10 ** plan["decimals"])
                            fee_display = plan["fee_lamports"] / 1e9
                            preview.append(
                                f"✅ {esc(sender_label)} → {esc(destination_label)}: "
                                f"{amount_display:,.{min(plan['decimals'], 9)}f} "
                                f"{esc(plan['symbol'])} · fee ~{fee_display:.6f} SOL"
                                + (f" · ATA rent {plan['rent_lamports'] / 1e9:.6f} SOL"
                                   if plan["rent_lamports"] else ""))
                        except (ValueError, RuntimeError) as ex:
                            skipped.append(f"{sender_label} → {destination_label}: {ex}")
                if skipped:
                    preview.extend(["", "<b>not prepared:</b>"] + [esc(s) for s in skipped])
                if not plans:
                    await _edit_or_send(None, chat_id, q.message.message_id,
                                        "\n".join(preview) + "\n\nnothing to send.", None, ctx)
                    return
                tok = _pending_token()
                PENDING[tok] = {"kind": "sol_tx", "owner": uid_, "plans": plans,
                                "expires": time.time() + 300}
                kb = M([[B("confirm SOL send", callback_data=f"sc:{tok}"),
                         B("cancel", callback_data=f"px:{tok}")]])
                await _edit_or_send(None, chat_id, q.message.message_id,
                                    "\n".join(preview) + "\n\nConfirm only after checking every wallet, amount, fee, and recipient.",
                                    kb, ctx)
                return

            by_chain = {}
            for sw in senders:
                lbl, addr, chn = (sw["label"], sw["address"], sw["chain"]) if isinstance(sw, dict) else sw
                by_chain.setdefault(wiz_chain, []).append((lbl, addr))
            token_text = {"native": "native", "usdc": "usdc", "usdt": "usdt", "usdg": "usdg"}.get(token_pick, token_pick)
            gas_speed = {"med": "medium"}.get(gas, gas)
            groups, notes = [], []
            for destination_index, (destination, destination_label) in enumerate(destinations):
                gs, ns = await asyncio.to_thread(
                    _plan_groups_sync, by_chain, token_text,
                    recipient_amounts[destination_index], destination, gas_speed)
                for group in gs:
                    for row in group["rows"]:
                        row["to_label"] = destination_label
                groups += gs
                notes += ns
            ok_rows = [r for g in groups for r in g["rows"] if r.get("ok")]
            if not groups or not ok_rows:
                await _edit_or_send(None, chat_id, q.message.message_id,
                                    "\n".join(_tx_text("send", to_label, groups, notes)) + "\n\n❌ insufficient funds, nothing to send.", None, ctx)
                return
            tok = _pending_token()
            PENDING[tok] = {"kind": "tx", "owner": uid_, "groups": groups, "to": to_addr,
                            "expires": time.time() + 300}
            kb = M([[B(f"confirm send ({gas_speed})", callback_data=f"sc:{tok}"), B("cancel", callback_data=f"px:{tok}")]])
            await _edit_or_send(None, chat_id, q.message.message_id,
                                "\n".join(_tx_text("send preview", to_label, groups, notes)) + f"\ngas: {gas_speed}", kb, ctx)
        except (ValueError, RuntimeError) as ex:
            await _edit_or_send(None, chat_id, q.message.message_id, f"❌ {ex}", None, ctx)
        except Exception as ex:
            log.exception("send preview failed")
            await _edit_or_send(
                None, chat_id, q.message.message_id,
                f"❌ could not prepare send preview ({type(ex).__name__}): {str(ex)[:240]}",
                None, ctx)
    elif data == "rst":
        if uid != ADMIN_ID:
            return
        await ctx.bot.send_message(chat_id, "restarting bot. back in a few seconds.")
        os.execv(sys.executable, [sys.executable, *sys.argv])
    elif data == "help":
        await _edit_or_send(update, chat_id, q.message.message_id, help_text(), help_kb(), ctx)
    elif data == "pf:open":
        await cmd_portfolio(update, ctx)


class _CmdShim:
    """Lets an AI-parsed action re-enter an existing slash-command handler unchanged,
    so the same preview/confirm panel runs. The command text is built only from validated args."""
    def __init__(self, update, text, args=None):
        self.effective_user = update.effective_user
        self.effective_chat = update.effective_chat
        self.effective_message = update.message
        self.message = _ShimMsg(update.message, text)
        self.args = args or []


class _ShimMsg:
    def __init__(self, msg, text):
        self._msg = msg
        self.text = text

    async def reply_text(self, *a, **k):
        return await self._msg.reply_text(*a, **k)


async def _ai_setup_key(update, ctx):
    if not _admin_dm(update):
        return
    ctx.user_data["await"] = ("ai_key", None)
    await update.message.reply_text("paste the api key in your next message. i'll delete it and store it encrypted. /cancel to stop.")


async def cmd_ai(update: Update, ctx):
    import acobot.ai as aimod
    uid = update.effective_user.id
    if uid != ADMIN_ID:
        return
    sub = (ctx.args[0].lower() if ctx.args else "status")
    if sub == "setup":
        await _ai_setup_key(update, ctx)
    elif sub == "clear":
        aimod.clear_key()
        await update.message.reply_text("ai key removed.")
    else:
        await update.message.reply_text(aimod.status_text())


async def _ai_understood(update, ctx, action, args, text):
    lbl = action + (" " + " ".join(f"{k}={v}" for k, v in args.items()) if args else "")
    await update.message.reply_text(f"🤖 understood: {lbl}")


def _brain_providers(uid):
    """Read-only data for the conversational brain. Never includes keys or raw key material."""
    def _mints():
        out = []
        for m in MINTS.values():
            out.append({"id": m.get("id"), "contract": m.get("contract"), "chain": m.get("chain"),
                        "status": m.get("status"), "opens": fmt_ts(m.get("start_ts")),
                        "price": fmt_eth(m.get("price_wei"))})
        return out or "no mints saved"

    def _wallets():
        return [{"label": w["label"], "masked": w["masked"], "chain": w["chain"], "main": w["main"],
                 "qty": w["qty"]} for w in wallets.list_wallets(uid)] or "no wallets saved"

    def _balances(chain=None):
        return {"text": re.sub(r"<[^>]+>", "", balance_panel_text(uid, chain or "chains"))}

    def _list_results():
        ids = resmod.list_result_ids(DATA)[-10:]
        out = []
        for mid in ids:
            rec = resmod.load_result(DATA, mid) or {}
            out.append({"id": mid, "summary": str(rec.get("summary") or rec.get("status") or "")[:200]})
        return out or "no results saved"

    def _speed():
        st = intel.speed_stats(DATA)
        return {"text": intel.format_speed(st), "detail": st}

    def _public():
        import urllib.request
        def fetch(u):
            req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/124 Safari/537.36"})
            with urllib.request.urlopen(req, timeout=12) as r:
                return r.read().decode("utf-8", "replace")
        res = intel.public_mints(fetch)
        return {"text": intel.format_mints(res), "mints": res["mints"], "errors": res["errors"]}

    def _get_result(mid):
        rec = resmod.load_result(DATA, mid)
        if not rec:
            return {"error": f"no result #{mid}"}
        keep = {k: rec.get(k) for k in ("id", "contract", "chain", "status", "summary", "wallets", "error_detail")
                if rec.get(k) is not None}
        return keep

    def _eligibility(contract=None, chain=None):
        from acobot import eligible as elig
        if not contract:
            return {"error": "need a contract address"}
        ch = chain or CHAIN_NAME
        owners = [ADMIN_ID] + list(CFG.whitelist_ids)
        wl = [{"label": lbl, "address": a} for o, lbl, a, _k, _q in wallets.signing_entries(owners)]
        if not wl:
            return {"error": "no wallets saved"}
        out = []
        for w in wl:
            r = elig.probe_sync(contract, ch, w["address"], {})
            out.append({"label": w["label"], "eligible": r["eligible"], "source": r["source"]})
        return {"results": out, "note": "unknown means no endpoint answered, not a no"}

    def _dig(name):
        from acobot import dig as digmod
        return digmod.dig(name, enabled_chains(), mintread.detect, lambda cn: RPC(CFG.chains[cn]))

    def _analyze_mint(contract=None, chain=None):
        from acobot import brain
        if not valid_address(contract or ""):
            return {"error": "contract must be a valid 0x address"}
        cname = chain or CHAIN_NAME
        if cname not in CFG.chains:
            return {"error": f"chain '{cname}' is not enabled"}
        d, error = analyze_contract_sync(contract, cname)
        if error:
            return {"error": error}
        return brain.mint_phase_report(d, cname, mintapi.supports_phase(contract))

    def _chain_arg(chain):
        return chain if chain in CFG.chains else CHAIN_NAME

    def _gas_now(chain=None):
        cn = _chain_arg(chain)
        try:
            r = gasguard.check(RPC(CFG.chains[cn]), cn)
            return {"chain": cn, "result": r}
        except Exception as e:
            return {"error": f"gas read failed: {type(e).__name__}"}

    def _floor(contract=None, chain=None):
        if not contract:
            return {"error": "need a contract address"}
        cn = _chain_arg(chain)
        if cn != "ethereum":
            return {"error": "no floor source for this chain (reservoir covers ethereum only)"}
        from acobot import portfolio as pf
        f = pf.fetch_floor(cn, contract)
        return {"floor": f} if f is not None else {"error": "no floor found (not listed or source down)"}

    def _scan(contract=None, chain=None):
        if not contract:
            return {"error": "need a contract address"}
        cn = _chain_arg(chain)
        try:
            return {"scan": rugcheck.scan(RPC(CFG.chains[cn]), contract, {})}
        except Exception as e:
            return {"error": f"scan failed: {type(e).__name__}"}

    def _eth_price(chain=None):
        cn = _chain_arg(chain)
        try:
            return {"chain": cn, "usd": prices.native_usd(cn)}
        except Exception:
            return {"error": "price unavailable"}

    def _health():
        out = {"chains": {}}
        for cn, f in _FEEDS.items():
            out["chains"][cn] = {"ws_up": f.available(), "last_error": f.last_error,
                                 "block_time_s": round(f.block_time_s, 2) if f.block_time_s else None}
        out["uptime_s"] = int(time.time() - BOT_STARTED)
        return out

    def _token_lookup(symbol=None, chain=None):
        cn = _chain_arg(chain)
        if not symbol:
            return {"error": "need a token symbol"}
        import urllib.request as _u, urllib.parse as _up
        q = f"{symbol} {cn}"
        req = _u.Request("https://api.dexscreener.com/latest/dex/search?q=" + _up.quote(q),
                         headers={"User-Agent": "Mozilla/5.0"})
        d = json.loads(_u.urlopen(req, timeout=15).read())
        best = None
        for p in (d.get("pairs") or []):
            if p.get("chainId") != cn:
                continue
            bt = p.get("baseToken") or {}
            if (bt.get("symbol") or "").upper() != symbol.upper():
                continue
            liq = (p.get("liquidity") or {}).get("usd") or 0
            if best is None or liq > best[1]:
                best = (bt, liq, p)
        if not best or best[1] < 10000:
            return {"error": f"could not verify {symbol} on {cn} (no liquid pair). do not guess an address."}
        bt, liq, p = best
        return {"symbol": bt.get("symbol"), "address": bt.get("address"), "chain": cn,
                "price_usd": p.get("priceUsd"), "liquidity_usd": round(liq), "dex": p.get("dexId")}

    return {"token_lookup": _token_lookup, "list_mints": _mints, "list_wallets": _wallets, "balances": _balances,
            "list_results": _list_results, "get_result": _get_result, "dig_project": _dig,
            "analyze_mint": _analyze_mint, "speed_stats": _speed, "public_mints": _public, "check_eligibility": _eligibility,
            "gas_now": _gas_now, "floor_price": _floor, "contract_scan": _scan,
            "eth_price": _eth_price, "bot_health": _health}


def _brain_turn(uid, text, is_admin, lc):
    """Blocking LLM turn (runs in a thread). Returns {"action","args"}."""
    from acobot import brain, ai as aimod
    snap = brain.snapshot_text(
        [{"id": m.get("id"), "contract": m.get("contract"), "chain": m.get("chain"),
          "status": m.get("status"), "opens": fmt_ts(m.get("start_ts"))} for m in MINTS.values()],
        [{"label": w["label"], "masked": w["masked"], "chain": w["chain"], "main": w["main"]}
         for w in wallets.list_wallets(uid)],
        [{"id": mid, "summary": ""} for mid in resmod.list_result_ids(DATA)[-3:]],
        last_contract=lc)
    return brain.converse(uid, text, is_admin, _brain_providers(uid), snap,
                          brain.openai_complete, last_contract=lc)


DIG_PROPOSALS = {}


async def _typing_loop(ctx, chat_id):
    """Keep the 'typing...' chat action alive (it lasts ~5s per send) while the llm runs."""
    try:
        while True:
            await ctx.bot.send_chat_action(chat_id, "typing")
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass
    except Exception:
        pass


def _ai_error_card(what, fix):
    return "\n".join([
        "❌ <b>ai hiccup</b>",
        DIVIDER_LINE,
        f"{panels.esc(what)}.",
        f"👉 {panels.esc(fix)}",
    ])


async def ai_route(update: Update, ctx, text):
    """Plain DM text -> AI intent -> existing handler. Returns nothing; replies on failure."""
    import acobot.ai as aimod
    uid = update.effective_user.id
    is_admin = uid == ADMIN_ID
    if not (is_admin or allowed(uid) or username_allowed(update.effective_user.username)):
        return
    key, _ = aimod.stored_key()
    if not key:
        if is_admin:
            await update.message.reply_text("ai isn't set up yet. send /ai setup to add a key.")
        return
    lc = aimod.last_contract(uid)
    from acobot import brain
    # typing indicator while the llm thinks; one auto-retry before the friendly error line
    typing = asyncio.create_task(_typing_loop(ctx, update.effective_chat.id))
    res = None
    try:
        for attempt in (1, 2):
            try:
                res = await asyncio.to_thread(_brain_turn, uid, text, is_admin, lc)
                break
            except aimod.AIError as e:
                log.warning("ai turn failed (attempt %s): %s", attempt, e)
                if attempt == 2:
                    await update.message.reply_text(_ai_error_card("the model didn't answer", "try again in a minute, or use the slash commands (/ai shows status)."))
                    return
                await asyncio.sleep(1.5)
            except Exception:
                log.exception("ai turn crashed (attempt %s)", attempt)
                if attempt == 2:
                    await update.message.reply_text(_ai_error_card("that one tripped me up", "rephrase it, or use the slash commands."))
                    return
                await asyncio.sleep(1.5)
    finally:
        typing.cancel()
    aimod.remember(uid, "user", text)
    if res["action"] == "say":
        reply = res["args"]["reply"]
        prop = res["args"].get("proposal")
        if prop and is_admin and prop.get("contract") and prop.get("chain"):
            price = prop.get("price_wei")
            price_txt = "free" if price == 0 else (fmt_eth(price) if price is not None else "price unknown")
            opens = fmt_ts(prop.get("start_ts")) if prop.get("start_ts") else "start time not on chain"
            per = f"{prop['per_wallet']}/wallet" if prop.get("per_wallet") else "per-wallet not on chain"
            reply = (f"found {prop.get('name') or 'it'}: contract {prop['contract']} on {prop['chain']}, "
                     f"{price_txt}, opens {opens}, {per}. add to schedule?")
            pid = max(DIG_PROPOSALS.keys(), default=0) + 1
            DIG_PROPOSALS[pid] = {"contract": prop["contract"], "chain": prop["chain"]}
            kb = M([[B("add to schedule", callback_data=f"dga:{pid}"), B("not now", callback_data=f"dgn:{pid}")]])
            await update.message.reply_text(reply, reply_markup=kb)
        else:
            await update.message.reply_text(reply)
        aimod.remember(uid, "assistant", reply)
        return
    action, a = res["action"], res["args"]
    if action == "unknown":
        reply = a.get("reply") or aimod.HELP
        await update.message.reply_text(reply)
        aimod.remember(uid, "assistant", reply)
        return
    if action == "help":
        await update.message.reply_text(aimod.HELP)
        return
    if action == "addmint":
        await _ai_understood(update, ctx, action, {k: v for k, v in a.items() if k != "contract"} | {"contract": a["contract"]}, text)
        aimod.set_last_contract(uid, a["contract"])
        await start_draft(update.effective_chat.id, ctx, a["contract"], chain_name=a.get("chain") or CHAIN_NAME)
        return
    if action == "cancel_mint":
        await _ai_understood(update, ctx, action, a, text)
        await cancel_mint_id(update.effective_chat.id, ctx, a["id"], uid)
        return
    if action == "balance":
        await _ai_understood(update, ctx, action, a, text)
        if not wallets.list_wallets(uid):
            await update.message.reply_text("no wallets yet. add one in /wallets first.")
            return
        t = await asyncio.to_thread(balance_panel_text, uid, "chains")
        await update.message.reply_text(t, reply_markup=balance_kb())
        return
    if action == "wallets_list":
        await cmd_wallets(_CmdShim(update, "/wallets"), ctx)
        return
    if action in ("send", "fund", "sweep"):
        await _ai_understood(update, ctx, action, a, text)
        if action == "send":
            cmd = f"/send {a['who']} {a['amount']} {a['token']} {a['to']}"
            await cmd_send(_CmdShim(update, cmd), ctx)
        elif action == "fund":
            cmd = f"/fund {a['who']} {a['amount']}"
            await cmd_fund(_CmdShim(update, cmd, cmd.split()[1:]), ctx)
        else:
            cmd = f"/sweep {a.get('contract', '')}".strip()
            await cmd_sweep(_CmdShim(update, cmd, cmd.split()[1:]), ctx)
        return
    if action == "snipetoken":
        await _ai_understood(update, ctx, action, a, text)
        await _prepare_token_panel(update.effective_chat.id, ctx, a["contract"])
        return
    if action == "wallet_main":
        await _ai_understood(update, ctx, action, a, text)
        await update.message.reply_text("use /wallets to set the main wallet (tap set-main).")
        return
    if action == "wallet_remove":
        await _ai_understood(update, ctx, action, a, text)
        await update.message.reply_text("use /wallets to remove a wallet (tap it, then remove).")
        return
    if action == "allow":
        await _ai_understood(update, ctx, action, a, text)
        await cmd_allow(_CmdShim(update, f"/allow {a['user_id']}", [str(a["user_id"])]), ctx)
        return



async def cmd_cawatch(update: Update, ctx):
    """Track X accounts / groups for CAs. /cawatch add x jackbutcher auto 0.02 robinhood"""
    from acobot import cawatch
    if not _admin_only(update):
        return
    args = ctx.args or []
    if not args or args[0].lower() in ("list", ""):
        rows = cawatch.list_sources()
        if not rows:
            await update.message.reply_text(
                "no CA watchers yet.\n\n"
                "/cawatch add x <handle> manual|auto [fund eth] [chain]\n"
                "/cawatch add dc <invite or server id> manual|auto [fund eth] [chain]\n"
                "/cawatch add tg <group/channel id or @link> manual|auto [fund eth] [chain]\n"
                "auto = buys/mints instantly up to fund (max 0.05 eth). manual = u get pinged and confirm.\n"
                "/cawatch list | /cawatch remove <name>")
            return
        lines = [f"{s['kind']} {s['name']}: {s['mode']}" + (f" cap {s['cap_eth']} eth, chain {s['chain']}" if s['mode'] == 'auto' else "") for s in rows]
        await update.message.reply_text("\n".join(lines))
        return
    sub = args[0].lower()
    if sub == "remove" and len(args) > 1:
        ok = cawatch.remove_source(args[1])
        await update.message.reply_text("removed." if ok else "not found.")
        return
    if sub == "add" and len(args) >= 3:
        kind = args[1].lower()
        name = args[2]
        mode = args[3].lower() if len(args) > 3 else ""
        cap = args[4] if len(args) > 4 else "0"
        chain = args[5].lower() if len(args) > 5 else "robinhood"
        if mode not in ("manual", "auto"):
            # no mode given: show the picker buttons
            kb = M([[B("manual (confirm each)", callback_data=f"cw:m:{kind}:{name}:{chain}"),
                     B("auto (instant)", callback_data=f"cw:a:{kind}:{name}:{chain}")]])
            await update.message.reply_text(
                f"how should {kind} {name} act when a CA drops?", reply_markup=kb)
            return
        try:
            s = cawatch.add_source(kind, name, mode, cap_eth=float(cap), chain=chain)
        except ValueError as e:
            await update.message.reply_text(str(e))
            return
        if kind == "x":
            await update.message.reply_text(
                f"watching x {name}: {s['mode']}.\nX posts are scanned by folk's monitors; a CA there pings u here, then /trade or /outmint. "
                + ("auto fires up to " + str(s['cap_eth']) + " eth the moment a group CA lands, no confirm." if s['mode'] == 'auto' else "nothing buys without ur confirm."))
            return
        if kind in ("dc", "tg", "group"):
            if kind == "dc":
                await update.message.reply_text(
                    f"dc {name} saved as {s['mode']}. discord needs a bot invited to that server to read it live; "
                    "add @acobot to the server (read messages perm) and the CA alerts flow through here. "
                    + (f"auto capped at {s['cap_eth']} eth." if s['mode'] == 'auto' else "manual: u confirm each buy."))
                return
            if kind == "tg":
                await update.message.reply_text(
                    f"tg {name} saved as {s['mode']}. "
                    + ("for public channels/groups the bot must be a member: add @acobot there, it reads CAs and acts here. "
                       if s['mode'] else "add @acobot to that group/channel so it can read messages. ")
                    + (f"auto buys up to {s['cap_eth']} eth instantly, no confirm." if s['mode'] == 'auto' else "manual: u confirm each buy."))
                return
    await update.message.reply_text("usage: /cawatch add x <handle> manual|auto [cap eth] [chain] | /cawatch add group <name> ... | /cawatch list | /cawatch remove <name>")


async def cawatch_on_group_text(update: Update, ctx):
    """Fires for every text in groups the bot sits in. Extracts CAs, acts per source mode."""
    from acobot import cawatch
    try:
        sources = [s for s in cawatch.list_sources() if s["kind"] == "group"]
        if not sources:
            return
        msg = update.message or update.effective_message
        text = (msg.text or msg.caption or "") if msg else ""
        if not text:
            return
        chat = update.effective_chat
        title = chat.title or chat.username or str(chat.id)
        hits = [s for s in sources if s["name"].lower() in title.lower()]
        if not hits:
            return
        evm, sol = cawatch.extract_cas(text)
        for s in hits:
            for ca in evm:
                if not cawatch.is_new("group", s["name"], ca):
                    continue
                cawatch.mark_seen("group", s["name"], ca)
                if s["mode"] == "auto" and cawatch.cap_ok(s, s["cap_eth"]):
                    chain = CFG.chains.get(s.get("chain", "robinhood"))
                    wallet = wallets.main_wallet_for(ADMIN_ID, s.get("chain", "robinhood"))
                    if not chain or not wallet:
                        await ctx.bot.send_message(ADMIN_ID, f"CA {ca} from {s['name']}: auto-buy skipped, no {s.get('chain')} wallet. nothing was spent.")
                        continue
                    try:
                        from acobot import tokenbuy
                        prepared = await asyncio.to_thread(tokenbuy.prepare_buy, ca, str(s["cap_eth"]), wallet["address"], RPC(chain))
                        prepared["expires"] = time.time() + 120
                        ok, tx = await asyncio.to_thread(tokenbuy.execute_prepared, prepared,
                                                         wallets.signing_entries([ADMIN_ID], only={wallet["address"].lower()})[0][3],
                                                         RPC(chain, broadcast=True))
                        await ctx.bot.send_message(ADMIN_ID, f"AUTO-BUY from {s['name']}: {'submitted ' + str(tx)[:20] if ok else 'failed, nothing spent'} ({s['cap_eth']} eth, {ca})")
                    except Exception:
                        log.exception("cawatch auto-buy failed")
                        await ctx.bot.send_message(ADMIN_ID, f"CA {ca} from {s['name']}: auto-buy quote failed, nothing was spent.")
                else:
                    await ctx.bot.send_message(ADMIN_ID,
                        f"CA spotted in group {s['name']}: <code>{ca}</code>\nreact with /trade to buy manually.")
            for mint in sol:
                if cawatch.is_new("group", s["name"], mint):
                    cawatch.mark_seen("group", s["name"], mint)
                    await ctx.bot.send_message(ADMIN_ID, f"SOL mint spotted in group {s['name']}: <code>{mint}</code>\n/trade to buy manually (sol auto not supported).")
    except Exception:
        log.exception("cawatch group scan failed")

async def on_text(update: Update, ctx):
    uid = update.effective_user.id
    text = (update.message.text or "").strip()
    if not is_dm(update):
        await cawatch_on_group_text(update, ctx)
        return
    st = ctx.user_data.get("await")
    if st:
        # Any registered await state reserves this text for its flow. Only a
        # message with no pending await may fall through to conversational AI.
        field, arg = st
        if text.lower() == "/cancel":
            ctx.user_data.pop("await", None)
            await update.message.reply_text("cancelled.")
            return
    if st and st[0] == "send_wiz_amount":
        amt = text.strip().lower()
        if amt != "all" and not re.fullmatch(r"[0-9]*\.?[0-9]+", amt):
            await update.message.reply_text("send a number or 'all'")
            return
        w = ctx.user_data.get("send_wiz") or {}
        chain_name = w.get("chain")
        try:
            _send_wizard_sources(uid, w.get("src"), chain_name)
        except ValueError as ex:
            await update.message.reply_text(str(ex))
            return
        ctx.user_data.pop("await", None)
        w["amount"] = amt
        w["step"] = "dst"
        ctx.user_data["send_wiz"] = w
        source_addresses = {a.lower() for _, a, _ in _send_wizard_sources(
            uid, w.get("src"), chain_name)}
        rows = []
        main_wallet = wallets.main_wallet_for(uid, chain_name)
        if main_wallet and main_wallet["address"].lower() not in source_addresses:
            rows.append([B("main wallet", callback_data="snd:dst:main")])
        if w.get("src") != "all":
            rows.append([B("all fleet wallets", callback_data="snd:dst:all"),
                         B("pick wallets (multi)", callback_data="snd:dst:PICK")])
            rows.append([B("paste wallet address", callback_data="snd:dst:CA")])
            seen2 = set()
            src_addr = str(w.get("src") or "").lower()
            for w2 in wallets.list_wallets(uid):
                a2 = (w2["address"] or "").lower()
                compatible = (w2["chain"] == "solana" if chain_name == "solana"
                              else w2["chain"] in wallets.EVM_CHAINS)
                if not compatible or a2 in seen2 or a2 in source_addresses or a2 == src_addr:
                    continue
                seen2.add(a2)
                rows.append([B(f"to {w2['label']} ({wallets.mask(w2['address'])})", callback_data=f"snd:dst:{w2['address']}")])
        else:
            rows.append([B("paste wallet address", callback_data="snd:dst:CA")])
        if not rows:
            await update.message.reply_text("no compatible destination wallet; paste the recipient address.")
            ctx.user_data["await"] = ("send_wiz_ca", None)
            return
        await update.message.reply_text("send to where?", reply_markup=M(rows))
        return
    if st and st[0] == "send_wiz_token_ca":
        w = ctx.user_data.get("send_wiz") or {}
        token = text.strip()
        if w.get("chain") == "solana":
            from acobot import solana
            valid = solana.valid_address(token)
            message = "enter a valid Solana SPL mint address."
        else:
            valid = bool(ADDR_RE.fullmatch(token))
            message = "enter a valid 0x token contract address."
        if not valid:
            await update.message.reply_text(message)
            return
        ctx.user_data.pop("await", None)
        w["token"] = token
        w["step"] = "amount"
        ctx.user_data["send_wiz"] = w
        ctx.user_data["await"] = ("send_wiz_amount", None)
        await update.message.reply_text("how much? send a number or 'all'")
        return
    if st and st[0] == "send_wiz_ca":
        addr = text.strip()
        w = ctx.user_data.get("send_wiz") or {}
        if w.get("chain") == "solana":
            from acobot import solana
            valid = solana.valid_address(addr)
            message = "that's not a valid Solana wallet address. send it again or /cancel."
        else:
            valid = bool(ADDR_RE.fullmatch(addr))
            message = "that's not a valid 0x address. send it again or /cancel."
        if not valid:
            await update.message.reply_text(message)
            return
        ctx.user_data.pop("await", None)
        w["dst"] = addr
        ctx.user_data["send_wiz"] = w
        if w.get("chain") == "solana":
            kb = M([[B("review SOL transfer", callback_data="snd:gas:fast")]])
            prompt = "SOL fees are estimated during preview; SPL sends may also need recipient token-account rent."
        else:
            kb = M([[B("slow", callback_data="snd:gas:slow"), B("medium", callback_data="snd:gas:med")],
                    [B("fast", callback_data="snd:gas:fast")]])
            prompt = "gas mode?"
        await update.message.reply_text(prompt, reply_markup=kb)
        return
    if st and st[0] == "cawatch_fund":
        kind, name, chain = st[1]
        ctx.user_data.pop("await", None)
        if not text or not re.fullmatch(r"[0-9]*\.?[0-9]+", text.strip()):
            await update.message.reply_text("send a number like 0.02 (max 0.05)")
            return
        from acobot import cawatch as cwmod
        try:
            s = cwmod.add_source(kind, name, "auto", cap_eth=float(text), chain=chain)
        except ValueError as e:
            await update.message.reply_text(str(e))
            return
        await update.message.reply_text(
            f"ARMED auto: {kind} {name} buys up to {s['cap_eth']} eth on {chain} instantly when a CA drops. no confirm.")
        return
    if st and st[0] == "trade_ca":
        try:
            await _trade_ca(update, ctx, st, text)
        except Exception:
            log.exception("trade CA step failed")
            await update.message.reply_text("something broke on that step. send the CA again or /cancel.")
        return
    if st and st[0] == "trade_amt":
        try:
            await _trade_amount(update, ctx, st, text)
        except Exception:
            log.exception("trade amount step failed")
            await update.message.reply_text("something broke on that step. send the amount again or /cancel.")
        return
    if st and st[0] == "snipetoken_ca":
        try:
            await _prepare_token_panel(update.effective_chat.id, ctx, text)
        except Exception:
            log.exception("snipetoken CA preparation failed")
            await update.message.reply_text("couldnt prepare that quote. send the CA again or /cancel.")
            return
        if valid_address(text):
            ctx.user_data.pop("await", None)
        return
    pending_ops = None if st else ctx.user_data.pop("await_walletops", None)
    if pending_ops:
        if uid != ADMIN_ID: return
        mode, chain_name = pending_ops
        if mode == "balance":
            if ((chain_name == "solana" and 32 <= len(text) <= 44 and all(c.isalnum() for c in text)) or
                    (chain_name != "solana" and ADDR_RE.fullmatch(text))):
                ctx.user_data["balance_view"] = {"chain": chain_name, "asset": text, "selected": None, "page": 0}
                await _show_balance_wallet_picker(update.effective_chat.id, ctx, uid)
            else: await update.message.reply_text("send a valid token address.")
            return
        if mode == "sweep_asset":
            state = ctx.user_data.get("sweepall", {})
            state.update({"step":"destination", "chain":chain_name, "asset":text})
            ctx.user_data["sweepall"] = state
            main = wallets.main_wallet(uid)
            await update.message.reply_text("pick destination", reply_markup=M([[B("main wallet", callback_data=f"sw:dest:{chain_name}:main")],[B("paste address", callback_data=f"sw:destpaste:{chain_name}")]]))
            return
        if mode == "sweep_nft_ca":
            state = ctx.user_data.get("sweepall", {})
            if not valid_address(text):
                await update.message.reply_text("invalid contract address. send the NFT CA."); return
            state.update({"step": "source", "asset": "nft", "nft_ca": cs(text)})
            ctx.user_data["sweepall"] = state
            rows = [[B("all fleet wallets", callback_data=f"sw:src:{chain_name}:all")]]
            seen = set()
            for w2 in wallets.list_wallets(uid):
                a2 = (w2["address"] or "").lower()
                if a2 in seen: continue
                seen.add(a2)
                rows.append([B(f"{w2['label']} ({wallets.mask(w2['address'])})", callback_data=f"sw:src:{chain_name}:{a2}")])
            await update.message.reply_text("sweep from which wallet?", reply_markup=M(rows))
            return
        if mode == "sweep_destination":
            state = ctx.user_data.get("sweepall", {})
            if (chain_name == "solana" and not (32 <= len(text) <= 44 and all(c.isalnum() for c in text))) or (chain_name != "solana" and not valid_address(text)):
                await update.message.reply_text("invalid destination address."); return
            await _make_sweep_preview(update.effective_chat.id, ctx, uid, chain_name, state.get("asset", "native"), cs(text), source=state.get("src"), nft_ca=state.get("nft_ca"))
            return
    st = ctx.user_data.get("await")
    if not st:
        if text and not text.startswith("/"):
            await ai_route(update, ctx, text)
        return
    if st[0] == "ai_key":
        if uid != ADMIN_ID:
            return
        import acobot.ai as aimod
        if text.lower() == "/cancel":
            ctx.user_data.pop("await", None)
            await update.message.reply_text("cancelled.")
            return
        try:
            aimod.save_key(text)
        except ValueError as e:
            await update.message.reply_text(f"{e}. paste it again or /cancel.")
            return
        except RuntimeError as e:
            await update.message.reply_text(f"cant store keys right now: {e}")
            return
        except Exception:
            await update.message.reply_text("cant store keys right now. try again later.")
            return
        ctx.user_data.pop("await", None)
        try:
            await update.message.delete()
        except Exception:
            pass
        await ctx.bot.send_message(update.effective_chat.id, "ai key saved (encrypted). " + aimod.status_text())
        return
    if text.lower() == "/cancel":
        ctx.user_data.pop("await", None)
        await update.message.reply_text("cancelled.")
        return
    field, arg = st
    if field == "wgen_qty":
        if not is_dm(update):
            return
        if not text.isdigit() or not 1 <= int(text) <= 10:
            await update.message.reply_text("send a number 1-10, or /cancel.")
            return
        n = int(text)
        ctx.user_data.pop("await", None)
        evm_all = arg == "evm_all"
        chain_pick = "ethereum" if evm_all else (arg or "ethereum")
        room = wallets.MAX_WALLETS - len(wallets.list_wallets(uid))
        if room <= 0:
            await update.message.reply_text(f"wallet cap ({wallets.MAX_WALLETS}) reached. remove one first.")
            return
        n = min(n, room)
        lines = []
        try:
            await update.message.delete()
        except Exception:
            pass
        for _ in range(n):
            priv, addr = await asyncio.to_thread(wallets.generate_wallet, chain_pick)
            if evm_all:
                for cp in wallets.EVM_CHAINS:
                    try: await asyncio.to_thread(wallets.add_wallet, uid, priv, 1, cp)
                    except ValueError: pass
                lines.append(f"<code>{addr}</code> —&gt; <code>{priv}</code>  (EVM: all chains)")
            else:
                _a, masked, _l = await asyncio.to_thread(wallets.add_wallet, uid, priv, 1, chain_pick)
                lines.append(f"<code>{addr}</code> —&gt; <code>{priv}</code>")
        out = "\n".join(lines)
        await update.message.reply_text(
            f"🛡 {n} new {chain_pick} wallet(s) generated and saved.\n\n{out}\n\n"
            "⚠️ this is the ONLY time the keys are shown. save them somewhere safe now, they will never be shown again.")
        await show_wallets(update.effective_chat.id, ctx, uid)
        return
    if field == "wallet_multi_count":
        if not text.isdigit() or not (2 <= int(text) <= 20):
            await update.message.reply_text("send a number 2-20")
            return
        ctx.user_data["await"] = ("wallet_multi_keys", (arg, int(text), []))
        await update.message.reply_text(
            f"paste {text} private keys, comma separated or one per line (0x... each). i add them all, then delete ur message. /cancel to abort.")
        return
    if field == "wallet_multi_keys":
        if not is_dm(update):
            return
        chain_pick, want, done_lines = arg
        raw = text.replace(",", " ").replace(";", " ").replace("\n", " ")
        keys = [k.strip() for k in raw.split() if k.strip() and not k.strip().startswith("/")]
        if len(done_lines) + len(keys) < want:
            ctx.user_data["await"] = ("wallet_multi_keys", (chain_pick, want, done_lines + keys))
            await update.message.reply_text(f"{len(done_lines) + len(keys)}/{want} so far, send the rest.")
            return
        all_keys = (done_lines + keys)[:want]
        chains_for_key = list(wallets.EVM_CHAINS) if chain_pick == "evm_all" else [chain_pick or CHAIN_NAME]
        saved, failed = [], []
        for k in all_keys:
            try:
                addr, masked, _ = await asyncio.to_thread(wallets.add_wallet, uid, k, 1, chains_for_key[0])
                for cp in chains_for_key[1:]:
                    try:
                        await asyncio.to_thread(wallets.add_wallet, uid, k, 1, cp)
                    except ValueError:
                        pass
                saved.append(masked)
            except (ValueError, RuntimeError) as e:
                failed.append(str(e)[:40])
        ctx.user_data.pop("await", None)
        try:
            await update.message.delete()
        except Exception:
            pass
        msg = f"added {len(saved)} wallets:\n" + "\n".join(saved)
        if failed:
            msg += f"\n\n{len(failed)} failed (dup or bad key):\n" + "\n".join(failed[:5])
        await ctx.bot.send_message(update.effective_chat.id, msg)
        return
    if field == "wallet_key":
        if not is_dm(update):
            return
        chains_for_key = list(wallets.EVM_CHAINS) if arg == "evm_all" else [arg or CHAIN_NAME]
        saved, skipped = [], []
        addr = masked = label = None
        for cp in chains_for_key:
            try:
                addr, masked, label = await asyncio.to_thread(wallets.add_wallet, uid, text, 1, cp)
                saved.append(cp)
            except ValueError as e:
                if "already saved" in str(e):
                    skipped.append(cp)
                else:
                    await update.message.reply_text(f"{e} send it again or /cancel.")
                    return
            except RuntimeError as e:
                await update.message.reply_text(f"cant store keys right now: {e}")
                return
        if not saved and not skipped:
            await update.message.reply_text("couldnt save that key. send it again or /cancel.")
            return
        ctx.user_data.pop("await", None)
        try:
            await update.message.delete()
        except Exception:
            pass
        parts = []
        if saved:
            parts.append(f"saved {masked} on {', '.join(saved)}")
        if skipped:
            parts.append(f"already on {', '.join(skipped)} (skipped)")
        parts.append("one key, all EVM chains. set mint amounts in my wallets.")
        await ctx.bot.send_message(update.effective_chat.id, "\n".join(parts))
        return
    if field == "wallet_qty":
        if not text.isdigit() or int(text) < 1:
            await update.message.reply_text("send a whole number, 1 or more.")
            return
        wallets.set_qty(uid, arg, int(text))
        ctx.user_data.pop("await", None)
        await update.message.reply_text("saved.")
        return
    if not allowed(uid) or uid != ADMIN_ID:
        return
    if field == "chain":
        await update.message.reply_text("tap a chain button above, or /cancel.")
        return
    if field == "contract":
        await start_draft(update.effective_chat.id, ctx, text, chain_name=arg or CHAIN_NAME)
        return
    if field == "sell_floor":
        try:
            from decimal import Decimal
            floor = Decimal(text)
            if not floor.is_finite() or floor <= 0:
                raise ValueError
        except Exception:
            await update.message.reply_text("send a positive floor in ETH, or /cancel.")
            return
        sell = dict(arg, floor_eth=float(floor), active=True, created_at=int(time.time()))
        SELL_TARGETS.append(sell)
        SELL_TARGETS_FILE.write_text(json.dumps(SELL_TARGETS, indent=2))
        ctx.user_data.pop("await", None)
        await update.message.reply_text(f"sell mode armed for {len(sell['tokens'])} token(s). floor {floor:g} ETH. every offer needs your confirm tap.")
        return
    d = DRAFTS.get(arg)
    if d is None:
        ctx.user_data.pop("await", None)
        await update.message.reply_text("that draft is gone. /addmint again.")
        return
    try:
        if field == "price":
            d["price_wei"] = parse_mint_price_wei(text)
        elif field == "start":
            if text.isdigit():
                ts = int(text)
            else:
                ts = int(datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=UTC).timestamp())
            if ts <= 0:
                raise ValueError("start time must be a positive Unix timestamp")
            d["start_ts"] = ts
        elif field == "mint_call":
            sig = text.strip()
            sniper.encode_mint(sig, 1)
            d["mint_sig"] = sig
            d["mint_sig_manual"] = True
        elif field == "per_wallet":
            limit = int(text)
            if limit < 1:
                raise ValueError("per-wallet limit must be at least 1")
            d["per_wallet"] = limit
        elif field == "gas_limit":
            if text.lower() == "auto":
                d["gas_limit"] = "auto"
            else:
                g = int(text)
                if g < 21000:
                    raise ValueError("gas limit must be at least 21000")
                d["gas_limit"] = g
    except (ValueError, InvalidOperation) as exc:
        await update.message.reply_text("couldnt read that: " + str(exc))
        return
    ctx.user_data.pop("await", None)
    save_drafts()
    await refresh_fees(d)
    await update.message.reply_text(draft_text(d), reply_markup=draft_kb(arg, d))




def zero_send_reason(owners, chain_name, excluded, eligible):
    """Plain reason a fire sends nothing. Always non-empty when eligible == 0."""
    if eligible:
        return None
    saved = [w for o in owners for w in wallets.list_wallets(o)]
    if not saved:
        return "0 wallets: no keys loaded. add keys in /wallets"
    if wallets.DECRYPT_FAILS:
        return (f"{len(wallets.DECRYPT_FAILS)} saved wallet(s) could not be unlocked with the current key store. "
                "re-add those keys in /wallets")
    on_chain = [w for w in saved if wallets.supports_chain(w["chain"], chain_name)]
    if not on_chain:
        other = sorted({w["chain"] for w in saved})
        return (f"0 wallets on {chain_name}: your {len(saved)} saved wallet(s) are tagged {', '.join(other)}. "
                f"switch the chain tag in /wallets")
    if excluded and all(w["address"].lower() in excluded for w in on_chain):
        return "0 wallets: every wallet on this chain is excluded for this mint (see wallet selection)"
    return "0 wallets selected for this mint"


def finish_results(mid, res, chain_name, read_rpc, app, loop):
    """After a fire: poll receipts, save the per-wallet stats for this mint, and send the result panel."""
    kb = None
    try:
        usd = prices.native_usd(chain_name)
    except Exception:
        usd = None
    try:
        rows = resmod.track(read_rpc, chain_name, res, usd_per_eth=usd, feed=head_feed(chain_name))
        m = MINTS.get(mid, {})
        rec = {"id": mid, "contract": m.get("contract"), "name": m.get("name"), "chain": chain_name,
               "fire_mode": m.get("fire_mode"), "qty": m.get("mint_qty") or 1, "fired_at": res.get("fired_at"), "usd_per_eth": usd,
               "rows": rows, "summary": resmod.summarize_rows(rows), "saved_at": int(time.time()),
               "note": res.get("note")}
        resmod.save_result(DATA, mid, rec)
        try:
            portfolio.record_result(rec, m.get("price_wei") or 0, usd, path=DATA / "portfolio.json")
        except Exception:
            log.exception("portfolio ledger write failed for #%s", mid)
        text = "\n".join(panels.result_lines(mid, rec))
        kb = M([[B("my mints", callback_data="mymints")],
                [B("📈 sell mode", callback_data=f"sell:set:{mid}")]])
    except Exception as e:
        log.exception("results for #%s failed: %s", mid, type(e).__name__)
        text = f"couldnt read the receipts for #{mid} right now ({type(e).__name__}). the fire line above still holds."

    async def _send_results():
        for attempt in range(3):
            try:
                await app.bot.send_message(ADMIN_ID, text, reply_markup=kb)
                return
            except Exception:
                log.warning("results send attempt %d failed for #%s", attempt + 1, mid)
                await asyncio.sleep(2)
        _notify(f"mint #{mid} finished. send /mymints to see the results panel.")

    asyncio.run_coroutine_threadsafe(_send_results(), loop)


WATCH_RESTARTS = 5         # watcher crash -> restart this many times before giving up
WATCH_RETRY_WAIT_S = 5     # pause before restarting a crashed watcher
WATCHDOG_EVERY_S = 60      # watchdog / self-heal sweep period
WS_DOWN_NOTICE_S = 60      # a feed down this long for a scheduled mint gets one admin notice
_FIRED = set()             # mint ids whose fire callback has run (never re-fire a crashed mint)
_WS_NOTICED = set()        # chains already reported as down (one notice per outage)
_POLL_RESTARTS = [0]
WATCHDOG_STATE = {"started": False}


def _scrub(text):
    return re.sub(r"\d{8,}:[A-Za-z0-9_-]{30,}", "[token]", str(text))


def _detail(e):
    """Real error detail for admin: type, message, and the last traceback frames (token-scrubbed)."""
    import traceback
    frames = traceback.format_exception(type(e), e, e.__traceback__)
    tail = "".join(frames[-4:]).strip()
    return _scrub(f"{type(e).__name__}: {e}\n{tail}")[-1200:]


def _notify(text):
    """Thread-safe admin message on the bot's main loop. Never raises."""
    try:
        app = WATCH_APP[0]
        loop = MAIN_LOOP[0]
        if app is None or loop is None:
            return
        asyncio.run_coroutine_threadsafe(app.bot.send_message(ADMIN_ID, _scrub(text)[:3900]), loop)
    except Exception:
        log.exception("admin notify failed")


WATCH_APP = [None]


def start_watcher(mid, app):
    ev = threading.Event()
    CANCEL[mid] = ev
    WATCH_APP[0] = app
    state = {"m2": None}

    def status(kind, payload):
        if mid not in MINTS:
            return
        MINTS[mid]["status"] = kind
        if kind == "firing":
            MINTS[mid]["fire_started"] = True
        persist()
        if kind == "expired":
            _notify(f"⌛ mint #{mid} reached its known sale end before firing. no transaction was sent.")
        elif kind == "fired":
            _FIRED.add(mid)
            attempted = len(payload.get("results", []))
            skipped = len(payload.get("skipped", []))
            text = (f"🚀 mint #{mid}: broadcast attempted for {attempted} wallet tx(s) "
                    f"in {payload.get('elapsed_s', '?')}s. waiting for receipts.")
            if skipped:
                text += f" {skipped} wallet(s) were skipped because no signed transaction was available."
            _notify(text)

    def watch_once():
        """One full watch attempt: presign box is fresh each time, so a restart re-presigns cleanly."""
        m = MINTS.get(mid)
        if m is None:
            raise RuntimeError(f"mint #{mid} record is missing")
        chain_name = m.get("chain") or CHAIN_NAME
        chain = CFG.chains.get(chain_name)
        if chain is None:
            raise RuntimeError(f"chain '{chain_name}' is not in config.toml")
        read_rpc = RPC(chain)
        bcast = RPC(chain, broadcast=True)
        read_rpc.assert_chain_id(chain.chain_id)
        bcast.assert_chain_id(chain.chain_id)
        m2 = dict(m)
        m2.update({"chain": chain})
        state["m2"] = m2

        def entries():
            owners = [ADMIN_ID] + list(CFG.whitelist_ids)
            signers = wallets.signing_entries(owners, chain=chain_name)
            ex = set(m.get("wex") or [])
            keep = [x for x in signers if x[2].lower() not in ex]
            chosen = m.get("wallets")
            if chosen:
                ch = {a.lower() for a in chosen}
                keep = [x for x in keep if x[2].lower() in ch]
            gate_lines = []
            gated = mint_router.is_gated(m2)
            if gated:
                authorization_candidates = list(keep)
                api = {}
                for _owner, _label, address, _key, wallet_qty in keep:
                    qty = int(m.get("mint_qty") or wallet_qty or 1)
                    tx, why = auth_resolver.resolve_evm_authorization(
                        m2, address, qty, chain, read_rpc,
                        attempt_on_simulation_failure=bool(m.get("force")))
                    if not tx:
                        gate_lines.append(f"{wallets.mask(address)} — {why}")
                        continue
                    api[address.lower()] = tx
                keep = [item for item in keep if item[2].lower() in api]
                m2["_api_calldata"] = api
                m2["_authorization_pending"] = bool(authorization_candidates and not api)
                if api:
                    m2["_authorization_pending"] = False
                    if status_wrap:
                        status_wrap("authorization_ready", {})
            else:
                m2["_authorization_pending"] = False
            m2["_gate_lines"] = gate_lines
            m2["_zero_reason"] = ("; ".join(gate_lines[:3]) if gate_lines and not keep
                                  else zero_send_reason(owners, chain_name, ex, len(keep)))
            return sniper.presign(m2, chain, read_rpc,
                                  [(f"{o}:{lbl}", a, k, (m.get("mint_qty") or q)) for o, lbl, a, k, q in keep])
        m2["_entries_fn"] = entries

        def status_wrap(kind, payload):
            if kind == "no_transactions":
                reason = payload.get("reason") or "no signed transactions were ready"
                m2["_terminal_no_transactions"] = reason
                if mid in MINTS:
                    MINTS[mid]["status"] = "error"
                    MINTS[mid]["error_detail"] = reason
                    persist()
                _notify(f"⛔ mint #{mid} stopped: no transaction was broadcast. {reason}")
                return
            if kind == "waiting_authorization":
                if mid in MINTS:
                    MINTS[mid]["authorization_status"] = "waiting"
                    MINTS[mid]["authorization_detail"] = payload.get("reason")
                    persist()
                message = payload.get("reason") or "wallet-specific phase authorization is still unavailable"
                if m2.get("_last_auth_notice") != message:
                    m2["_last_auth_notice"] = message
                    _notify(f"⏳ mint #{mid} waiting for authorization: {message}. no transaction was sent; the resolver will retry.")
                return
            if kind == "authorization_ready":
                if mid in MINTS:
                    MINTS[mid]["authorization_status"] = "ready"
                    MINTS[mid].pop("authorization_detail", None)
                    persist()
                return
            if kind == "fired" and not payload.get("results"):
                skipped_errors = [
                    str(item["error"]) for item in payload.get("skipped") or []
                    if item.get("error")
                ]
                reason = ("; ".join(skipped_errors[:3]) or m2.get("_zero_reason")
                          or "no signed transactions were ready")
                m2["_terminal_no_transactions"] = reason
                if mid in MINTS:
                    MINTS[mid]["status"] = "error"
                    MINTS[mid]["error_detail"] = reason
                    persist()
                _notify(f"⛔ mint #{mid} stopped: no transaction was broadcast. {reason}")
                return
            if kind == "fired" and m2.get("_gate_lines"):
                _notify("gated phase wallet results:\n" + "\n".join(m2["_gate_lines"][:10]))
            status(kind, payload)
        res = sniper.wait_and_fire(m2, chain, read_rpc, {}, ev.is_set,
                                   status_cb=status_wrap, bcast=bcast, feed=head_feed(chain.name))
        if res is None:
            if m2.get("_terminal_no_transactions"):
                return
            if not m2.get("expired"):
                MINTS[mid]["status"] = "cancelled"; persist()
        elif not res.get("results"):
            if m2.get("_terminal_no_transactions"):
                return
            reason = "no wallet transaction was broadcast"
            MINTS[mid]["status"] = "error"
            MINTS[mid]["error_detail"] = reason
            persist()
            _notify(f"⛔ mint #{mid} stopped: {reason}.")
        else:
            finish_results(mid, res, chain_name, read_rpc, app, MAIN_LOOP[0])

    def run():
        restarts = 0
        while True:
            try:
                watch_once()
                return
            except Exception as e:
                detail = _detail(e)
                log.error("watcher %s crashed (restart %d/%d)\n%s", mid, restarts, WATCH_RESTARTS, detail)
                m2 = state.get("m2") or {}
                if ev.is_set():
                    if mid in MINTS: MINTS[mid]["status"] = "cancelled"; persist()
                    return
                if mid in _FIRED or m2.get("fire_started") or MINTS.get(mid, {}).get("fire_started"):
                    # the broadcast already started: re-presigning would risk a second send. stop, tell admin.
                    if mid in MINTS: MINTS[mid]["status"] = "error"; persist()
                    _notify(f"⛔ watcher #{mid} crashed AFTER the fire started. not re-firing. check the results.\n{detail}")
                    return
                if restarts >= WATCH_RESTARTS:
                    if mid in MINTS:
                        MINTS[mid]["status"] = "error"
                        MINTS[mid]["error_detail"] = detail
                        persist()
                    _notify(f"❌ watcher #{mid} crashed {restarts + 1} times, giving up. last error:\n{detail}\n"
                            f"fix the cause, then re-arm the mint.")
                    return
                restarts += 1
                if mid in MINTS:
                    MINTS[mid]["status"] = "retrying"
                    MINTS[mid]["error_detail"] = detail
                    persist()
                _notify(f"⚠️ watcher #{mid} crashed, restarting in {WATCH_RETRY_WAIT_S}s (attempt {restarts}/{WATCH_RESTARTS}).\n{detail}")
                if ev.wait(WATCH_RETRY_WAIT_S):   # cancel during the pause stops it
                    if mid in MINTS: MINTS[mid]["status"] = "cancelled"; persist()
                    return

    t = threading.Thread(target=run, daemon=True, name=f"mint-{mid}")
    THREADS[mid] = t
    t.start()


def watcher_alive(mid):
    t = THREADS.get(mid)
    return bool(t and t.is_alive())


def sweep_watchers(app):
    """Self-heal: scheduled/presigned/retrying mints with no live watcher thread get one restarted. Returns mint ids restarted."""
    fixed = []
    for mid, m in list(MINTS.items()):
        if m.get("status") in ("scheduled", "presigned", "retrying") and not watcher_alive(mid):
            log.warning("sweep: mint #%s has no live watcher (status %s), restarting", mid, m.get("status"))
            start_watcher(mid, app)
            fixed.append(mid)
    return fixed


def recover_interrupted_mints():
    """Fail closed on mints whose process stopped after durable broadcast-start state."""
    uncertain = []
    for mid, mint in MINTS.items():
        status = mint.get("status")
        if status == "firing" or (status in ("scheduled", "presigned", "retrying") and mint.get("fire_started")):
            mint["status"] = "error"
            mint["error_detail"] = "process restarted after broadcast began; check the chain before re-arming"
            uncertain.append(mid)
    if uncertain:
        persist()
    return uncertain


def watchdog_tick(app):
    """One watchdog pass. Fixes what it can, returns a list of human lines describing repairs (empty = all healthy)."""
    fixes = []
    # 1) watchers for scheduled mints
    for mid in sweep_watchers(app):
        fixes.append(f"restarted the watcher for #{mid} (it had no live thread)")
    # 2) ws feeds for chains with scheduled mints
    live = [m for m in MINTS.values() if m.get("status") in ("scheduled", "presigned", "retrying")]
    chains = sorted({m.get("chain") or CHAIN_NAME for m in live})
    now = time.time()
    for cn in chains:
        feed = head_feed(cn)
        if feed is None:
            continue
        if feed.ensure_running():
            fixes.append(f"restarted the {cn} ws feed thread")
        if feed.subscribed():
            _WS_NOTICED.discard(cn)
            continue
        if feed.down_for(now) >= WS_DOWN_NOTICE_S and cn not in _WS_NOTICED:
            _WS_NOTICED.add(cn)
            ids = ", ".join(f"#{m['id']}" for m in live if (m.get("chain") or CHAIN_NAME) == cn)
            _notify(f"⚠️ ws down for {ids} ({cn}), polling fallback active. "
                    f"last error: {feed.last_error or 'unknown'}")
    # 2b) gas spike guard: one admin warning per scheduled mint, within 30 min of its open
    for m in live:
        st = m.get("start_ts")
        if not st or m.get("gas_warned") or not (0 < st - now <= 1800):
            continue
        cn = m.get("chain") or CHAIN_NAME
        try:
            v = gasguard.check(RPC(CFG.chains[cn]), cn, prices.native_usd(cn))
            m["gas_warned"] = True
            persist()
            if v["level"] == "warn":
                fixes.append(f"gas warning sent for #{m['id']}")
                _notify(gasguard.warning_text(m.get("name") or m["contract"][:10], fmt_ts(st), v))
        except Exception:
            log.warning("gas guard skipped for #%s", m.get("id"))
    # 3) telegram polling alive
    upd = getattr(app, "updater", None)
    task = getattr(upd, "_Updater__polling_task", None) if upd else None
    if upd is not None and (not upd.running or (task is not None and task.done())):
        fixes.append("telegram polling had stopped, restarting it")
        _POLL_RESTARTS[0] += 1
        if _POLL_RESTARTS[0] <= 5:
            asyncio.run_coroutine_threadsafe(_restart_polling(app), MAIN_LOOP[0])
    return fixes


async def _restart_polling(app):
    try:
        if app.updater.running:
            await app.updater.stop()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        _notify("🔧 telegram polling had stopped. restarted it, bot is taking commands again.")
    except Exception as e:
        log.exception("polling restart failed")
        _notify(f"❌ telegram polling stopped and the restart failed: {_detail(e)[:600]}")


def start_watchdog(app):
    if WATCHDOG_STATE["started"]:
        return
    WATCHDOG_STATE["started"] = True

    def loop_forever():
        while True:
            time.sleep(WATCHDOG_EVERY_S)
            try:
                fixes = watchdog_tick(app)
                if fixes:
                    log.warning("watchdog fixed: %s", "; ".join(fixes))
                    _notify("🛠 watchdog fixed something:\n- " + "\n- ".join(fixes))
            except Exception:
                log.exception("watchdog tick failed")

    threading.Thread(target=loop_forever, daemon=True, name="watchdog").start()


async def show_mints(chat_id, ctx, uid, msg_id=None):
    live = [m for m in MINTS.values() if m.get("status") in ("scheduled", "presigned", "retrying", "firing")]
    parts, rows = [], []
    if live:
        lines = []
        for m in live:
            lines.append(f"#{m['id']} {esc(m['contract'])}\nopens {fmt_ts(m['start_ts'])}  price {fmt_eth(m['price_wei'])}  status {m['status']}")
            if m.get("status") != "firing":
                rows.append([B(f"cancel #{m['id']}", callback_data=f"mc:{m['id']}")])
        parts.append("📅 <b>scheduled mints</b>\n\n" + "\n\n".join(lines))
    else:
        parts.append("no scheduled mints.")
    past = resmod.list_result_ids(DATA)[-10:]
    if past:
        plines = ["<b>past results</b>"]
        for mid in reversed(past):
            rec = resmod.load_result(DATA, mid)
            if not rec:
                continue
            label = rec.get("name") or (rec.get("contract") or "")[:10]
            plines.append(f"#{mid} {esc(label)}\n{esc(resmod.summary_line(rec.get('summary') or {}))}")
            rows.append([B(f"view results #{mid}", callback_data=f"vr:{mid}")])
        parts.append("\n\n".join(plines))
    rows.append([B("home", callback_data="home")])
    await _edit_or_send(None, chat_id, msg_id, "\n\n".join(parts), M(rows), ctx)


async def cancel_mint_id(chat_id, ctx, mid, uid):
    if uid != ADMIN_ID:
        return
    m = MINTS.get(mid)
    if not m or m.get("status") in ("cancelled", "fired", "error", "firing") or m.get("fire_started"):
        await ctx.bot.send_message(chat_id, f"#{mid} is not cancellable.")
        return
    if mid in CANCEL:
        CANCEL[mid].set()
    m["status"] = "cancelled"; persist()
    await ctx.bot.send_message(chat_id, f"cancelled #{mid}.")


async def cmd_mymints(update: Update, ctx):
    if update.effective_user.id != ADMIN_ID:
        return
    await show_mints(update.effective_chat.id, ctx, update.effective_user.id)


async def cmd_cancelmint(update: Update, ctx):
    if update.effective_user.id != ADMIN_ID:
        return
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text("usage: /cancelmint 3   (the mint id)")
        return
    await cancel_mint_id(update.effective_chat.id, ctx, int(ctx.args[0]), update.effective_user.id)


async def cmd_wallets(update: Update, ctx):
    if not is_dm(update):
        await update.message.reply_text("👉 dm me to manage wallets.")
        return
    if not (allowed(update.effective_user.id) or username_allowed(update.effective_user.username)):
        return
    await show_wallets(update.effective_chat.id, ctx, update.effective_user.id)


async def show_wallets(chat_id, ctx, uid, msg_id=None):
    ws = wallets.list_wallets(uid)
    # group by address: one EVM key saved on several chains shows ONCE
    seen, grouped = set(), []
    for w in ws:
        key = w["address"].lower()
        if key in seen:
            seen_chains = next(g["chains"] for g in grouped if g["address"].lower() == key)
            seen_chains.append(w["chain"])
            g_main = next(g for g in grouped if g["address"].lower() == key)
            g_main["main"] = g_main["main"] or w["main"]
            continue
        seen.add(key)
        grouped.append({"address": w["address"], "masked": w["masked"], "qty": w["qty"],
                        "main": w["main"], "chains": [w["chain"]], "label": w["label"]})
    lines = []
    for g in grouped:
        is_evm = g["chains"][0] != "solana"
        chain_txt = "EVM: " + ", ".join(g["chains"]) if is_evm else "SOL"
        lines.append(("★ " if g["main"] else "• ") + f"<b>{esc(g['label'])}</b> · {esc(chain_txt)} · x{g['qty']}"
                      + ("  <i>(main)</i>" if g["main"] else "") + f"\n   <code>{esc(g['masked'])}</code>")
    if not ws:
        lines = ["📭 no wallets yet.", "👉 tap <b>add wallet</b> below to load your first key (dm only)."]
    head = "\n".join(panels.header(f"wallets ({len(grouped)}/{wallets.MAX_WALLETS})")) + "\n"
    rows = [[B("add wallet", callback_data="w:add")]]
    for g in grouped:
        main_btn = B("★ main", callback_data="wnop") if g["main"] else B("set as main", callback_data=f"wmain:{g['address']}")
        rows.append([B("amount", callback_data=f"wq:{g['address']}"),
                     main_btn,
                     B("remove", callback_data=f"wr:{g['address']}")])
    if ws:
        rows.append([B("🗑 remove all wallets", callback_data="wrall")])
    rows.append([B("💰 balances", callback_data="bal:chains"), B("🔄 refresh", callback_data="w:list")])
    rows.append([B("🏠 main", callback_data="home")])
    await _edit_or_send(None, chat_id, msg_id, head + "\n" + "\n".join(lines), M(rows), ctx)


def mint_wallets(d):
    """[(owner, label, address, qty)] for signers on this draft's chain. Owners: admin + whitelist."""
    owners = [ADMIN_ID] + list(CFG.whitelist_ids)
    chain = d.get("chain", CHAIN_NAME)
    return [(o, lbl, a, q) for o, lbl, a, _k, q in wallets.signing_entries(owners, chain=chain)]


async def _draft_checks(d, selected_only=True):
    """Read-only per-wallet balance/gas/limit and gated-stage eligibility checks."""
    d = draft_phase_view(d)
    allw = mint_wallets(d)
    excluded = {a.lower() for a in d.get("wex") or []}
    chosen = [(lbl, a, q) for _o, lbl, a, q in allw if not selected_only or a.lower() not in excluded]
    if mint_router.is_gated(d):
        status = phase_authorization_status(d)
        reason = ("authorization required; " + status["detail"]
                  + "; local eligibility is informational and does not block the mint")
        return allw, [{"label": label, "address": address, "masked": wallets.mask(address),
                       "verdict": "warn", "eligibility": None,
                       "authorization": "required" if status["required"] else "not_required",
                       "reasons": [reason]}
                      for label, address, _qty in chosen]
    chain_name = d.get("chain", CHAIN_NAME)
    chain = CFG.chains.get(chain_name)
    if not chain:
        return allw, [{"label": l, "address": a, "masked": wallets.mask(a), "verdict": "fail", "reasons": [f"chain {chain_name} is not configured"]} for l,a,q in chosen]
    try:
        verdicts = await asyncio.to_thread(fleet.check_all, chain, d, chosen)
    except Exception as exc:
        log.warning("wallet preflight failed: %s", type(exc).__name__)
        verdicts = [{"label": l, "address": a, "masked": wallets.mask(a), "verdict": "warn", "reasons": [f"check failed: {type(exc).__name__}"]} for l,a,q in chosen]
    # eligibility now comes from the on-chain simulation in fleet.check_all:
    # the mint tx is eth_call'd FROM each wallet, so allowlist membership is verified for real.
    for v in verdicts:
        if v.get("eligibility") is True:
            v.setdefault("reasons", [])[:] = [r for r in v.get("reasons", []) if "allowlist" not in r]
            if v.get("verdict") == "warn" and not v.get("reasons"):
                v["verdict"] = "ok"
        elif v.get("eligibility") is False:
            v["verdict"] = "fail"
    return allw, verdicts


async def _split_by_local_eligibility(draft, entries):
    """Split gated-phase wallets into locally eligible and bypass-only groups."""
    view = draft_phase_view(draft)
    if not mint_router.is_gated(view):
        return list(entries), [], {}
    from acobot import eligible as eligibility

    stage = view.get("_selected_stage") or {
        "key": view.get("phase"),
        "label": view.get("phase_label") or view.get("phase"),
    }
    wallet_rows = [{"label": label, "address": address}
                   for _owner, label, address, _qty in entries]
    results = await eligibility.probe_fleet(
        view.get("contract"), view.get("chain", CHAIN_NAME), wallet_rows, stage)
    result_by_address = {
        wallet["address"].lower(): result
        for wallet, result in results
    }
    normal_entries = [
        entry for entry in entries
        if (result_by_address.get(entry[2].lower()) or {}).get("eligible") is True
    ]
    force_entries = [entry for entry in entries if entry not in normal_entries]
    return normal_entries, force_entries, result_by_address


async def show_mint_wallet_picker(chat_id, ctx, mid, msg_id=None):
    m = MINTS.get(mid)
    if not m:
        return
    sel = ctx.user_data.setdefault("mintpick", {}).setdefault(str(mid), set())
    if not sel:
        sel.update(w["address"].lower() for w in wallets.list_wallets(ADMIN_ID))
    entries = mint_wallets(m)
    chain = CFG.chains.get(m.get("chain", CHAIN_NAME))
    selected_entries = [(l, a, q) for _o,l,a,q in entries if a.lower() in sel]
    if mint_router.is_gated(m):
        verdicts = [{"label": label, "address": address, "masked": wallets.mask(address),
                     "verdict": "warn", "reasons": ["phase authorization checked at fire time"]}
                    for label, address, _qty in selected_entries]
    else:
        verdicts = await asyncio.to_thread(fleet.check_all, chain, m, selected_entries) if chain and selected_entries else []
    by = {v.get("address", "").lower(): v for v in verdicts}
    rows = []
    lines = [f"<b>mint #{mid} wallet selection · {esc(m.get('chain', CHAIN_NAME))}</b>", "balance status is current selection; tap a wallet to toggle", ""]
    if not entries:
        lines.append("⚠️ no signer wallets on this chain")
    for _o, label, addr, qty in entries:
        a = addr.lower(); chosen = a in sel; v = by.get(a)
        state = "not selected"
        if chosen and v:
            state = f"balance {panels.eth(v.get('balance_wei'))}, price+gas ~{panels.eth(v.get('cost_wei'))}"
            reasons = v.get("reasons") or []
            if reasons: state += " ⚠️ " + "; ".join(reasons)
            else: state += " ✅ ready"
        elif chosen:
            state = "⚠️ status unavailable"
        lines.append(f"{'✅' if chosen else '⬜'} {esc(label)} {esc(wallets.mask(addr))}: {esc(state)}")
        rows.append([B(f"{'✅' if chosen else '⬜'} {label} {wallets.mask(addr)}", callback_data=f"mpick:{mid}:{a}")])
    rows.append([B("save selection", callback_data=f"mpick:{mid}:save"), B("back to my mints", callback_data="m:list")])
    await _edit_or_send(None, chat_id, msg_id, "\n".join(lines), M(rows), ctx)


async def show_wallet_step(chat_id, ctx, did, msg_id=None):
    d = DRAFTS.get(did)
    if d is None:
        await ctx.bot.send_message(chat_id, "that draft is gone.")
        return
    view = draft_phase_view(d)
    allw = mint_wallets(d)
    text = "\n".join(panels.wallet_step_lines(view, allw))
    rows = []
    for i, (_o, lbl, a, _q) in enumerate(allw):
        mark = "⬜" if a.lower() in {x.lower() for x in d.get("wex") or []} else "✅"
        rows.append([B(f"{mark} {lbl} {wallets.mask(a)}", callback_data=f"wt:{did}:{i}")])
    rows.append([B("all wallets", callback_data=f"wall:{did}")])
    if mint_router.is_gated(view):
        status = phase_authorization_status(view)
        text += "\n\nphase authorization: " + esc(status["detail"])
    else:
        rows[-1].append(B("simulate mint call", callback_data=f"wchk:{did}"))
    rows.append([B("schedule", callback_data=f"wgo:{did}"), B("back", callback_data=f"wback:{did}")])
    await _edit_or_send(None, chat_id, msg_id, text, M(rows), ctx)


async def run_check(chat_id, ctx, did, msg_id=None):
    d = DRAFTS.get(did)
    if d is None:
        return
    _allw, verdicts = await _draft_checks(d)
    text = "\n".join(panels.check_lines(verdicts, d.get("price_wei") or 0))
    kb = M([[B("back to wallets", callback_data=f"wsel:{did}"), B("schedule pre-flight", callback_data=f"wgo:{did}")]])
    await _edit_or_send(None, chat_id, msg_id, text, kb, ctx)


async def run_schedule_check(chat_id, ctx, did, msg_id=None, workflow="normal"):
    d = DRAFTS.get(did)
    if d is None:
        await ctx.bot.send_message(chat_id, "that draft is gone.")
        return
    if workflow not in {"normal", "force", "both"}:
        await ctx.bot.send_message(chat_id, "unknown schedule workflow; reopen the mint draft.")
        return
    view = draft_phase_view(d)
    if mint_router.is_gated(view):
        status = phase_authorization_status(view)
        allw = mint_wallets(d)
        excluded = {a.lower() for a in d.get("wex") or []}
        selected = [(o, label, addr, qty) for o, label, addr, qty in allw if addr.lower() not in excluded]
        if not selected:
            await ctx.bot.send_message(chat_id, "no wallets selected. pick at least one wallet before scheduling.")
            return
        if workflow == "force":
            normal_entries, force_entries = [], selected
        else:
            try:
                normal_entries, force_entries, _ = await _split_by_local_eligibility(d, selected)
            except Exception as exc:
                log.warning("eligibility partition failed: %s", type(exc).__name__)
                await ctx.bot.send_message(
                    chat_id,
                    "local eligibility could not be checked, so Normal or Both was not scheduled. "
                    "Use Force Schedule to bypass this local check, or retry later.")
                return
        if workflow == "normal":
            groups = [("Normal Schedule · locally eligible", normal_entries)]
            not_scheduled = len(force_entries)
        elif workflow == "force":
            groups = [("Force Schedule · eligibility bypassed", selected)]
            not_scheduled = 0
        else:
            groups = [
                ("Normal Schedule · locally eligible", normal_entries),
                ("Force Schedule · ineligible or unknown", force_entries),
            ]
            not_scheduled = 0
        groups = [(label, entries) for label, entries in groups if entries]
        if not groups:
            text = (
                "<b>⛔ no locally eligible wallets</b>\n\n"
                "Normal Schedule will not queue an ineligible or unknown wallet. "
                "Use Force Schedule to attempt the selected wallets at the configured phase time, "
                "or Both to put them in the Force workflow."
            )
            kb = M([[B("🔥 Force Schedule", callback_data=f"fsched:{did}"),
                     B("🔀 Both", callback_data=f"bsched:{did}")],
                    [B("back to draft", callback_data=f"wback:{did}")]])
            await _edit_or_send(None, chat_id, msg_id, text, kb, ctx)
            return
        lines = ["<b>🔐 gated phase schedule</b>", ""]
        if workflow == "normal":
            lines.append(
                f"Normal Schedule includes only locally eligible wallets; "
                f"{not_scheduled} ineligible or unknown wallet(s) are excluded.")
        elif workflow == "force":
            lines.append(
                "Force Schedule waits for the same phase open time as an eligible wallet. "
                "It broadcasts the real phase transaction even when this wallet is not on the allowlist. "
                "If the chain reverts, that result is reported. No public-mint substitute is used.")
        else:
            lines.append(
                "Both uses disjoint workflows: eligible wallets go to Normal; "
                "ineligible or unknown wallets go to Force. No wallet is queued twice.")
        lines.append(
            ("Authorization source is configured. " if status["available"] else
             "Waiting for a supported project authorization API. ")
            + "At execution each wallet uses a real phase transaction. "
            "Force still broadcasts that transaction when simulation says the wallet is not eligible. "
            "A wallet with no phase transaction is not sent, and a public mint is never substituted.")
        lines.append("")
        for label, entries in groups:
            lines.append(f"<b>{esc(label)} ({len(entries)})</b>")
            lines.extend(
                f"✅ {esc(wallet_label)} {wallets.mask(address)}"
                for _owner, wallet_label, address, _qty in entries)
        if not view.get("start_ts") and not view.get("sale_started_fn"):
            lines += [
                "",
                "⚠️ missing phase opening time or on-chain live-sale signal. Set a phase time before scheduling.",
            ]
        callback = f"wconfirm:{did}" if workflow == "normal" else f"wconfirm:{did}:{workflow}"
        rows = [[B("✅ Confirm schedule", callback_data=callback),
                 B("🛠 Fix first", callback_data=f"wsel:{did}")]]
        text = "\n".join(lines)
        await _edit_or_send(None, chat_id, msg_id, text, M(rows), ctx)
        return
    allw, verdicts = await _draft_checks(d)
    text = "\n".join(panels.preflight_lines(verdicts, gated=False))
    if workflow == "both":
        text += (
            "\n\nLocal phase eligibility does not apply to this public phase. "
            "To avoid duplicate mints and fees, Both will queue these wallets only in Normal."
        )
    elif workflow == "force":
        text += "\n\nForce Schedule bypasses local eligibility only; it waits for the configured mint time."
    rows = []
    for i, (_o, label, addr, _q) in enumerate(allw):
        mark = "⬜" if addr.lower() in {x.lower() for x in d.get("wex") or []} else "✅"
        rows.append([B(f"{mark} {label} {wallets.mask(addr)}", callback_data=f"wt:{did}:{i}")])
    issues = any(v.get("reasons") or v.get("verdict") in ("warn", "fail") for v in verdicts)
    callback = f"wconfirm:{did}" if workflow == "normal" else f"wconfirm:{did}:{workflow}"
    rows.append([
        B("✅ Confirm schedule (attempt selected wallets)", callback_data=callback),
        B("🛠 Fix first", callback_data=f"wsel:{did}"),
    ])
    if issues:
        text += (
            "\n\nWarnings do not change wallet selection. Selected wallets will be attempted; "
            "transaction simulation or execution failures will be reported per wallet."
        )
    await _edit_or_send(None, chat_id, msg_id, text, M(rows), ctx)


async def force_mint(chat_id, ctx, did, msg_id=None):
    d = DRAFTS.get(did)
    if d is None:
        await ctx.bot.send_message(chat_id, "that draft is gone.")
        return
    view = draft_phase_view(d)
    stage = view.get("_selected_stage")
    gated = mint_router.is_gated(view) or bool(stage and stage.get("gated"))
    if gated:
        status = phase_authorization_status(view)
    elif d.get("price_wei") is None or not (d.get("mint_sig") or d.get("seadrop")):
        await ctx.bot.send_message(chat_id, "force mint needs a known mint price and a supported public mint call.")
        return
    if view.get("end_ts") and view["end_ts"] <= int(time.time()):
        await ctx.bot.send_message(chat_id, "this mint's known sale window has ended. force mint will not bypass the end time.")
        return
    allw = mint_wallets(d)
    excluded = {a.lower() for a in d.get("wex") or []}
    selected = [entry for entry in allw if entry[2].lower() not in excluded]
    if not selected:
        await ctx.bot.send_message(chat_id, "no wallets selected. pick at least one wallet before force mint.")
        return
    if gated:
        auth_status = phase_authorization_status(view)
    else:
        _allw, verdicts = await _draft_checks(d)
    d["force_pending_until"] = int(time.time()) + 90
    text = (
        "<b>⚠️ FORCE MINT NOW</b>\n\n"
        "This attempts the selected phase immediately, bypassing only this bot's scheduled start time. "
        + (f"Authorization required: {esc(auth_status['description'])}. "
           + ("A supported phase API is configured. " if auth_status["available"]
              else "Supported public eligibility/project endpoints will be checked automatically. ")
           + "The resolver still requires a real phase transaction. "
           "It broadcasts that transaction at confirmation even when the wallet is not eligible and simulation reverts. "
           "A missing phase transaction is not sent, and a public mint is not substituted. "
           if gated else
           "It cannot open a sale that is not live; the contract may reject the transaction and gas can still be spent. ")
        + "\n\n"
        f"Selected wallets: {len(selected)}\n\n"
        + ("\n".join(
            f"✅ {esc(label)} {wallets.mask(address)} · authorization discovery and simulation before/at execution"
            for _owner, label, address, _qty in selected)
           if gated else "\n".join(panels.preflight_lines(verdicts, gated=False)))
    )
    rows = [[B("🔥 Confirm mint now", callback_data=f"fmconfirm:{did}"),
             B("Cancel", callback_data=f"fmcancel:{did}")]]
    save_drafts()
    await _edit_or_send(None, chat_id, msg_id, text, M(rows), ctx)


async def confirm_force_mint(chat_id, ctx, did, msg_id=None):
    d = DRAFTS.get(did)
    if d is None:
        await ctx.bot.send_message(chat_id, "that draft is gone.")
        return
    if d.get("force_pending_until", 0) < int(time.time()):
        d.pop("force_pending_until", None)
        save_drafts()
        await ctx.bot.send_message(chat_id, "force-mint confirmation expired. review the draft and try again.")
        return
    view = draft_phase_view(d)
    stage = view.get("_selected_stage")
    gated = mint_router.is_gated(view) or bool(stage and stage.get("gated"))
    if gated:
        status = phase_authorization_status(view)
    elif d.get("price_wei") is None or not (d.get("mint_sig") or d.get("seadrop")):
        await ctx.bot.send_message(chat_id, "the public mint price or supported mint call is no longer available.")
        return
    if view.get("end_ts") and view["end_ts"] <= int(time.time()):
        await ctx.bot.send_message(chat_id, "this mint's known sale window has ended; no transaction was sent.")
        return
    excluded = {a.lower() for a in d.get("wex") or []}
    if not any(a.lower() not in excluded for _o, _label, a, _qty in mint_wallets(d)):
        await ctx.bot.send_message(chat_id, "no wallets are selected. no transaction was sent.")
        return
    d["force"] = True
    d["force_start_ts"] = int(time.time())
    d.pop("force_pending_until", None)
    await schedule_draft(chat_id, ctx, did, msg_id)


async def schedule_draft(chat_id, ctx, did, msg_id=None, workflow=None):
    d = DRAFTS.get(did)
    if d is None:
        await ctx.bot.send_message(chat_id, "that draft is gone.")
        return
    if workflow is None:
        workflow = "immediate_force" if d.get("force_start_ts") is not None else "normal"
    if workflow not in {"normal", "force", "both", "immediate_force"}:
        await ctx.bot.send_message(chat_id, "unknown schedule workflow; reopen the mint draft.")
        return

    view = draft_phase_view(d)
    gated = mint_router.is_gated(view)
    auth_status = phase_authorization_status(view)
    force_start_ts = d.get("force_start_ts")
    if force_start_ts is not None:
        view["start_ts"] = force_start_ts
    missing_schedule_time = not view.get("start_ts") and not view.get("sale_started_fn")
    missing_public_route = d.get("price_wei") is None or not (
        d.get("mint_sig") or d.get("seadrop"))
    if missing_schedule_time or (not gated and missing_public_route):
        if gated and not view.get("start_ts") and not view.get("sale_started_fn"):
            await ctx.bot.send_message(
                chat_id,
                "missing phase opening time or on-chain live-sale signal. Set a phase time before Normal or Force Schedule; Force Mint Now is separate.")
        else:
            await ctx.bot.send_message(chat_id, "missing start time or supported transaction route. fill those in first.")
        return
    if gated and view.get("end_ts") and view["end_ts"] <= int(time.time()):
        await ctx.bot.send_message(chat_id, "this gated phase has already ended according to the drop schedule.")
        return

    excluded = {a.lower() for a in d.get("wex") or []}
    entries = [(o, label, address, qty)
               for o, label, address, qty in mint_wallets(d)
               if address.lower() not in excluded]
    if not entries:
        await ctx.bot.send_message(chat_id, "⚠️ no wallets selected. pick at least one wallet before scheduling.")
        return

    if workflow in {"force", "immediate_force"}:
        workflows = [("force", entries)]
    elif gated:
        try:
            eligible_entries, force_entries, _eligibility = await _split_by_local_eligibility(d, entries)
        except Exception as exc:
            log.warning("eligibility partition failed: %s", type(exc).__name__)
            await ctx.bot.send_message(
                chat_id,
                "local eligibility could not be checked, so Normal or Both was not scheduled. "
                "Use Force Schedule to bypass this local check, or retry later.")
            return
        if workflow == "both":
            workflows = [("normal", eligible_entries), ("force", force_entries)]
        else:
            workflows = [("normal", eligible_entries)]
    else:
        # Public phases have no local eligibility gate; Both must not duplicate a mint.
        workflows = [("normal", entries)]
    workflows = [(mode, chosen) for mode, chosen in workflows if chosen]
    if not workflows:
        await ctx.bot.send_message(
            chat_id,
            "no locally eligible wallets were found for Normal Schedule. "
            "Use Force Schedule to attempt the selected wallets at the configured phase time.",
        )
        return

    selected_stage = view.get("_selected_stage")
    all_selected_addresses = {entry[2].lower() for entry in entries}
    scheduled = []
    for mode, chosen in workflows:
        mid = _next_id[0]
        _next_id[0] += 1
        record_draft = dict(d)
        for field in ("price_wei", "start_ts", "end_ts", "per_wallet"):
            record_draft[field] = view.get(field)
        if selected_stage:
            record_draft["phase"] = (
                selected_stage.get("chain_key") or selected_stage.get("key") or d.get("phase"))
            record_draft["phase_label"] = (
                selected_stage.get("label") or record_draft.get("phase_label"))
            record_draft["list_mode"] = (
                selected_stage.get("list_mode") or record_draft.get("list_mode"))
            record_draft["stage_index"] = (
                selected_stage.get("index") if selected_stage.get("index") is not None
                else d.get("phase_idx"))
            for field in ("merkle_root", "signers", "mint_site"):
                record_draft[field] = selected_stage.get(field)
        if workflow == "immediate_force":
            record_draft["start_ts"] = force_start_ts
            record_draft.pop("force_start_ts", None)
        if gated:
            record_draft["start_ts"] = view.get("start_ts")
            record_draft["end_ts"] = view.get("end_ts")
            record_draft["price_wei"] = (
                selected_stage.get("price_wei") if selected_stage else view.get("price_wei"))
            record_draft["display_price_wei"] = (
                selected_stage.get("display_price_wei") if selected_stage else None)
            record_draft["authorization_at_fire"] = False
        chosen_addresses = {entry[2].lower() for entry in chosen}
        record_excluded = excluded | (all_selected_addresses - chosen_addresses)
        record_draft["wex"] = sorted(record_excluded)
        record_draft["schedule_workflow"] = mode
        MINTS[mid] = panels.mint_record(mid, record_draft, record_excluded, ADMIN_ID)
        MINTS[mid]["source_draft_id"] = did
        MINTS[mid]["force"] = mode == "force"
        MINTS[mid]["workflow_mode"] = mode
        MINTS[mid]["gated"] = gated
        MINTS[mid]["authorization_at_fire"] = bool(
            gated and record_draft.get("authorization_at_fire"))
        MINTS[mid]["authorization_status"] = (
            "waiting" if gated and not auth_status["available"]
            else "required" if gated else "not_required")
        MINTS[mid]["phase_idx"] = d.get("phase_idx")
        MINTS[mid]["phase_label"] = selected_stage.get("label") if selected_stage else None
        MINTS[mid]["stage_index"] = (
            selected_stage.get("index") if selected_stage else d.get("phase_idx"))
        scheduled.append((mid, mode, chosen))

    persist()
    DRAFTS.pop(did, None)
    save_drafts()
    lines = []
    rows = []
    for mid, mode, chosen in scheduled:
        start_watcher(mid, ctx.application)
        label = "Force Schedule" if mode == "force" else "Normal Schedule"
        if workflow == "immediate_force":
            label = "Force Mint Now"
        fire_time = MINTS[mid].get("start_ts")
        time_text = (
            datetime.fromtimestamp(fire_time, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
            if fire_time else "on-chain sale signal")
        lines.append(
            f"📅 <b>{label} #{mid}</b> on {esc(d.get('chain', CHAIN_NAME))}: "
            f"{len(chosen)} wallet(s), at {time_text}")
        lines.append(
            ", ".join(wallets.mask(address) for _owner, _label, address, _qty in chosen))
        if gated and mode == "force":
            lines.append(
                "force will broadcast the real phase transaction at the phase time even if the wallet is not eligible. "
                "an allowlist revert is reported after the send. a public mint is not substituted.")
        elif gated:
            lines.append(
                "authorization discovery runs per wallet before/at execution; valid authorization is required and public fallback is disabled.")
        rows.append([B(f"pick wallets for #{mid}", callback_data=f"mpick:{mid}")])
    rows.append([B("my mints", callback_data="mymints")])
    await _edit_or_send(None, chat_id, msg_id, "\n".join(lines), M(rows), ctx)


def _pending_token():
    t = _tok[0]; _tok[0] += 1
    return t


async def cmd_fund(update: Update, ctx):
    uid = update.effective_user.id
    if uid != ADMIN_ID:
        return
    parts = (update.message.text or "").split()
    if len(parts) != 3:
        await update.message.reply_text("usage: /fund label|all amount in ETH, e.g. /fund all 0.01")
        return
    ws = wallets.list_wallets(uid)
    main = wallets.main_wallet(uid)
    if not main:
        await update.message.reply_text("add your main wallet in /wallets first.")
        return
    try:
        amount_wei = int(Decimal(parts[2]) * 10**18)
        if amount_wei <= 0:
            raise ValueError
    except Exception:
        await update.message.reply_text("amount must be a positive number, e.g. 0.01")
        return
    targets = [(w["label"], w["address"]) for w in ws if w["address"].lower() != main["address"].lower()
               and (parts[1].lower() == "all" or w["label"].lower() == parts[1].lower())]
    if not targets:
        await update.message.reply_text("no matching fleet wallet (use a label like Wallet 2, or all).")
        return
    chain = CFG.chains[main["chain"]]
    rpc = RPC(chain)
    bal = int(rpc.call("eth_getBalance", [main["address"], "latest"]), 16)
    plan = fleet.fund_plan(main["address"], bal, targets, amount_wei, chain, rpc)
    tok = _pending_token()
    PENDING[tok] = {"kind": "fund", "owner": uid, "plan": plan, "main": main, "chain": main["chain"]}
    text = "\n".join(panels.fund_lines(main["label"], main["address"], plan, parts[2], main["chain"]))
    kb = M([[B("confirm send", callback_data=f"fc:{tok}"), B("cancel", callback_data=f"px:{tok}")]])
    await update.message.reply_text(text, reply_markup=kb)


async def cmd_sweep(update: Update, ctx):
    uid = update.effective_user.id
    if uid != ADMIN_ID:
        return
    main = wallets.main_wallet(uid)
    if not main:
        await update.message.reply_text("add your main wallet in /wallets first.")
        return
    parts = (update.message.text or "").split()
    if len(parts) > 1 and parts[1].lower() in ("tokens", "token"):
        await cmd_sweep_tokens(update, ctx, parts)
        return
    if len(parts) > 1 and parts[1].lower() == "nft":
        parts = [parts[0]] + parts[2:]
    contract = parts[1] if len(parts) > 1 else None
    all_mode = contract and contract.lower() == "all"
    if not contract:
        fired = [m for m in MINTS.values() if m.get("status") == "fired" and m.get("chain") == main["chain"]]
        if not fired:
            await update.message.reply_text("no fired mint on this chain. use /sweep <contract address> or /sweep nft all.")
            return
        contract = fired[-1]["contract"]
    if not all_mode and not valid_address(contract):
        await update.message.reply_text("that doesn't look like a contract address.")
        return
    chain = CFG.chains[main["chain"]]
    rpc = RPC(chain)
    holders = [(w["label"], w["address"]) for w in wallets.list_wallets(uid) if w["address"].lower() != main["address"].lower()]
    contracts = fleet.discover_nft_contracts(rpc, [a for _, a in holders]) if all_mode else [contract]
    if all_mode and not contracts:
        await update.message.reply_text("no NFT contracts found in recent fleet transfers.")
        return
    rows, notes, per_contract = [], [], {}
    for c in contracts:
        r2, n2 = await asyncio.to_thread(fleet.sweep_plan, rpc, c, holders)
        rows += [(lbl, addr, ids, c) for lbl, addr, ids in r2]
        notes += n2
        per_contract[c] = sum(len(i) for _, _, i in r2)
    tok = _pending_token()
    PENDING[tok] = {"kind": "sweep", "owner": uid, "rows": rows, "contract": contracts[0], "contracts": contracts, "main": main, "chain": main["chain"]}
    if all_mode:
        text = (f"<b>NFT sweep-all to {esc(main['label'])}</b>\ncontracts: {len(contracts)}\n"
                + "\n".join(f"· <code>{c[:10]}…</code> {n} nft" for c, n in per_contract.items())
                + ("\n" + "\n".join(esc(n) for n in notes) if notes else ""))
    else:
        text = "\n".join(panels.sweep_lines(main["label"], main["address"], [(l, a, i) for l, a, i, _ in rows], notes, contracts[0]))
    kb = M([[B("confirm sweep", callback_data=f"sc:{tok}"), B("cancel", callback_data=f"px:{tok}")]])
    await update.message.reply_text(text, reply_markup=kb)


async def run_pending(chat_id, ctx, uid, token, msg_id=None):
    p = PENDING.get(token)
    if p is None or p.get("owner") != uid:
        await _edit_or_send(None, chat_id, msg_id, "that confirmation expired or isnt yours. nothing was sent.", None, ctx)
        return
    if p.get("expires") and time.time() > p["expires"]:
        PENDING.pop(token, None)
        await _edit_or_send(None, chat_id, msg_id, "that confirmation expired. nothing was sent. run the command again.", None, ctx)
        return
    PENDING.pop(token, None)
    if p["kind"] in ("tx", "tsweep"):
        await run_transfer_pending(chat_id, ctx, uid, p, token, msg_id)
        return
    elif p["kind"] == "sol_tx":
        from acobot import solana
        addresses = {plan["from"].lower() for plan in p["plans"]}
        entries = wallets.signing_entries([uid], only=addresses, chain="solana")
        secrets = {entry[2].lower(): entry[3] for entry in entries}
        await _edit_or_send(None, chat_id, msg_id, "sending SOL/SPL transfers...", None, ctx)
        lines = []
        sent = 0
        for plan in p["plans"]:
            key = secrets.get(plan["from"].lower())
            if not key:
                lines.append(
                    f"❌ {esc(plan['sender_label'])} → {esc(plan['destination_label'])}: "
                    "saved Solana key unavailable; nothing sent.")
                continue
            try:
                signature = await asyncio.to_thread(
                    solana.execute_transfer, plan, key, confirmed=True)
                sent += 1
                lines.append(
                    f"✅ {esc(plan['sender_label'])} → {esc(plan['destination_label'])}: "
                    f"submitted <code>{esc(signature)}</code>")
            except Exception as ex:
                lines.append(
                    f"❌ {esc(plan['sender_label'])} → {esc(plan['destination_label'])}: "
                    f"{esc(type(ex).__name__)}: {esc(str(ex)[:240])}")
        lines.append(f"summary: {sent} submitted, {len(p['plans']) - sent} failed")
        await _edit_or_send(None, chat_id, msg_id, "\n".join(lines), None, ctx)
        return
    elif p["kind"] == "asset_sweep":
        chain = CFG.chains[p["chain"]]
        results = await asyncio.to_thread(walletops.execute_sweep, uid, p["plan"], p["token"], chain, RPC(chain), RPC(chain, broadcast=True), confirmed=True)
        lines = [f"{'✅' if r.get('sent') else '❌'} {esc(r['label'])}: " + ("broadcast accepted" if r.get("sent") else esc(r.get("why") or "failed")) for r in results]
        lines.append(f"summary: {sum(1 for r in results if r.get('sent'))} sent, {sum(1 for r in results if not r.get('sent'))} skipped/failed")
        await _edit_or_send(None, chat_id, msg_id, "\n".join(lines), None, ctx)
        return
    elif p["kind"] == "sol_sweep":
        from acobot import solana
        addrs = {r["address"].lower() for r in p["plan"]["rows"] if r.get("ok")}
        entries = wallets.signing_entries([uid], only=addrs, chain="solana")
        secrets = {e[2].lower(): e[3] for e in entries}
        def send_native(frm, to, raw):
            key = secrets.get(frm.lower())
            if not key: raise RuntimeError("key unavailable")
            amount = format(Decimal(int(raw)) / Decimal(10**9), "f")
            plan = solana.plan_transfer(frm, to, "native", amount)
            return solana.execute_transfer(plan, key, confirmed=True)
        def send_token(frm, token_account, to, mint, raw):
            key = secrets.get(frm.lower())
            if not key: raise RuntimeError("key unavailable")
            decimals = int(p["token"]["decimals"])
            amount = format(Decimal(int(raw)) / Decimal(10**decimals), "f")
            plan = solana.plan_transfer(frm, to, mint, amount)
            return solana.execute_transfer(plan, key, confirmed=True)
        results = await asyncio.to_thread(walletops.execute_solana_sweep, uid, p["plan"], p["token"], p["plan"]["destination"], send_native=send_native, send_token=send_token, confirmed=True)
        lines = [f"{'✅' if r.get('ok') else '❌'} {esc(r['label'])}: " + ("broadcast accepted" if r.get("ok") else esc(r.get("why") or "failed")) for r in results]
        lines.append(f"summary: {sum(1 for r in results if r.get('ok'))} sent, {sum(1 for r in results if not r.get('ok'))} skipped/failed")
        await _edit_or_send(None, chat_id, msg_id, "\n".join(lines), None, ctx)
        return
    chain = CFG.chains[p["chain"]]
    read_rpc = RPC(chain)
    bcast = RPC(chain, broadcast=True)
    main = p["main"]
    await _edit_or_send(None, chat_id, msg_id, "sending...", None, ctx)
    if p["kind"] == "fund":
        res = await asyncio.to_thread(fleet.execute_fund, uid, main["label"], main["address"], p["plan"]["rows"], chain, read_rpc, bcast)
        ok = sum(1 for r in res if r["ok"])
        lines = [f"{'✅' if r['ok'] else '❌'} {esc(r['label'])} {'sent' if r['ok'] else 'failed: ' + esc(r['why'])}" for r in res]
        lines.append(f"summary: {ok} sent, {len(res) - ok} failed")
    elif p.get("contracts") and len(p["contracts"]) > 1:
        results = []
        for c in p["contracts"]:
            crows = [(lbl, addr, ids) for lbl, addr, ids, cc in p["rows"] if cc == c]
            if not crows: continue
            res2 = await asyncio.to_thread(fleet.execute_sweep, uid, c, crows, main["address"], chain, read_rpc, bcast)
            results += res2
        ok = sum(1 for r in results if r.get("ok"))
        lines = [f"{'✅' if r.get('ok') else '❌'} {esc(r['label'])} moved {r.get('moved', 0)} nft" + ("" if r.get("ok") else f" ({esc(r.get('why', ''))})") for r in results]
        lines.append(f"summary: {ok} wallets swept, {len(results) - ok} failed")
        await _edit_or_send(None, chat_id, msg_id, "\n".join(lines), None, ctx)
        return
    else:
        res = await asyncio.to_thread(fleet.execute_sweep, uid, p["contract"], [(l, a, i) for l, a, i, _ in p["rows"]] if p["rows"] and isinstance(p["rows"][0], tuple) and len(p["rows"][0]) == 4 else p["rows"], main["address"], chain, read_rpc, bcast)
        ok = sum(1 for r in res if r.get("ok"))
        lines = [f"{'✅' if r.get('ok') else '❌'} {esc(r['label'])} moved {r.get('moved', 0)} nft" + ("" if r.get("ok") else f" ({esc(r.get('why', ''))})") for r in res]
        lines.append(f"summary: {ok} wallets swept, {len(res) - ok} failed")
    await _edit_or_send(None, chat_id, msg_id, "\n".join(lines), None, ctx)


# ---------- balances, token send, token sweep (admin DM only) ----------
from acobot import tokens as tkn
from acobot import walletops


def _usd_for(chain_name):
    def f(contract, raw, dec):
        if contract is None:
            px = prices.native_usd(chain_name)
            return None if px is None else px * raw / 10**18
        px = prices.token_usd(chain_name, contract)
        return None if px is None else px * raw / 10**dec
    return f


def _admin_dm(update):
    return update.effective_user and update.effective_user.id == ADMIN_ID and is_dm(update)


def _wallet_matches_chain(w, chain_name):
    address = str(w.get("address", ""))
    declared = w.get("chain")
    if not wallets.supports_chain(declared, chain_name):
        return False
    if chain_name == "solana":
        return not address.startswith("0x") and 32 <= len(address) <= 44
    return chain_name in wallets.EVM_CHAINS and bool(ADDR_RE.fullmatch(address))


def _chain_rows(uid, chain_filter=None):
    return [(w["label"], w["address"], w["chain"]) for w in wallets.list_wallets(uid)
            if w["chain"] in (*wallets.EVM_CHAINS, "solana")
            and _wallet_matches_chain(w, chain_filter or w["chain"])
            and (chain_filter is None or _wallet_matches_chain(w, chain_filter))]


def balance_panel_text(uid, want):
    return "pick a chain from the buttons below"



def balance_kb():
    chains = list(enabled_chains())
    if "solana" in wallets.CHAINS:
        chains.append("solana")
    return M([[B(chain_label(c), callback_data=f"bal:chain:{c}")] for c in chains] + [[B("home", callback_data="home")]])



def _balance_wallets(uid, chain_name):
    return [w for w in wallets.list_wallets(uid) if _wallet_matches_chain(w, chain_name)]


def _balance_wallet_label(wallet, index):
    address = wallet.get("address", "")
    short = (address if len(address) <= 12 else f"{address[:6]}..{address[-4:]}")
    return f"W{index + 1} {short}"


def _balance_wallet_picker_kb(chain_name, wallet_rows, selected=None, page=0):
    rows = []
    page_size = 8
    start = page * page_size
    selected_set = None if selected is None else {str(a).lower() for a in selected}
    for index in range(start, min(start + page_size, len(wallet_rows))):
        wallet = wallet_rows[index]
        checked = selected_set is not None and wallet["address"].lower() in selected_set
        label = ("✓ " if checked else "") + _balance_wallet_label(wallet, index)
        rows.append([B(label, callback_data=f"bv:toggle:{chain_name}:{index}")])
    nav = []
    if page > 0: nav.append(B("‹ prev", callback_data=f"bv:page:{chain_name}:{page - 1}"))
    if start + page_size < len(wallet_rows): nav.append(B("more ›", callback_data=f"bv:page:{chain_name}:{page + 1}"))
    if nav: rows.append(nav)
    rows.append([B("all wallets" + (" ✓" if selected is None else ""), callback_data=f"bv:all:{chain_name}")])
    rows.append([B("show balances", callback_data=f"bv:balances:{chain_name}")])
    return M(rows)


async def _show_balance_wallet_picker(chat_id, ctx, uid, msg_id=None):
    state = ctx.user_data.get("balance_view", {})
    chain_name = state.get("chain")
    ws = _balance_wallets(uid, chain_name)
    if not ws:
        text = f"no compatible wallets on {esc(chain_name)}."
        kb = _balance_asset_kb(chain_name)
    else:
        page = int(state.get("page", 0))
        kb = _balance_wallet_picker_kb(chain_name, ws, state.get("selected"), page)
        text = f"<b>balances · {esc(chain_name)}</b>\npick wallets"
    await _edit_or_send(None, chat_id, msg_id, text, kb, ctx)


async def _balance_chain_text(uid, chain_name, token_text, selected_addresses=None):
    ws = _balance_wallets(uid, chain_name)
    ws = walletops.filter_selected_wallets(ws, selected_addresses)
    if not ws:
        return "no wallets selected.", _balance_asset_kb(chain_name)
    if chain_name == "solana":
        from acobot import solana
        if token_text.lower() in ("native", "sol"):
            def reader(a):
                raw = solana.get_balance(a)
                usd_px = prices.native_usd("solana")
                amount = raw / 1e9
                return {"amount": amount, "symbol": "SOL", "usd": amount * usd_px if usd_px is not None else None}
            sym = "SOL"
        else:
            def reader(a):
                snap = solana.get_token_balance(a, token_text)
                return {"amount": snap["amount"], "symbol": snap["symbol"], "usd": None}
            sym = "SPL"
    else:
        rpc = RPC(CFG.chains[chain_name])
        try:
            tk = tkn.resolve(rpc, chain_name, token_text)
        except ValueError as ex:
            return f"token not available on {chain_name}: {esc(str(ex))}", _balance_asset_kb(chain_name)
        reader = lambda a: walletops.evm_token_snapshot(rpc, chain_name, a, tk, _usd_for(chain_name))
        sym = tk["symbol"]
    from acobot.walletops import aggregate_balances
    agg = aggregate_balances([(w["label"], w["address"]) for w in ws], reader)
    lines = [f"<b>{esc(chain_name)} · {esc(sym)}</b>", f"total: {agg['total']:,.6f} {esc(sym)}" + (f" · ${agg['usd_total']:,.2f}" if agg["usd_total"] is not None else ""), ""]
    for r in agg["rows"]:
        if r["error"]:
            lines.append(f"{esc(r['label'])} · balance unavailable")
        else:
            line = f"{esc(r['label'])} · <code>{esc(wallets.mask(r['address']))}</code>\n{r['amount']:,.6f} {esc(r['symbol'])}"
            if r["usd"] is not None: line += f" · ${r['usd']:,.2f}"
            lines.append(line)
    return "\n".join(lines), _balance_asset_kb(chain_name)


def _saved_tokens(chain_name):
    return list((getattr(CFG, "saved_tokens", {}) or {}).get(chain_name, []))


def _balance_asset_kb(chain_name):
    rows = [[B("native", callback_data=f"bv:show:{chain_name}:native")]]
    saved = _saved_tokens(chain_name)
    for i, t in enumerate(saved[:8]): rows.append([B(t["symbol"], callback_data=f"bv:show:{chain_name}:saved{i}")])
    rows += [[B("paste token CA / mint", callback_data=f"bv:paste:{chain_name}")], [B("back", callback_data="bal:chains")]]
    return M(rows)


async def cmd_sweepall(update: Update, ctx):
    if not _admin_dm(update): return
    ctx.user_data["sweepall"] = {"step": "chain"}
    chains = list(enabled_chains())
    if "solana" in wallets.CHAINS:
        chains.append("solana")
    kb = M([[B(chain_label(c), callback_data=f"sw:chain:{c}")] for c in chains])
    await update.message.reply_text("<b>sweep all</b>\npick the source chain", reply_markup=kb)


def _sweep_asset_kb(chain_name):
    rows = [[B("native", callback_data=f"sw:asset:{chain_name}:native"),
             B("NFT", callback_data=f"sw:asset:{chain_name}:nft")]]
    for i, t in enumerate(_saved_tokens(chain_name)[:8]): rows.append([B(t["symbol"], callback_data=f"sw:asset:{chain_name}:saved{i}")])
    rows += [[B("paste token CA / mint", callback_data=f"sw:paste:{chain_name}")]]
    return M(rows)


async def _make_sweep_preview(chat_id, ctx, uid, chain_name, asset, destination, msg_id=None, source=None, nft_ca=None):
    if asset == "nft" and nft_ca:
        holders = [(w["label"], w["address"]) for w in wallets.list_wallets(uid)
                   if wallets.supports_chain(w["chain"], chain_name)
                   and (not source or source == "all" or w["address"].lower() == source)]
        rpc = RPC(CFG.chains[chain_name])
        main = wallets.main_wallet(uid)
        r2, n2 = await asyncio.to_thread(fleet.sweep_plan, rpc, nft_ca, holders)
        tok = _pending_token()
        PENDING[tok] = {"kind": "sweep", "owner": uid, "rows": [(l, a, i, nft_ca) for l, a, i in r2],
                        "contract": nft_ca, "contracts": [nft_ca], "main": main, "chain": chain_name}
        text = "\n".join(panels.sweep_lines(main["label"], main["address"], r2, n2, nft_ca))
        kb = M([[B("confirm sweep", callback_data=f"sc:{tok}"), B("cancel", callback_data=f"px:{tok}")]])
        if msg_id:
            await _edit_or_send(None, chat_id, msg_id, text, kb, ctx)
        else:
            await ctx.bot.send_message(chat_id, text, reply_markup=kb)
        return
    ws = [w for w in wallets.list_wallets(uid) if _wallet_matches_chain(w, chain_name)]
    if not ws:
        await ctx.bot.send_message(chat_id, f"no compatible wallets on {chain_name}."); return
    if chain_name == "solana":
        from acobot import solana
        main = wallets.main_wallet(uid)
        native = asset.lower() in ("native", "sol", "eth")
        if native:
            token = {"kind": "native", "symbol": "SOL", "decimals": 9}
            plan = await asyncio.to_thread(walletops.plan_solana_sweep,
                [(w["label"], w["address"]) for w in ws], destination, token,
                get_balance=solana.get_balance, get_token_balance=solana.get_token_balance,
                rent_exempt=solana.get_rent_exempt_minimum)
        else:
            try:
                snap = solana.get_token_balance(ws[0]["address"], asset)
            except Exception as ex:
                await ctx.bot.send_message(chat_id, f"token mint not readable on solana ({type(ex).__name__}). no transaction prepared."); return
            token = {"kind": "spl", "symbol": snap["symbol"], "decimals": snap["decimals"], "mint": asset}
            plan = await asyncio.to_thread(walletops.plan_solana_sweep,
                [(w["label"], w["address"]) for w in ws], destination, token,
                get_balance=solana.get_balance, get_token_balance=solana.get_token_balance,
                rent_exempt=solana.get_rent_exempt_minimum)
        amount = plan["total_raw"] / (10 ** token["decimals"])
        gas_display = f"{plan['gas_lamports'] / 1e9:.6f} SOL"
        text_lines = ["<b>sweep all · review</b>", f"asset: {esc(token['symbol'])}", f"total amount: {amount:,.6f} {esc(token['symbol'])}", f"wallets: {plan['wallet_count']} of {len(ws)}", f"destination: <code>{esc(wallets.mask(destination))}</code>", f"estimated total gas: {gas_display}"]
        if plan["skipped"]:
            text_lines += ["\nskipped:"] + [f"{esc(r['label'])}: {esc(r['why'])}" for r in plan["skipped"]]
        if not plan["wallet_count"]:
            await ctx.bot.send_message(chat_id, "\n".join(text_lines) + "\n\nnothing can be swept."); return
        token_id = _pending_token()
        PENDING[token_id] = {"kind": "sol_sweep", "owner": uid, "chain": chain_name, "token": token, "plan": plan}
        await ctx.bot.send_message(chat_id, "\n".join(text_lines), reply_markup=M([[B("review final confirmation", callback_data=f"sw:review:{token_id}"), B("cancel", callback_data=f"px:{token_id}")]]))
        return
    chain = CFG.chains[chain_name]; rpc = RPC(chain)
    try: token = tkn.resolve(rpc, chain_name, asset)
    except ValueError as ex:
        await ctx.bot.send_message(chat_id, str(ex)); return
    from acobot.walletops import plan_sweep
    plan = await asyncio.to_thread(plan_sweep, rpc, chain, token, [(w["label"], w["address"]) for w in ws], destination)
    good = plan["wallet_count"]
    amount = tkn.fmt_amount(plan["total_raw"], token["decimals"])
    gas_eth = plan["gas_wei"] / 1e18
    gas_usd = prices.native_usd(chain_name)
    gas_display = f"{gas_eth:.6f} {tkn.NATIVE_SYMBOL.get(chain_name, 'ETH')}" + (f" · ${gas_eth * gas_usd:,.2f}" if gas_usd is not None else "")
    lines = ["<b>sweep all · review</b>", f"asset: {esc(token['symbol'])}", f"total amount: {amount} {esc(token['symbol'])}", f"wallets: {good} of {len(ws)}", f"destination: <code>{esc(wallets.mask(destination))}</code>", f"estimated total gas: {gas_display}"]
    if plan["skipped"]:
        lines.append("\nskipped:")
        lines += [f"• {esc(r['label'])}: {esc(r.get('why') or 'not eligible')}" for r in plan["skipped"]]
    if not good:
        await ctx.bot.send_message(chat_id, "\n".join(lines) + "\n\nnothing can be swept."); return
    token_id = _pending_token()
    PENDING[token_id] = {"kind":"asset_sweep", "owner":uid, "chain":chain_name, "token":token, "plan":plan}
    await ctx.bot.send_message(chat_id, "\n".join(lines), reply_markup=M([[B("review final confirmation", callback_data=f"sw:review:{token_id}"), B("cancel", callback_data=f"px:{token_id}")]]))


async def cmd_balance(update: Update, ctx):
    if not _admin_dm(update):
        return
    uid = update.effective_user.id
    if not wallets.list_wallets(uid):
        await update.message.reply_text(
            "📭 no wallets yet.\n👉 tap <b>add wallet</b> to load your first key.",
            reply_markup=M([[B("👛 add wallet", callback_data="w:add"), B("🏠 main", callback_data="home")]]))
        return
    await update.message.reply_text("<b>balance viewer</b>\npick a chain", reply_markup=balance_kb())


async def show_balance(chat_id, ctx, uid, want, msg_id=None):
    chain_name = want if want in (*enabled_chains(), "solana") else None
    if chain_name:
        text = f"<b>balances · {esc(chain_name)}</b>\npick an asset"
        kb = _balance_asset_kb(chain_name)
    else:
        text, kb = "<b>balance viewer</b>\npick a chain", balance_kb()
    await _edit_or_send(None, chat_id, msg_id, text, kb, ctx)


def _parse_send_args(parts):
    """/send <wallet|all> <amount|all> <token> <main|address>  ->  dict or raises ValueError."""
    if len(parts) != 5:
        raise ValueError("usage: /send wallet|all amount|all token|native|CA main|address\n"
                         "e.g. /send all all usdc main, /send \"Wallet 2\" 0.1 native 0xabc...")
    return {"who": parts[1], "amount": parts[2], "token": parts[3], "to": parts[4]}


def _send_wizard_destinations(uid, pick, chain_name, source_addresses):
    from acobot import solana

    source_addresses = {a.lower() for a in source_addresses}
    if chain_name == "solana":
        compatible = lambda w: w["chain"] == "solana" and solana.valid_address(w["address"])
    elif chain_name in wallets.EVM_CHAINS and CFG is not None and chain_name in CFG.chains:
        compatible = lambda w: w["chain"] in wallets.EVM_CHAINS and bool(ADDR_RE.fullmatch(w["address"]))
    else:
        raise ValueError(f"unsupported send chain: {chain_name}")

    if pick == "main":
        main = wallets.main_wallet_for(uid, chain_name)
        if not main:
            raise ValueError("no main wallet set. /wallets first.")
        destinations = [(main["address"], "main " + wallets.mask(main["address"]))]
    elif pick == "all":
        destinations, seen = [], set()
        for wallet in wallets.list_wallets(uid):
            address = wallet["address"]
            if (not compatible(wallet)
                    or address.lower() in source_addresses or address.lower() in seen):
                continue
            seen.add(address.lower())
            destinations.append((address, wallet["label"]))
    elif pick.startswith("multi:"):
        selected = [a for a in pick[6:].split(",") if a]
        if len(selected) != len(set(a.lower() for a in selected)):
            raise ValueError("duplicate destination wallet selected.")
        by_address = {
            w["address"].lower(): w for w in wallets.list_wallets(uid)
            if compatible(w)
        }
        destinations = []
        for address in selected:
            wallet = by_address.get(address.lower())
            if not wallet:
                raise ValueError("a selected destination wallet is no longer available.")
            destinations.append((wallet["address"], wallet["label"]))
    else:
        if chain_name == "solana":
            if not solana.valid_address(pick):
                raise ValueError("enter a valid Solana wallet address.")
            address, label = pick, wallets.mask(pick)
        else:
            address, label = _resolve_to(uid, pick, wallets.main_wallet_for(uid, chain_name))
        destinations = [(address, label)]

    if not destinations:
        raise ValueError("no compatible destination wallets selected.")
    if any(address.lower() in source_addresses for address, _ in destinations):
        raise ValueError("destination cannot be the same as a source wallet.")
    return destinations


def _send_wizard_sources(uid, pick, chain_name):
    from acobot import solana

    def compatible(wallet):
        if chain_name == "solana":
            return wallet["chain"] == "solana" and solana.valid_address(wallet["address"])
        return (chain_name in wallets.EVM_CHAINS and CFG is not None
                and chain_name in CFG.chains
                and wallet["chain"] in wallets.EVM_CHAINS
                and bool(ADDR_RE.fullmatch(wallet["address"])))

    if pick == "main":
        wallet = wallets.main_wallet_for(uid, chain_name)
        if not wallet or not compatible(wallet):
            raise ValueError(f"no compatible main wallet for {chain_name}. add one in /wallets.")
        return [(wallet["label"], wallet["address"], chain_name)]
    if pick == "all":
        out, seen = [], set()
        for wallet in wallets.list_wallets(uid):
            address = wallet["address"].lower()
            if compatible(wallet) and address not in seen:
                seen.add(address)
                out.append((wallet["label"], wallet["address"], chain_name))
        return out

    for wallet in wallets.list_wallets(uid):
        if compatible(wallet) and (
                wallet["address"].lower() == pick.lower()
                or wallet["label"].lower() == pick.lower()
                or wallet["label"].replace(" ", "").lower() == pick.replace(" ", "").lower()):
            return [(wallet["label"], wallet["address"], chain_name)]
    raise ValueError(f"selected source wallet is not available on {chain_name}.")


def _send_amounts_per_recipient(amount, recipient_count, decimals):
    if recipient_count <= 0:
        raise ValueError("select at least one destination wallet.")
    if amount.lower() == "all":
        if recipient_count > 1:
            raise ValueError("enter a specific amount to split between multiple wallets; 'all' cannot be split safely.")
        return ["all"]
    value = Decimal(amount)
    if not value.is_finite() or value <= 0:
        raise ValueError("amount must be a positive number.")
    scale = 10 ** decimals
    raw = value * scale
    if raw != raw.to_integral_value():
        raise ValueError(f"amount supports at most {decimals} decimal places.")
    share, remainder = divmod(int(raw), recipient_count)
    if share == 0:
        raise ValueError("amount is too small to split across those wallets.")
    return [format(Decimal(share + (1 if i < remainder else 0)) / scale, "f")
            for i in range(recipient_count)]


def _select_wallets(uid, who, main_addr):
    ws, seen = [], set()
    for wallet in wallets.list_wallets(uid):
        address = wallet["address"].lower()
        if address == (main_addr or "").lower() or address in seen:
            continue
        seen.add(address)
        ws.append(wallet)
    if who.lower() == "all":
        return ws
    sel = [w for w in ws if w["address"].lower() == who.lower()]
    if not sel:
        sel = [w for w in ws if w["label"].lower() == who.lower() or w["label"].lower().replace(" ", "") == who.lower().replace(" ", "")]
    if not sel and who.isdigit():
        sel = [w for w in ws if w["label"].split()[-1] == who]
    return sel


def _resolve_to(uid, to_text, main):
    if to_text.lower() == "main":
        if not main:
            raise ValueError("no main wallet set. add one in /wallets")
        return main["address"], "main " + wallets.mask(main["address"])
    if not valid_address(to_text):
        raise ValueError("'to' must be main or a valid 0x address")
    return cs(to_text), wallets.mask(cs(to_text))


async def _plan_groups(uid, senders_by_chain, token_text, amount_text, to_addr, speed="fast"):
    """Per chain: resolve token, plan rows. Returns (groups, notes)."""
    groups, notes = [], []
    for chain_name, senders in senders_by_chain.items():
        chain = CFG.chains[chain_name]
        rpc = RPC(chain)
        try:
            token = tkn.resolve(rpc, chain_name, token_text)
        except ValueError as ex:
            notes.append(f"{chain_name}: {ex}")
            continue
        rows = fleet.plan_transfers(rpc, chain, token, senders, to_addr, amount_text, speed)
        groups.append({"chain": chain_name, "token": token, "rows": rows, "speed": speed})
    return groups, notes


def _tx_text(title, to_label, groups, notes):
    out = [f"<b>{esc(title)}</b>", ""]
    total = 0
    for g in groups:
        out.append(f"<b>⛓ {esc(g['chain'].title())}</b> · {esc(g['token']['symbol'])}")
        for r in g["rows"]:
            if r.get("ok"):
                total += 1
                out.append(f"  ✅ {esc(r['label'])} <code>{esc(r['masked'])}</code>: {esc(panels.fmt_amount_safe(r['raw'], r['decimals']))} {esc(r['symbol'])} → {esc(to_label)}")
            else:
                out.append(f"  ❌ {esc(r['label'])} <code>{esc(r['masked'])}</code>")
                out.append(f"     {esc(r.get('why') or 'no balance')}")
        out.append("")
    if total:
        out.append(f"<b>{total} transfer{'s' if total != 1 else ''} ready</b>")
    else:
        out.append("⚠️ <b>nothing to send</b>\n👉 top up the wallet with /fund and try again")
    for n in notes:
        out.append("⚠️ " + esc(n))
    return out


async def cmd_send(update: Update, ctx):
    if not _admin_dm(update):
        return
    uid = update.effective_user.id
    try:
        raw = shlex.split(update.message.text or "")
    except ValueError:
        await update.message.reply_text("couldnt parse command. quote wallet labels with spaces, e.g. /send \"Wallet 2\" 0.1 native main")
        return
    if len(raw) == 1:
        # guided wizard: source -> token -> amount -> destinations -> gas -> confirm
        rows = [[B("main wallet", callback_data="snd:src:main"), B("all fleet wallets", callback_data="snd:src:all")]]
        seen0 = set()
        for w in wallets.list_wallets(uid):
            a0 = (w["address"] or "").lower()
            if a0 in seen0:
                continue
            seen0.add(a0)
            rows.append([B(f"{w['label']} ({wallets.mask(w['address'])})", callback_data=f"snd:src:{w['address']}")])
        ctx.user_data["send_wiz"] = {"step": "src"}
        await update.message.reply_text("send from which wallet?", reply_markup=M(rows))
        return
    try:
        a = _parse_send_args(raw)
        who = a["who"]
        main = wallets.main_wallet(uid)
        if who.lower() == "main" or (
                main and who.lower() in (main["address"].lower(), main["label"].lower())):
            senders = [main] if main else []
        else:
            senders = _select_wallets(uid, who, main["address"] if main else None)
        if not senders:
            raise ValueError("no matching wallet (use Wallet 2, main, or all)")
        source_chains = {wallet["chain"] for wallet in senders}
        if len(source_chains) != 1:
            raise ValueError("direct send needs source wallets from one chain; use /send to choose a chain.")
        chain_name = source_chains.pop()
        if chain_name == "solana":
            from acobot import solana
            if a["to"].lower() == "main":
                destination_wallet = wallets.main_wallet_for(uid, chain_name)
                if not destination_wallet:
                    raise ValueError("no Solana main wallet set.")
                to_addr, to_label = destination_wallet["address"], "main " + wallets.mask(destination_wallet["address"])
            elif solana.valid_address(a["to"]):
                to_addr, to_label = a["to"], wallets.mask(a["to"])
            else:
                raise ValueError("'to' must be main or a valid Solana address")
        else:
            if chain_name not in CFG.chains:
                raise ValueError(f"{chain_name} is not enabled.")
            to_addr, to_label = _resolve_to(
                uid, a["to"], wallets.main_wallet_for(uid, chain_name))
        if any(sender["address"].lower() == to_addr.lower() for sender in senders):
            raise ValueError("destination cannot be the same as a source wallet.")
        if a["amount"].lower() != "all":
            try:
                amount_value = Decimal(a["amount"])
            except InvalidOperation as ex:
                raise ValueError("amount must be a positive number or 'all'") from ex
            if not amount_value.is_finite() or amount_value <= 0:
                raise ValueError("amount must be a positive number or 'all'")
    except ValueError as ex:
        await update.message.reply_text(str(ex))
        return

    if chain_name == "solana":
        from acobot import solana
        plans, errors = [], []
        for sender in senders:
            try:
                plan = await asyncio.to_thread(
                    solana.plan_transfer, sender["address"], to_addr,
                    a["token"], a["amount"])
                plan["sender_label"] = sender["label"]
                plan["destination_label"] = to_label
                plans.append(plan)
            except Exception as ex:
                log.warning("Solana send preview failed for %s: %s", sender["label"], ex)
                errors.append(f"{sender['label']}: {ex}")
        if not plans:
            await update.message.reply_text("\n".join(errors) or "nothing to send.")
            return
        preview = [
            "SOL send preview",
            *[f"{plan['sender_label']} → {to_label}: "
              f"{plan['amount'] / (10 ** plan['decimals']):,.{min(plan['decimals'], 9)}f} "
              f"{plan['symbol']} · fee ~{plan['fee_lamports'] / 1e9:.6f} SOL"
              + (f" · ATA rent {plan['rent_lamports'] / 1e9:.6f} SOL"
                 if plan["rent_lamports"] else "")
              for plan in plans],
        ]
        if errors:
            preview.extend(["not prepared:"] + errors)
        tok = _pending_token()
        PENDING[tok] = {"kind": "sol_tx", "owner": uid, "plans": plans,
                        "expires": time.time() + 300}
        kb = M([[B("confirm SOL send", callback_data=f"sc:{tok}"),
                 B("cancel", callback_data=f"px:{tok}")]])
        await update.message.reply_text("\n".join(preview), reply_markup=kb)
        return

    by_chain = {}
    for w in senders:
        by_chain.setdefault(chain_name, []).append((w["label"], w["address"]))
    try:
        groups, notes = await asyncio.to_thread(
            _plan_groups_sync, by_chain, a["token"], a["amount"], to_addr)
    except Exception as ex:
        log.exception("send preview failed")
        await update.message.reply_text(
            f"could not prepare send preview ({type(ex).__name__}): {str(ex)[:240]}")
        return
    if not groups or not any(r.get("ok") for g in groups for r in g["rows"]):
        await update.message.reply_text("\n".join(_tx_text("send", to_label, groups, notes)))
        return
    tok = _pending_token()
    PENDING[tok] = {"kind": "tx", "owner": uid, "groups": groups, "to": to_addr, "expires": time.time() + 300}
    kb = M([[B("confirm send", callback_data=f"sc:{tok}"), B("cancel", callback_data=f"px:{tok}")]])
    await update.message.reply_text("\n".join(_tx_text("send preview", to_label, groups, notes)), reply_markup=kb)


def _plan_groups_sync(by_chain, token_text, amount_text, to_addr, speed="fast"):
    groups, notes = [], []
    for chain_name, senders in by_chain.items():
        chain = CFG.chains[chain_name]
        rpc = RPC(chain)
        try:
            token = tkn.resolve(rpc, chain_name, token_text)
        except ValueError as ex:
            notes.append(f"{chain_name}: {ex}")
            continue
        try:
            rows = fleet.plan_transfers(rpc, chain, token, senders, to_addr, amount_text, speed)
        except ValueError as ex:
            notes.append(f"{chain_name}: {ex}")
            continue
        groups.append({"chain": chain_name, "token": token, "rows": rows, "speed": speed})
    return groups, notes


async def cmd_sweep_tokens(update: Update, ctx, parts):
    uid = update.effective_user.id
    main = wallets.main_wallet(uid)
    if not main:
        await update.message.reply_text("add your main wallet in /wallets first.")
        return
    sel = parts[2] if len(parts) > 2 else "all"
    holders = [(w["label"], w["address"], w["chain"]) for w in wallets.list_wallets(uid)
               if w["address"].lower() != main["address"].lower()]
    if not holders:
        await update.message.reply_text("no fleet wallets besides main.")
        return
    by_chain = {}
    for label, addr, chain in holders:
        by_chain.setdefault(chain, []).append((label, addr))
    to_addr = main["address"]

    def build():
        groups, notes = [], []
        for chain_name, hs in by_chain.items():
            chain = CFG.chains[chain_name]
            rpc = RPC(chain)
            if sel.lower() == "all":
                metas = [{"contract": t["contract"], "symbol": t["symbol"],
                          "decimals": t["decimals"] if t["decimals"] is not None else tkn.read_erc20(rpc, t["contract"])[1]}
                         for t in tkn.tracked(chain_name)]
            else:
                try:
                    tk = tkn.resolve(rpc, chain_name, sel)
                except ValueError as ex:
                    notes.append(f"{chain_name}: {ex}")
                    continue
                if tk["kind"] != "erc20":
                    notes.append(f"{chain_name}: native is not swept here, use /send all native main")
                    continue
                metas = [{"contract": tk["contract"], "symbol": tk["symbol"], "decimals": tk["decimals"]}]
            rows, n = fleet.plan_token_sweep(rpc, chain, metas, hs, to_addr)
            for r in rows:
                r["chain"] = chain_name
            notes += n
            groups.append({"chain": chain_name, "rows": rows})
        return groups, notes

    groups, notes = await asyncio.to_thread(build)
    rows = [r for g in groups for r in g["rows"]]
    tok = _pending_token()
    PENDING[tok] = {"kind": "tsweep", "owner": uid, "rows": rows, "to": to_addr, "main": main, "expires": time.time() + 300}
    lines = [f"<b>sweep tokens to main</b> {esc(main['label'])} {esc(wallets.mask(main['address']))}", f"mode: {esc(sel)}", ""]
    for r in rows:
        lines.append(f"→ {esc(r['label'])} {esc(r['masked'])} ({esc(r['chain'])})  {esc(panels.fmt_amount_safe(r['raw'], r['decimals']))} {esc(r['symbol'])}")
    if not rows:
        lines.append("nothing to sweep.")
    else:
        lines += ["", f"transfers: {len(rows)}"]
    for n in notes:
        lines.append("⚠️ " + esc(n))
    kb = M([[B("confirm sweep", callback_data=f"sc:{tok}"), B("cancel", callback_data=f"px:{tok}")]])
    if not rows:
        PENDING.pop(tok, None)
        await update.message.reply_text("\n".join(lines))
        return
    await update.message.reply_text("\n".join(lines), reply_markup=kb)


async def run_transfer_pending(chat_id, ctx, uid, p, token, msg_id):
    await _edit_or_send(None, chat_id, msg_id, "sending...", None, ctx)
    all_res = []
    if p["kind"] == "tx":
        for g in p["groups"]:
            chain = CFG.chains[g["chain"]]
            res = await asyncio.to_thread(fleet.execute_transfers, uid, g["rows"], g["token"], chain,
                                          RPC(chain), RPC(chain, broadcast=True), g.get("speed", "fast"))
            all_res += res
    else:
        by_chain = {}
        for r in p["rows"]:
            by_chain.setdefault(r["chain"], []).append(r)
        for cname, rows in by_chain.items():
            chain = CFG.chains[cname]
            res = await asyncio.to_thread(fleet.execute_token_sweep, uid, rows, chain,
                                          RPC(chain), RPC(chain, broadcast=True))
            all_res += res
    await _edit_or_send(None, chat_id, msg_id, "\n".join(panels.receipt_lines(all_res)), None, ctx)


# ---------- portfolio, watchlist, listing sniper ----------
PORTFOLIO_FILE = DATA / "portfolio.json"
WATCH_FILE = DATA / "watchlist.json"
SNIPE_FILE = DATA / "snipeprice.json"
SELL_TARGETS_FILE = DATA / "sell_targets.json"
try:
    SELL_TARGETS = json.loads(SELL_TARGETS_FILE.read_text()) if SELL_TARGETS_FILE.exists() else []
except Exception:
    SELL_TARGETS = []
SNIPE_PENDING = {}     # short key -> listing dict (confirm step)
SNIPE_SEEN_LOCK = threading.Lock()
SNIPE_STARTED = [False]


def _admin_only(update):
    return update.effective_user and update.effective_user.id == ADMIN_ID


async def cmd_portfolio(update: Update, ctx):
    if not (allowed(update.effective_user.id) or username_allowed(update.effective_user.username)):
        return
    entries = portfolio.load(PORTFOLIO_FILE)
    def floor_fn(chain, contract):
        return portfolio.fetch_floor(chain, contract)
    report = await asyncio.to_thread(portfolio.build_report, entries, floor_fn, prices.native_usd)
    if not entries:
        text = "\n".join(panels.header("portfolio") + [
            "📭 nothing held yet.",
            "👉 mints you land show up here with pnl at floor.",
        ])
        await ctx.bot.send_message(update.effective_chat.id, text,
                                   reply_markup=M([[B("📅 my mints", callback_data="m:list"), B("🏠 main", callback_data="home")]]))
        return
    text = "\n".join(panels.header("portfolio") + ["<pre>" + esc("\n".join(portfolio.panel_lines(report, panels.mask))) + "</pre>",
                       "", "pnl = held x floor now (usd) minus mint price and gas paid (usd)."])
    await ctx.bot.send_message(update.effective_chat.id, text,
                               reply_markup=M([[B("🔄 refresh", callback_data="pf:open"), B("🏠 main", callback_data="home")]]))


def _alpha_rows(mode):
    events = alp.read_events()
    if mode == "up":
        rows = [(pnl, e) for pnl, e, _f in alp.best_buys(events, alp.fetch_floor)]
    elif mode == "new":
        rows = [(0, e) for e in alp.upcoming_mints(events)]
    else:
        rows = [(0, e) for e in events]
    return rows


def alpha_list_text(rows, mode):
    head = {"recent": "copy alpha · recent", "up": "copy alpha · best buys", "new": "copy alpha · upcoming mints"}[mode]
    lines = panels.header(head)
    if not rows:
        empty = {"recent": "no tracked trades yet. they show up here as soon as a tracked wallet trades.",
                 "up": "no buys with a price in the feed yet.",
                 "new": "no mints by tracked wallets yet."}[mode]
        lines.append("📭 " + empty)
    else:
        lines.append(f"{len(rows)} entries, newest first. tap one for the card.")
    return "\n".join(lines)


def alpha_list_kb(rows, mode):
    kb = []
    for _p, e in rows[:25]:
        kb.append([B(alp.entry_label(e)[:60], callback_data=f"alp:e:{e['id']}")])
    kb.append([B("recent", callback_data="alp:list:recent"), B("best buys", callback_data="alp:list:up"),
               B("new mints", callback_data="alp:list:new")])
    kb.append([B("🏠 main", callback_data="home")])
    return M(kb)


def alpha_card(e, is_admin=False):
    """Card text plus buttons: the add-to-mint draft button (admin only, valid CA on an enabled chain)."""
    price_usd = None
    try:
        price_usd = prices.native_usd(e["chain"])
    except Exception:
        price_usd = None
    floor = alp.fetch_floor(e["chain"], e.get("ca")) if e["chain"] in alp.RESERVOIR else None
    text = alp.card(e, floor_native=floor, usd=price_usd)
    rows = []
    if is_admin and alp.draft_contract(e.get("ca"), e["chain"], enabled_chains()):
        rows.append([B("🎯 add to mint", callback_data=f"alp:mint:{e['id']}")])
    rows.append([B("⬅ back to alpha", callback_data="alp:list:recent"), B("🏠 main", callback_data="home")])
    return text, M(rows)


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update.effective_user.id):
        return
    h = _health_panel()
    await update.effective_message.reply_text(h, parse_mode=ParseMode.HTML)


def _health_panel():
    up = int(time.time() - BOT_STARTED)
    lines = [f"<b>bot status</b>", f"uptime: {up // 3600}h {(up % 3600) // 60}m"]
    for cn, f in _FEEDS.items():
        bt = f"{f.block_time_s:.2f}s" if f.block_time_s else "n/a"
        state = "live" if f.available() else "down"
        err = f" | last err: {html.escape(f.last_error)}" if f.last_error else ""
        lines.append(f"{html.escape(cn)}: ws {state}, block {bt}{err}")
    if not _FEEDS:
        lines.append("no chain feeds started yet")
    return "\n".join(lines)


async def cmd_alpha(update: Update, ctx):
    uid = update.effective_user.id
    if not (allowed(uid) or username_allowed(update.effective_user.username)):
        await update.message.reply_text("not on the crew list. ask anish to /allow you.")
        return
    arg = (ctx.args[0] if ctx.args else "recent").lower()
    mode = {"up": "up", "new": "new"}.get(arg, "recent")
    if mode == "new" and uid != ADMIN_ID:
        await update.message.reply_text("/alpha new is admin only.")
        return
    rows = await asyncio.to_thread(_alpha_rows, mode)
    await update.message.reply_text(alpha_list_text(rows, mode), reply_markup=alpha_list_kb(rows, mode))


async def on_alpha_cb(update: Update, ctx):
    q = update.callback_query
    uid = q.from_user.id
    if not (allowed(uid) or username_allowed(q.from_user.username)):
        await q.answer("not on the crew list.")
        return
    data = q.data or ""
    if data.startswith("alp:list:"):
        await q.answer()
        mode = data.split(":")[2]
        mode = mode if mode in ("recent", "up", "new") else "recent"
        if mode == "new" and uid != ADMIN_ID:
            await q.answer("admin only.")
            return
        rows = await asyncio.to_thread(_alpha_rows, mode)
        try:
            await q.edit_message_text(alpha_list_text(rows, mode), reply_markup=alpha_list_kb(rows, mode))
        except BadRequest:
            await q.message.reply_text(alpha_list_text(rows, mode), reply_markup=alpha_list_kb(rows, mode))
        return
    if data.startswith("alp:e:"):
        await q.answer()
        eid = data.split(":")[2]
        e = await asyncio.to_thread(alp.get_event, eid)
        if not e:
            await q.message.reply_text("that trade is gone from the feed. tap /alpha for the list.")
            return
        text, kb = alpha_card(e, is_admin=(uid == ADMIN_ID))
        await q.message.reply_text(text, reply_markup=kb, disable_web_page_preview=True)
        return
    if data.startswith("alp:mint:"):
        if uid != ADMIN_ID:
            await q.answer("admin only.")
            return
        await q.answer()
        eid = data.split(":")[2]
        e = await asyncio.to_thread(alp.get_event, eid)
        ca = alp.draft_contract(e.get("ca") if e else None, e["chain"] if e else "", enabled_chains())
        if not ca:
            await q.message.reply_text("no valid contract on this trade to add to mint.")
            return
        ctx.user_data.pop("await", None)
        await start_draft(q.message.chat_id, ctx, ca, chain_name=e["chain"])
        return
    await q.answer()


async def cmd_watchlist(update: Update, ctx):
    uid = update.effective_user.id
    if not allowed(uid):
        return
    args = ctx.args or []
    rows = watchlist.load(WATCH_FILE)
    if args and args[0].lower() == "add":
        if uid != ADMIN_ID:
            await update.message.reply_text("only the admin can add to the watchlist.")
            return
        if len(args) < 2 or not valid_address(args[1]):
            await update.message.reply_text("usage: /watchlist add <contract address> [chain]")
            return
        cn = args[2].lower() if len(args) > 2 and args[2].lower() in CFG.chains else CHAIN_NAME
        contract = args[1]
        mint = next((m for m in MINTS.values() if (m.get("contract") or "").lower() == contract.lower()
                     and (m.get("chain") or CHAIN_NAME) == cn and m.get("status") in
                     ("scheduled", "presigned", "retrying")), None)
        opens = mint.get("start_ts") if mint else None
        sched = f"admin #{mint['id']}" if mint else None
        rows, entry = watchlist.add(rows, cn, contract, uid, name=(mint or {}).get("name"),
                                    opens_ts=opens, scheduled_by=sched)
        watchlist.save(rows, WATCH_FILE)
        await update.message.reply_text(f"added {contract[:10]}... ({cn}) to the watchlist.")
        return
    if not rows:
        text = "\n".join(panels.header("watchlist") + [
            "📭 nothing on the watchlist.",
            "👉 admin: <code>/watchlist add 0xCONTRACT [chain]</code> to start watching one.",
        ])
        await ctx.bot.send_message(update.effective_chat.id, text,
                                   reply_markup=M([[B("🏠 main", callback_data="home")]]))
        return
    lines = watchlist.panel_lines(rows)
    await ctx.bot.send_message(update.effective_chat.id,
                               "\n".join(panels.header("watchlist") + ["<pre>" + esc("\n".join(lines)) + "</pre>"]),
                               reply_markup=M([[B("🏠 main", callback_data="home")]]))


async def on_tokenbuy_cb(update: Update, ctx):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID or not is_dm(update):
        await q.answer("admin only")
        return
    await q.answer()
    try:
        _, action, key = q.data.split(":", 2)
    except Exception:
        return
    pending = TOKEN_PENDING.get(key)
    if not pending or pending.get("expires", 0) < time.time():
        TOKEN_PENDING.pop(key, None)
        await q.edit_message_text("quote expired. nothing was moved.")
        return
    if action == "n":
        TOKEN_PENDING.pop(key, None)
        await q.edit_message_text("cancelled. nothing was moved.")
        return
    if action == "c":
        verb = "sell" if pending.get("kind", "").endswith("sell") else "buy"
        kb=M([[B(f"confirm {verb}",callback_data=f"stb:y:{key}"),B("cancel",callback_data=f"stb:n:{key}")]])
        await q.edit_message_text(pending["confirm_text"],reply_markup=kb)
        return
    if action != "y": return
    entries=wallets.signing_entries([ADMIN_ID],only={pending["wallet"].lower()})
    if not entries:
        await q.edit_message_text("saved wallet key unavailable. nothing was moved.")
        return
    entries_sol = wallets.signing_entries([ADMIN_ID], only={pending["wallet"].lower()}, chain="solana")
    if pending.get("kind") == "sol" and not entries_sol:
        await q.edit_message_text("saved solana key unavailable. nothing was moved.")
        return
    kind = pending.get("kind", "evm_buy")
    if kind in (
        "ethereum_buy", "ethereum_sell", "robinhood_buy", "robinhood_sell",
        "ink_buy", "ink_sell",
    ):
        from acobot import evmswap
        ok, tx = await asyncio.to_thread(evmswap.execute_prepared, pending["prepared"], entries[0][3], RPC(pending["chain"], broadcast=True))
        verb = "buy" if kind.endswith("buy") else "sell"
        done_txt = (f"{pending.get('chain_name')} token {verb} submitted: <code>{esc(str(tx))}</code>"
                    if ok else f"{verb} failed: <code>{esc(str(tx))}</code>")
    elif kind == "sol":
        from acobot import solana
        ok, tx = await asyncio.to_thread(solana.execute_prepared, pending["prepared"], entries_sol[0][3])
        done_txt = f"solana swap submitted: <code>{esc(str(tx))}</code>" if ok else "solana broadcast failed. nothing was moved."
    elif kind == "bars":
        from acobot import outmint
        prep = pending["prepared"]
        if prep.get("wallets"):  # multi-wallet bundle: one confirm fires every wallet
            lines = []
            for item in prep["wallets"]:
                ok, tx = await asyncio.to_thread(outmint.execute_prepared, item["prepared"], prep["keys"][item["address"].lower()],
                                                 RPC(pending["chain"], broadcast=True))
                lines.append(f"{esc(item['label'])} ({wallets.mask(item['address'])}): " +
                             (f"ok, {len(tx)} tx" if ok and isinstance(tx, list) else ("ok" if ok else "FAILED")))
            done_txt = f"bars {prep['action']} from {len(prep['wallets'])} wallets:\n" + "\n".join(lines)
        else:
            ok, tx = await asyncio.to_thread(outmint.execute_prepared, prep, entries[0][3], RPC(pending["chain"], broadcast=True))
            if ok and isinstance(tx, list):
                done_txt = f"bars {prep['action']} submitted, {len(tx)} tx(s): " + " ".join(f"<code>{esc(t[:10])}...</code>" for t in tx[:5])
            elif ok:
                done_txt = f"bars tx submitted: <code>{esc(str(tx))}</code>"
            else:
                done_txt = "bars broadcast failed. nothing was moved."
    elif kind == "evm_sell":
        from acobot import tokensell
        ok, tx = await asyncio.to_thread(tokensell.execute_sell, pending["prepared"], entries[0][3], RPC(pending["chain"], broadcast=True))
        done_txt = f"token sell submitted: <code>{esc(tx)}</code>" if ok else f"sell failed: <code>{esc(str(tx))}</code>"
    else:
        from acobot import tokenbuy
        ok, tx = await asyncio.to_thread(tokenbuy.execute_prepared, pending["prepared"], entries[0][3], RPC(pending["chain"], broadcast=True))
        done_txt = f"token buy submitted: <code>{esc(tx)}</code>" if ok else "broadcast failed. nothing was bought."
    TOKEN_PENDING.pop(key, None)
    await q.edit_message_text(done_txt)



# ---------------- unified /trade (buy and sell) ----------------

def _is_sol_ca(text):
    return 32 <= len(text) <= 44 and all(c.isalnum() for c in text) and not text.startswith("0x")


def _detect_trade_chain(ca):
    ca = ca.strip()
    if ca.startswith("0x") and len(ca) == 42:
        return "evm", cs(ca)
    if _is_sol_ca(ca):
        return "sol", ca
    return None, ca


async def cmd_trade(update: Update, ctx):
    if not _admin_only(update) or not is_dm(update):
        return
    kb = M([[B("buy", callback_data="trd:b"), B("sell", callback_data="trd:s")]])
    await update.message.reply_text("buy or sell?", reply_markup=kb)


async def on_trade_mode_cb(update: Update, ctx):
    q = update.callback_query
    await q.answer()
    if q.from_user.id != ADMIN_ID or not is_dm(update):
        await q.answer("admin only")
        return
    mode = "buy" if q.data.endswith(":b") else "sell"
    chains = [c for c in (*enabled_chains(), "solana") if c in (*cfgmod.SUPPORTED_EVM_CHAIN_IDS, "solana")]
    kb = M([[B(chain_label(c), callback_data=f"trd:ch:{mode}:{c}")] for c in chains])
    await q.message.reply_text(f"{mode} on which chain?", reply_markup=kb)


async def on_trade_chain_cb(update: Update, ctx):
    q = update.callback_query
    await q.answer()
    if q.from_user.id != ADMIN_ID or not is_dm(update):
        await q.answer("admin only")
        return
    _, _, mode, chain = q.data.split(":")
    ctx.user_data["await"] = ("trade_ca", (mode, chain))
    await q.message.reply_text(f"{mode} on {chain_label(chain)}: paste the token contract.")


async def _trade_ca(update: Update, ctx, st, text):
    mode, chain = st[1]
    ca = text.strip()
    if chain == "solana":
        ok = _is_sol_ca(ca)
    else:
        ok = ca.startswith("0x") and len(ca) == 42 and ca == cs(ca)
    if not ok:
        want = "a 32-44 char SPL mint" if chain == "solana" else "a 0x plus 40 hex chars address"
        await update.message.reply_text(f"that isnt valid on {chain}. send {want}.")
        return
    native = "SOL" if chain == "solana" else "ETH"
    if mode == "buy":
        ctx.user_data["await"] = ("trade_amt", {"mode": "buy", "chain": chain, "ca": ca})
        await update.message.reply_text(f"how much {native} to spend? (e.g. 0.01)")
    else:
        ctx.user_data["await"] = ("trade_amt", {"mode": "sell", "chain": chain, "ca": ca})
        await update.message.reply_text("how many tokens to sell? send a number, or 'all' for the full balance.")


NUM_RE = re.compile(r"[0-9]*\.?[0-9]+")


async def _trade_amount(update: Update, ctx, st, text):
    spec = st[1] or {}
    mode, chain, ca = spec.get("mode"), spec.get("chain"), spec.get("ca")
    if not (mode and chain and ca):
        ctx.user_data.pop("await", None)
        return
    chat_id = update.effective_chat.id
    if chain in cfgmod.SUPPORTED_EVM_CHAIN_IDS and mode in ("buy", "sell"):
        amount = text.strip().lower()
        is_buy_num = mode == "buy" and text and NUM_RE.fullmatch(amount)
        is_sell_amt = mode == "sell" and (amount == "all" or (text and NUM_RE.fullmatch(amount)))
        if not (is_buy_num or is_sell_amt):
            await update.message.reply_text("send a positive number" + ("" if mode == "buy" else " or 'all'"))
            return
        ctx.user_data.pop("await", None)
        if chain == "robinhood" and not os.environ.get("UNISWAP_API_KEY", "").strip():
            if mode == "buy":
                await _prepare_token_panel(chat_id, ctx, ca, amount_eth=amount)
            else:
                await _prepare_sell_panel(chat_id, ctx, ca, amount)
        else:
            await _prepare_evm_panel(chat_id, ctx, chain, mode, ca, amount)
        return
    wallet = wallets.main_wallet_for(ADMIN_ID, "solana")
    if not wallet:
        await update.message.reply_text("save a Solana main wallet first. nothing was signed.")
        ctx.user_data.pop("await", None)
        return
    if mode == "buy":
        if not text or not NUM_RE.fullmatch(text.strip()):
            await update.message.reply_text("send a positive number, e.g. 0.5")
            return
        ctx.user_data.pop("await", None)
        await _prepare_sol_panel(chat_id, ctx, ca, "buy", text.strip())
        return
    amount = text.strip().lower()
    if amount != "all" and not NUM_RE.fullmatch(amount):
        await update.message.reply_text("send a positive number or 'all'")
        return
    ctx.user_data.pop("await", None)
    await _prepare_sol_panel(chat_id, ctx, ca, "sell", amount)


async def _prepare_sell_panel(chat_id, ctx, token_address, amount):
    chain = CFG.chains.get("robinhood")
    wallet = wallets.main_wallet_for(ADMIN_ID, "robinhood")
    if not wallet or not chain:
        await ctx.bot.send_message(chat_id, "save a Robinhood Chain main wallet first. nothing was signed.")
        return
    try:
        from acobot import tokenbuy, tokensell
        prepared = await asyncio.to_thread(tokensell.prepare_sell, token_address, amount,
                                           wallet["address"], RPC(chain))
    except Exception as exc:
        log.info("sell quote unavailable: %s", type(exc).__name__)
        await ctx.bot.send_message(chat_id, str(exc) if isinstance(exc, (ValueError, RuntimeError)) else
                                   "couldnt prepare a live sell quote; nothing was signed or sent.")
        return
    key = _pending_token()
    prepared["expires"] = time.time() + 120
    amt_txt = "full balance" if amount == "all" else amount
    text = (f"<b>robinhood token sell</b>\nvenue: {esc(prepared['venue'])}\n"
            f"token: {esc(prepared['symbol'])} <code>{esc(token_address)}</code>\n"
            f"wallet: <code>{esc(wallet['address'])}</code>\n"
            f"sell: {amt_txt}\n"
            f"quoted output: {tokenbuy.format_amount(prepared['amount_out'])} ETH\n"
            f"minimum output (2% slippage): {tokenbuy.format_amount(prepared['min_out'])} ETH\n"
            f"{'approval tx goes first, then the swap. ' if prepared['needs_approval'] else ''}"
            "quote only. tap review, then confirm to sign and broadcast.")
    prepared["confirm_text"] = text
    TOKEN_PENDING[key] = {"kind": "evm_sell", "prepared": prepared, "wallet": wallet["address"],
                          "chain": chain, "expires": time.time() + 120, "confirm_text": text}
    kb = M([[B("review quote", callback_data=f"stb:c:{key}"), B("cancel", callback_data=f"stb:n:{key}")]])
    await ctx.bot.send_message(chat_id, text, reply_markup=kb)


async def _prepare_sol_panel(chat_id, ctx, mint, mode, amount):
    from acobot import solana, jupiter
    wallet = wallets.main_wallet_for(ADMIN_ID, "solana")
    if not wallet:
        await ctx.bot.send_message(chat_id, "save a Solana main wallet first. nothing was signed.")
        return
    try:
        if mode == "buy":
            lamports = int(Decimal(amount) * 10**9)
            prepared = await asyncio.to_thread(solana.prepare_buy, mint, lamports, wallet["address"])
            in_txt = f"{amount} SOL"
            out_txt = f"{prepared['out_amount'] / 10**6:,.6f} tokens (assumed 6 dec)"
        else:
            bal = await asyncio.to_thread(solana.get_token_balance, wallet["address"], mint)
            raw = bal["raw"] if amount == "all" else int(Decimal(amount) * 10 ** bal["decimals"])
            if raw <= 0:
                raise ValueError("no token balance to sell")
            quote = await asyncio.to_thread(jupiter.quote, mint, solana.WSOL_MINT, raw)
            swap = await asyncio.to_thread(jupiter.build_swap, quote, wallet["address"])
            prepared = {"chain": "solana", "wallet": wallet["address"], "token_mint": mint,
                        "swap_transaction": swap["swapTransaction"],
                        "out_amount": int(quote["outAmount"]), "min_out": int(quote["otherAmountThreshold"])}
            in_txt = "full balance" if amount == "all" else amount
            out_txt = f"{prepared['out_amount'] / 10**9:,.6f} SOL"
    except Exception as exc:
        log.info("solana quote unavailable: %s", type(exc).__name__)
        msg = str(exc) if isinstance(exc, ValueError) else "couldnt get a live Jupiter quote; nothing was signed or sent."
        await ctx.bot.send_message(chat_id, msg)
        return
    key = _pending_token()
    text = (f"<b>solana token {mode}</b>\nvenue: jupiter\nmint: <code>{esc(mint)}</code>\n"
            f"wallet: <code>{esc(wallet['address'])}</code>\n"
            f"in: {in_txt}\nquoted output: {out_txt}\n"
            "quote only. tap review, then confirm to sign and broadcast.")
    prepared["confirm_text"] = text
    TOKEN_PENDING[key] = {"kind": "sol", "prepared": prepared, "wallet": wallet["address"],
                          "expires": time.time() + 120, "confirm_text": text}
    kb = M([[B("review quote", callback_data=f"stb:c:{key}"), B("cancel", callback_data=f"stb:n:{key}")]])
    await ctx.bot.send_message(chat_id, text, reply_markup=kb)


async def _prepare_evm_panel(chat_id, ctx, chain_name, mode, token_address, amount):
    from acobot import evmswap
    chain = CFG.chains.get(chain_name)
    wallet = wallets.main_wallet_for(ADMIN_ID, chain_name)
    if not wallet or not chain:
        await ctx.bot.send_message(chat_id, f"save a {chain_name} main wallet first. nothing was signed.")
        return
    try:
        api_key = os.environ.get("UNISWAP_API_KEY", "").strip()
        if api_key:
            from acobot import uniswap_api
            prepared = await asyncio.to_thread(
                uniswap_api.prepare, chain_name, token_address, amount, wallet["address"],
                RPC(chain), mode=mode, api_key=api_key,
            )
        else:
            prepared = await asyncio.to_thread(evmswap.prepare, chain_name, token_address, amount,
                                               wallet["address"], RPC(chain), mode=mode)
    except Exception as exc:
        log.info("evm quote unavailable: %s", type(exc).__name__)
        msg = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else \
            f"couldnt get a live {chain_name} quote; nothing was signed or sent."
        await ctx.bot.send_message(chat_id, msg)
        return
    key = _pending_token()
    prepared["expires"] = time.time() + 120
    if mode == "buy":
        head = f"<b>{chain_name} token buy</b>"
        in_txt = f"{amount} ETH"
        out_txt = f"{evmswap.format_amount(prepared['amount_out'], prepared['decimals'])} {esc(prepared['symbol'])}"
        min_txt = f"minimum output (2% slippage): {evmswap.format_amount(prepared['min_out'], prepared['decimals'])} {esc(prepared['symbol'])}"
    else:
        head = f"<b>{chain_name} token sell</b>"
        in_txt = "full balance" if amount == "all" else f"{amount} {esc(prepared['symbol'])}"
        out_txt = f"{evmswap.format_amount(prepared['amount_out'])} ETH"
        min_txt = f"minimum output (2% slippage): {evmswap.format_amount(prepared['min_out'])} ETH"
    text = (f"{head}\nvenue: {prepared['venue']}\n<code>{esc(token_address)}</code>\n"
            f"wallet: <code>{esc(wallet['address'])}</code>\n"
            f"in: {in_txt}\nquoted output: {out_txt}\n{min_txt}\n"
            f"{'approval tx goes first, then the swap. ' if prepared['needs_approval'] else ''}"
            "quote only. tap review, then confirm to sign and broadcast.")
    prepared["confirm_text"] = text
    TOKEN_PENDING[key] = {"kind": prepared["kind"], "prepared": prepared, "wallet": wallet["address"],
                          "chain": chain, "chain_name": chain_name, "expires": time.time() + 120,
                          "confirm_text": text}
    kb = M([[B("review quote", callback_data=f"stb:c:{key}"), B("cancel", callback_data=f"stb:n:{key}")]])
    await ctx.bot.send_message(chat_id, text, reply_markup=kb)



async def cmd_outmint(update: Update, ctx):
    """Bars cast/melt. Refuses cleanly until a verified bar contract is set."""
    from acobot import outmint
    if not _admin_only(update) or not is_dm(update):
        return
    cfg = outmint.load_config()
    if not cfg.get("contract"):
        await update.message.reply_text(
            "bars: no verified contract deployed yet. nothing to mint.\n"
            "verified spec so far: bar N holds N balance, ascending.\n"
            "the engine is live and prebuilt; the moment a contract is verified, /outmint casts up to 10 bars with one confirm tap.")
        return
    args = (ctx.args or [])
    action = args[0].lower() if args else ""
    auto = False
    if action.endswith(":auto"):
        auto = True
        action = action[:-5]
    if action not in ("cast", "melt"):
        await update.message.reply_text("usage: /outmint cast <bar id> [count] | /outmint melt <bar id> [count] | add :auto to skip confirm")
        return
    if len(args) < 2 or not args[1].isdigit():
        await update.message.reply_text("send the bar id, e.g. /outmint cast 5 2")
        return
    bar_id = int(args[1])
    count = int(args[2]) if len(args) > 2 and args[2].isdigit() else 1
    chain = CFG.chains.get(cfg.get("chain", "ethereum"))
    if not chain:
        await update.message.reply_text("unknown chain in bars config. nothing was signed.")
        return
    # multi-wallet: w=all -> every fleet+main wallet on this chain, or w=main,w2 comma list
    warg = next((a for a in args if a.lower().startswith("w=")), None)
    if warg:
        want = {x.strip().lower() for x in warg[2:].split(",") if x.strip()}
        if "all" in want:
            sel = [w for w in wallets.list_wallets(ADMIN_ID)
                   if wallets.supports_chain(w["chain"], cfg.get("chain", "ethereum"))]
            want = set()
        else:
            sel = [w for w in wallets.list_wallets(ADMIN_ID)
                   if wallets.supports_chain(w["chain"], cfg.get("chain", "ethereum"))
                   and (w["label"].lower() in want or w["address"].lower() in want)]
        if not sel:
            await update.message.reply_text("no matching wallets on that chain. nothing was signed.")
            return
    else:
        sel = [wallets.main_wallet_for(ADMIN_ID, cfg.get("chain", "ethereum"))]
        sel = [w for w in sel if w]
    if not sel:
        await update.message.reply_text("save a main wallet on that chain first. nothing was signed.")
        return
    prepared = None
    try:
        if len(sel) == 1:
            prepared = await asyncio.to_thread(outmint.prepare, cfg, action, bar_id, count,
                                               sel[0]["address"], RPC(chain))
        else:
            # one prepared bundle per wallet, fired back to back on one confirm
            entries = wallets.signing_entries([ADMIN_ID], only={w["address"].lower() for w in sel})
            secrets = {e[2].lower(): e[3] for e in entries}
            prepared = {"wallets": [], "bar_id": bar_id, "count": count, "action": action, "keys": secrets, "chain": cfg.get("chain", "ethereum")}
            total = 0
            for w in sel:
                p = await asyncio.to_thread(outmint.prepare, cfg, action, bar_id, count, w["address"], RPC(chain))
                prepared["wallets"].append({"address": w["address"], "label": w["label"], "prepared": p})
                total += p.get("total_cost", 0)
            prepared["total_cost"] = total
            prepared["txs"] = [tx for w in prepared["wallets"] for tx in w["prepared"]["txs"]]
    except Exception as exc:
        log.exception("outmint prepare failed")
        await update.message.reply_text(f"couldnt prepare: {str(exc)[:140]}. nothing was signed.")
        return
    wlist = ", ".join(w["label"] + "(" + wallets.mask(w["address"]) + ")" for w in sel)
    if auto:
        await update.message.reply_text(
            f"bars AUTO {action}: {len(sel)} wallet(s) {wlist}, firing {prepared.get('total_cost')} balance for bar {prepared['bar_id']}"
            + (f"..{prepared['bar_id'] + count - 1}" if count > 1 else "") + " now, no confirm.")
        if len(sel) == 1:
            ok, tx = await asyncio.to_thread(outmint.execute_prepared, prepared,
                                             wallets.signing_entries([ADMIN_ID], only={sel[0]["address"].lower()})[0][3],
                                             RPC(chain, broadcast=True))
            if ok and isinstance(tx, list):
                await update.message.reply_text(f"bars fired, {len(tx)} tx(s): " + " ".join(f"<code>{esc(t[:10])}...</code>" for t in tx[:5]))
            elif ok:
                await update.message.reply_text(f"bars tx submitted: <code>{esc(str(tx))}</code>")
            else:
                await update.message.reply_text("bars broadcast failed. nothing was moved.")
        else:
            results = []
            for item in prepared["wallets"]:
                key_ = item["prepared"]
                ok, tx = await asyncio.to_thread(outmint.execute_prepared, key_, prepared["keys"][item["address"].lower()],
                                                 RPC(chain, broadcast=True))
                results.append((item["label"], ok, tx))
            sent = sum(1 for _, ok, _ in results if ok)
            await update.message.reply_text(f"bars fired: {sent}/{len(results)} wallets ok. " +
                                            " ".join(f"{esc(l)}:{'ok' if ok else 'fail'}" for l, ok, _ in results))
        return
    key = _pending_token()
    text = (f"<b>bars {action}</b>\ncontract: <code>{esc(cfg['contract'])}</code>\n"
            f"wallets ({len(sel)}): {esc(wlist)}\n"
            f"bars: {prepared['bar_id']}" + (f"..{prepared['bar_id'] + count - 1}" if count > 1 else "") + f" x{count} each\n"
            f"cost: {prepared['total_cost']} balance units total\n"
            f"{len(prepared['txs'])} tx(s) prebuilt, fires back to back on confirm\n"
            "quote only. tap review, then confirm to sign and broadcast.")
    prepared["confirm_text"] = text
    prepared["expires"] = time.time() + 120
    TOKEN_PENDING[key] = {"kind": "bars", "prepared": prepared, "wallet": sel[0]["address"], "wallets": [w["address"] for w in sel],
                          "chain": chain, "expires": time.time() + 120, "confirm_text": text}
    kb = M([[B("review", callback_data=f"stb:c:{key}"), B("cancel", callback_data=f"stb:n:{key}")]])
    await update.message.reply_text(text, reply_markup=kb)

async def cmd_snipetoken(update: Update, ctx):
    if not _admin_only(update) or not is_dm(update):
        return
    if ctx.args:
        await _prepare_token_panel(update.effective_chat.id, ctx, ctx.args[0])
    else:
        ctx.user_data["await"] = ("snipetoken_ca", None)
        await update.message.reply_text("paste the Robinhood Chain ERC-20 contract address. the quote is read-only until both confirm taps.")


async def _prepare_token_panel(chat_id, ctx, token_address, amount_eth=None):
    if not valid_address(token_address):
        await ctx.bot.send_message(chat_id, "that token address is invalid. use 0x plus 40 hex characters.")
        return
    wallet = wallets.main_wallet_for(ADMIN_ID, "robinhood")
    if not wallet:
        await ctx.bot.send_message(chat_id, "save a Robinhood Chain main wallet first. nothing was bought.")
        return
    wallet_on_chain = wallet
    amount_eth = amount_eth or "0.01"
    if len(ctx.args or []) > 1 and not amount_eth:
        amount_eth = ctx.args[1]
    try:
        from acobot import tokenbuy
        chain = CFG.chains.get("robinhood")
        if not chain or chain.chain_id != tokenbuy.CHAIN_ID:
            raise RuntimeError("robinhood chain config missing")
        prepared = await asyncio.to_thread(tokenbuy.prepare_buy, token_address, amount_eth,
                                           wallet["address"], RPC(chain))
    except Exception as exc:
        detail = str(exc).lower()
        log.exception("token quote unavailable")
        if "quotable pool" in detail or "v4 quote reverted" in detail:
            message = str(exc)
        else:
            message = f"couldnt prepare live quote ({type(exc).__name__}: {str(exc)[:120]}); nothing was signed or sent."
        await ctx.bot.send_message(chat_id, message)
        return
    key = _pending_token()
    prepared["expires"] = time.time() + 120
    text = (f"<b>robinhood token quote</b>\nvenue: {esc(prepared['venue'])}\n<code>{esc(token_address)}</code>\n"
            f"wallet: <code>{esc(wallet['address'])}</code>\n"
            f"spend: {amount_eth} ETH\n"
            f"quoted output: {tokenbuy.format_amount(prepared['amount_out'])} tokens\n"
            f"minimum output (2% slippage): {tokenbuy.format_amount(prepared['min_out'])}\n"
            f"estimated gas: {prepared['gas_estimate']:,} (buffered {prepared['gas']:,})\n"
            "this is only a quote. tap review, then confirm to sign and broadcast.")
    prepared["confirm_text"] = text
    TOKEN_PENDING[key] = {"prepared": prepared, "wallet": wallet["address"],
                          "chain": chain, "expires": time.time() + 120,
                          "confirm_text": text}
    kb = M([[B("review quote", callback_data=f"stb:c:{key}"), B("cancel", callback_data=f"stb:n:{key}")]])
    await ctx.bot.send_message(chat_id, text, reply_markup=kb)


async def cmd_snipeprice(update: Update, ctx):
    if not _admin_only(update):
        return
    args = ctx.args or []
    if not args:
        ctx.user_data["await"] = ("snipetoken_ca", None)
        await update.message.reply_text("paste the token CA to prepare a Robinhood Chain quote")
        return
    if len(args) == 1:
        ctx.user_data["await"] = ("snipetoken_ca", None)
        await _prepare_token_panel(update.effective_chat.id, ctx, args[0])
        return
    if len(args) < 2 or not valid_address(args[0]):
        await update.message.reply_text("usage: /snipeprice <contract> <max price in ETH> [chain]")
        return
    try:
        max_eth = float(args[1])
        if max_eth <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("max price must be a positive number of ETH, e.g. 0.05")
        return
    cn = args[2].lower() if len(args) > 2 and args[2].lower() in CFG.chains else CHAIN_NAME
    rows = snipeprice.add_target(snipeprice.load(SNIPE_FILE), cn, args[0], max_eth, ADMIN_ID)
    snipeprice.save(rows, SNIPE_FILE)
    await update.message.reply_text(f"watching {args[0][:10]}... on {cn} for listings at or under {max_eth:g} ETH. you get a confirm panel for each hit, nothing buys without your tap.")


def _snipe_send(text, kb=None):
    try:
        app = WATCH_APP[0]
        loop = MAIN_LOOP[0]
        if app is None or loop is None:
            return
        asyncio.run_coroutine_threadsafe(app.bot.send_message(ADMIN_ID, text, reply_markup=kb), loop)
    except Exception:
        log.exception("snipe alert send failed")


def _snipe_tick():
    # Inspect live offers and ask for an explicit review/confirm before selling.
    for target in list(SELL_TARGETS):
        if not target.get("active"):
            continue
        for token_id in target.get("tokens", []):
            offer = snipeprice.fetch_top_offer(target.get("chain"), target.get("contract"), str(token_id),
                                               api_key=os.environ.get("RESERVOIR_API_KEY"))
            if not offer or offer.get("price_eth", 0) < float(target.get("floor_eth", 0)):
                continue
            key = f"sell{target['chain'][:2]}{offer['id'][:28]}"[:40]
            with SNIPE_SEEN_LOCK:
                if key in SNIPE_PENDING:
                    continue
                SNIPE_PENDING[key] = dict(offer, mode="sell", chain=target["chain"],
                                          floor_eth=float(target["floor_eth"]), expires=time.time()+300)
            kb = M([[B("review offer", callback_data=f"snb:sellc:{key}")]])
            _snipe_send(f"<b>offer meets your floor</b>\nitem #{esc(token_id)}\noffer {offer['price_eth']:g} ETH\nyour floor {float(target['floor_eth']):g} ETH\n\nreview then confirm; nothing sells automatically.", kb)
    rows = snipeprice.load(SNIPE_FILE)
    changed = False
    for t in rows:
        if not t.get("active"):
            continue
        listings = snipeprice.fetch_cheapest(t["chain"], t["contract"])
        if listings is None:
            continue
        with SNIPE_SEEN_LOCK:
            hits, seen = snipeprice.find_hits(listings, t["max_eth"], set(t.get("seen") or []))
            if hits:
                t["seen"] = seen[-500:]
                changed = True
        for h in hits:
            key = f"{t['chain'][:2]}{h['id'][:24]}"[:40]
            SNIPE_PENDING[key] = dict(h, chain=t["chain"], contract=h.get("contract") or t["contract"], max_eth=t["max_eth"])
            usd = prices.native_usd(t["chain"])
            text = snipeprice.confirm_text(t, h, usd, t["contract"][:10])
            kb = M([[B(f"buy {h['price_eth']:g} ETH", callback_data=f"snb:c:{key}")]])
            _snipe_send(text, kb)
    if changed:
        snipeprice.save(rows, SNIPE_FILE)


def _snipe_loop():
    import time as _t
    while True:
        try:
            _snipe_tick()
        except Exception:
            log.exception("snipe watcher tick failed")
        _t.sleep(snipeprice.POLL_S)


def start_snipe_watcher():
    if SNIPE_STARTED[0]:
        return
    SNIPE_STARTED[0] = True
    threading.Thread(target=_snipe_loop, daemon=True, name="snipeprice").start()


async def on_snipe_cb(update: Update, ctx):
    q = update.callback_query
    await q.answer()
    if q.from_user.id != ADMIN_ID:
        return
    parts = q.data.split(":")
    if len(parts) != 3:
        return
    _, step, key = parts
    if step in {"c", "y"}:
        listing = SNIPE_PENDING.get(key)
        if not listing or listing.get("mode") == "sell":
            await q.edit_message_text("this snipe hit expired. it was not bought.")
            return
        if step == "c":
            kb = M([[B("confirm buy", callback_data=f"snb:y:{key}")]])
            await q.edit_message_text(
                f"confirm buy: {listing['price_eth']:g} ETH (~${(prices.native_usd(listing['chain']) or 0) * listing['price_eth']:,.2f}). "
                "tap confirm to buy.", reply_markup=kb)
            return
        buyer = wallets.main_wallet(ADMIN_ID)
        if not buyer:
            await q.edit_message_text("no saved main wallet to buy with. nothing was bought.")
            return
        entries = wallets.signing_entries([ADMIN_ID], only={buyer["address"].lower()})
        if not entries:
            await q.edit_message_text("saved buyer key is unavailable. nothing was bought.")
            return
        listing["owner_id"] = ADMIN_ID
        ok, msg = await asyncio.to_thread(
            snipeprice.execute_buy, listing, buyer["address"], chain=CFG.chains.get(listing["chain"]),
            private_key=entries[0][3], api_key=os.environ.get("RESERVOIR_API_KEY"))
        SNIPE_PENDING.pop(key, None)
        await q.edit_message_text(("fill submitted. " if ok else "") + msg)
        return
    if step == "sellc":
        offer = SNIPE_PENDING.get(key)
        if not offer or offer.get("mode") != "sell":
            await q.edit_message_text("this offer expired. no sale was made.")
            return
        kb = M([[B("confirm sale", callback_data=f"snb:selly:{key}")]])
        await q.edit_message_text(
            f"<b>accept offer?</b>\nitem #{offer['token_id']}\n"
            f"offer {offer['price_eth']:g} ETH\nyour floor {offer['floor_eth']:g} ETH\n\n"
            "tap confirm to accept. nothing sells without your tap.", reply_markup=kb)
        return
    if step == "selly":
        offer = SNIPE_PENDING.get(key)
        if not offer or offer.get("mode") != "sell":
            await q.edit_message_text("this offer expired. no sale was made.")
            return
        # Refresh the bid immediately before the fill and enforce both the saved floor and the tap.
        live = await asyncio.to_thread(snipeprice.fetch_top_offer, offer["chain"], offer["contract"],
                                       offer["token_id"], api_key=os.environ.get("RESERVOIR_API_KEY"))
        if not live or live.get("id") != offer.get("id") or live["price_eth"] < offer["floor_eth"]:
            SNIPE_PENDING.pop(key, None)
            await q.edit_message_text("top offer changed or fell below your floor. nothing was sold.")
            return
        seller = wallets.main_wallet(ADMIN_ID)
        if not seller:
            await q.edit_message_text("no saved main wallet to sell from. nothing was sold.")
            return
        entries = wallets.signing_entries([ADMIN_ID], only={seller["address"].lower()})
        if not entries:
            await q.edit_message_text("saved seller key is unavailable. nothing was sold.")
            return
        ok, msg = await asyncio.to_thread(
            snipeprice.execute_sell, live, offer["floor_eth"], seller["address"], entries[0][3],
            chain=CFG.chains.get(offer["chain"]), api_key=os.environ.get("RESERVOIR_API_KEY"))
        SNIPE_PENDING.pop(key, None)
        await q.edit_message_text(("offer fill submitted. " if ok else "") + msg)


def build_app():
    app = Application.builder().token(CFG.telegram_token).defaults(Defaults(parse_mode=ParseMode.HTML)).build()
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("allow", cmd_allow))
    app.add_handler(CommandHandler("block", cmd_block))
    app.add_handler(CommandHandler("addmint", cmd_addmint))
    app.add_handler(CommandHandler("solprog", cmd_solprog))
    app.add_handler(CommandHandler("eligibleset", cmd_eligibleset))
    app.add_handler(CommandHandler("mintapiset", cmd_mintapiset))
    app.add_handler(CommandHandler("mymints", cmd_mymints))
    app.add_handler(CommandHandler("cancelmint", cmd_cancelmint))
    app.add_handler(CommandHandler("wallets", cmd_wallets))
    app.add_handler(CommandHandler("fund", cmd_fund))
    app.add_handler(CommandHandler("sweep", cmd_sweep))
    app.add_handler(CommandHandler("sweepall", cmd_sweepall))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("send", cmd_send))
    app.add_handler(CommandHandler("ai", cmd_ai))
    app.add_handler(CommandHandler("portfolio", cmd_portfolio))
    app.add_handler(CommandHandler("watchlist", cmd_watchlist))
    app.add_handler(CommandHandler("snipeprice", cmd_snipeprice))
    app.add_handler(CommandHandler("trade", cmd_trade))
    app.add_handler(CommandHandler("outmint", cmd_outmint))
    app.add_handler(CommandHandler("cawatch", cmd_cawatch))
    app.add_handler(CommandHandler("alpha", cmd_alpha))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(on_alpha_cb, pattern=r"^alp:"))
    app.add_handler(CallbackQueryHandler(on_snipe_cb, pattern=r"^snb:"))
    app.add_handler(CallbackQueryHandler(on_tokenbuy_cb, pattern=r"^stb:"))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    async def on_error(update, ctx):
        import traceback as _tb
        log.error("handler error: %s\n%s", type(ctx.error).__name__, _tb.format_exception(ctx.error))
        try:
            if isinstance(update, Update) and update.effective_message:
                await update.effective_message.reply_text("something broke on my side. try again, or /start.")
        except Exception:
            pass
    app.add_error_handler(on_error)
    return app


def main():
    global CFG
    load_env()
    CFG = cfgmod.load(str(HOME / "config.toml"))
    from acobot import solana
    solana.configure_rpc_urls(CFG.solana_rpc_urls)
    if not getattr(CFG, "whitelist_usernames", None):
        CFG.whitelist_usernames = []
    if not CFG.admin_ids:
        CFG.admin_ids = [ADMIN_ID]
    load_whitelist()
    if not CFG.telegram_token:
        raise SystemExit("ACO_BOT_TOKEN missing from .env")
    load_mints()
    load_drafts()
    app = build_app()

    async def post_init(a):
        MAIN_LOOP[0] = asyncio.get_running_loop()
        WATCH_APP[0] = a   # admin notify path
        await a.bot.set_my_commands(BOT_COMMANDS)
        uncertain = recover_interrupted_mints()
        for mid, m in list(MINTS.items()):
            status = m.get("status")
            if status in ("scheduled", "presigned", "retrying"):
                start_watcher(mid, a)
        if uncertain:
            for mid in uncertain:
                _notify(f"⛔ mint #{mid} stopped in an uncertain fire state after restart. Check the chain before re-arming; it was not automatically resent.")
        for cn in enabled_chains():   # keep newHeads ws hot at boot, not only when a mint is scheduled
            if CFG.chains[cn].ws_rpc:
                head_feed(cn)
        start_watchdog(a)
        start_snipe_watcher()
    app.post_init = post_init
    log.info("acobot starting polling")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
