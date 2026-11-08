"""Per-user wallet store. Keys are Fernet-encrypted at rest under keys/ (chmod 600).

Never logged or displayed. Only masked addresses leave this module. Refuses to save
without the cryptography package (no silent plaintext). Labels are positional
("Wallet 1" = first added = main wallet) and computed on read.
"""
import json, os, re, threading
from pathlib import Path
from eth_account import Account

KEYS = Path(os.path.expanduser("~/.folk/acobot/keys")); KEYS.mkdir(parents=True, exist_ok=True)
os.chmod(KEYS, 0o700)
STORE = KEYS / "wallets.json"
if os.path.exists(STORE):
    os.chmod(STORE, 0o600)
MASTER = KEYS / "master.key"
PRIV_RE = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")
MAX_WALLETS = 20
CHAINS = ("ethereum", "robinhood", "ink", "solana")
EVM_CHAINS = ("ethereum", "robinhood", "ink")
_lock = threading.Lock()


def supports_chain(wallet_chain, requested_chain):
    """Wallets stay scoped to the selected supported network."""
    return wallet_chain == requested_chain


def mask(addr):
    """Display form of a wallet. Addresses are public, so the full address is shown.
    Private keys never reach this module's output."""
    return str(addr or "")


def validate_key(text):
    return bool(PRIV_RE.match(text.strip()))


def address_of(priv_hex):
    p = priv_hex.strip()
    p = p if p.startswith("0x") else "0x" + p
    return Account.from_key(p).address


def _address_and_key(secret, chain):
    if chain == "solana":
        from acobot import solana
        if not solana.validate_secret(secret):
            raise ValueError("not a valid Solana private key")
        return solana.public_key(secret), secret.strip()
    if not validate_key(secret):
        raise ValueError("not a valid private key")
    return address_of(secret), secret.strip()


def _fernet():
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        raise RuntimeError("cryptography not installed; refusing to store keys in plaintext")
    if not MASTER.exists():
        MASTER.write_bytes(Fernet.generate_key())
        os.chmod(MASTER, 0o600)
    return Fernet(MASTER.read_bytes())


def _load():
    return json.loads(STORE.read_text()) if STORE.exists() else {}


def _save(data):
    tmp = STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data)); os.chmod(tmp, 0o600); os.replace(tmp, STORE)


def count(user_id):
    return len(_load().get(str(user_id), []))


def add_wallet(user_id, priv_hex, qty=1, chain="ethereum"):
    """Returns (address, masked, label). Raises ValueError on invalid, duplicate, or cap."""
    if chain not in CHAINS:
        raise ValueError("unknown chain")
    addr, key_to_encrypt = _address_and_key(priv_hex, chain)
    f = _fernet()
    with _lock:
        data = _load()
        entries = data.setdefault(str(user_id), [])
        if len(entries) >= MAX_WALLETS:
            raise ValueError(f"you already have {MAX_WALLETS} wallets (the cap). remove one first")
        if any(e["address"].lower() == addr.lower() and e.get("chain", "ethereum") == chain for e in entries):
            raise ValueError("that wallet is already saved on this chain")
        entries.append({"address": addr, "qty": int(qty), "chain": chain,
                        "enc_key": f.encrypt(key_to_encrypt.encode()).decode()})
        _save(data)
        label = f"Wallet {len(entries)}"
    return addr, mask(addr), label


def _main_addr(data, user_id):
    return (data.get("_main") or {}).get(str(user_id))


def list_wallets(user_id):
    """Labels are positional. The main wallet is the one starred in _main; if none is set
    (or it was removed) the first wallet is main."""
    data = _load()
    entries = data.get(str(user_id), [])
    main = _main_addr(data, user_id)
    if not any(e["address"].lower() == (main or "").lower() for e in entries):
        main = entries[0]["address"] if entries else None
    out = []
    for i, e in enumerate(entries):
        out.append({"label": f"Wallet {i + 1}", "address": e["address"], "masked": mask(e["address"]),
                    "qty": e.get("qty", 1), "chain": e.get("chain", "ethereum"),
                    "main": bool(main) and e["address"].lower() == main.lower()})
    return out


