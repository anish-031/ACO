"""Repeat the bot's pure feature paths with different input shapes.

Each case runs ten times. The point is the crash that hid behind
"something broke on my side": a panel, command string, or keyboard that
Telegram cannot accept.
"""
import unittest

from acobot import bot, config, gasguard, mint_engine, opensea, wallets, watchlist
from acobot.scheduler import ScheduledMint, is_live

ALLOWED_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
                "span", "tg-spoiler", "a", "code", "pre", "blockquote"}


def _telegram_html_ok(text):
    import re
    stack = []
    for closing, name in re.findall(r"<(/?)([a-zA-Z0-9-]+)(?:\s[^>]*)?>", text):
        if name.lower() not in ALLOWED_TAGS:
            return False
        if closing:
            if not stack or stack[-1] != name.lower():
                return False
            stack.pop()
        else:
            stack.append(name.lower())
    if stack:
        return False
    # A bare "<" or ">" outside a tag breaks Telegram HTML.
    stripped = re.sub(r"</?[a-zA-Z0-9-]+(?:\s[^>]*)?>", "", text)
    stripped = stripped.replace("&lt;", "").replace("&gt;", "").replace("&amp;", "").replace("&quot;", "")
    return "<" not in stripped and ">" not in stripped and "&" not in stripped


def _buttons(markup):
    return [button for row in markup.inline_keyboard for button in row]


CASES = [
    {"name": "plain", "per_wallet": 2, "price_wei": 0, "start_ts": 1794510660,
     "gas_limit": "auto", "label": "public"},
    {"name": "long gtd", "per_wallet": 500, "price_wei": 2 * 10**15, "start_ts": 1793620800,
     "gas_limit": 500000, "label": "GTD (CCFF00, Vaultlings, SAS, ApeHood, VAMP(44.4), Reefers, Workers etc)"},
    {"name": "free huge wallet cap", "per_wallet": 10**18, "price_wei": 0, "start_ts": 1,
     "gas_limit": "auto", "label": "FCFS " + ("x" * 80)},
    {"name": "bad numbers", "per_wallet": "unlimited", "price_wei": "nope", "start_ts": 10**30,
     "gas_limit": "lots", "label": "phase <script> & friends>"},
    {"name": "missing optionals", "per_wallet": None, "price_wei": None, "start_ts": None,
     "gas_limit": None, "label": ""},
    {"name": "zero price", "per_wallet": 1, "price_wei": 0, "start_ts": 1794169800,
     "gas_limit": "250000", "label": "server-signed allowlist"},
    {"name": "custom gas", "per_wallet": 3, "price_wei": 10**16, "start_ts": "1794510660",
     "gas_limit": "180000", "label": "public mint"},
    {"name": "ampersand name", "per_wallet": 4, "price_wei": 1, "start_ts": 1790000000,
     "gas_limit": "auto", "label": "Team & Treasury <vip>"},
    {"name": "negative junk", "per_wallet": -5, "price_wei": -1, "start_ts": -50,
     "gas_limit": "", "label": "presale"},
    {"name": "bool-like", "per_wallet": True, "price_wei": False, "start_ts": False,
     "gas_limit": True, "label": "airdrop"},
]


def _draft(case, fees=True):
    label = case["label"] or "public"
    stage = {
        "label": label, "chain_key": "public", "key": "public", "gated": False,
        "price_wei": 0 if isinstance(case["price_wei"], int) and case["price_wei"] >= 0 else None,
        "start_ts": case["start_ts"] if isinstance(case["start_ts"], int) and case["start_ts"] > 0 else None,
        "per_wallet": case["per_wallet"] if isinstance(case["per_wallet"], int) and case["per_wallet"] > 0 else None,
        "list_mode": "public",
    }
    draft = {
        "contract": "0x" + "ab" * 20,
        "name": case["name"],
        "chain": "ethereum",
        "symbol": "TST",
        "standard": "erc721",
        "price_wei": case["price_wei"] if isinstance(case["price_wei"], int) else None,
        "per_wallet": case["per_wallet"],
        "start_ts": case["start_ts"] if case["start_ts"] else None,
        "phase": "public",
        "phases": ["public", "signed"],
        "schedule": [stage, dict(stage, label="signed", chain_key="signed", key="signed", gated=True, list_mode="gtd")],
        "seadrop": True,
        "mint_sig": "mintPublic(address,address,address,uint256)",
        "gas_limit": case["gas_limit"],
        "speed": "fast",
        "safety": ["owner: held", "note with <tag> & friends"],
        "notes": ["verified ABI lookup failed"],
    }
    if fees:
        draft["fees"] = {"rows": {
            k: {"label": lab, "cost_eth": 0.001 * (n + 1)}
            for n, (k, lab) in enumerate((
                ("slow", "Slow"), ("medium", "Medium"), ("fast", "Fast"), ("superfast", "Super Fast")))
        }}
        draft["usd_per_eth"] = 2100
    return draft


