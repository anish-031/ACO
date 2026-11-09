import unittest
from types import SimpleNamespace
from unittest import mock

from acobot import bot, eligible


WALLET_ELIGIBLE = "0x" + "11" * 20
WALLET_OTHER = "0x" + "22" * 20


class ScheduleWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_gated_eligibility_split_routes_unknown_and_ineligible_to_force(self):
        entries = [
            (bot.ADMIN_ID, "eligible", WALLET_ELIGIBLE, 1),
            (bot.ADMIN_ID, "unknown", WALLET_OTHER, 1),
        ]

        async def probe_fleet(_contract, _chain, wallets, _stage):
            return [
                (wallets[0], {"eligible": True}),
                (wallets[1], {"eligible": None}),
            ]

        draft = {
            "contract": "0x" + "33" * 20,
            "chain": "ethereum",
            "phase": "allowlist",
            "gated": True,
        }
        with mock.patch.object(eligible, "probe_fleet", new=probe_fleet):
            normal, force, results = await bot._split_by_local_eligibility(draft, entries)

        self.assertEqual(normal, [entries[0]])
        self.assertEqual(force, [entries[1]])
        self.assertIsNone(results[WALLET_OTHER.lower()]["eligible"])

    async def test_both_creates_disjoint_scheduled_workflows_at_phase_time(self):
        old_drafts, old_mints = dict(bot.DRAFTS), dict(bot.MINTS)
        old_next_id, old_mint_wallets = bot._next_id[0], bot.mint_wallets
        entries = [
            (bot.ADMIN_ID, "eligible", WALLET_ELIGIBLE, 1),
            (bot.ADMIN_ID, "ineligible", WALLET_OTHER, 1),
        ]
        bot.DRAFTS.clear()
        bot.MINTS.clear()
        bot._next_id[0] = 1
        bot.DRAFTS[91] = {
            "contract": "0x" + "33" * 20,
            "chain": "ethereum",
            "phase": "allowlist",
            "gated": True,
            "start_ts": 12345,
            "price_wei": None,
            "mint_sig": None,
        }
        bot.mint_wallets = lambda _draft: entries
        ctx = SimpleNamespace(
            application=object(),
            bot=SimpleNamespace(send_message=mock.AsyncMock()),
        )
        try:
            with mock.patch.object(bot, "_split_by_local_eligibility",
                                  new=mock.AsyncMock(return_value=([entries[0]], [entries[1]], {}))), \
                    mock.patch.object(bot.mintapi, "supports_phase", return_value=False), \
                    mock.patch.object(bot, "persist"), \
                    mock.patch.object(bot, "save_drafts"), \
                    mock.patch.object(bot, "start_watcher"), \
                    mock.patch.object(bot, "_edit_or_send", new=mock.AsyncMock()):
                await bot.schedule_draft(1, ctx, 91, workflow="both")

            normal, force = bot.MINTS[1], bot.MINTS[2]
            self.assertEqual(normal["workflow_mode"], "normal")
            self.assertEqual(force["workflow_mode"], "force")
            self.assertFalse(normal["force"])
            self.assertTrue(force["force"])
            self.assertEqual((normal["start_ts"], force["start_ts"]), (12345, 12345))
            self.assertIn(WALLET_OTHER.lower(), normal["wex"])
            self.assertIn(WALLET_ELIGIBLE.lower(), force["wex"])
            self.assertNotIn(WALLET_ELIGIBLE.lower(), normal["wex"])
            self.assertNotIn(WALLET_OTHER.lower(), force["wex"])
            self.assertNotIn(91, bot.DRAFTS)
        finally:
            bot.DRAFTS.clear()
            bot.DRAFTS.update(old_drafts)
            bot.MINTS.clear()
            bot.MINTS.update(old_mints)
            bot._next_id[0] = old_next_id
            bot.mint_wallets = old_mint_wallets

    async def test_force_schedule_skips_local_eligibility_and_waits_for_phase(self):
        old_drafts, old_mints = dict(bot.DRAFTS), dict(bot.MINTS)
        old_next_id, old_mint_wallets = bot._next_id[0], bot.mint_wallets
        entry = (bot.ADMIN_ID, "wallet", WALLET_OTHER, 1)
        bot.DRAFTS.clear()
        bot.MINTS.clear()
        bot._next_id[0] = 1
        bot.DRAFTS[93] = {
            "contract": "0x" + "33" * 20,
            "chain": "ethereum",
            "phase": "allowlist",
            "gated": True,
            "start_ts": 12345,
            "price_wei": None,
            "per_wallet": 2,
            "fire_mode": "prefire",
            "schedule": [{
                "chain_key": "allowlist",
                "gated": True,
                "index": 4,
                "merkle_root": "0x" + "ab" * 32,
                "signers": 2,
                "mint_site": "https://mint.example",
                "display_start_ts": 12345,
                "display_per_wallet": 2,
            }],
        }
        bot.mint_wallets = lambda _draft: [entry]
        ctx = SimpleNamespace(
            application=object(),
            bot=SimpleNamespace(send_message=mock.AsyncMock()),
        )
        try:
            with mock.patch.object(
                    bot, "_split_by_local_eligibility",
                    new=mock.AsyncMock(side_effect=AssertionError("must bypass check"))) as split, \
                    mock.patch.object(bot.mintapi, "supports_phase", return_value=False), \
                    mock.patch.object(bot, "persist"), \
                    mock.patch.object(bot, "save_drafts"), \
                    mock.patch.object(bot, "start_watcher"), \
                    mock.patch.object(bot, "_edit_or_send", new=mock.AsyncMock()):
                await bot.schedule_draft(1, ctx, 93, workflow="force")

            split.assert_not_awaited()
            self.assertTrue(bot.MINTS[1]["force"])
            self.assertEqual(bot.MINTS[1]["start_ts"], 12345)
            self.assertEqual(bot.MINTS[1]["fire_mode"], "prefire")
            self.assertFalse(bot.MINTS[1]["authorization_at_fire"])
            self.assertIsNone(bot.MINTS[1]["price_wei"])
            self.assertEqual(bot.MINTS[1]["per_wallet"], 2)
            self.assertEqual(bot.MINTS[1]["stage_index"], 4)
            self.assertEqual(bot.MINTS[1]["merkle_root"], "0x" + "ab" * 32)
            self.assertEqual(bot.MINTS[1]["signers"], 2)
            self.assertEqual(bot.MINTS[1]["mint_site"], "https://mint.example")
        finally:
            bot.DRAFTS.clear()
            bot.DRAFTS.update(old_drafts)
            bot.MINTS.clear()
            bot.MINTS.update(old_mints)
            bot._next_id[0] = old_next_id
            bot.mint_wallets = old_mint_wallets

    def test_schedule_panel_shows_one_action_for_the_wallets(self):
        future = int(__import__("time").time()) + 3600
        draft = {
            "contract": "0x" + "33" * 20,
            "chain": "ethereum",
            "phase": "allowlist",
            "gated": True,
            "start_ts": future,
            "wallet_selected": 1,
            "wallet_report": [{"verdict": "ok", "reasons": [], "eligibility": True}],
        }
        callbacks = [
            button.callback_data
            for row in bot.draft_kb(92, draft).inline_keyboard
            for button in row
        ]
        self.assertIn("dsched:92", callbacks)
        self.assertNotIn("fsched:92", callbacks)
        self.assertNotIn("bsched:92", callbacks)

        draft["wallet_report"] = [{"verdict": "warn", "eligibility": None,
                                   "reasons": ["not enough for the mint fee"]}]
        blocked = [
            button.callback_data
            for row in bot.draft_kb(92, draft).inline_keyboard
            for button in row
        ]
        self.assertIn("fsched:92", blocked)
        self.assertNotIn("dsched:92", blocked)
        self.assertNotIn("bsched:92", blocked)