def main_wallet(user_id):
    ws = list_wallets(user_id)
    return next((w for w in ws if w["main"]), None)


def main_wallet_for(user_id, chain):
    """Main wallet usable on `chain`: an entry saved on that exact chain, else
    the starred main if it's an EVM wallet (same key/address works on every EVM chain)."""
    ws = list_wallets(user_id)
    for w in ws:
        if w["chain"] == chain:
            return w
    m = next((w for w in ws if w["main"]), None)
    if m and m["chain"] in EVM_CHAINS and chain in EVM_CHAINS:
        return m
    return None


def set_main(user_id, address):
    """Star any saved wallet as main. Returns False if the address is not one of this user's wallets."""
    with _lock:
        data = _load()
        entries = data.get(str(user_id), [])
        hit = next((e for e in entries if e["address"].lower() == address.lower()), None)
        if hit is None:
            return False
        data.setdefault("_main", {})[str(user_id)] = hit["address"]
        _save(data)
    return True


def set_chain(user_id, address, chain):
    if chain not in CHAINS:
        raise ValueError("unknown chain")
    with _lock:
        data = _load()
        for e in data.get(str(user_id), []):
            if e["address"].lower() == address.lower():
                e["chain"] = chain; _save(data); return True
    return False


def chain_map(user_id):
    """{lowercase address: chain} for signer/fleet filtering. Never returns keys."""
    return {e["address"].lower(): e.get("chain", "ethereum") for e in _load().get(str(user_id), [])}


def set_qty(user_id, address, qty):
    with _lock:
        data = _load()
        for e in data.get(str(user_id), []):
            if e["address"].lower() == address.lower():
                e["qty"] = int(qty); _save(data); return True
    return False


def remove_wallet(user_id, address):
    with _lock:
        data = _load()
        before = len(data.get(str(user_id), []))
        data[str(user_id)] = [e for e in data.get(str(user_id), []) if e["address"].lower() != address.lower()]
        if (data.get("_main") or {}).get(str(user_id), "").lower() == address.lower():
            data["_main"].pop(str(user_id), None)
        _save(data)
        return before != len(data[str(user_id)])


DECRYPT_FAILS = []  # masked addresses of saved wallets whose key no longer decrypts (last call)


def signing_entries(user_ids, only=None, chain=None):
    """[(owner_id, label, address, priv_hex, qty)] for signer use only; never display.

    only: optional set of lowercase addresses to restrict to.
    A wallet whose key cannot be decrypted is skipped (recorded in DECRYPT_FAILS), not fatal.
    """
    f = _fernet(); out = []; seen = set()
    data = _load()
    DECRYPT_FAILS.clear()
    for uid in user_ids:
        for i, e in enumerate(data.get(str(uid), [])):
            entry_chain = e.get("chain", "ethereum")
            if chain is not None and not supports_chain(entry_chain, chain):
                continue
            if only is not None and e["address"].lower() not in only:
                continue
            identity = (str(uid), e["address"].lower())
            if identity in seen:
                continue
            try:
                key = f.decrypt(e["enc_key"].encode()).decode()
            except Exception:
                DECRYPT_FAILS.append(mask(e["address"]))
                continue
            seen.add(identity)
            out.append((uid, f"Wallet {i + 1}", e["address"], key, e.get("qty", 1)))
    return out


def generate_wallet(chain="ethereum"):
    """Generate a fresh key in-process. The key never travels over Telegram."""
    if chain == "solana":
        from acobot import solana
        return solana.generate_wallet()
    if chain not in EVM_CHAINS:
        raise ValueError("unknown chain")
    from eth_account import Account
    acct = Account.create()
    priv = acct.key.hex()
    if priv.startswith("0x"):
        priv = priv[2:]
    return priv, acct.address

def remove_all(user_id):
    with _lock:
        data = _load()
        n = len(data.get(str(user_id), []))
        data[str(user_id)] = []
        _save(data)
        return n


def clear_main(user_id):
    with _lock:
        data = _load()
        if isinstance(data.get("_main"), dict):
            data["_main"].pop(str(user_id), None)
            _save(data)
