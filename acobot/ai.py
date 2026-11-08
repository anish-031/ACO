"""AI intent layer: plain DM text -> one known action with validated args.

The LLM only proposes {action, args}. Nothing executes from here: bot.py maps the
action onto the existing command handlers, so money moves still go through the
same confirm panels as the button/slash flows.
"""
import json, os, re, time
from collections import deque
from pathlib import Path

HOME = Path(os.path.expanduser("~/.folk/acobot"))
KEY_FILE = HOME / "keys" / "ai.enc"
EVM_CHAINS = ("ethereum", "robinhood", "ink")
CHAINS = (*EVM_CHAINS, "solana")
SPEEDS = ("slow", "medium", "fast", "super fast")
ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
AMOUNT_RE = re.compile(r"^(all|\d+(\.\d+)?)$", re.I)
TOKEN_RE = re.compile(r"^(native|[A-Za-z0-9]{1,12}|0x[0-9a-fA-F]{40})$", re.I)
WHO_RE = re.compile(r"^(all|[A-Za-z0-9]{1,12})$", re.I)
CTX_LEN = 10

# action -> (admin_only, {arg: (type, required)})
ACTIONS = {
    "addmint": (True, {"contract": ("addr", True), "chain": ("evm_chain", False), "phase": ("str", False),
                       "qty": ("int", False), "speed": ("speed", False), "mode": ("str", False),
                       "time": ("str", False)}),
    "cancel_mint": (True, {"id": ("int", True)}),
    "wallets_list": (True, {}),
    "wallet_main": (True, {"who": ("who", True)}),
    "wallet_remove": (True, {"who": ("who", True)}),
    "balance": (False, {"chain": ("chain", False)}),
    "send": (True, {"who": ("who", True), "amount": ("amount", True), "token": ("token", True),
                    "to": ("to", True)}),
    "sweep": (True, {"contract": ("addr", False)}),
    "snipetoken": (True, {"contract": ("addr", True)}),
    "fund": (True, {"who": ("who", True), "amount": ("amount", True)}),
    "allow": (True, {"user_id": ("who", True)}),
    "help": (False, {}),
    "unknown": (False, {"reply": ("str", False)}),
}

SYSTEM = """You map a Telegram DM from the owner of an NFT mint bot to ONE action.
Reply with JSON only: {"action": "<name>", "args": {...}}.
Actions and args:
addmint {contract (0x address or "last" for the last analyzed contract), chain (ethereum|robinhood|ink), phase, qty (int), speed (slow|medium|fast|super fast), mode, time (UTC text)}
cancel_mint {id (int)}
wallets_list {}
wallet_main {who (wallet label or number)}
wallet_remove {who}
balance {chain optional}. the wired balance tools can read real balances on supported chains and assets; answer from their returned numbers, never invent them. for balance requests, route to /balance so the user can choose chain, asset, and wallets.
send {who (wallet label/number or all), amount (number or all), token (native, symbol or 0x address), to (main or 0x address)}
snipetoken {contract}: if the user says buy plus a token contract address, route to /snipetoken with that exact contract. this prepares a quote only; the two explicit confirmation taps are still required before broadcast.
fund {who (label/number or all), amount (ETH number)}
allow {user_id (string)}: pass the @username if the user gave one (e.g. "@RAggarwal60276" -> user_id "@RAggarwal60276"), or the numeric id if they gave one. never strip the @, never guess digits from a username
help {}
unknown {reply (short plain text)}
Pronouns like "that contract" refer to the last analyzed contract (use "last").
DEFAULTS, never ask questions: send with no wallet -> who "all"; no token mentioned -> "native"; fund -> who "all". to is always given or "main".
token: use "native" when the user says ETH/robinhood gas coin or no token; only pass a symbol or CA if the user wrote one. Never output "***" or placeholders.
NEVER invent a 0x address; if the destination address is missing entirely use "main".
If unsure, use unknown. Never invent addresses. Do not add fields not listed.

VOICE: you are folk, the user's own bot, talking to Anish (he runs this bot).
For the unknown action's reply text: talk like a friend texting. lowercase, short, dry, direct.
casual english, "bro" ok, no trailing punctuation, no corporate words, rare emoji (one at most).
a question gets a real answer. a mint question gets numbers. never lecture. max 2 sentences unless he asks for detail.
FEATURES (for "what can you do"/help/feature questions — summarize supported features and state important limits accurately):
EVM mint analysis and scheduling on Ethereum, Robinhood Chain, and Ink (speed tiers, fire modes), wallet fleet 20 with fund/send/sweep/balance, Solana wallet/balance/send/swap, portfolio pnl, snipeprice listing alerts, copy alpha (recent trades by tracked wallets: /alpha, /alpha up, /alpha new), crew watchlist, rugcheck on addmint, gas spike guard, public SeaDrop and supported simple EVM contract mints, plus gated/custom phases only where a supported phase-specific route exists. Gated SeaDrop needs real wallet-specific proof/signature calldata from the configured official API and the exact transaction is simulated before signing; eligibility checks do not create authorization and force never bypasses it. Solana NFT minting is a separate protocol adapter and must not be described as supported unless the selected program and mint route have been implemented. For phase questions, inspect the selected EVM network and report every returned phase with its source; distinguish on-chain execution data from OpenSea display metadata and detected function names. Phase discovery can be incomplete or stale, so never claim 100% accuracy or that every phase is mintable. Opensea keyless or api-key mode, allowlist eligibility prober, wallet tracker alerts, and you (the ai) for anything about bot state or upcoming mints. when listing features, format premium: "<b>Feature name</b> — short clean description", one per line (html allowed in the reply), proud not robotic but do not claim unsupported behavior. end with "what do u wanna run"."""


