import pytest
from eth_abi import encode
from eth_account import Account

from acobot import evmswap, uniswap_api
from acobot.tokenbuy import _selector


KEY = "0x" + "11" * 32
WALLET = Account.from_key(KEY).address
TOKEN = "0x" + "22" * 20
ETH_ROUTER = "0x66a9893cc07d91d95644aedd05d03f95e1dba8af"
NATIVE = uniswap_api.NATIVE


class FakeRPC:
    def __init__(self, chain_id=1, balance=5_000_000, allowance=0):
        self.chain_id = chain_id
        self.balance = balance
        self.allowance = allowance
        self.calls = []

    def call(self, method, params):
        self.calls.append((method, params))
        if method == "eth_chainId":
            return hex(self.chain_id)
        if method == "eth_getTransactionCount":
            return hex(7)
        if method == "eth_estimateGas":
            return hex(110_000)
        if method == "eth_gasPrice":
            return hex(1_000_000_000)
        if method == "eth_call":
            data = bytes.fromhex(params[0]["data"][2:])
            if data.startswith(_selector("decimals()")):
                return "0x" + encode(["uint256"], [6]).hex()
            if data.startswith(_selector("symbol()")):
                return "0x" + encode(["string"], ["TOK"]).hex()
            if data.startswith(_selector("balanceOf(address)")):
                return "0x" + encode(["uint256"], [self.balance]).hex()
            if data.startswith(_selector("allowance(address,address)")):
                return "0x" + encode(["uint256"], [self.allowance]).hex()
        raise AssertionError(method)


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def api_responses(*, chain_id=1, token_in=NATIVE, token_out=TOKEN, amount="10000000000000000",
                  router=ETH_ROUTER, routing="CLASSIC", output_recipient=WALLET,
                  aggregate_recipient=WALLET):
    quote = {
        "chainId": chain_id,
        "swapper": WALLET,
        "input": {"token": token_in, "amount": amount},
        "output": {"token": token_out, "amount": "2500000", "recipient": output_recipient},
        "aggregatedOutputs": [{
            "minAmount": "2450000", "token": token_out, "recipient": aggregate_recipient,
        }],
    }
    return [
        FakeResponse({"routing": routing, "quote": quote}),
        FakeResponse({
            "swap": {
                "chainId": chain_id,
                "from": WALLET,
                "to": router,
                "data": "0x12345678",
                "value": "0x2386f26fc10000" if token_in == NATIVE else "0x0",
                "gasLimit": "0x186a0",
                "maxFeePerGas": "0x77359400",
                "maxPriorityFeePerGas": "0x3b9aca00",
            },
        }),
    ]


def mock_api(monkeypatch, responses):
    seen = []

    def post(url, **kwargs):
        seen.append((url, kwargs))
        return responses.pop(0)

    monkeypatch.setattr(uniswap_api.httpx, "post", post)
    return seen


def test_prepare_buy_requests_classic_quote_and_validates_router(monkeypatch):
    seen = mock_api(monkeypatch, api_responses())
    rpc = FakeRPC()
    prepared = uniswap_api.prepare("ethereum", TOKEN, "0.01", WALLET, rpc, api_key="test-key")

    assert prepared["kind"] == "ethereum_buy"
    assert prepared["venue"] == "uniswap_trading_api_classic"
    assert prepared["amount_in"] == 10**16
    assert prepared["amount_out"] == 2_500_000
    assert prepared["min_out"] == 2_450_000
    assert prepared["needs_approval"] is False
    assert prepared["max_fee_per_gas"] == 2_000_000_000
    quote_request = seen[0][1]
    assert quote_request["headers"]["x-api-key"] == "test-key"
    assert quote_request["headers"]["x-permit2-disabled"] == "true"
    assert quote_request["json"]["protocols"] == ["V2", "V3", "V4"]
    assert quote_request["json"]["tokenIn"] == NATIVE
    assert quote_request["json"]["recipient"] == WALLET
    assert seen[1][1]["json"]["simulateTransaction"] is True
    assert prepared["gas_estimate"] == 110_000
    assert all(method != "eth_sendRawTransaction" for method, _ in rpc.calls)


