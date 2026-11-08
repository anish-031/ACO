import unittest, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from acobot import panels


class PanelTests(unittest.TestCase):
    def test_eth_and_none(self):
        self.assertEqual(panels.eth(None), "?")
        self.assertEqual(panels.eth(10**18 // 2), "0.50000 ETH")

    def test_label_strips_owner_prefix(self):
        self.assertEqual(panels.label_of("2078639025:Wallet 2"), "Wallet 2")
        self.assertEqual(panels.label_of("Wallet 1"), "Wallet 1")

    def test_mask(self):
        self.assertEqual(panels.mask("0x95e72f45dfdb5004a4733210d6034480d6c5637a"), "0x95e72f45dfdb5004a4733210d6034480d6c5637a")
        self.assertEqual(panels.mask("0x12"), "0x12")

    def test_wallet_step_counts_and_excludes(self):
        allw = [(1, "Wallet 1", "0x" + "a" * 40, 1), (1, "Wallet 2", "0x" + "b" * 40, 2)]
        lines = panels.wallet_step_lines({"name": "m", "chain": "robinhood", "contract": "0xc", "wex": ["0x" + "b" * 40]}, allw)
        text = "\n".join(lines)
        self.assertIn("minting from 1 wallet", text)
        self.assertIn("⬜ Wallet 2", text)

    def test_wallet_step_empty(self):
        text = "\n".join(panels.wallet_step_lines({"name": "m", "contract": "0xc"}, []))
        self.assertIn("no wallets on this chain yet", text)

    def test_mint_record_fields(self):
        d = {"contract": "0xc", "chain": "robinhood", "price_wei": 5, "mint_sig": "mint(uint256)",
             "start_ts": 99, "end_ts": 199}
        r = panels.mint_record(7, d, ["0xAB"], 1)
        self.assertEqual(r["status"], "scheduled")
        self.assertEqual(r["wex"], ["0xab"])
        self.assertEqual(r["id"], 7)
        self.assertEqual(r["end_ts"], 199)

    def test_mint_record_preserves_unknown_price_and_selected_phase_metadata(self):
        root = "0x" + "ab" * 32
        r = panels.mint_record(8, {
            "contract": "0xc", "price_wei": None, "stage_index": 3,
            "merkle_root": root, "signers": 2, "mint_site": "https://mint.example",
        }, [], 1)

        self.assertIsNone(r["price_wei"])
        self.assertEqual(r["stage_index"], 3)
        self.assertEqual(r["merkle_root"], root)
        self.assertEqual(r["signers"], 2)
        self.assertEqual(r["mint_site"], "https://mint.example")

    def test_check_lines_never_block_and_counts(self):
        v = [{"label": "Wallet 1", "masked": "0xaa", "verdict": "ok", "balance_wei": 10**18, "reasons": []},
             {"label": "Wallet 2", "masked": "0xbb", "verdict": "fail", "balance_wei": 0, "reasons": ["allowlist stage"]}]
        text = "\n".join(panels.check_lines(v, 0))
        self.assertIn("1 ready", text)
        self.assertIn("1 failed", text)
        self.assertIn("allowlist stage", text)

    def test_fund_lines_shortfall(self):
        plan = {"rows": [("Wallet 2", "0x" + "b" * 40, 10**16)], "total_wei": 10**16, "fee_wei": 1,
                "need_wei": 10**16 + 1, "ok": False, "why": "main wallet short"}
        text = "\n".join(panels.fund_lines("Wallet 1", "0x" + "a" * 40, plan, "0.01", "robinhood"))
        self.assertIn("main wallet short", text)
        self.assertIn("0.01000 ETH", text.replace("0.010000", "0.01000") or text)

    def test_feed_minted_vs_accepted_vs_failed(self):
        h = "0x" + "1" * 64
        payload = {"elapsed_s": 1.2,
                   "results": [{"label": "2078639025:Wallet 1", "address": "0xa", "rpc": {"x": h}},
                               {"label": "2078639025:Wallet 2", "address": "0xb", "rpc": {"x": h}},
                               {"label": "2078639025:Wallet 3", "address": "0xc", "rpc": {"x": "ERR nonce too low"}}],
                   "skipped": [{"label": "Wallet 4", "address": "0xd", "error": "signing failed"}]}
        text = "\n".join(panels.feed_lines(9, payload, receipts={h: "ok"}))
        self.assertIn("✅ Wallet 1: minted", text)
        self.assertIn("❌ Wallet 3: failed", text)
        self.assertIn("❌ Wallet 4: skipped", text)
        self.assertIn("summary: 2 minted, 0 accepted (unconfirmed), 2 failed of 4 wallets", text)

    def test_feed_no_receipt_is_not_minted(self):
        payload = {"elapsed_s": 1, "results": [{"label": "Wallet 1", "address": "0xa", "rpc": {"x": "0x" + "2" * 64}}], "skipped": []}
        text = "\n".join(panels.feed_lines(1, payload))
        self.assertNotIn("✅ Wallet 1: minted", text)
        self.assertIn("accepted by rpc, receipt not checked", text)


    def test_timing_full_breakdown_sums_to_total(self):
        # fire at t=1000.0, bot 40ms, block stamped 1002s -> inclusion 2000ms after fire, receipt seen at 2.5s
        w = {"bot_ms": 40.0, "fire_at": 1000.0, "block_ts": 1002, "receipt_s": 2.5}
        lines = panels.timing_lines(w)
        self.assertEqual(lines[0], "⚡ submit: 40ms (broadcast launch → RPC response)")
        self.assertEqual(lines[1], "⛓ chain: 1960ms (broadcast → included in block)")
        self.assertEqual(lines[2], "📥 receipt: 500ms (inclusion → receipt confirmed)")
        self.assertEqual(lines[3], "total: 2500ms")

    def test_timing_no_receipt_dashes_rest(self):
        w = {"bot_ms": 40.0, "fire_at": 1000.0, "block_ts": None, "receipt_s": None}
        lines = panels.timing_lines(w)
        self.assertEqual(lines[0], "⚡ submit: 40ms (broadcast launch → RPC response)")
        self.assertIn("chain: -", lines[1])
        self.assertIn("receipt: -", lines[2])
        self.assertEqual(lines[3], "total: -")

    def test_timing_pre_fire_without_fire_stamp(self):
        w = {"bot_ms": 12.0, "receipt_s": 1.1, "block_ts": 1001}
        lines = panels.timing_lines(w)
        self.assertEqual(lines[0], "⚡ submit: 12ms (broadcast launch → RPC response)")
        self.assertIn("chain: -", lines[1])
        self.assertEqual(lines[3], "total: 1100ms")

    def test_avg_bot_speed_header_in_result_panel(self):
        from acobot import results as _res
        rows_fx = [{"label": "W1", "address": "0x" + "a" * 40, "status": "minted", "bot_ms": 30.0,
                    "fire_at": 1000.0, "block_ts": 1001, "receipt_s": 1.5, "tx_hash": "0x" + "1" * 64, "gas_usd": 0.25},
                   {"label": "W2", "address": "0x" + "b" * 40, "status": "minted", "bot_ms": 50.0,
                    "fire_at": 1000.0, "block_ts": 1001, "receipt_s": 1.6, "tx_hash": "0x" + "2" * 64, "gas_usd": 0.25}]
        rec = {"id": 3, "qty": 1, "summary": _res.summarize_rows(rows_fx), "fire_mode": "normal",
               "rows": rows_fx}
        text = "\n".join(panels.result_lines(3, rec))
        self.assertIn("avg submit time: 40ms", text)
        self.assertIn("⚡ submit: 30ms", text)
        self.assertIn("⛓ chain:", text)
        self.assertNotIn("mint time:", text)


if __name__ == "__main__":
    unittest.main()
