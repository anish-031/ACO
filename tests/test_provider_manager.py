import json
import ssl
import urllib.parse
import urllib.error

from acobot.provider_manager import ProviderManager


NFT = "0x" + "12" * 20
ABI = [{"type": "function", "name": "mint", "inputs": []}]


class Response:
    status = 200

    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _size=-1):
        return json.dumps(self.value).encode()


def test_sourcify_full_match_abi_is_cached_and_provider_metrics_recorded():
    calls = []

    def opener(request, timeout):
        calls.append((request.full_url, timeout))
        return Response({"output": {"abi": ABI}})

    manager = ProviderManager(opener=opener)
    abi_items, source, errors = manager.verified_abi(1, NFT)
    second, second_source, _second_errors = manager.verified_abi(1, NFT)

    assert abi_items == ABI and second == ABI
    assert source == second_source == "Sourcify"
    assert errors == []
    assert len(calls) == 1
    assert "/full_match/1/" in calls[0][0]
    assert manager.metrics()["Sourcify full_match"]["requests"] == 1


def test_blockscout_used_only_when_chain_endpoint_is_configured(monkeypatch):
    seen = []

    def opener(request, timeout):
        seen.append(request.full_url)
        if "sourcify" in request.full_url:
            raise urllib.error.HTTPError(request.full_url, 404, "not found", None, None)
        return Response({"abi": ABI})

    monkeypatch.setenv("INK_BLOCKSCOUT_API_URL", "https://explorer.example/api/v2")
    manager = ProviderManager(opener=opener)
    abi_items, source, _errors = manager.verified_abi(57073, NFT)

    assert abi_items == ABI
    assert source == "Blockscout"
    assert seen[-1] == f"https://explorer.example/api/v2/smart-contracts/{NFT}"


def test_etherscan_v2_is_ethereum_only_and_uses_configured_api_key(monkeypatch):
    seen = []

    def opener(request, _timeout):
        seen.append(urllib.parse.urlsplit(request.full_url))
        if "etherscan.io" not in request.full_url:
            raise urllib.error.HTTPError(request.full_url, 404, "not found", None, None)
        return Response({"status": "1", "result": json.dumps(ABI)})

    monkeypatch.setenv("ETHERSCAN_API_KEY", "unit-test-key")
    manager = ProviderManager(opener=opener)
    abi_items, source, _errors = manager.verified_abi(1, NFT)

    assert abi_items == ABI
    assert source == "Etherscan V2"
    etherscan = next(item for item in seen if item.netloc == "api.etherscan.io")
    assert urllib.parse.parse_qs(etherscan.query)["chainid"] == ["1"]
    assert urllib.parse.parse_qs(etherscan.query)["apikey"] == ["unit-test-key"]


def test_provider_opener_uses_certifi_with_tls_verification_enabled(monkeypatch):
    import acobot.provider_manager as providers

    manager = ProviderManager()
    context = ssl.create_default_context(cafile=providers.certifi.where())
    with monkeypatch.context() as patch:
        patch.setattr(providers.ssl, "create_default_context", lambda **_kwargs: context)
        patch.setattr(providers.urllib.request, "urlopen", lambda *args, **kwargs: (args, kwargs))
        _args, kwargs = manager._open(urllib.request.Request("https://example.com"), 3)

    assert kwargs["context"] is context
    assert kwargs["timeout"] == 3
    assert context.check_hostname
    assert context.verify_mode == ssl.CERT_REQUIRED


def test_rate_limited_provider_is_cooled_down_and_falls_back(monkeypatch):
    calls = []

    def opener(request, _timeout):
        url = request.full_url
        calls.append(url)
        if "sourcify" in url:
            raise urllib.error.HTTPError(url, 429, "rate limit", None, None)
        return Response({"abi": ABI})

    monkeypatch.setenv("INK_BLOCKSCOUT_API_URL", "https://explorer.example/api/v2")
    now = [100.0]
    manager = ProviderManager(opener=opener, clock=lambda: now[0])
    abi_items, source, errors = manager.verified_abi(57073, NFT)

    assert abi_items == ABI
    assert source == "Blockscout"
    assert any("HTTP 429" in error for error in errors)
    assert manager.metrics()["Sourcify full_match"]["rate_limited"] == 1
    assert manager.verified_abi(57073, NFT)[0] == ABI
    assert len(calls) == 2


def test_public_keyless_explorers_are_used_without_an_etherscan_key(monkeypatch):
    monkeypatch.delenv("ETHERSCAN_API_KEY", raising=False)
    monkeypatch.delenv("ETHEREUM_BLOCKSCOUT_API_URL", raising=False)
    calls = []
    manager = ProviderManager(opener=lambda request, timeout: calls.append(request.full_url))

    abi_items, source, errors = manager.verified_abi(1, NFT)

    assert abi_items == [] and source is None
    assert any("repo.sourcify.dev" in url for url in calls)
    assert any(url.startswith("https://eth.blockscout.com/api/v2/smart-contracts/") for url in calls)
    assert any("api.routescan.io/v2/network/mainnet/evm/1/etherscan/api" in url for url in calls)
    assert all("api.etherscan.io" not in url for url in calls)
    assert errors
