"""Allowlist eligibility prober.

SeaDrop stores its allowlist root on-chain, but the project's signer or mint service
provides wallet-specific proofs/signatures. This module checks eligibility only; it
also preserves authorization returned by the same endpoint for a separate resolver.
Result is always one of:
  eligible True  -> endpoint explicitly said yes
  eligible False -> endpoint explicitly said no
  eligible None  -> no endpoint answered. unknown, NOT false.
"""
import asyncio
import json
import re
from pathlib import Path
from urllib.parse import quote

import httpx

SOURCES_FILE = Path(__file__).resolve().parent.parent / "data" / "eligible_sources.json"

ME_HOSTS = (
    "https://api-mainnet.magiceden.dev",
    "https://api-mainnet.magiceden.us",
)
TIMEOUT = 8.0
UA = {"user-agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
      "accept": "application/json"}

TRUE_KEYS = ("eligible", "isEligible", "allowed", "isAllowed", "whitelisted", "isWhitelisted",
             "inAllowlist", "allowlisted", "canMint")
FALSE_KEYS = TRUE_KEYS
SIG_KEYS = ("signature", "signedMint", "mintSignature", "proof", "merkleProof")

_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")


def _unknown(detail="no eligibility endpoint answered"):
    return {"eligible": None, "authorization": "unknown", "source": None, "detail": detail}


def _verdict(data):
    """Read a JSON body. Returns (eligible_bool_or_None, detail)."""
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        return None, "unparsed body"
    for k in TRUE_KEYS:
        if k in data and isinstance(data[k], bool):
            return data[k], f"{k}={data[k]}"
    for k in SIG_KEYS:
        v = data.get(k)
        if v and (isinstance(v, str) and v not in ("0x", "[]") or isinstance(v, list) and v):
            return None, f"authorization field {k} returned; eligibility was not provided"
    inner = data.get("data")
    if isinstance(inner, (dict, list)):
        return _verdict(inner)
    return None, "no eligibility field"


def _authorization_payload(data):
    """Keep the original endpoint response when it contains transaction authorization."""
    if isinstance(data, list):
        return next((payload for item in data
                     if (payload := _authorization_payload(item)) is not None), None)
    if not isinstance(data, dict):
        return None

    transaction = data.get("transaction")
    if isinstance(transaction, dict) and any(
            isinstance(transaction.get(key), str) and transaction[key].startswith("0x")
            for key in ("data", "input", "input_data")):
        return data
    if any(isinstance(data.get(key), str) and data[key].startswith("0x")
           for key in ("data", "calldata", "input", "input_data")):
        return data

    authorization = data.get("authorization") or data.get("auth") or data.get("mintAuthorization")
    if isinstance(authorization, dict):
        if any(authorization.get(key) for key in
               ("signature", "signedMint", "mintSignature", "proof", "merkleProof", "merkle_proof")):
            return data

    if any(data.get(key) for key in (
            "signature", "signedMint", "mintSignature", "proof", "merkleProof", "merkle_proof")):
        return data

    for key in ("data", "result", "payload"):
        nested = data.get(key)
        if isinstance(nested, (dict, list)):
            payload = _authorization_payload(nested)
            if payload is not None:
                return data
    return None


def _get(client, url):
    try:
        r = client.get(url)
    except Exception as e:  # network / timeout / tls
        return None, f"{type(e).__name__}"
    if r.status_code != 200:
        return None, f"http {r.status_code}"
    try:
        return r.json(), "ok"
    except Exception:
        return None, "not json"


def load_sources(path=None):
    p = Path(path) if path else SOURCES_FILE
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def save_source(contract, template, path=None):
    """admin-set manual endpoint for one contract. template must hold {wallet}."""
    if "{wallet}" not in template or not template.startswith("https://"):
        raise ValueError("template must be an https url containing {wallet}")
    p = Path(path) if path else SOURCES_FILE
    data = load_sources(p)
    data[contract.lower()] = template
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2))


def _manual(contract):
    tpl = load_sources().get((contract or "").lower())
    return tpl


def _candidates(contract, chain, wallet, stage_info):
    """Ordered endpoint list (source label, url). Discovery is data-driven: the
    magic eden collection is keyed by chain slug, the project signer by mint_site."""
    out = []
    manual = _manual(contract)
    if manual:
        out.append(("manual", manual.replace("{wallet}", wallet)))
    stage = stage_info or {}
    stage_key = stage.get("label") or stage.get("key") or stage.get("list_mode") or ""
    stage_index = "" if stage.get("index") is None else str(stage.get("index"))
    list_mode = stage.get("list_mode") or ""
    if (chain or "").lower() != "robinhood_testnet":
        chain_slug = "robinhood" if (chain or "").lower().startswith("robin") else (chain or "ethereum").lower()
        query = (
            f"?address={wallet}&stage={quote(str(stage_key), safe='')}"
            f"&stage_index={quote(stage_index, safe='')}"
            f"&list_mode={quote(str(list_mode), safe='')}"
        )
        for host in ME_HOSTS:
            out.append(("magiceden",
                        f"{host}/v3/rtp/{chain_slug}/collections/{contract}/eligibility{query}"))
    # c. the project's own signer / mint site, when known
    site = ((stage_info or {}).get("mint_site") or "").rstrip("/")
    if site:
        out.append(("mint_site", f"{site}/api/allowlist/{wallet}"))
        out.append(("mint_site", f"{site}/api/eligibility?address={wallet}"))
    return out


