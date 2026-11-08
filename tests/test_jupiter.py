import json
import urllib.parse

import pytest

from acobot import jupiter


class Response:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.value).encode()


def test_quote_builds_encoded_request_and_returns_executable_route():
    seen = {}

    def opener(request, timeout):
        seen["request"] = request
        seen["timeout"] = timeout
        return Response({"outAmount": "42", "otherAmountThreshold": "40", "routePlan": [{}]})

    result = jupiter.quote("input/mint", "output mint", 100, slippage_bps=125,
                           api_key="test-key", opener=opener)
    parsed = urllib.parse.urlparse(seen["request"].full_url)
    query = urllib.parse.parse_qs(parsed.query)
    assert parsed.path == "/swap/v1/quote"
    assert query["inputMint"] == ["input/mint"]
    assert query["outputMint"] == ["output mint"]
    assert query["amount"] == ["100"]
    assert query["slippageBps"] == ["125"]
    assert seen["request"].get_header("X-api-key") == "test-key"
    assert seen["timeout"] == 12
    assert result["outAmount"] == "42"


@pytest.mark.parametrize(
    ("input_mint", "output_mint", "amount", "slippage"),
    [("", "out", 1, 200), ("in", "", 1, 200), ("in", "out", 0, 200),
     ("in", "out", 1, -1), ("in", "out", 1, 5001)],
)
def test_quote_rejects_invalid_inputs(input_mint, output_mint, amount, slippage):
    with pytest.raises(ValueError):
        jupiter.quote(input_mint, output_mint, amount, slippage_bps=slippage,
                      opener=lambda *_a, **_k: pytest.fail("invalid quote made a request"))


def test_quote_rejects_non_executable_response():
    with pytest.raises(RuntimeError, match="no executable quote"):
        jupiter.quote("in", "out", 1, opener=lambda *_a, **_k: Response({"outAmount": "1"}))


def test_build_swap_posts_quote_without_signing_and_returns_unsigned_transaction():
    seen = {}

    def opener(request, timeout):
        seen["request"] = request
        seen["timeout"] = timeout
        return Response({"swapTransaction": "unsigned-base64"})

    quote = {"routePlan": [{"swapInfo": {}}], "outAmount": "10"}
    result = jupiter.build_swap(quote, "wallet-public-key", api_key="test-key", opener=opener)
    request = seen["request"]
    body = json.loads(request.data)
    assert urllib.parse.urlparse(request.full_url).path == "/swap/v1/swap"
    assert body["quoteResponse"] == quote
    assert body["userPublicKey"] == "wallet-public-key"
    assert body["wrapAndUnwrapSol"] is True
    assert body["dynamicComputeUnitLimit"] is True
    assert request.get_header("X-api-key") == "test-key"
    assert seen["timeout"] == 20
    assert result["swapTransaction"] == "unsigned-base64"


def test_build_swap_requires_route_and_wallet():
    with pytest.raises(ValueError, match="valid Jupiter quote"):
        jupiter.build_swap({}, "wallet", opener=lambda *_a, **_k: pytest.fail("request made"))
    with pytest.raises(ValueError, match="valid Jupiter quote"):
        jupiter.build_swap({"routePlan": [{}]}, "", opener=lambda *_a, **_k: pytest.fail("request made"))


def test_build_swap_rejects_missing_transaction():
    with pytest.raises(RuntimeError, match="no swap transaction"):
        jupiter.build_swap({"routePlan": [{}]}, "wallet",
                           opener=lambda *_a, **_k: Response({}))
