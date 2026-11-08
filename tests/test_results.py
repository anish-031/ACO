import unittest, sys, os, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from eth_utils import keccak
from acobot import results as R


def _error_string_data(msg):
    b = msg.encode()
    pad = (32 - len(b) % 32) % 32
    body = (32).to_bytes(32, "big") + len(b).to_bytes(32, "big") + b + b"\x00" * pad
    return "0x" + (keccak(text="Error(string)")[:4] + body).hex()


class FakeRPC:
    def __init__(self, receipts, txs=None):
        self.receipts, self.txs, self.urls = receipts, txs or {}, ["x"]

    def call(self, method, params):
        if method == "eth_getTransactionReceipt":
            return self.receipts.get(params[0])
        if method == "eth_getTransactionByHash":
            return self.txs.get(params[0])
        if method == "eth_getBlockByNumber":
            return {"timestamp": "0x64"}
        raise RuntimeError(method)


class T(unittest.TestCase):
    def test_decode_error_string(self):
        self.assertEqual(R.decode_revert(_error_string_data("sold out")), "sold out")

    def test_decode_panic(self):
        data = "0x" + (keccak(text="Panic(uint256)")[:4] + (0x11).to_bytes(32, "big")).hex()
        self.assertEqual(R.decode_revert(data), "panic: arithmetic overflow")

    def test_decode_custom_and_empty(self):
        self.assertEqual(R.decode_revert("0x"), "reverted with no reason")
        self.assertEqual(R.decode_revert("0xdeadbeef"), "custom error 0xdeadbeef")

    def test_gas_cost(self):
        self.assertEqual(R.gas_cost_wei({"gasUsed": "0x5208", "effectiveGasPrice": "0x3b9aca00"}), 21000 * 10**9)
        self.assertIsNone(R.gas_cost_wei({"gasUsed": "0x1"}))

    def test_explorer_per_chain(self):
        self.assertEqual(R.explorer_url("ethereum", "0xab"), "https://etherscan.io/tx/0xab")
        self.assertEqual(R.explorer_url("robinhood", "0xab"),
                         "https://robinhoodchain.blockscout.com/tx/0xab")
        self.assertIsNone(R.explorer_url("ethereum", None))

    def test_track_minted_and_failed(self):
        h1, h2 = "0x" + "a1" * 32, "0x" + "b2" * 32
        ok = {"status": "0x1", "blockNumber": "0x10", "gasUsed": "0x5208", "effectiveGasPrice": "0x3b9aca00"}
        bad = {"status": "0x0", "blockNumber": "0x11", "gasUsed": "0x6000", "effectiveGasPrice": "0x3b9aca00"}
        rpc = FakeRPC({h1: ok, h2: bad},
                      txs={h2: {"from": "0x1", "to": "0x2", "input": "0x", "value": "0x0"}})
        R._replay_data = lambda *a, **k: ("sold out", None, "execution reverted: sold out", False)  # no network in tests
        fire = {"fired_at": 1000.0, "results": [
            {"label": "w1", "address": "0x1", "sent_s": 0.2, "rpc": {"u": h1}},
            {"label": "w2", "address": "0x2", "sent_s": 0.3, "rpc": {"u": h2}},
            {"label": "w3", "address": "0x3", "sent_s": 0.1, "rpc": {"u": "ERR X"}},
        ], "skipped": [{"label": "w4", "address": "0x4", "error": "unsigned"}]}
        t = [1000.0]
        def clock():
            t[0] += 0.5
            return t[0]
        rows = R.track(rpc, "ethereum", fire, timeout=60, poll=0, sleep=lambda s: None,
                       clock=clock, usd_per_eth=2000.0)
        by = {r["label"]: r for r in rows}
        self.assertEqual(by["w1"]["status"], "minted")
        self.assertAlmostEqual(by["w1"]["gas_eth"], 21000 * 1e9 / 1e18)
        self.assertAlmostEqual(by["w1"]["gas_usd"], 21000 * 1e9 / 1e18 * 2000)
        self.assertEqual(by["w2"]["status"], "failed")
        self.assertEqual(by["w2"]["revert"], "sold out")
        self.assertEqual(by["w3"]["status"], "failed")
        self.assertEqual(by["w4"]["status"], "failed")
        s = R.summarize_rows(rows)
        self.assertEqual((s["wallets"], s["minted"]), (4, 1))
        self.assertIsNotNone(R.summary_line(s))

    def test_store_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            R.save_result(d, 7, {"id": 7, "rows": []})
            self.assertEqual(R.load_result(d, 7)["id"], 7)
            self.assertEqual(R.list_result_ids(d), [7])
            self.assertIsNone(R.load_result(d, 8))



class FailureDetail(unittest.TestCase):
    def test_hints(self):
        self.assertEqual(R.hint_for("insufficient funds for gas * price + value"), "insufficient funds for gas or mint price, top up the wallet")
        self.assertEqual(R.hint_for("transaction underpriced"), "gas price too low for the network right now")
        self.assertEqual(R.hint_for("nonce too low"), "nonce already used, an earlier tx from this wallet went through")
        self.assertIsNone(R.hint_for("something unrelated"))

    def test_rpc_err_text_verbatim(self):
        from acobot.rpc import err_text
        e = RuntimeError({"code": -32000, "message": "intrinsic gas too low", "data": "0xabcd"})
        self.assertEqual(err_text(e), "code -32000: intrinsic gas too low (data 0xabcd)")
        self.assertEqual(err_text(ValueError("boom")), "ValueError: boom")

    def test_revert_detail_error_string(self):
        data = _error_string_data("sale not open yet")
        class Rp: urls = ["x"]
        orig = R._replay_data
        R._replay_data = lambda rpc, tx, blk: (R.decode_revert(data), data, "execution reverted", False)
        try:
            d = R.revert_detail(Rp(), {"to": "0x2", "input": "0x"}, 16)
        finally:
            R._replay_data = orig
        self.assertEqual(d["reason"], "sale not open yet")
        self.assertEqual(d["raw"], data)
        self.assertEqual(d["replay"], "reverted")
        self.assertEqual(d["hint"], "sale not open yet")

    def test_failure_lines_show_reason_and_detail(self):
        from acobot import panels
        rec = {"id": 7, "qty": 1, "summary": {}, "rows": [{
            "label": "w1", "address": "0x" + "1" * 40, "status": "failed", "sent_s": 0.1,
            "receipt_s": 2.0, "tx_hash": "0x" + "a" * 64, "explorer": None, "gas_eth": 0.0001, "gas_usd": 0.3,
            "reason": "sale not open yet", "hint": "sale not open yet", "rpc_error": "execution reverted",
            "revert_raw": "0x08c379a0", "tx_status": "0x0", "block": 16, "replay": "reverted"}]}
        rec["summary"] = R.summarize_rows(rec["rows"])
        text = "\n".join(panels.result_lines(7, rec))
        self.assertIn("❌ <b>reason:</b> sale not open yet", text)
        self.assertIn("hint: sale not open yet", text)
        self.assertIn("revert data: 0x08c379a0", text)
        self.assertIn("tx status: 0x0 (reverted)", text)
        self.assertIn("block: 16", text)


if __name__ == "__main__":
    unittest.main()