class FeatureMatrixTests(unittest.TestCase):
    def test_mint_panel_survives_ten_input_shapes(self):
        for case in CASES:
            with self.subTest(case=case["name"]):
                draft = _draft(case)
                text = bot.draft_text(draft)
                markup = bot.draft_kb(7, draft)
                self.assertTrue(_telegram_html_ok(text), text[:500])
                self.assertLessEqual(len(text), 4096)
                buttons = _buttons(markup)
                self.assertTrue(buttons)
                for button in buttons:
                    self.assertLessEqual(len(button.text), bot.BUTTON_TEXT_LIMIT, button.text)
                    self.assertLessEqual(len((button.callback_data or "").encode()), 64, button.callback_data)
                qty = [b for b in buttons if b.callback_data and b.callback_data.startswith("dq:")]
                self.assertLessEqual(len(qty), bot.QTY_NO_LIMIT_CAP + 1)

    def test_fee_block_and_clocks_do_not_raise(self):
        for n in range(10):
            with self.subTest(n=n):
                self.assertIn("unavailable", " ".join(bot.fee_block({"fees": None})))
                self.assertIn("unavailable", " ".join(bot.fee_block({"fees": {"rows": {}}})))
                self.assertIn("&gt;", bot.esc("\n".join(bot.fee_block(_draft(CASES[n])))))
                self.assertEqual(bot.fmt_ts(None), "unknown")
                self.assertEqual(bot.fmt_ts(10**40), "unknown")
                self.assertEqual(bot.fmt_count("nope"), "nope")
                self.assertEqual(bot.fmt_count(11111), "11,111")
                self.assertIn("unknown", bot._when("soon"))
                self.assertTrue(bot.phase_block({"label": "A" * 200, "price_wei": "x", "start_ts": "y"}, False))

    def test_address_key_schedule_and_gas_variants(self):
        samples = [
            "0x" + "ab" * 20,
            "0x" + "AB" * 20,
            "0x" + "aB" * 19 + "cd",
            "not-an-address",
            "",
            None,
            "0x1234",
            "0x" + "ff" * 20,
            "0x" + "00" * 20,
            "0X" + "11" * 20,
        ]
        keys = ["0x" + "11" * 32, "11" * 32, "0xzz", "", "0x" + "ab" * 31, "key", "0x" + "ff" * 32,
                "0x" + "00" * 32, "  0x" + "22" * 32 + " ", "0x" + "33" * 33]
        for n in range(10):
            with self.subTest(n=n):
                sample = samples[n]
                try:
                    mint_engine.validate_evm_address(sample)
                except mint_engine.MintEngineError:
                    pass
                self.assertEqual(wallets.validate_key(keys[n]), n in (0, 1, 6, 7, 8))
                self.assertIn(config._expand_endpoint("${MISSING_KEY_" + str(n) + "}"), (None,))
                self.assertEqual(config._expand_endpoint("https://rpc.example"), "https://rpc.example")
                mint = ScheduledMint(contract="0x" + "ab" * 20, chain="ethereum", start_ts=100 + n, price_wei=n)
                self.assertFalse(is_live(mint, 50))
                self.assertTrue(is_live(mint, 100 + n))
                self.assertTrue(is_live(mint, 0, sale_started_value=True))
                history = {"baseFeePerGas": [hex(10**9 * (i + 1)) for i in range(10)]}
                verdict = gasguard.analyze(history, current_base_wei=10**9 * (n + 1))
                self.assertIn(verdict["state"], {"ok", "unknown"})
                rows = []
                rows, entry = watchlist.add(rows, "ethereum" if n % 2 == 0 else "robinhood",
                                            "0x" + f"{n:02x}" * 20, 1, name=f"drop {n} <x>")
                lines = watchlist.panel_lines(rows)
                self.assertTrue(lines)
                self.assertIn(f"drop {n}", "\n".join(lines))
                mode = opensea.classify_list_mode({"label": CASES[n]["label"], "type": "PUBLIC_SALE" if n % 2 == 0 else "SIGNED_PRESALE"})
                self.assertIn(mode, {"gtd", "fcfs", "public", "presale"})

    def test_command_help_has_no_raw_telegram_markup(self):
        text = bot.help_text()
        self.assertTrue(_telegram_html_ok(text))
        for section in bot.HELP_SECTIONS:
            self.assertTrue(_telegram_html_ok(bot.help_text(section)))
        empty = "\n".join(watchlist.panel_lines([]))
        self.assertNotIn("<", empty)
        self.assertNotIn(">", empty)
