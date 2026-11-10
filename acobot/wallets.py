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
    """An EVM key works on every EVM chain. Solana stays on Solana."""
    if wallet_chain == requested_chain:
        return True
    return wallet_chain in EVM_CHAINS and requested_chain in EVM_CHAINS


def mask(addr):
    """Display form of a wallet. Addresses are public, so the full address is shown.
    Private keys never reach this module's output."""
    return str(addr or "")


def validate_key(text):
    return bool(PRIV_RE.match(str(text or "").strip()))


def address_of(priv_hex):
    p = str(priv_hex).strip()
    p = p if p.startswith("0x") else "0x" + p
    return Account.from_key(p).address


def normalize_secret(secret, chain):
    """Pull one private key out of a pasted message.

    Accepts a bare key, a quoted key, a key buried in other text, a 12/24 word
    seed, and a Solana base58 key or byte array.
    """
    text = str(secret or "").strip()
    if not text:
        raise ValueError("paste a private key")
    if chain == "solana":
        from acobot import solana
        if "[" in text and "]" in text:
            blob = text[text.find("["):text.rfind("]") + 1]
            if solana.validate_secret(blob):
                return blob
        for token in text.replace(",", " ").replace("\n", " ").split():
            token = token.strip().strip("`").strip('"').strip("'")
            if token and solana.validate_secret(token):
                return token
        raise ValueError("not a valid Solana private key")
    compact = text.replace("\n", " ")
    words = [word.strip("`,.\"'").lower() for word in compact.split() if word.strip("`,.\"'")]
    if len(words) in (12, 15, 18, 21, 24) and all(re.fullmatch(r"[a-z]+", word) for word in words):
        Account.enable_unaudited_hdwallet_features()
        try:
            account = Account.from_mnemonic(" ".join(words))
        except Exception as exc:
            raise ValueError("that seed phrase could not be read") from exc
        priv = account.key.hex()
        return priv if priv.startswith("0x") else "0x" + priv
    match = re.search(r"(?i)(?<![0-9a-f])(?:0x)?([0-9a-f]{64})(?![0-9a-f])", text)
    if not match:
        raise ValueError("not a valid private key. paste the 0x key or the 12/24 word seed")
    return "0x" + match.group(1)


def _address_and_key(secret, chain):
    secret = normalize_secret(secret, chain)
    if chain == "solana":
        from acobot import solana
        return solana.public_key(secret), secret
    return address_of(secret), secret


def _fernet():
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        from acobot import deps
        deps.ensure()
        try:
            from cryptography.fernet import Fernet
        except ImportError:
            raise RuntimeError("cryptography not installed; refusing to store keys in plaintext")
    if not MASTER.exists():
        MASTER.write_bytes(Fernet.generate_key())
        os.chmod(MASTER, 0o600)
    return Fernet(MASTER.read_bytes())


def _load():
    if not STORE.exists():
        return {}
    try:
        data = json.loads(STORE.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"wallet file could not be read ({type(exc).__name__})") from exc
    if not isinstance(data, dict):
        raise RuntimeError("wallet file could not be read")
    return data


def _save(data):
    tmp = STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data)); os.chmod(tmp, 0o600); os.replace(tmp, STORE)


def count(user_id):
    entries = _load().get(str(user_id), [])
    return len({e["address"].lower() for e in entries})


def add_wallet(user_id, priv_hex, qty=1, chain="ethereum"):
    """Returns (address, masked, label). Raises ValueError on invalid, duplicate, or cap."""
    if chain not in CHAINS:
        raise ValueError("unknown chain")
    addr, key_to_encrypt = _address_and_key(priv_hex, chain)
    f = _fernet()
    with _lock:
        data = _load()
        entries = data.setdefault(str(user_id), [])
        same = [e for e in entries if e["address"].lower() == addr.lower()]
        if any(e.get("chain", "ethereum") == chain or
               (chain in EVM_CHAINS and e.get("chain", "ethereum") in EVM_CHAINS) for e in same):
            raise ValueError("that wallet is already saved on this chain")
        unique = {e["address"].lower() for e in entries}
        if addr.lower() not in unique and len(unique) >= MAX_WALLETS:
            raise ValueError(f"you already have {MAX_WALLETS} wallets (the cap). remove one first")
        entries.append({"address": addr, "qty": int(qty), "chain": chain,
                        "enc_key": f.encrypt(key_to_encrypt.encode()).decode()})
        _save(data)
        label = f"Wallet {len(unique) + (0 if addr.lower() in unique else 1)}"
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
    seen = {}
    for e in entries:
        key = e["address"].lower()
        chain = e.get("chain", "ethereum")
        if key in seen:
            row = seen[key]
            if chain not in row["chains"]:
                row["chains"].append(chain)
            row["qty"] = max(int(row["qty"]), int(e.get("qty", 1)))
            continue
        row = {"address": e["address"], "masked": mask(e["address"]),
               "qty": e.get("qty", 1), "chain": chain, "chains": [chain],
               "main": bool(main) and key == main.lower()}
        seen[key] = row
        out.append(row)
    for i, row in enumerate(out):
        row["label"] = f"Wallet {i + 1}"
    return out


def main_wallet(user_id):
    ws = list_wallets(user_id)
    return next((w for w in ws if w["main"]), None)


def main_wallet_for(user_id, chain):
    """Starred wallet if it can sign on `chain`, otherwise the first wallet that can."""
    ws = list_wallets(user_id)
    for w in ws:
        if w["main"] and supports_chain(w["chain"], chain):
            return w
    for w in ws:
        if supports_chain(w["chain"], chain):
            return w
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
        hit = False
        for e in data.get(str(user_id), []):
            if e["address"].lower() == address.lower():
                e["qty"] = int(qty)
                hit = True
        if hit:
            _save(data)
        return hit


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


def signing_entries(user_ids, only=None, chain=None, evm_any=False):
    """[(owner_id, label, address, priv_hex, qty)] for signer use only; never display.

    only: optional set of lowercase addresses to restrict to.
    evm_any: a mint on one EVM chain can use a key saved under another EVM chain.
    The address is the same on every EVM network. Solana stays separate.
    A wallet whose key cannot be decrypted is skipped (recorded in DECRYPT_FAILS), not fatal.
    """
    f = _fernet(); out = []; seen = set()
    data = _load()
    DECRYPT_FAILS.clear()
    for uid in user_ids:
        for e in data.get(str(uid), []):
            entry_chain = e.get("chain", "ethereum")
            if chain is not None and not supports_chain(entry_chain, chain):
                if not (evm_any and entry_chain in EVM_CHAINS and chain in EVM_CHAINS):
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
            out.append((uid, f"Wallet {len(out) + 1}", e["address"], key, e.get("qty", 1)))
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
