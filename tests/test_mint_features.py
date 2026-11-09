import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from acobot import abi, bot, config, mintread, sniper, wallets


class MintInputTests(unittest.TestCase):
    def test_price_input_uses_exact_wei_conversion(self):
        self.assertEqual(bot.parse_mint_price_wei("0.1"), 10**17)
        self.assertEqual(bot.parse_mint_price_wei("0.000000000000000001"), 1)
        self.assertEqual(bot.parse_mint_price_wei("0.123456789012345678"), 123456789012345678)
        self.assertEqual(bot.parse_mint_price_wei("0"), 0)

    def test_price_input_rejects_negative_nonfinite_and_excess_precision(self):
        too_large = "115792089237316195423570985008687907853269984665640564039457.584007913129639936"
        for value in ("-0.1", "NaN", "Infinity", "0.0000000000000000001", "nope",
                      too_large, "1e100000000", "1e-100000000"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                bot.parse_mint_price_wei(value)

    def test_manual_mint_encoder_only_accepts_supported_signatures(self):
        self.assertTrue(sniper.encode_mint("mint()", 1).startswith("0x"))
        self.assertTrue(sniper.encode_mint("publicMint(uint256)", 2).startswith("0x"))
        for signature in ("mint(address,uint256)", "mint(address)", "mint(bytes32)", "bad signature"):
            with self.subTest(signature=signature), self.assertRaises(ValueError):
                sniper.encode_mint(signature, 1)

    def test_presign_uses_exact_selected_mint_price_and_quantity(self):
        price = 123456789012345678
        quantity = 3
        address = "0x" + "12" * 20
        chain = config.Chain("ethereum", 1, ["rpc"], ["rpc"])
        captured = {}

        class FakeRPC:
            def call(self, method, _params):
                if method == "eth_getBlockByNumber":
                    return {"baseFeePerGas": "0x3b9aca00"}
                if method == "eth_maxPriorityFeePerGas":
                    return "0x3b9aca00"
                if method == "eth_getTransactionCount":
                    return "0x0"
                if method == "eth_estimateGas":
                    return "0x186a0"
                raise AssertionError(f"unexpected RPC method: {method}")

        def fake_sign(entry, *_args):
            captured["value"] = entry["value"]
            return "0xraw", {"gas": 100_000}

        mint = {"contract": "0x" + "34" * 20, "price_wei": price,
                "mint_sig": "mint(uint256)", "fire_mode": "normal"}
        with mock.patch.object(sniper, "_sign", side_effect=fake_sign):
            result = sniper.presign(mint, chain, FakeRPC(),
                                    [("Wallet 1", address, "0x" + "11" * 32, quantity)])

        self.assertEqual(captured["value"], price * quantity)
        self.assertEqual(result[0]["value"], price * quantity)

    def test_live_refresh_preserves_manual_fallbacks_when_unavailable(self):
        draft = {
            "price_wei": 5,
            "start_ts": 100,
            "end_ts": 200,
            "per_wallet": 3,
            "mint_sig": "customMint(uint256)",
            "mint_sig_manual": True,
            "name": "old",
        }
        bot.apply_live_draft_snapshot(draft, {"name": "new", "price_wei": None, "mint_sig": "mint()"})
        self.assertEqual(draft["name"], "new")
        self.assertEqual(draft["price_wei"], 5)
        self.assertEqual(draft["start_ts"], 100)
        self.assertEqual(draft["mint_sig"], "customMint(uint256)")

    def test_live_refresh_replaces_stale_schedule_and_failure_state(self):
        draft = {"schedule": [{"label": None}], "schedule_unavailable": True}
        live = {"schedule": [{"label": "Public"}], "schedule_unavailable": False}
        bot.apply_live_draft_snapshot(draft, live)
        self.assertEqual(draft["schedule"], [{"label": "Public"}])
        self.assertFalse(draft["schedule_unavailable"])

    def test_phase_switch_restores_base_values_for_missing_stage_fields(self):
        draft = {"price_wei": 5, "start_ts": 100, "end_ts": 150, "per_wallet": 2}
        bot.apply_mint_stage(draft, {
            "chain_key": "allowlist", "gated": True, "price_wei": 0, "start_ts": 200,
            "end_ts": 220, "per_wallet": 0,
        })
        self.assertEqual((draft["price_wei"], draft["start_ts"], draft["per_wallet"]), (0, 200, 0))
        bot.apply_mint_stage(draft, {"chain_key": "presale", "gated": True})
        self.assertEqual((draft["price_wei"], draft["start_ts"], draft["end_ts"], draft["per_wallet"]),
                         (5, 100, None, 2))
        bot.apply_mint_stage(draft, {"chain_key": "public", "gated": False})
        self.assertEqual((draft["price_wei"], draft["start_ts"], draft["end_ts"], draft["per_wallet"]),
                         (5, 100, 150, 2))

    def test_draft_save_load_and_consumed_draft_filter(self):
        original_file = bot.DRAFTS_FILE
        original_drafts = dict(bot.DRAFTS)
        original_mints = dict(bot.MINTS)
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                bot.DRAFTS_FILE = Path(temp_dir) / "drafts.json"
                bot.DRAFTS.clear()
                bot.MINTS.clear()
                bot.DRAFTS[3] = {"contract": "0x" + "11" * 20, "mint_qty": 2}
                bot.save_drafts()
                bot.DRAFTS.clear()
                bot.load_drafts()
                self.assertEqual(bot.DRAFTS[3]["mint_qty"], 2)

                bot.MINTS[9] = {"source_draft_id": 3}
                bot.DRAFTS_FILE.write_text(json.dumps({"3": {"contract": "stale"}, "4": {"contract": "open"}}))
                bot.DRAFTS.clear()
                bot.load_drafts()
                self.assertNotIn(3, bot.DRAFTS)
                self.assertEqual(bot.DRAFTS[4]["contract"], "open")
        finally:
            bot.DRAFTS_FILE = original_file
            bot.DRAFTS.clear()
            bot.DRAFTS.update(original_drafts)
            bot.MINTS.clear()
            bot.MINTS.update(original_mints)


class ForceMintTests(unittest.IsolatedAsyncioTestCase):
    def test_mint_card_has_one_wallet_button_and_one_action(self):
        future = int(time.time()) + 3600
        draft = {
            "contract": "0x" + "12" * 20,
            "chain": "ethereum",
            "phase": "allowlist",
            "gated": True,
            "start_ts": future,
            "wallet_selected": 0,
            "wallet_report": [],
            "wallet_note": "0 wallets: no keys loaded. add keys in /wallets",
        }
        buttons = [button for row in bot.draft_kb(30, draft).inline_keyboard for button in row]
        callbacks = [button.callback_data for button in buttons]
        self.assertEqual(callbacks.count("wsel:30"), 1)
        self.assertIn("dsched:30", callbacks)
        self.assertNotIn("fsched:30", callbacks)
        self.assertNotIn("bsched:30", callbacks)
        self.assertEqual(sum(1 for button in buttons if "pick wallet" in button.text.lower()), 1)
        text = bot.draft_text(draft)
        self.assertIn("no keys loaded", text)
        self.assertIn("Schedule mint", text)

    def test_schedule_callback_is_not_the_start_time_prompt(self):
        self.assertNotIn("dsched", {"dp", "ds", "dw", "dgc", "dmc"})
        self.assertEqual("dsched:9".split(":", 1)[0], "dsched")

    async def test_schedule_button_opens_the_schedule_flow(self):
        draft = {
            "contract": "0x" + "12" * 20,
            "chain": "ethereum",
            "phase": "public",
            "start_ts": int(time.time()) + 3600,
            "wallet_selected": 1,
            "wallet_report": [{"verdict": "ok", "reasons": [], "selected": True}],
        }
        query = SimpleNamespace(
            answer=mock.AsyncMock(),
            from_user=SimpleNamespace(id=bot.ADMIN_ID, username=None),
            data="dsched:31",
            message=SimpleNamespace(chat_id=1, message_id=2),
        )
        ctx = SimpleNamespace(user_data={}, bot=SimpleNamespace(send_message=mock.AsyncMock()))
        wallet = (bot.ADMIN_ID, "Wallet 1", "0x" + "ab" * 20, 1)
        with mock.patch.dict(bot.DRAFTS, {31: draft}, clear=True), \
                mock.patch.object(bot, "allowed", return_value=True), \
                mock.patch.object(bot, "username_allowed", return_value=False), \
                mock.patch.object(bot, "mint_wallets", return_value=[wallet]), \
                mock.patch.object(bot, "run_schedule_check", new=mock.AsyncMock()) as schedule, \
                mock.patch.object(bot, "show_wallet_step", new=mock.AsyncMock()) as picker:
            await bot.on_callback(SimpleNamespace(callback_query=query), ctx)
        schedule.assert_awaited()
        picker.assert_not_awaited()
        self.assertIsNone(ctx.user_data.get("await"))

    def test_ready_public_sale_without_a_clock_offers_mint_now(self):
        draft = {
            "contract": "0x" + "12" * 20,
            "chain": "ethereum",
            "phase": "public",
            "price_wei": 10**16,
            "mint_sig": "mint(uint256)",
            "start_ts": None,
            "wallet_selected": 1,
            "wallet_report": [{"verdict": "ok", "reasons": []}],
        }

        keyboard = bot.draft_kb(23, draft)
        callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]

        self.assertIn("mnow:23", callbacks)
        self.assertNotIn("fm:23", callbacks)
        self.assertNotIn("bsched:23", callbacks)

    def test_ineligible_wallet_offers_only_force_mint(self):
        draft = {
            "contract": "0x" + "12" * 20,
            "chain": "ethereum",
            "phase": "allowlist",
            "gated": True,
            "price_wei": 0,
            "mint_sig": "mint(uint256)",
            "start_ts": int(time.time()) + 3600,
            "wallet_selected": 1,
            "wallet_report": [{"verdict": "fail", "eligibility": False,
                               "reasons": ["allowlist check: the chain rejected this wallet's mint"]}],
        }

        keyboard = bot.draft_kb(24, draft)
        callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]

        self.assertIn("fsched:24", callbacks)
        self.assertNotIn("dsched:24", callbacks)
        self.assertNotIn("bsched:24", callbacks)

    def test_live_ineligible_wallet_offers_only_force_mint_now(self):
        draft = {
            "contract": "0x" + "12" * 20,
            "chain": "ethereum",
            "phase": "public",
            "price_wei": 0,
            "mint_sig": "mint(uint256)",
            "start_ts": int(time.time()) - 60,
            "wallet_selected": 1,
            "wallet_report": [{"verdict": "warn", "reasons": ["not enough for the mint fee"]}],
        }
        callbacks = [button.callback_data for row in bot.draft_kb(25, draft).inline_keyboard for button in row]
        self.assertIn("fm:25", callbacks)
        self.assertNotIn("mnow:25", callbacks)
        self.assertNotIn("dsched:25", callbacks)
        self.assertNotIn("bsched:25", callbacks)

    async def test_gated_force_ignores_local_ineligibility_and_uses_one_tx_confirmation(self):
        original_drafts, original_mint_wallets = dict(bot.DRAFTS), bot.mint_wallets
        entry = (bot.ADMIN_ID, "Wallet 1", "0x" + "34" * 20, 1)
        ctx = SimpleNamespace(bot=SimpleNamespace(send_message=mock.AsyncMock()))
        bot.DRAFTS.clear()
        bot.DRAFTS[26] = {
            "contract": "0x" + "12" * 20,
            "chain": "ethereum",
            "phase": "allowlist",
            "gated": True,
            "schedule": [{
                "chain_key": "allowlist", "gated": True, "label": "GTD",
                "start_ts": 100, "merkle_root": "0x" + "00" * 32,
            }],
            "phase_idx": 0,
        }
        bot.mint_wallets = lambda _draft: [entry]
        try:
            with tempfile.TemporaryDirectory() as temp_dir, \
                    mock.patch.object(bot.mintapi, "STORE", Path(temp_dir) / "mint_apis.json"), \
                    mock.patch.object(bot, "save_drafts"), \
                    mock.patch.object(bot, "_edit_or_send", new=mock.AsyncMock()) as edit, \
                    mock.patch.object(bot, "_draft_checks", new=mock.AsyncMock(
                        return_value=([], [{"eligibility": False, "verdict": "fail"}]))) as draft_checks, \
                    mock.patch("acobot.bot.time.time", return_value=12_345):
                bot.mintapi.save(
                    bot.DRAFTS[26]["contract"],
                    "https://mint.example/api/{wallet}?phase={phase}&qty={qty}")
                await bot.force_mint(1, ctx, 26, 9)

            self.assertEqual(bot.DRAFTS[26]["force_pending_until"], 12_435)
            text = edit.await_args.args[3]
            markup = edit.await_args.args[4]
            callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
            self.assertIn("fmconfirm:26", callbacks)
            self.assertIn("even when the wallet is not eligible and simulation reverts", text.lower())
            self.assertIn("authorization required: requires wallet-specific merkle proof", text.lower())
            draft_checks.assert_not_awaited()
        finally:
            bot.DRAFTS.clear()
            bot.DRAFTS.update(original_drafts)
            bot.mint_wallets = original_mint_wallets

    async def test_gated_preflight_keeps_eligibility_and_authorization_separate(self):
        wallet = "0x" + "34" * 20
        draft = {
            "contract": "0x" + "12" * 20,
            "chain": "ethereum",
            "phase": "allowlist",
            "gated": True,
        }
        verdict = {"label": "Wallet 1", "address": wallet, "verdict": "ok",
                   "eligibility": None, "reasons": []}
        with mock.patch.object(
                bot, "mint_wallets",
                return_value=[(bot.ADMIN_ID, "Wallet 1", wallet, 1)]), \
                mock.patch.object(bot, "CFG", SimpleNamespace(chains={"ethereum": object()})), \
                mock.patch.object(bot.fleet, "check_all", return_value=[verdict]):
            _all_wallets, results = await bot._draft_checks(draft)

        self.assertIsNone(results[0]["eligibility"])
        self.assertEqual(results[0]["verdict"], "ok")
        self.assertFalse(bot.wallet_blocks_schedule(results[0]))

    async def test_force_schedule_uses_immediate_start_time(self):
        original_drafts, original_mints = dict(bot.DRAFTS), dict(bot.MINTS)
        original_next_id = bot._next_id[0]
        original_mint_wallets = bot.mint_wallets
        entry = (bot.ADMIN_ID, "Wallet 1", "0x" + "34" * 20, 1)
        bot.DRAFTS.clear()
        bot.MINTS.clear()
        bot._next_id[0] = 1
        bot.DRAFTS[7] = {
            "contract": "0x" + "12" * 20,
            "chain": "ethereum",
            "phase": "public",
            "price_wei": 10**16,
            "mint_sig": "mint(uint256)",
            "start_ts": 9_999_999_999,
            "force_start_ts": 123,
            "force": True,
        }
        bot.mint_wallets = lambda _draft: [entry]
        ctx = SimpleNamespace(application=object(), bot=SimpleNamespace(send_message=mock.AsyncMock()))
        try:
            with mock.patch.object(bot, "persist"), \
                    mock.patch.object(bot, "save_drafts"), \
                    mock.patch.object(bot, "start_watcher"), \
                    mock.patch.object(bot, "_edit_or_send", new=mock.AsyncMock()):
                await bot.schedule_draft(1, ctx, 7)
            self.assertEqual(bot.MINTS[1]["start_ts"], 123)
            self.assertNotIn("force_start_ts", bot.DRAFTS.get(7, {}))
        finally:
            bot.DRAFTS.clear()
            bot.DRAFTS.update(original_drafts)
            bot.MINTS.clear()
            bot.MINTS.update(original_mints)
            bot._next_id[0] = original_next_id
            bot.mint_wallets = original_mint_wallets

    async def test_gated_force_schedules_api_route_without_local_price_or_mint_signature(self):
        original_drafts, original_mints = dict(bot.DRAFTS), dict(bot.MINTS)
        original_next_id, original_mint_wallets = bot._next_id[0], bot.mint_wallets
        entry = (bot.ADMIN_ID, "Wallet 1", "0x" + "34" * 20, 1)
        ctx = SimpleNamespace(
            application=object(),
            bot=SimpleNamespace(send_message=mock.AsyncMock()),
        )
        bot.DRAFTS.clear()
        bot.MINTS.clear()
        bot._next_id[0] = 1
        bot.DRAFTS[27] = {
            "contract": "0x" + "12" * 20,
            "chain": "ethereum",
            "phase": "allowlist",
            "gated": True,
            "price_wei": None,
            "start_ts": None,
            "mint_sig": None,
            "force_start_ts": 12_345,
            "force": True,
        }
        bot.mint_wallets = lambda _draft: [entry]
        try:
            with tempfile.TemporaryDirectory() as temp_dir, \
                    mock.patch.object(bot.mintapi, "STORE", Path(temp_dir) / "mint_apis.json"), \
                    mock.patch.object(bot, "persist"), \
                    mock.patch.object(bot, "save_drafts"), \
                    mock.patch.object(bot, "start_watcher"), \
                    mock.patch.object(bot, "_edit_or_send", new=mock.AsyncMock()), \
                    mock.patch("acobot.bot.time.time", return_value=12_345):
                bot.mintapi.save(
                    bot.DRAFTS[27]["contract"],
                    "https://mint.example/api/{wallet}?phase={phase}&qty={qty}")
                await bot.schedule_draft(1, ctx, 27)

            self.assertTrue(bot.MINTS[1]["gated"])
            self.assertFalse(bot.MINTS[1]["authorization_at_fire"])
            self.assertEqual(bot.MINTS[1]["start_ts"], 12_345)
        finally:
            bot.DRAFTS.clear()
            bot.DRAFTS.update(original_drafts)
            bot.MINTS.clear()
            bot.MINTS.update(original_mints)
            bot._next_id[0] = original_next_id
            bot.mint_wallets = original_mint_wallets

    async def test_force_confirmation_sets_now_for_public_and_authorized_gated_phases(self):
        original_drafts, original_mint_wallets = dict(bot.DRAFTS), bot.mint_wallets
        entry = (bot.ADMIN_ID, "Wallet 1", "0x" + "34" * 20, 1)
        ctx = SimpleNamespace(bot=SimpleNamespace(send_message=mock.AsyncMock()))
        bot.DRAFTS.clear()
        bot.DRAFTS[8] = {
            "contract": "0x" + "12" * 20,
            "chain": "ethereum",
            "phase": "public",
            "price_wei": 10**16,
            "mint_sig": "mint(uint256)",
            "force_pending_until": 20_000,
        }
        bot.mint_wallets = lambda _draft: [entry]
        try:
            with mock.patch.object(bot, "schedule_draft", new=mock.AsyncMock()) as schedule, \
                    mock.patch("acobot.bot.time.time", return_value=12_345):
                await bot.confirm_force_mint(1, ctx, 8)
                self.assertEqual(bot.DRAFTS[8]["force_start_ts"], 12_345)
                self.assertTrue(bot.DRAFTS[8]["force"])
                schedule.assert_awaited_once_with(1, ctx, 8, None)

            bot.DRAFTS[8]["phase"] = "allowlist"
            bot.DRAFTS[8]["gated"] = True
            bot.DRAFTS[8]["force_pending_until"] = 20_000
            with tempfile.TemporaryDirectory() as temp_dir, \
                    mock.patch.object(bot.mintapi, "STORE", Path(temp_dir) / "mint_apis.json"), \
                    mock.patch.object(bot, "schedule_draft", new=mock.AsyncMock()) as schedule, \
                    mock.patch.object(bot, "save_drafts"), \
                    mock.patch("acobot.bot.time.time", return_value=12_345):
                bot.mintapi.save(
                    bot.DRAFTS[8]["contract"],
                    "https://mint.example/api/{wallet}?phase={phase}")
                await bot.confirm_force_mint(1, ctx, 8)
                self.assertEqual(bot.DRAFTS[8]["force_start_ts"], 12_345)
                schedule.assert_awaited_once_with(1, ctx, 8, None)
        finally:
            bot.DRAFTS.clear()
            bot.DRAFTS.update(original_drafts)
            bot.mint_wallets = original_mint_wallets