def test_prepare_sell_all_uses_balance_and_requires_exact_approval(monkeypatch):
    responses = api_responses(
        token_in=TOKEN, token_out=NATIVE, amount="5000000",
        router=ETH_ROUTER,
    )
    seen = mock_api(monkeypatch, responses)
    rpc = FakeRPC(allowance=1)
    prepared = uniswap_api.prepare("ethereum", TOKEN, "all", WALLET, rpc, mode="sell", api_key="test-key")

    assert prepared["kind"] == "ethereum_sell"
    assert prepared["amount_in"] == 5_000_000
    assert prepared["needs_approval"] is True
    assert prepared["approve_data"].startswith("0x")
    assert seen[0][1]["json"]["tokenIn"] == TOKEN
    assert seen[0][1]["json"]["tokenOut"] == NATIVE


def test_prepare_uses_rpc_gas_estimate_when_api_omits_it(monkeypatch):
    responses = api_responses()
    responses[1].payload["swap"].pop("gasLimit")
    mock_api(monkeypatch, responses)

    prepared = uniswap_api.prepare("ethereum", TOKEN, "0.01", WALLET, FakeRPC(), api_key="test-key")

    assert prepared["gas_estimate"] == 110_000
    assert prepared["gas"] == 132_000


@pytest.mark.parametrize(
    ("chain_name", "chain_id", "router", "mode"),
    [
        ("robinhood", 4663, "0x204faca1764b154221e35c0d20abb3c525710498", "buy"),
        ("ink", 57073, "0x661e93cca42afacb172121ef892830ca3b70f08d", "sell"),
    ],
)
def test_prepare_supports_robinhood_and_ink_mainnet(monkeypatch, chain_name, chain_id, router, mode):
    if mode == "buy":
        responses = api_responses(chain_id=chain_id, router=router)
        amount = "0.01"
    else:
        responses = api_responses(
            chain_id=chain_id, token_in=TOKEN, token_out=NATIVE, amount="5000000", router=router
        )
        amount = "all"
    mock_api(monkeypatch, responses)

    prepared = uniswap_api.prepare(
        chain_name, TOKEN, amount, WALLET, FakeRPC(chain_id), mode=mode, api_key="test-key"
    )

    assert prepared["chain_id"] == chain_id
    assert prepared["kind"] == f"{chain_name}_{mode}"
    assert prepared["to"].lower() == router


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"routing": "DUTCH_V2"}, "non-Classic"),
        ({"router": "0x" + "44" * 20}, "unverified router"),
        ({"chain_id": 57073}, "quote chain id mismatch"),
        ({"output_recipient": "0x" + "44" * 20}, "output recipient"),
        ({"aggregate_recipient": "0x" + "44" * 20}, "output token or recipient"),
    ],
)
def test_prepare_rejects_unsupported_or_mismatched_api_results(monkeypatch, changes, message):
    responses = api_responses(**changes)
    mock_api(monkeypatch, responses)
    with pytest.raises(ValueError, match=message):
        uniswap_api.prepare("ethereum", TOKEN, "0.01", WALLET, FakeRPC(), api_key="test-key")


def test_robinhood_testnet_is_not_accepted():
    with pytest.raises(ValueError, match="not supported"):
        uniswap_api.prepare(
            "robinhood_testnet", TOKEN, "0.01", WALLET, FakeRPC(46630), api_key="test-key"
        )


def test_prepare_requires_api_key():
    with pytest.raises(ValueError, match="UNISWAP_API_KEY"):
        uniswap_api.prepare("ethereum", TOKEN, "0.01", WALLET, FakeRPC(), api_key="")
