import json, unittest, sys, os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from acobot import ai

A = "0x" + "ab" * 20

def mock(obj):
    return lambda msgs: json.dumps(obj)

class T(unittest.TestCase):
    def p(self, obj, admin=True, lc=None):
        return ai.parse(1, "x", admin, lc, complete=mock(obj))

    def test_addmint_maps(self):
        r = self.p({"action": "addmint", "args": {"contract": A, "chain": "Robinhood", "qty": 5, "speed": "super fast", "time": "20:00 UTC"}})
        self.assertEqual(r["action"], "addmint")
        self.assertEqual(r["args"]["chain"], "robinhood")
        self.assertEqual(r["args"]["qty"], 5)

    def test_addmint_accepts_ink_mainnet(self):
        r = self.p({"action": "addmint", "args": {"contract": A, "chain": "ink"}})
        self.assertEqual(r["action"], "addmint")
        self.assertEqual(r["args"]["chain"], "ink")

    def test_addmint_rejects_non_mainnet_evm_network(self):
        r = self.p({"action": "addmint", "args": {"contract": A, "chain": "robinhood_testnet"}})
        self.assertEqual(r["action"], "unknown")

    def test_balance_accepts_solana_mainnet(self):
        self.assertEqual(self.p({"action": "balance", "args": {"chain": "solana"}})["action"], "balance")

    def test_last_contract_pronoun(self):
        r = self.p({"action": "addmint", "args": {"contract": "last", "qty": 2}}, lc=A)
        self.assertEqual(r["args"]["contract"], A)

    def test_last_contract_missing_is_unknown(self):
        r = self.p({"action": "addmint", "args": {"contract": "last"}}, lc=None)
        self.assertEqual(r["action"], "unknown")

    def test_bad_address_rejected(self):
        self.assertEqual(self.p({"action": "addmint", "args": {"contract": "0xnope"}})["action"], "unknown")

    def test_unknown_action_rejected(self):
        self.assertEqual(self.p({"action": "rm_rf", "args": {}})["action"], "unknown")

    def test_extra_args_rejected(self):
        self.assertEqual(self.p({"action": "cancel_mint", "args": {"id": 3, "gas": 1}})["action"], "unknown")

    def test_cancel_mint(self):
        r = self.p({"action": "cancel_mint", "args": {"id": "3"}})
        self.assertEqual(r, {"action": "cancel_mint", "args": {"id": 3}})

    def test_send_validated(self):
        r = self.p({"action": "send", "args": {"who": "Wallet 2", "amount": "0.1", "token": "native", "to": "main"}})
        self.assertEqual(r["action"], "send")
        self.assertEqual(r["args"]["who"], "2")
        self.assertEqual(r["args"]["to"], "main")

    def test_send_bad_amount_rejected(self):
        self.assertEqual(self.p({"action": "send", "args": {"who": "all", "amount": "1; rm", "token": "native", "to": "main"}})["action"], "unknown")

    def test_send_missing_required(self):
        self.assertEqual(self.p({"action": "send", "args": {"who": "all", "amount": "all", "token": "native"}})["action"], "unknown")

    def test_admin_only_blocked_for_crew(self):
        r = self.p({"action": "send", "args": {"who": "all", "amount": "all", "token": "native", "to": "main"}}, admin=False)
        self.assertEqual(r["action"], "unknown")
        self.assertIn("admin", r["args"]["reply"])

    def test_crew_can_ask_balance(self):
        self.assertEqual(self.p({"action": "balance", "args": {}}, admin=False)["action"], "balance")

    def test_garbage_output_is_unknown_not_crash(self):
        r = ai.parse(1, "x", True, None, complete=lambda m: "not json at all")
        self.assertEqual(r["action"], "unknown")

    def test_llm_error_raises_aierror(self):
        def boom(m): raise ai.AIError("down")
        with self.assertRaises(ai.AIError):
            ai.parse(1, "x", True, None, complete=boom)

    def test_context_is_sent_and_capped(self):
        ai._HISTORY.pop(7, None)
        for i in range(15):
            ai.remember(7, "user", f"m{i}")
        seen = {}
        def cap(msgs):
            seen["m"] = msgs
            return '{"action":"help","args":{}}'
        ai.parse(7, "now", True, None, complete=cap)
        contents = [m["content"] for m in seen["m"]]
        self.assertIn("m14", contents)
        self.assertNotIn("m0", contents)
        self.assertEqual(len([c for c in contents if c.startswith("m")]), 10)

    def test_key_roundtrip_and_status(self):
        import acobot.ai as _ai
        from acobot import wallets
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(os.environ, {"HOME": tmp}, clear=False), \
                    patch.object(_ai, "KEY_FILE", root / "keys" / "ai.enc"), \
                    patch.object(wallets, "MASTER", root / "keys" / "master.key"):
                os.environ.pop("ACO_AI_API_KEY", None)
                self.assertIsNone(ai.stored_key()[0])
                self.assertIn("not set up", ai.status_text())
                with self.assertRaises(ValueError):
                    ai.save_key("short")
                key = "test-api-key-that-is-long-enough"
                ai.save_key(key)
                self.assertEqual(ai.stored_key(), (key, "stored"))
                self.assertIn("ai: on (stored encrypted)", ai.status_text())
                ai.clear_key()
                self.assertIsNone(ai.stored_key()[0])

if __name__ == "__main__":
    unittest.main()
