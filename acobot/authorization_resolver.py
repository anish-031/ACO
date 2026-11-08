"""Safe project authorization inspection and resolution.

Only explicitly configured project APIs can return mint authorization. Website
inspection is read-only and reports candidate routes; it never probes unknown API
paths or bypasses project controls.
"""
import html
import ipaddress
import re
import socket
import threading
import time
from urllib.parse import urljoin, urlsplit

import httpx

from acobot import mintapi, mint_router

MAX_PAGE_BYTES = 1_000_000
MAX_SCRIPT_BYTES = 1_000_000
MAX_SCRIPTS = 8
SITE_CACHE_TTL_S = 3600
AUTH_CACHE_MAX_TTL_S = 30
_SCRIPT_RE = re.compile(r"""<script\b[^>]*\bsrc\s*=\s*["']([^"']+)["']""", re.I)
_ROUTE_RE = re.compile(
    r"""(?:"|')((?:https?://[^"' ]+)?/[^"' ]*(?:api|mint|allowlist|whitelist|claim|phase|stage)[^"' ]*)(?:"|')""",
    re.I,
)
_STATIC_ASSET_RE = re.compile(r"\.(?:js|css|map|png|jpe?g|gif|svg|ico|woff2?)(?:$|[?#])", re.I)
_SITE_CACHE = {}
_AUTH_CACHE = {}
_LOCK = threading.RLock()


def authorization_requirement(mint):
    view = dict(mint or {})
    stage = view.get("_selected_stage") or {}
    if not mint_router.is_gated(view) and not stage.get("gated"):
        return {
            "required": False,
            "type": "none",
            "missing": [],
            "description": "no wallet-specific phase authorization detected",
        }
    key = str(stage.get("chain_key") or stage.get("key") or view.get("phase") or "").lower()
    if key in {"allowlist", "allow_list"} or stage.get("merkle_root"):
        kind = "merkle_proof"
        required = ["wallet-specific Merkle proof", "phase mint parameters", "payment value"]
    elif key in {"signed", "signature"} or stage.get("signers"):
        kind = "project_signature"
        required = ["wallet-specific project signature", "phase mint parameters", "payment value"]
    else:
        kind = "project_specific"
        required = ["wallet-specific phase authorization", "phase mint parameters", "payment value"]
    return {
        "required": True,
        "type": kind,
        "missing": required,
        "description": "requires " + ", ".join(required),
    }


def project_report(mint):
    """Build the current analysis facts without conflating eligibility and authorization."""
    mint = mint or {}
    requirement = authorization_requirement(mint)
    gated = requirement["required"]
    api_configured = mintapi.supports_phase(mint.get("contract")) if gated else False
    phase_rows = mint.get("schedule") or mint.get("stages") or []
    phase_names = [
        str(stage.get("label") or stage.get("chain_key") or stage.get("key"))
        for stage in phase_rows if isinstance(stage, dict)
        and (stage.get("label") or stage.get("chain_key") or stage.get("key"))
    ]
    missing = []
    if gated:
        if not api_configured:
            missing.extend(requirement["missing"])
            missing.append("a supported endpoint returning usable authorization")
    else:
        if mint.get("price_wei") is None:
            missing.append("mint price")
        if not (mint.get("mint_sig") or mint.get("seadrop")):
            missing.append("supported mint transaction route")
            protocol = mint.get("protocol") or "unidentified contract"
            missing.append(f"registered transaction adapter for {protocol}")
    if not mint.get("start_ts") and not mint.get("sale_started_fn"):
        missing.append("phase opening time or on-chain live-sale signal")
    site = mint.get("official_site")
    site_result = mint.get("site_inspection") or {}
    route_available = (False if gated else
                       bool((mint.get("seadrop") or mint.get("mint_sig"))
                            and mint.get("price_wei") is not None))
    return {
        "network": mint.get("chain") or "unknown",
        "contract": mint.get("contract") or "unknown",
        "standard": mint.get("standard") or "not detected",
        "protocol": mint.get("protocol") or (
            "SeaDrop" if mint.get("seadrop") else "custom EVM contract"),
        "transaction_adapter": mint.get("transaction_adapter"),
        "official_site": site or "not discovered from available collection metadata",
        "api_candidates": site_result.get("api_candidates") or [],
        "phases": phase_names or list(mint.get("phases") or []),
        "selected_phase": mint.get("phase_label") or mint.get("phase") or "not selected",
        "price_wei": mint.get("price_wei"),
        "wallet_limit": mint.get("per_wallet"),
        "eligibility": "not checked (per-wallet eligibility is separate)",
        "authorization_type": requirement["type"],
        "authorization_available": ("not required" if not gated else
                                    "resolver configured; wallet response pending" if api_configured
                                    else "automatic endpoint discovery pending"),
        "auto_retrieval": ("not required" if not gated else
                           "yes, via configured phase API" if api_configured
                           else "will check Reservoir, Magic Eden, and supported eligibility endpoints"
                           if str(mint.get("chain") or "").lower() == "ethereum"
                           else "will check supported eligibility and project endpoints"),
        "missing": missing,
        "schedule_possible": bool(mint.get("start_ts") or mint.get("sale_started_fn")),
        "transaction_construction_possible": route_available,
    }