class AIError(Exception):
    pass


# ---------- config + key storage ----------
def _fernet():
    from acobot import wallets
    return wallets._fernet()


def stored_key():
    """Env var wins; else the encrypted key file. Returns (key, source) or (None, None)."""
    env = os.environ.get("ACO_AI_API_KEY", "").strip()
    if env:
        return env, "env"
    try:
        import tomllib
        cfg = tomllib.load(open(os.path.expanduser("~/.folk/acobot/config.toml"), "rb"))
        k = cfg.get("secrets", {}).get("ai_key", "").strip()
        if k:
            return k, "config"
    except Exception:
        pass
    if KEY_FILE.exists():
        try:
            return _fernet().decrypt(KEY_FILE.read_bytes()).decode(), "stored"
        except Exception:
            return None, "unreadable"
    return None, None


def save_key(key):
    key = (key or "").strip()
    if len(key) < 16 or any(c.isspace() for c in key):
        raise ValueError("that doesn't look like an api key")
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_bytes(_fernet().encrypt(key.encode()))
    os.chmod(KEY_FILE, 0o600)


def clear_key():
    if KEY_FILE.exists():
        KEY_FILE.unlink()


def base_url():
    return os.environ.get("ACO_AI_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")


def model_name():
    return os.environ.get("ACO_AI_MODEL", "openai/gpt-5.6-luna")


def status_text():
    key, src = stored_key()
    if not key:
        return "ai: not set up. admin: /ai setup, then paste the key."
    where = "from server env" if src == "env" else "stored encrypted"
    return f"ai: on ({where}). model {model_name()}."


# ---------- context ----------
_HISTORY = {}      # uid -> deque of {"role","text"}
_LAST_CONTRACT = {}  # uid -> 0x address


def remember(uid, role, text):
    dq = _HISTORY.setdefault(uid, deque(maxlen=CTX_LEN))
    dq.append({"role": role, "text": (text or "")[:500]})


def history(uid):
    return list(_HISTORY.get(uid, []))


def set_last_contract(uid, addr):
    if addr and ADDR_RE.match(addr):
        _LAST_CONTRACT[uid] = addr


def last_contract(uid):
    return _LAST_CONTRACT.get(uid)


# ---------- LLM call ----------
def call_llm(messages):
    """OpenAI-compatible chat completion. Returns the message content string."""
    key, _ = stored_key()
    if not key:
        raise AIError("ai not configured")
    import requests
    msgs = list(messages)
    if not any("json" in (m.get("content") or "").lower() for m in msgs):
        msgs = [{"role": "system", "content": "Respond only with a valid JSON object."}] + msgs
    payload_msgs = msgs
    models = [model_name(), "openai/gpt-4o-mini"] if model_name() != "openai/gpt-4o-mini" else [model_name()]
    last = None
    for m in models:
        r = requests.post(base_url() + "/chat/completions",
                          headers={"Authorization": f"Bearer {key}"},
                          json={"model": m, "messages": payload_msgs, "temperature": 0,
                                "response_format": {"type": "json_object"}, "max_tokens": 800},
                          timeout=90)
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"]
        last = r.status_code
        if r.status_code not in (402, 429, 500, 502, 503):
            break
    raise AIError(f"llm http {last}")


