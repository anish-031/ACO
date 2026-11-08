"""Provider selection and caching for verified EVM contract metadata.

Contract execution stays on the chain's configured RPC. Explorers are read-only
fallbacks for ABI metadata and never provide project mint authorization.
"""
import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import certifi

SUPPORTED_CHAIN_IDS = {"ethereum": 1, "robinhood": 4663, "ink": 57073}
# Public, keyless hosts verified against live responses. An env URL replaces the default.
PUBLIC_BLOCKSCOUT = {
    "ethereum": "https://eth.blockscout.com/api/v2",
    "ink": "https://explorer.inkonchain.com/api/v2",
}
ROUTESCAN_API = "https://api.routescan.io/v2/network/mainnet/evm/{chain_id}/etherscan/api"
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
CACHE_TTL_S = 600
MISS_TTL_S = 30
RATE_LIMIT_COOLDOWN_S = 15
MAX_RESPONSE_BYTES = 2_000_000


class ProviderManager:
    def __init__(self, *, opener=None, clock=time.monotonic):
        self._opener = opener or self._open
        self._clock = clock
        self._lock = threading.RLock()
        self._cache = {}
        self._cooldown_until = {}
        self._metrics = {}

    @staticmethod
    def _open(request, timeout):
        context = ssl.create_default_context(cafile=certifi.where())
        return urllib.request.urlopen(request, timeout=timeout, context=context)

    def _record(self, name, started, status, error=None, cooldown_key=None):
        metric = self._metrics.setdefault(name, {
            "requests": 0, "failures": 0, "rate_limited": 0, "last_ms": None,
        })
        metric["requests"] += 1
        metric["last_ms"] = round((self._clock() - started) * 1000, 1)
        if error:
            metric["failures"] += 1
        if status == 429:
            metric["rate_limited"] += 1
            self._cooldown_until[cooldown_key or name] = self._clock() + RATE_LIMIT_COOLDOWN_S

    def metrics(self):
        with self._lock:
            return {name: dict(value) for name, value in self._metrics.items()}

    @staticmethod
    def _configured_blockscout(chain):
        value = os.environ.get(f"{chain.upper()}_BLOCKSCOUT_API_URL", "").strip()
        if not value:
            return None
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            return None
        return value.rstrip("/")

    def _providers(self, chain):
        providers = [("Sourcify", "sourcify", None)]
        blockscout = self._configured_blockscout(chain) or PUBLIC_BLOCKSCOUT.get(chain)
        if blockscout:
            providers.append(("Blockscout", "blockscout", blockscout))
        etherscan_key = os.environ.get("ETHERSCAN_API_KEY", "").strip()
        if chain == "ethereum" and etherscan_key:
            providers.append(("Etherscan V2", "etherscan", etherscan_key))
        if chain == "ethereum":
            providers.append(("Routescan", "routescan", str(SUPPORTED_CHAIN_IDS["ethereum"])))
        return providers

    @staticmethod
    def _abi_from_body(provider, body):
        if not isinstance(body, dict):
            return None
        if provider == "Sourcify":
            output = body.get("output")
            abi_items = output.get("abi") if isinstance(output, dict) else None
            return abi_items or body.get("abi")
        if provider == "Blockscout":
            abi_items = body.get("abi")
            if isinstance(abi_items, str):
                try:
                    return json.loads(abi_items)
                except json.JSONDecodeError:
                    return None
            return abi_items
        result = body.get("result")
        if isinstance(result, str):
            try:
                return json.loads(result)
            except json.JSONDecodeError:
                return None
        return result

    def _fetch_provider(self, chain_id, chain, address, provider, kind, config):
        if kind == "sourcify":
            url = f"https://repo.sourcify.dev/contracts/{config}/{chain_id}/{address}/metadata.json"
            name = f"Sourcify {config}"
        elif kind == "blockscout":
            base = config
            url = (base.format(address=address) if "{address}" in base
                   else f"{base}/smart-contracts/{address}")
            name = f"Blockscout {chain}"
        elif kind == "routescan":
            query = urllib.parse.urlencode({
                "module": "contract", "action": "getabi", "address": address,
            })
            url = f"{ROUTESCAN_API.format(chain_id=config)}?{query}"
            name = "Routescan"
        else:
            query = urllib.parse.urlencode({
                "chainid": str(chain_id), "module": "contract", "action": "getabi",
                "address": address, "apikey": config,
            })
            url = f"https://api.etherscan.io/v2/api?{query}"
            name = "Etherscan V2"

        cooldown_key = provider if provider == "Sourcify" else name
        with self._lock:
            if self._cooldown_until.get(cooldown_key, 0) > self._clock():
                return None, f"{name} rate limited; cooldown active"
        request = urllib.request.Request(
            url, headers={"User-Agent": "acobot/0.1", "Accept": "application/json"})
        started = self._clock()
        status = None
        try:
            with self._opener(request, 5) as response:
                status = getattr(response, "status", 200)
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError("provider response exceeded size limit")
            body = json.loads(raw)
            abi_items = self._abi_from_body(provider, body)
            if not isinstance(abi_items, list):
                raise ValueError("provider response did not contain a verified ABI")
            abi_items = [item for item in abi_items if isinstance(item, dict)]
            if not abi_items:
                raise ValueError("verified ABI not found")
            self._record(name, started, status, cooldown_key=cooldown_key)
            return abi_items, None
        except urllib.error.HTTPError as exc:
            status = exc.code
            self._record(name, started, status, error=True, cooldown_key=cooldown_key)
            if status == 404:
                return None, f"{name} has no verified contract metadata"
            return None, f"{name} HTTP {status}"
        except Exception as exc:
            self._record(name, started, status, error=True, cooldown_key=cooldown_key)
            return None, f"{name} {type(exc).__name__}"

    def verified_abi(self, chain_id, address):
        if not isinstance(address, str) or not ADDRESS_RE.fullmatch(address):
            return [], None, ["invalid contract address"]
        chain = next((name for name, value in SUPPORTED_CHAIN_IDS.items()
                      if value == chain_id), None)
        if chain is None:
            return [], None, [f"no ABI providers configured for chain ID {chain_id}"]
        key = (chain_id, address.lower())
        now = self._clock()
        with self._lock:
            cached = self._cache.get(key)
            if cached and cached[0] > now:
                return list(cached[1]), cached[2], list(cached[3])

            errors = []
            for provider, kind, config in self._providers(chain):
                if kind == "sourcify":
                    matches = ("full_match", "partial_match")
                else:
                    matches = (None,)
                for match in matches:
                    abi_items, error = self._fetch_provider(
                        chain_id, chain, address, provider, kind, match or config)
                    if abi_items:
                        self._cache[key] = (
                            now + CACHE_TTL_S, tuple(abi_items), provider, tuple(errors))
                        return list(abi_items), provider, errors
                    if error:
                        errors.append(error)
            self._cache[key] = (now + MISS_TTL_S, (), None, tuple(errors))
            return [], None, errors


_manager = ProviderManager()


def verified_abi(chain_id, address):
    return _manager.verified_abi(chain_id, address)


def provider_metrics():
    return _manager.metrics()