def _public_https_url(value):
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return False
    host = parsed.hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith((".localhost", ".local", ".internal")):
        return False
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal and not literal.is_global:
        return False
    try:
        addresses = socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
    except OSError:
        return False
    return bool(addresses) and all(ipaddress.ip_address(item[4][0]).is_global for item in addresses)


def inspect_project_site(site_url, *, get=None, clock=time.time):
    """Inspect an explicitly identified project website without calling its API."""
    parsed = urlsplit(site_url or "")
    if not _public_https_url(site_url):
        return {
            "site": None, "script_count": 0, "api_candidates": [],
            "error": "official project site is unavailable or not a public HTTPS URL",
        }
    origin = f"https://{parsed.netloc.lower()}"
    cache_key = site_url.rstrip("/")
    now = clock()
    with _LOCK:
        cached = _SITE_CACHE.get(cache_key)
        if cached and cached[0] > now:
            return dict(cached[1])

    client_get = get or httpx.get

    def fetch(url, limit):
        target = urljoin(origin + "/", url)
        target_parts = urlsplit(target)
        if target_parts.scheme != "https" or target_parts.netloc.lower() != parsed.netloc.lower():
            return None
        if not _public_https_url(target):
            return None
        try:
            response = client_get(
                target,
                headers={"User-Agent": mintapi.UA, "Accept": "text/html,application/javascript"},
                timeout=5,
                follow_redirects=False,
            )
            if response.status_code != 200:
                return None
            content = response.content
            if len(content) > limit:
                return None
            return content.decode("utf-8", "replace")
        except Exception:
            return None

    page = fetch(site_url, MAX_PAGE_BYTES)
    if page is None:
        return {
            "site": site_url, "script_count": 0, "api_candidates": [],
            "error": "could not inspect the public site page",
        }
    scripts = []
    for source in _SCRIPT_RE.findall(page):
        absolute = urljoin(site_url, html.unescape(source))
        if urlsplit(absolute).netloc.lower() == parsed.netloc.lower():
            scripts.append(absolute)
    candidates = set()
    for match in _ROUTE_RE.findall(page):
        parsed_match = urlsplit(match)
        if (not parsed_match.path.startswith(("/static/", "/assets/", "/_next/"))
                and not _STATIC_ASSET_RE.search(match)):
            candidates.add(match[:240])
    fetched_scripts = 0
    for script_url in list(dict.fromkeys(scripts))[:MAX_SCRIPTS]:
        script = fetch(script_url, MAX_SCRIPT_BYTES)
        if script is None:
            continue
        fetched_scripts += 1
        for match in _ROUTE_RE.findall(script):
            parsed_match = urlsplit(match)
            if (not parsed_match.path.startswith(("/static/", "/assets/", "/_next/"))
                    and not _STATIC_ASSET_RE.search(match)):
                candidates.add(match[:240])
    result = {
        "site": site_url,
        "script_count": fetched_scripts,
        "api_candidates": sorted(candidates)[:30],
        "error": None,
    }
    with _LOCK:
        _SITE_CACHE[cache_key] = (now + SITE_CACHE_TTL_S, result)
    return dict(result)


def _cache_key(chain_id, contract, wallet, phase, stage_index, qty):
    return (chain_id, (contract or "").lower(), (wallet or "").lower(),
            phase or "", stage_index, int(qty or 1))


def resolve_evm_authorization(mint, wallet, qty, chain, rpc, *, force_refresh=False):
    """Resolve and simulate one wallet's gated transaction from a supported endpoint."""
    requirement = authorization_requirement(mint)
    if not requirement["required"]:
        return None, "phase does not require wallet-specific authorization"
    contract = mint.get("contract")
    phase = mint.get("phase_label") or mint.get("phase")
    stage_index = mint.get("stage_index")
    key = _cache_key(chain.chain_id, contract, wallet, phase, stage_index, qty)
    now = time.time()
    if not force_refresh:
        with _LOCK:
            cached = _AUTH_CACHE.get(key)
            if cached and cached[0] > now:
                tx = dict(cached[1])
                reason = _simulate(tx, wallet, rpc)
                return (tx, "ok") if reason is None else (None, reason)
            if cached:
                _AUTH_CACHE.pop(key, None)

    expected_to = mint.get("mint_to") or contract
    phase_key = mint.get("phase")
    tx, reason = _load_authorization(
        mint, wallet, qty, chain, requirement, contract, phase, phase_key,
        stage_index, expected_to,
    )
    if tx is None:
        return None, reason
    if tx.get("value") is None:
        return None, "authorization omitted the required transaction payment value"
    try:
        mint_router.validate_api_selector(
            tx["data"], gated=True, seadrop=bool(mint.get("seadrop")), phase=phase_key)
    except mint_router.MintRouteError as exc:
        return None, str(exc)
    simulation_error = _simulate(tx, wallet, rpc)
    if simulation_error:
        return None, simulation_error
    expires_at = tx.get("authorization_expires_at")
    if tx.get("authorization_cacheable") and isinstance(expires_at, (int, float)):
        ttl = min(AUTH_CACHE_MAX_TTL_S, expires_at - now)
        if ttl > 0:
            with _LOCK:
                _AUTH_CACHE[key] = (now + ttl, dict(tx))
    return tx, "ok"


