"""Generate, import, and mint-wallet picking. Each case runs 10 times."""
import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from telegram.constants import ChatType
from acobot import bot, wallets

KEY = "0x" + "11" * 32
RUNS = 10


def _store(case):
    folder = Path(tempfile.mkdtemp())
    wallets.STORE = folder / "wallets.json"
    wallets.MASTER = folder / "master.key"
    case.addCleanup(lambda: None)


def _update(text, uid=9):
    message = SimpleNamespace(text=text, reply_text=AsyncMock(), delete=AsyncMock())
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=uid, username="tester"),
        effective_chat=SimpleNamespace(id=uid, type=ChatType.PRIVATE),
        message=message,
    )


class WalletFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _store(self)

    async def test_generate_saves_one_record_and_works_on_every_evm_chain(self):
        for run in range(RUNS):
            with self.subTest(run=run):
                _store(self)
                uid = 100 + run
                update = _update(str((run % 10) + 1), uid)
                ctx = SimpleNamespace(
                    user_data={"await": ("wgen_qty", "evm_all")},
                    bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1))),
                )
                with patch.object(bot, "show_wallets", new=AsyncMock()):
                    await bot.on_text(update, ctx)
                made = (run % 10) + 1
                self.assertEqual(wallets.count(uid), made)
                raw = __import__("json").loads(wallets.STORE.read_text())
                self.assertEqual(len(raw[str(uid)]), made)
                self.assertEqual(len(wallets.signing_entries([uid], chain="ethereum")), made)
                self.assertEqual(len(wallets.signing_entries([uid], chain="robinhood")), made)
                self.assertEqual(len(wallets.signing_entries([uid], chain="ink")), made)
                self.assertEqual(wallets.signing_entries([uid], chain="solana"), [])
                reveal = update.message.reply_text.await_args.args[0]
                self.assertIn(f"saved {made}", reveal)
                self.assertNotIn("of " + str(made), reveal)

    async def test_solana_generate_saves_a_key_the_bot_can_sign_with(self):
        for run in range(RUNS):
            with self.subTest(run=run):
                _store(self)
                uid = 400 + run
                update = _update("1", uid)
                ctx = SimpleNamespace(
                    user_data={"await": ("wgen_qty", "solana")},
                    bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1))),
                )
                with patch.object(bot, "show_wallets", new=AsyncMock()):
                    await bot.on_text(update, ctx)
                self.assertEqual(wallets.count(uid), 1)
                entries = wallets.signing_entries([uid], chain="solana")
                self.assertEqual(len(entries), 1)
                _owner, _label, address, secret, qty = entries[0]
                from acobot import solana
                self.assertEqual(solana.public_key(secret), address)
                self.assertEqual(qty, 1)
                self.assertEqual(wallets.signing_entries([uid], chain="ethereum"), [])
                reveal = update.message.reply_text.await_args.args[0]
                self.assertIn("saved 1.", reveal)
                self.assertIn(address, reveal)

    async def test_import_one_key_then_reject_the_duplicate(self):
        for run in range(RUNS):
            with self.subTest(run=run):
                _store(self)
                uid = 200 + run
                ctx = SimpleNamespace(
                    user_data={"await": ("wallet_key", "evm_all")},
                    bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1))),
                )
                update = _update(KEY, uid)
                with patch.object(bot, "show_wallets", new=AsyncMock()):
                    await bot.on_text(update, ctx)
                self.assertEqual(wallets.count(uid), 1)
                self.assertIsNone(ctx.user_data.get("await"))
                ctx.user_data["await"] = ("wallet_key", "ethereum")
                again = _update(KEY, uid)
                with patch.object(bot, "show_wallets", new=AsyncMock()):
                    await bot.on_text(again, ctx)
                self.assertEqual(wallets.count(uid), 1)
                self.assertIn("already saved", again.message.reply_text.await_args.args[0])
                self.assertEqual(ctx.user_data.get("await"), ("wallet_key", "ethereum"))

    async def test_import_rejects_a_bad_key_and_keeps_the_prompt(self):
        for run in range(RUNS):
            with self.subTest(run=run):
                _store(self)
                uid = 300 + run
                ctx = SimpleNamespace(
                    user_data={"await": ("wallet_key", "ethereum")},
                    bot=SimpleNamespace(send_message=AsyncMock()),
                )
                update = _update("not-a-key", uid)
                await bot.on_text(update, ctx)
                self.assertEqual(wallets.count(uid), 0)
                self.assertIn("not a valid private key", update.message.reply_text.await_args.args[0])
                self.assertEqual(ctx.user_data.get("await"), ("wallet_key", "ethereum"))

    async def test_mint_picker_lists_wallets_while_the_chain_check_hangs(self):
        for run in range(RUNS):
            with self.subTest(run=run):
                addr = "0x" + format(run + 1, "040x")
                draft = {"chain": "ethereum", "phase": "public", "name": "Drop", "wex": []}
                entries = [(bot.ADMIN_ID, "Wallet 1", addr, 1)]

                async def hang(*_args, **_kwargs):
                    await asyncio.sleep(30)
                    return entries, []

                edit = AsyncMock()
                ctx = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
                with patch.dict(bot.DRAFTS, {8: draft}, clear=True), \
                        patch.object(bot, "mint_wallets", return_value=entries), \
                        patch.object(bot, "_draft_checks", new=hang), \
                        patch.object(bot, "save_drafts"), \
                        patch.object(bot, "WALLET_CHECK_TIMEOUT", 0.05), \
                        patch.object(bot, "_edit_or_send", new=edit):
                    await bot.show_wallet_step(1, ctx, 8)
                self.assertEqual(edit.await_count, 1)
                first_text = edit.await_args_list[0].args[3]
                self.assertIn("Wallet 1", first_text)
                self.assertIn(addr, first_text)
                buttons = [button for row in edit.await_args_list[0].args[4].inline_keyboard for button in row]
                self.assertTrue(any(button.callback_data == "wt:8:0" for button in buttons))
                self.assertTrue(all(len(button.text) <= 64 for button in buttons))
                self.assertTrue(any(button.callback_data == "mnow:8" for button in buttons))

    async def test_tapping_a_wallet_toggles_it_ten_times(self):
        addr = "0x" + "ab" * 20
        draft = {"chain": "ethereum", "phase": "public", "name": "Drop", "wex": []}
        entries = [(bot.ADMIN_ID, "Wallet 1", addr, 1)]
        ctx = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        query = SimpleNamespace(
            answer=AsyncMock(),
            from_user=SimpleNamespace(id=bot.ADMIN_ID, username=None),
            data=f"wt:4:{addr}",
            message=SimpleNamespace(chat_id=1, message_id=2),
        )
        for run in range(RUNS):
            with self.subTest(run=run), patch.dict(bot.DRAFTS, {4: draft}, clear=False), \
                    patch.object(bot, "allowed", return_value=True), \
                    patch.object(bot, "username_allowed", return_value=False), \
                    patch.object(bot, "mint_wallets", return_value=entries), \
                    patch.object(bot, "show_wallet_step", new=AsyncMock()) as picker, \
                    patch.object(bot, "save_drafts"):
                await bot.on_callback(SimpleNamespace(callback_query=query), ctx)
            excluded = addr.lower() in {item.lower() for item in draft.get("wex") or []}
            self.assertEqual(excluded, run % 2 == 0)
            picker.assert_awaited()