# ---------- parse + validate ----------
def _coerce(kind, val):
    if kind == "addr":
        if not isinstance(val, str) or not ADDR_RE.match(val):
            raise ValueError("bad address")
        return val
    if kind in ("chain", "evm_chain"):
        v = str(val).lower()
        if v not in (EVM_CHAINS if kind == "evm_chain" else CHAINS):
            raise ValueError("bad chain")
        return v
    if kind == "speed":
        v = str(val).lower()
        if v not in SPEEDS:
            raise ValueError("bad speed")
        return v
    if kind == "int":
        if isinstance(val, bool) or not str(val).lstrip("-").isdigit():
            raise ValueError("bad int")
        return int(val)
    if kind == "amount":
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            v = f"{val:.12f}".rstrip("0").rstrip(".") or "0"
        else:
            v = str(val).strip()
        if not AMOUNT_RE.match(v):
            raise ValueError("bad amount")
        return v.lower()
    if kind == "token":
        v = str(val).strip()
        if v in ("***", "*", "x", "TOKEN"):
            v = "native"
        if v.lower() in ("eth", "ether", "ethereum", "rbh"):
            v = "native"
        if not TOKEN_RE.match(v):
            raise ValueError("bad token")
        return v
    if kind == "to":
        v = str(val).strip()
        if v.lower() == "main":
            return "main"
        if not ADDR_RE.match(v):
            raise ValueError("bad destination")
        return v
    if kind == "who":
        v = re.sub(r"(?i)^wallet\s*", "", str(val).strip())
        if v.startswith("@") and len(v) <= 33:
            return v
        if not WHO_RE.match(v):
            raise ValueError("bad wallet")
        return v
    if kind == "str":
        v = str(val).strip()
        if len(v) > 200:
            raise ValueError("too long")
        return v
    raise ValueError("unknown type")


def validate(obj, uid, is_admin, lc=None):
    """Strict check of the LLM output. Returns {"action","args"} or raises ValueError."""
    if not isinstance(obj, dict):
        raise ValueError("not an object")
    action = obj.get("action")
    if action not in ACTIONS:
        raise ValueError("unknown action")
    admin_only, schema = ACTIONS[action]
    if admin_only and not is_admin:
        raise PermissionError("admin only")
    raw = obj.get("args") or {}
    if not isinstance(raw, dict):
        raise ValueError("args not an object")
    extra = set(raw) - set(schema)
    if extra:
        raise ValueError("unexpected args")
    args = {}
    for name, (kind, required) in schema.items():
        if name not in raw or raw[name] in (None, ""):
            if required:
                raise ValueError(f"missing {name}")
            continue
        val = raw[name]
        if kind == "addr" and isinstance(val, str) and val.lower() in ("last", "last contract", "that"):
            if not lc:
                raise ValueError("no last contract yet")
            val = lc
        args[name] = _coerce(kind, val)
    return {"action": action, "args": args}


def parse(uid, text, is_admin, lc=None, complete=None):
    """Return validated {"action","args"}. Unknown/invalid output -> unknown action. Never raises for bad LLM output."""
    complete = complete or call_llm
    msgs = [{"role": "system", "content": SYSTEM}]
    for h in history(uid):
        msgs.append({"role": "user" if h["role"] == "user" else "assistant", "content": h["text"]})
    msgs.append({"role": "user", "content": text})
    try:
        content = complete(msgs)
        obj = json.loads(content)
        return validate(obj, uid, is_admin, lc)
    except PermissionError:
        return {"action": "unknown", "args": {"reply": "that one's admin only. you can ask for your balances or help."}}
    except AIError:
        raise
    except Exception:
        return {"action": "unknown", "args": {"reply": "i didn't get that. try /help or rephrase."}}


HELP = ("say it plain, e.g. \"schedule robinhood mint 0x... at 20:00 utc\", \"check my balances\", "
        "\"send 0.1 eth from wallet 2 to main\", \"cancel mint 3\". money moves always show a confirm button first.")