def _load_authorization(mint, wallet, qty, chain, requirement, contract, phase,
                        phase_key, stage_index, expected_to):
    """Try the configured phase API, then indexed drop APIs, then eligibility payloads."""
    common = dict(
        phase=phase, phase_key=phase_key, stage_index=stage_index, qty=qty,
        expected_to=expected_to, expected_chain_id=chain.chain_id, gated=True,
        seadrop=bool(mint.get("seadrop")),
        expected_fee_recipient=mint.get("fee_recipient"),
        expected_price_wei=mint.get("price_wei"),
    )
    errors = []
    if mintapi.supports_phase(contract):
        tx, reason = mintapi.fetch_transaction(contract, wallet, **common)
        if tx is not None:
            return tx, reason
        errors.append(reason or "configured phase API returned no transaction")

    from acobot import dropmint

    indexed, indexed_reason = dropmint.fetch_authorization(mint, wallet, qty, chain)
    if indexed is not None:
        tx, reason = mintapi.transaction_from_response(indexed, contract, wallet, **common)
        if tx is not None:
            return tx, reason
        errors.append(f"indexed mint API: {reason}")
    elif indexed_reason:
        errors.append(f"indexed mint API: {indexed_reason}")

    from acobot import eligible

    stage = dict(mint.get("_selected_stage") or {})
    stage.update({
        "key": stage.get("chain_key") or stage.get("key") or phase_key,
        "label": stage.get("label") or phase,
        "merkle_root": stage.get("merkle_root") or mint.get("merkle_root"),
        "mint_site": stage.get("mint_site") or mint.get("mint_site"),
        "list_mode": stage.get("list_mode") or mint.get("list_mode"),
        "index": stage.get("index", stage_index),
    })
    result = eligible.probe_sync(contract, chain.name, wallet, stage)
    payload = result.get("authorization_payload")
    if payload is None:
        missing = ", ".join(requirement["missing"])
        detail = result.get("detail") or "no supported endpoint returned authorization data"
        suffix = f" {' | '.join(errors)}" if errors else ""
        return None, f"missing {missing}; supported authorization discovery: {detail}{suffix}"
    tx, reason = mintapi.transaction_from_response(payload, contract, wallet, **common)
    if tx is None and errors:
        return None, f"{requirement['description']}: {reason}; {' | '.join(errors)}"
    if tx is None:
        return None, f"{requirement['description']}: {reason}"
    return tx, reason


def inspect_solana_program(program_id, *, rpc_url=None):
    """Return read-only Solana Anchor program facts; never infers a mint transaction."""
    from acobot import solana

    result = solana.inspect_program(program_id, rpc_url=rpc_url)
    instructions = result.get("instructions") or []
    mint_instructions = [
        item for item in instructions
        if re.search(r"(mint|claim|purchase|redeem)", str(item.get("name") or ""), re.I)
    ]
    auth_terms = ("proof", "signature", "allowlist", "whitelist", "allocation", "phase", "merkle")
    auth_args = sorted({
        str(argument)
        for instruction in mint_instructions
        for argument in instruction.get("args", [])
        if any(term in str(argument).lower() for term in auth_terms)
    })
    return {
        "network": "Solana Mainnet",
        "program_id": result["program_id"],
        "protocol": "Anchor IDL" if result.get("name") else "Solana program",
        "name": result.get("name"),
        "version": result.get("version"),
        "instructions": instructions,
        "mint_instructions": mint_instructions,
        "authorization_arguments": auth_args,
        "authorization_type": ("IDL exposes authorization-like inputs: " + ", ".join(auth_args)
                               if auth_args else
                               "unknown; a read-only IDL cannot prove that authorization is not required"),
        "transaction_construction_possible": False,
        "missing": ["verified protocol-specific Solana mint adapter",
                    "wallet-specific authorization and mint account mapping"],
    }


def _simulate(transaction, wallet, rpc):
    try:
        rpc.call("eth_estimateGas", [{
            "from": wallet,
            "to": transaction["to"],
            "data": transaction["data"],
            "value": hex(transaction["value"]),
        }])
    except Exception as exc:
        from acobot.rpc import err_text
        return f"phase transaction simulation failed: {err_text(exc)[:120]}"
    return None
