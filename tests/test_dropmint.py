from acobot import dropmint

NFT = "0x" + "12" * 20
WALLET = "0x" + "34" * 20


def test_indexed_mint_is_ethereum_only():
    assert dropmint.indexed_mint_supported("ethereum")
    assert not dropmint.indexed_mint_supported("robinhood")
    assert not dropmint.indexed_mint_supported("ink")


def test_fetch_skips_unsupported_chain_without_a_request():
    def post(*_args, **_kwargs):
        raise AssertionError("unsupported chains must not be called")

    body, reason = dropmint.fetch_authorization(
        {"contract": NFT, "phase": "allowlist"}, WALLET, 1, "robinhood", post=post)

    assert body is None
    assert "no indexed mint API" in reason


def test_fetch_returns_the_first_real_transaction_and_ignores_a_failed_host():
    seen = []

    def post(url, payload):
        seen.append((url, payload["taker"], payload["items"][0]["collection"]))
        if "reservoir.tools" in url:
            raise RuntimeError("http 429")
        return {"steps": [{"kind": "transaction", "items": [{
            "data": {"to": "0x" + "56" * 20, "data": "0x1234abcd", "value": "0"},
        }]}]}

    body, source = dropmint.fetch_authorization(
        {"contract": NFT, "phase": "allowlist", "list_mode": "gtd"},
        WALLET, 2, "ethereum", post=post)

    assert source == "magiceden"
    assert body["transaction"]["data"] == "0x1234abcd"
    assert body["transaction"]["value"] == "0"
    assert body["phase"] == "allowlist"
    assert body["wallet"] == WALLET
    assert len(seen) == 2
    assert seen[0][1:] == (WALLET, NFT)


def test_opensea_drop_transaction_is_used_before_the_other_hosts():
    seen = []

    def post(url, payload):
        seen.append((url, payload))
        if "api.opensea.io" not in url:
            raise AssertionError(url)
        assert payload == {"minter": WALLET, "quantity": 1, "skip_balance_check": True}
        return {"to": "0x" + "56" * 20, "data": "0xabcdef", "value": "1000", "chain": "ethereum"}

    body, source = dropmint.fetch_authorization(
        {"contract": NFT, "slug": "cool-cats", "phase": "allowlist"},
        WALLET, 1, "ethereum", post=post)

    assert source == "opensea"
    assert body["transaction"]["data"] == "0xabcdef"
    assert body["transaction"]["value"] == "1000"
    assert len(seen) == 1


def test_fetch_ignores_steps_that_omit_a_payment_value():
    def post(_url, _payload):
        return {"steps": [{"kind": "transaction", "items": [{
            "data": {"to": "0x" + "56" * 20, "data": "0x1234abcd"},
        }]}]}

    body, reason = dropmint.fetch_authorization(
        {"contract": NFT}, WALLET, 1, "ethereum", post=post)

    assert body is None
    assert "no transaction" in reason