class MintWalletChainTests(unittest.TestCase):
    def test_mint_wallets_filters_before_deduplicating_cross_chain_address(self):
        original_cfg = bot.CFG
        original_load, original_fernet = wallets._load, wallets._fernet
        address = "0x" + "12" * 20
        entry = {"address": address, "enc_key": "encrypted", "qty": 1}
        data = {
            str(bot.ADMIN_ID): [
                {**entry, "chain": "ethereum"},
                {**entry, "chain": "ink"},
            ]
        }

        class FakeFernet:
            def decrypt(self, value):
                return b"0x" + b"11" * 32

        try:
            bot.CFG = SimpleNamespace(whitelist_ids=[])
            wallets._load = lambda: data
            wallets._fernet = lambda: FakeFernet()
            evm = bot.mint_wallets({"chain": "ethereum"})
            ink = bot.mint_wallets({"chain": "ink"})
            self.assertEqual([row[2] for row in evm], [address])
            self.assertEqual([row[2] for row in ink], [address])
            self.assertEqual(wallets.DECRYPT_FAILS, [])
        finally:
            bot.CFG = original_cfg
            wallets._load, wallets._fernet = original_load, original_fernet


class SeaDropPhaseDetectionTests(unittest.TestCase):
    contract = "0x" + "12" * 20

    def test_malformed_merkle_root_does_not_create_allowlist_phase(self):
        class FakeRPC:
            def eth_call(self, _to, data):
                if data.startswith("0x" + abi.selector_of("getAllowListMerkleRoot(address)")):
                    return "0x"
                return "0x" + "0" * 128

        with mock.patch.object(mintread, "seadrop_public", return_value=None):
            self.assertEqual(mintread.seadrop_stages(self.contract, FakeRPC()), [])

    def test_valid_merkle_root_creates_allowlist_phase(self):
        root = "0x" + "ab" * 32

        class FakeRPC:
            def eth_call(self, _to, data):
                if data.startswith("0x" + abi.selector_of("getAllowListMerkleRoot(address)")):
                    return root
                return "0x" + "0" * 128

        with mock.patch.object(mintread, "seadrop_public", return_value=None):
            stages = mintread.seadrop_stages(self.contract, FakeRPC())
        self.assertEqual([stage["key"] for stage in stages], ["allowlist"])
        self.assertEqual(stages[0]["merkle_root"], root)

    def test_gated_only_seadrop_is_detected_without_a_public_phase(self):
        class FakeRPC:
            def get_code(self, _contract):
                return "0x6000"

        gated = [{"key": "signed", "gated": True, "signers": 1,
                  "price_wei": None, "start_ts": None, "end_ts": None,
                  "per_wallet": None, "fee_recipient": None}]
        named = {"selector_count": 0, "named": {}, "mint_functions": []}
        with mock.patch.object(abi, "selectors_from_bytecode", return_value=set()), \
                mock.patch.object(abi, "analyze", return_value=named), \
                mock.patch.object(mintread, "_call_str", return_value=None), \
                mock.patch.object(mintread, "_call_word", return_value=0), \
                mock.patch.object(mintread, "_call_addr", return_value=None), \
                mock.patch.object(mintread, "nft_standard", return_value=None), \
                mock.patch.object(mintread, "seadrop_stages", return_value=gated):
            result = mintread.detect(self.contract, FakeRPC())

        self.assertTrue(result["seadrop"])
        self.assertEqual(result["phases"], ["signed"])
        self.assertEqual(result["stages"], gated)
        self.assertEqual(result["mint_to"], mintread.SEADROP)
        self.assertIsNone(result["price_wei"])


