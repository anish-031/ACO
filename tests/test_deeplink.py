import unittest, sys, os, asyncio
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from unittest import mock
from acobot import bot

ADDR = "0x" + "a" * 40  # all lower case: passes valid_address


class DeepLinkArg(unittest.TestCase):
    def test_valid_address_arg(self):
        self.assertEqual(bot.start_arg_contract([ADDR]), ADDR)

    def test_no_args(self):
        self.assertIsNone(bot.start_arg_contract([]))
        self.assertIsNone(bot.start_arg_contract(None))

    def test_non_address_args(self):
        self.assertIsNone(bot.start_arg_contract(["hello"]))
        self.assertIsNone(bot.start_arg_contract(["0x123"]))
        self.assertIsNone(bot.start_arg_contract(["0x" + "z" * 40]))


class _Ctx:
    def __init__(self, args):
        self.args = args
        self.user_data = {}
        self.bot = mock.MagicMock()
        self.bot.send_message = mock.AsyncMock()


def _update(uid, chat_id=None):
    u = mock.MagicMock()
    u.effective_user.id = uid
    u.effective_user.username = None
    u.effective_chat.id = chat_id or uid
    u.message.reply_text = mock.AsyncMock()
    return u


class StartHandler(unittest.TestCase):
    def run_start(self, uid, args):
        upd, ctx = _update(uid), _Ctx(args)
        with mock.patch.object(bot, "is_dm", return_value=True), \
             mock.patch.object(bot, "allowed", return_value=True), \
             mock.patch.object(bot, "start_draft", new=mock.AsyncMock()) as sd, \
             mock.patch.object(bot, "_edit_or_send", new=mock.AsyncMock(return_value=1)), \
             mock.patch.object(bot, "main_panel", return_value=("home", None)):
            asyncio.run(bot.cmd_start(upd, ctx))
        return sd

    def test_admin_deeplink_opens_addmint_draft(self):
        sd = self.run_start(bot.ADMIN_ID, [ADDR])
        sd.assert_awaited_once()
        self.assertEqual(sd.await_args.args[2], ADDR)

    def test_non_admin_deeplink_gets_home_not_draft(self):
        sd = self.run_start(12345, [ADDR])
        sd.assert_not_awaited()

    def test_admin_plain_start_gets_home(self):
        sd = self.run_start(bot.ADMIN_ID, [])
        sd.assert_not_awaited()

    def test_admin_bad_arg_gets_home(self):
        sd = self.run_start(bot.ADMIN_ID, ["garbage"])
        sd.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
