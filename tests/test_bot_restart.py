from unittest.mock import patch

from acobot import bot


def test_restart_replaces_process_instead_of_exiting():
    with patch.object(bot.os, "execv") as execv:
        bot.os.execv(bot.sys.executable, [bot.sys.executable, *bot.sys.argv])
    execv.assert_called_once_with(bot.sys.executable, [bot.sys.executable, *bot.sys.argv])
