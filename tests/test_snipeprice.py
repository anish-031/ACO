import unittest
from types import SimpleNamespace
from unittest.mock import patch

from acobot import snipeprice


class Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        import json
        return json.dumps(self.payload).encode()


class Broadcast:
    def __init__(self):
        self.raw = None

    def broadcast(self, raw):
        self.raw = raw
        return {"rpc": "0x" + "a" * 64}


class SnipePriceExecutorTests(unittest.TestCase):
    def test_listing_parser_preserves_exact_token_address(self):
        with patch("acobot.snipeprice.urllib.request.urlopen", return_value=Response({"orders": [{
            "id": "order1", "price": {"amount": {"decimal": 0.2}},
            "tokenSetId": "token:0x" + "1" * 40 + ":42", "maker": "0x" + "2" * 40,
            "source": {"name": "market"}}]})):
            row = snipeprice.fetch_cheapest("ethereum", "0x" + "1" * 40)[0]
        self.assertEqual(row["token_id"], "42")
        self.assertEqual(row["contract"], "0x" + "1" * 40)

    def test_missing_exact_token_does_not_broadcast(self):
        bcast = Broadcast()
        ok, msg = snipeprice.execute_buy({"id": "x", "price_eth": 0.1}, "0x" + "1" * 40,
            chain=SimpleNamespace(name="ethereum", chain_id=1), private_key="0x" + "1" * 64, bcast=bcast)
        self.assertFalse(ok)
        self.assertIn("nothing was bought", msg)
        self.assertIsNone(bcast.raw)

    def test_buy_requires_explicit_wallet_config(self):
        bcast = Broadcast()
        listing = {"id": "order1", "contract": "0x" + "1" * 40, "token_id": "1", "price_eth": 0.1}
        ok, _ = snipeprice.execute_buy(listing, "0x" + "1" * 40,
            chain=SimpleNamespace(name="ethereum", chain_id=1), bcast=bcast)
        self.assertFalse(ok)
        self.assertIsNone(bcast.raw)

    def test_ethereum_opensea_listing_uses_opensea_and_unsigned_tx(self):
        listing = {"id": "0xorder", "order_hash": "0xorder", "protocol_address": "0x" + "3" * 40,
                   "source": "opensea", "contract": "0x" + "1" * 40, "token_id": "42", "price_eth": .1}
        tx = {"to": "0x" + "2" * 40, "data": "0x12345678", "value": 0, "nonce": 1, "gas": 21000}
        chain = SimpleNamespace(name="ethereum", chain_id=1, rpc=[], broadcast_rpc=[])
        bcast = Broadcast()
        fake_account = type("FakeAccount", (), {
            "from_key": staticmethod(lambda _key: SimpleNamespace(address="0x" + "1" * 40)),
            "sign_transaction": staticmethod(lambda _tx, _key: SimpleNamespace(raw_transaction=b"signed")),
        })
        fake_eth_account = type("EthAccount", (), {"Account": fake_account})
        fake_eth_utils = type("EthUtils", (), {"to_checksum_address": staticmethod(lambda x: x)})
        with patch.dict("sys.modules", {"eth_account": fake_eth_account, "eth_utils": fake_eth_utils}), \
             patch("acobot.opensea.fulfill_listing", return_value=tx) as fulfill, \
             patch.object(snipeprice, "_reservoir_post") as reservoir:
            ok, msg = snipeprice.execute_buy(listing, "0x" + "1" * 40, chain=chain,
                                             private_key="fake", api_key="test", bcast=bcast)
        self.assertTrue(ok, msg)
        self.assertIsNotNone(bcast.raw)
        fulfill.assert_called_once()
        reservoir.assert_not_called()

    def test_reservoir_opensea_order_falls_back_only_when_reservoir_fill_unusable(self):
        listing = {"id": "0xorder", "order_hash": "0xorder", "protocol_address": "0x" + "3" * 40,
                   "source": "opensea", "fetched_via": "reservoir", "contract": "0x" + "1" * 40,
                   "token_id": "42", "price_eth": .1}
        tx = {"to": "0x" + "2" * 40, "data": "0x12345678", "value": 0, "nonce": 1, "gas": 21000}
        chain = SimpleNamespace(name="ethereum", chain_id=1, rpc=[], broadcast_rpc=[])
        bcast = Broadcast()
        fake_account = type("FakeAccount", (), {
            "from_key": staticmethod(lambda _key: SimpleNamespace(address="0x" + "1" * 40)),
            "sign_transaction": staticmethod(lambda _tx, _key: SimpleNamespace(raw_transaction=b"signed")),
        })
        fake_eth_account = type("EthAccount", (), {"Account": fake_account})
        fake_eth_utils = type("EthUtils", (), {"to_checksum_address": staticmethod(lambda x: x)})
        reservoir_response = {"steps": [{"items": [{"kind": "transaction", "data": {"to": "", "data": ""}}]}]}
        with patch.dict("sys.modules", {"eth_account": fake_eth_account, "eth_utils": fake_eth_utils}), \
             patch.object(snipeprice, "_reservoir_post", return_value=reservoir_response) as reservoir, \
             patch("acobot.opensea.fulfill_listing", return_value=tx) as fulfill:
            ok, msg = snipeprice.execute_buy(listing, "0x" + "1" * 40, chain=chain,
                                             private_key="fake", api_key="test", bcast=bcast)
        self.assertTrue(ok, msg)
        reservoir.assert_called_once()
        fulfill.assert_called_once()

    def test_reservoir_opensea_order_keeps_working_reservoir_fill(self):
        listing = {"id": "0xorder", "order_hash": "0xorder", "protocol_address": "0x" + "3" * 40,
                   "source": "opensea", "fetched_via": "reservoir", "contract": "0x" + "1" * 40,
                   "token_id": "42", "price_eth": .1}
        tx = {"to": "0x" + "2" * 40, "data": "0x12345678", "value": 0, "nonce": 1, "gas": 21000}
        response = {"steps": [{"items": [{"kind": "transaction", "data": tx}]}]}
        chain = SimpleNamespace(name="ethereum", chain_id=1, rpc=[], broadcast_rpc=[])
        bcast = Broadcast()
        fake_account = type("FakeAccount", (), {
            "from_key": staticmethod(lambda _key: SimpleNamespace(address="0x" + "1" * 40)),
            "sign_transaction": staticmethod(lambda _tx, _key: SimpleNamespace(raw_transaction=b"signed")),
        })
        fake_eth_account = type("EthAccount", (), {"Account": fake_account})
        fake_eth_utils = type("EthUtils", (), {"to_checksum_address": staticmethod(lambda x: x)})
        with patch.dict("sys.modules", {"eth_account": fake_eth_account, "eth_utils": fake_eth_utils}), \
             patch.object(snipeprice, "_reservoir_post", return_value=response) as reservoir, \
             patch("acobot.opensea.fulfill_listing") as fulfill:
            ok, msg = snipeprice.execute_buy(listing, "0x" + "1" * 40, chain=chain,
                                             private_key="fake", api_key="test", bcast=bcast)
        self.assertTrue(ok, msg)
        reservoir.assert_called_once()
        fulfill.assert_not_called()

    def test_ethereum_opensea_offer_uses_offer_fulfillment(self):
        offer = {"id": "0xbid", "order_hash": "0xbid", "protocol_address": "0x" + "3" * 40,
                 "source": "opensea", "contract": "0x" + "1" * 40, "token_id": "42", "price_eth": 1}
        chain = SimpleNamespace(name="ethereum", chain_id=1, rpc=[], broadcast_rpc=[])
        bcast = Broadcast()
        fake_account = type("FakeAccount", (), {
            "from_key": staticmethod(lambda _key: SimpleNamespace(address="0x" + "1" * 40)),
            "sign_transaction": staticmethod(lambda _tx, _key: SimpleNamespace(raw_transaction=b"signed")),
        })
        fake_eth_account = type("EthAccount", (), {"Account": fake_account})
        fake_eth_utils = type("EthUtils", (), {"to_checksum_address": staticmethod(lambda x: x)})
        tx = {"to": "0x" + "2" * 40, "data": "0x12345678", "value": 0, "nonce": 1, "gas": 21000}
        with patch.dict("sys.modules", {"eth_account": fake_eth_account, "eth_utils": fake_eth_utils}), \
             patch("acobot.opensea.fulfill_offer", return_value=tx) as fulfill, \
             patch.object(snipeprice, "_reservoir_post") as reservoir:
            ok, msg = snipeprice.execute_sell(offer, .5, "0x" + "1" * 40, "fake", chain=chain,
                                              api_key="test", bcast=bcast)
        self.assertTrue(ok, msg)
        self.assertIsNotNone(bcast.raw)
        fulfill.assert_called_once()
        reservoir.assert_not_called()

    def test_transaction_steps_reject_non_transaction(self):
        steps = [{"items": [{"kind": "signature", "data": {"signature": "0x"}},
                            {"kind": "transaction", "data": {"to": "0x" + "1" * 40,
                             "data": "0x12345678", "value": "0x0"}}]}]
        self.assertEqual(len(snipeprice._transaction_steps({"steps": steps})), 1)

    def test_transaction_steps_accept_kind_on_the_step(self):
        steps = [{"kind": "transaction", "items": [
            {"status": "incomplete", "data": {"to": "0x" + "2" * 40, "data": "0xabcdef", "value": "0"}},
        ]}, {"kind": "signature", "items": [{"data": {"signature": "0xabc"}}]}]
        found = snipeprice._transaction_steps({"steps": steps})
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["data"], "0xabcdef")


if __name__ == "__main__":
    unittest.main()