class SourcifyAbiTests(unittest.TestCase):
    def test_default_lookup_uses_provider_manager(self):
        verified_abi = [{"type": "function", "name": "mint", "inputs": []}]
        with mock.patch.object(
                mintread.provider_manager, "verified_abi",
                return_value=(verified_abi, "Sourcify", [])) as lookup:
            result, error = mintread.fetch_verified_abi(1, "0x" + "12" * 20)

        lookup.assert_called_once_with(1, "0x" + "12" * 20)
        self.assertEqual(result, verified_abi)
        self.assertIsNone(error)

    def test_verified_abi_supplies_mint_signature_without_4byte_lookup(self):
        signature = "mint(uint256)"
        selector = abi.selector_of(signature)

        class FakeRPC:
            expected_chain_id = 1

            def get_code(self, _contract):
                return "0x6000"

            def eth_call(self, _contract, _data):
                return "0x" + "0" * 128

        verified_abi = [{
            "type": "function",
            "name": "mint",
            "inputs": [{"type": "uint256"}],
        }]
        with mock.patch.object(abi, "selectors_from_bytecode", return_value={selector}), \
                mock.patch.object(abi, "lookup_4byte", side_effect=AssertionError("unexpected lookup")), \
                mock.patch.object(mintread, "fetch_verified_abi", return_value=(verified_abi, None)), \
                mock.patch.object(mintread, "seadrop_stages", return_value=[]):
            result = mintread.detect("0x" + "12" * 20, FakeRPC())

        self.assertEqual(result["mint_sig"], signature)
        self.assertEqual(result["abi_source"], "Sourcify")

    def test_verified_abi_lookup_falls_back_to_partial_match(self):
        import urllib.error
        import json

        requests = []
        metadata = {"output": {"abi": [{"type": "function", "name": "mint", "inputs": []}]}}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(metadata).encode()

        def opener(request, timeout):
            self.assertEqual(timeout, 3)
            requests.append(request.full_url)
            if len(requests) == 1:
                raise urllib.error.HTTPError(request.full_url, 404, "missing", None, None)
            return Response()

        result, error = mintread.fetch_verified_abi(
            1, "0x" + "12" * 20, opener=opener)

        self.assertEqual(result, metadata["output"]["abi"])
        self.assertIsNone(error)
        self.assertIn("/full_match/1/", requests[0])
        self.assertIn("/partial_match/1/", requests[1])


if __name__ == "__main__":
    unittest.main()