def _merkle_file(stage_info, wallet):
    """b. merkle: public allowlist json some projects host. only checked when the stage has a root."""
    root = (stage_info or {}).get("merkle_root")
    site = ((stage_info or {}).get("mint_site") or "").rstrip("/")
    if not root or not site:
        return None
    return [("merkle_file", f"{site}/allowlist.json")]


def _check_merkle_file(client, url, wallet):
    body, why = _get(client, url)
    if body is None:
        return None, why
    entries = body if isinstance(body, list) else (body.get("addresses") or body.get("entries") or [])
    if not isinstance(entries, list):
        return None, "unexpected shape"
    addrs = set()
    for e in entries:
        a = e.get("address") if isinstance(e, dict) else e
        if isinstance(a, str):
            addrs.add(a.lower())
    return (wallet.lower() in addrs), f"{len(addrs)} listed"


def probe_sync(contract, chain, wallet, stage_info=None, client=None):
    if not _ADDR.match(contract or "") or not _ADDR.match(wallet or ""):
        return _unknown("bad address")
    own = client is None
    client = client or httpx.Client(headers=UA, timeout=TIMEOUT, follow_redirects=True)
    try:
        tried = []
        for source, url in _candidates(contract, chain, wallet, stage_info or {}):
            body, why = _get(client, url)
            tried.append(f"{source}:{why}")
            if body is None:
                continue
            ok, detail = _verdict(body)
            authorization = _authorization_payload(body)
            if ok is None:
                if authorization is not None:
                    return {"eligible": None, "authorization": "available",
                            "authorization_payload": authorization,
                            "source": source, "detail": detail}
                continue
            result = {"eligible": ok,
                      "authorization": "available" if authorization is not None else "not_returned",
                      "source": source, "detail": detail}
            if authorization is not None:
                result["authorization_payload"] = authorization
            return result
        for source, url in _merkle_file(stage_info or {}, wallet) or []:
            ok, why = _check_merkle_file(client, url, wallet)
            tried.append(f"{source}:{why}")
            if ok is not None:
                return {"eligible": ok, "authorization": "not_returned",
                        "source": source, "detail": why}
        return _unknown("; ".join(tried) or "no endpoint")
    finally:
        if own:
            client.close()


async def probe(contract, chain, wallet, stage_info=None, client=None):
    """Async wrapper: httpx runs in a worker thread so the bot loop never blocks."""
    return await asyncio.to_thread(probe_sync, contract, chain, wallet, stage_info, client)


async def probe_fleet(contract, chain, wallets, stage_info=None):
    """wallets: list of {label, address}. returns [(wallet_dict, result)] in input order."""
    results = await asyncio.gather(*[probe(contract, chain, w["address"], stage_info) for w in wallets],
                                   return_exceptions=True)
    out = []
    for w, r in zip(wallets, results):
        if isinstance(r, Exception):
            r = _unknown(f"probe error: {type(r).__name__}")
        out.append((w, r))
    return out


def panel_text(stage_label, results, mint_site_hint=False):
    """Plain html panel: eligible / not eligible / unknown."""
    yes = [w["label"] for w, r in results if r["eligible"] is True]
    no = [w["label"] for w, r in results if r["eligible"] is False]
    unk = [w["label"] for w, r in results if r["eligible"] is None]
    with_auth = [w["label"] for w, r in results if r.get("authorization") == "available"]
    lines = [f"<b>🎟 eligibility · {stage_label}</b>", ""]
    lines.append("✅ eligible: " + (", ".join(yes) if yes else "none"))
    lines.append("❌ not eligible: " + (", ".join(no) if no else "none"))
    lines.append("❓ unknown (no eligibility verdict): " + (", ".join(unk) if unk else "none"))
    lines.append(
        "🔐 authorization payload returned (not yet validated): " + ", ".join(with_auth)
        if with_auth else "🔐 mint authorization: not provided by this eligibility response")
    if yes:
        lines.append("")
        lines.append("informational only: wallet selection is unchanged.")
    elif unk and not no:
        lines.append("")
        lines.append("the project signer did not answer. eligibility is unverified, not a no.")
        lines.append("authorization discovery is retried at mint execution; unsupported sites need a documented adapter.")
    return "\n".join(lines)


def override_allowlist(cfg_path=None):
    """Read data/eligible_overrides.json: {"contract_lower:stage": {"mode": "fcfs_open"}}.

    The note records that the owner recognized an FCFS stage. It does not remove
    the allowlist gate or authorize a public mint."""
    p = Path(cfg_path or SOURCES_FILE).parent / "eligible_overrides.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text() or "{}")
    except Exception:
        return {}


def is_fcfs_open(contract, stage_key, cfg_path=None):
    ov = override_allowlist(cfg_path)
    mode = ov.get(f"{(contract or '').lower()}:{(stage_key or '').lower()}", {}).get("mode")
    return mode == "fcfs_open"


def set_fcfs_open(contract, stage_key, cfg_path=None):
    """Remember that this stage was identified as FCFS. The mint gate stays in place."""
    p = Path(cfg_path or SOURCES_FILE).parent / "eligible_overrides.json"
    try:
        data = json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        data = {}
    data[f"{(contract or '').lower()}:{(stage_key or '').lower()}"] = {"mode": "fcfs_open"}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2))
