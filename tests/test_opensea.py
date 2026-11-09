import tempfile
import unittest
import time
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from acobot import opensea, bot

SAMPLE = ('"dropBySlug":{"__typename":"Erc721SeaDropV1","maxSupply":1555,"totalSupply":0,"stages":['
 '{"label":"Reefers Whales","stageType":"SIGNED_PRESALE","stageIndex":1,"startTime":"2026-10-26T21:30:00.000Z",'
 '"endTime":"2026-10-26T22:30:00.000Z","maxTotalMintableByWallet":18,"perWalletLimit":18,"allowlistMemberCount":18,'
 '"allowlistMintAllocation":324,"price":{"usd":0,"token":{"unit":0,"symbol":"ETH"}}},'
 '{"label":"FCFS","stageType":"PUBLIC_SALE","stageIndex":0,"startTime":"2026-10-27T04:30:00.000Z",'
 '"endTime":"2026-10-27T06:30:00.000Z","maxTotalMintableByWallet":1,"perWalletLimit":1,'
 '"price":{"usd":0,"token":{"unit":0,"symbol":"ETH"}}}]}')

SAVED_STAGES_JSON = r'''{"dropBySlug":{"stages":[
{"label":"Team \u0026 Treasury","stageType":"SIGNED_PRESALE","stageIndex":1,"startTime":"2026-11-02T12:00:00.000Z","endTime":"2026-11-02T12:30:00.000Z","maxTotalMintableByWallet":500,"perWalletLimit":500,"price":{"usd":0,"token":{"unit":0,"symbol":"ETH"}}},
{"label":"GTD (CCFF00, Vaultlings, SAS, ApeHood, VAMP(44.4), Reefers, Workers etc)","stageType":"SIGNED_PRESALE","stageIndex":2,"startTime":"2026-11-02T12:30:00.000Z","endTime":"2026-11-02T15:30:00.000Z","maxTotalMintableByWallet":2,"perWalletLimit":2,"price":{"usd":0,"token":{"unit":0,"symbol":"ETH"}}},
{"label":"WL/FCFS","stageType":"SIGNED_PRESALE","stageIndex":3,"startTime":"2026-11-02T15:30:00.000Z","endTime":"2026-11-02T18:00:00.000Z","maxTotalMintableByWallet":1,"perWalletLimit":1,"price":{"usd":0,"token":{"unit":0,"symbol":"ETH"}}},
{"label":"PUBLIC: 0 Goats left","stageType":"PUBLIC_SALE","stageIndex":0,"startTime":"2026-11-02T18:00:00.000Z","endTime":"2026-11-03T18:00:00.000Z","maxTotalMintableByWallet":20,"perWalletLimit":20,"price":{"usd":4.57626,"token":{"unit":0.002,"symbol":"ETH"}}}
]}}'''


class T4(unittest.TestCase):
    def test_parse(self):
        st = opensea.parse_stages(SAMPLE)
        self.assertEqual([s["label"] for s in st], ["Reefers Whales", "FCFS"])
        self.assertEqual(st[0]["per_wallet"], 18)
        self.assertEqual(st[0]["start_ts"], 1793050200)
        self.assertEqual(st[1]["type"], "PUBLIC_SALE")

    def test_parse_empty(self):
        self.assertEqual(opensea.parse_stages("<html>nothing</html>"), [])

    def test_parse_saved_four_stage_drop_json(self):
        st = opensea.parse_stages("x" * 7000 + SAVED_STAGES_JSON)
        self.assertEqual(len(st), 4)
        self.assertEqual([s["label"] for s in st], [
            "Team & Treasury",
            "GTD (CCFF00, Vaultlings, SAS, ApeHood, VAMP(44.4), Reefers, Workers etc)",
            "WL/FCFS",
            "PUBLIC: 0 Goats left",
        ])
        self.assertEqual([s["per_wallet"] for s in st], [500, 2, 1, 20])
        self.assertEqual([s["price_eth"] for s in st], [0, 0, 0, 0.002])
        self.assertEqual(st[0]["start_ts"], 1793620800)
        self.assertEqual(st[-1]["end_ts"], 1793728800)

    def test_wallet_limit_uses_max_total_and_visible_schedule_text(self):
        html = (
            '"dropBySlug":{"maxSupply":11111,"totalSupply":0,"stages":['
            '{"label":"TREASURY","stageType":"SIGNED_PRESALE","stageIndex":1,'
            '"startTime":"2026-11-11T16:00:00.000Z","endTime":"2026-11-11T16:11:00.000Z",'
            '"maxTotalMintableByWallet":50,"price":{"token":{"unit":0}}},'
            '{"label":"FREE TO CLAIM - FCFS","stageType":"SIGNED_PRESALE","stageIndex":3,'
            '"startTime":"2026-11-11T19:11:00.000Z","endTime":"2026-11-12T19:11:00.000Z",'
            '"price":{"token":{"unit":0}}}]}}'
            '<span class="font-medium">FREE TO CLAIM - FCFS</span>'
            '<span>Free | Limit 2 per wallet</span>'
        )
        stages = opensea.parse_stages(html)
        self.assertEqual([stage["per_wallet"] for stage in stages], [50, 2])
        self.assertEqual(stages[0]["drop_supply"], 11111)
        self.assertEqual(stages[0]["drop_minted"], 0)
        self.assertEqual(stages[1]["start_ts"], 1794424260)

    def test_richer_stage_list_wins_when_both_have_the_same_count(self):
        thin = (
            '"stages":[{"label":"TREASURY","stageType":"SIGNED_PRESALE","stageIndex":1,'
            '"startTime":"2026-11-11T16:00:00.000Z","price":{"token":{"unit":0}}}]'
        )
        rich = (
            '"stages":[{"label":"TREASURY","stageType":"SIGNED_PRESALE","stageIndex":1,'
            '"startTime":"2026-11-11T16:00:00.000Z","perWalletLimit":50,"price":{"token":{"unit":0}}}]'
        )
        self.assertEqual(opensea.parse_stages(thin + rich)[0]["per_wallet"], 50)
        self.assertEqual(opensea.parse_stages(rich + thin)[0]["per_wallet"], 50)

    def test_parse_chooses_complete_stages_array_when_page_has_multiple(self):
        partial = (
            '{"dropBySlug":{"stages":[{"label":"Public only","stageType":"PUBLIC_SALE",'
            '"stageIndex":0,"startTime":"2026-11-02T18:00:00Z","endTime":"2026-11-03T18:00:00Z",'
            '"perWalletLimit":20,"price":{"token":{"unit":0.002}}}]}}'
        )
        stages = opensea.parse_stages(partial + SAVED_STAGES_JSON)
        self.assertEqual(len(stages), 4)
        self.assertEqual(stages[0]["label"], "Team & Treasury")

    def test_slug_lookup_sends_configured_key_and_uses_cached_slug_on_401(self):
        contract = "0x" + "12" * 20
        cache_key = f"slug:robinhood:{contract.lower()}"
        response = Mock(status_code=401)
        with patch.object(opensea, "_loaded", [True]), \
                patch.object(opensea, "_cache", {cache_key: {"slug": "goat-ranch-club"}}), \
                patch.object(opensea, "api_key", return_value="configured-key"), \
                patch("httpx.get", return_value=response) as get:
            slug = opensea._slug_for("robinhood", contract)
        self.assertEqual(slug, "goat-ranch-club")
        self.assertEqual(get.call_args_list[0].kwargs["headers"]["X-API-KEY"], "configured-key")

    def test_slug_lookup_falls_back_to_public_asset_redirect(self):
        contract = "0x" + "12" * 20
        api_response = Mock(status_code=401)
        asset_response = Mock(
            status_code=200,
            url=f"https://opensea.io/collection/vibe-town",
        )
        with patch.object(opensea, "_loaded", [True]), \
                patch.object(opensea, "_cache", {}), \
                patch.object(opensea, "api_key", return_value=""), \
                patch("httpx.get", side_effect=[api_response, asset_response]) as get:
            slug = opensea._slug_for("robinhood", contract)
        self.assertEqual(slug, "vibe-town")
        self.assertIn("/assets/robinhood/" + contract + "/1", get.call_args_list[1].args[0])

    def test_collection_fetch_uses_httpx(self):
        response = Mock(status_code=200, text=SAVED_STAGES_JSON)
        with patch.object(opensea, "_slug_for", return_value="goat-ranch-club"), \
                patch("httpx.get", return_value=response) as get:
            slug, html = opensea._fetch("robinhood", "0x" + "12" * 20)
        self.assertEqual(slug, "goat-ranch-club")
        self.assertEqual(len(opensea.parse_stages(html)), 4)
        get.assert_called_once()
        self.assertEqual(get.call_args.args[0], "https://opensea.io/collection/goat-ranch-club")

    def test_forced_schedule_refresh_retries_cached_miss(self):
        original_cache = dict(opensea._cache)
        original_loaded = opensea._loaded[0]
        try:
            contract = "0x" + "12" * 20
            opensea._cache.clear()
            opensea._cache[f"robinhood:{contract}"] = {"ts": time.time(), "stages": None}
            opensea._loaded[0] = True
            with tempfile.TemporaryDirectory() as temp_dir, \
                    patch.object(opensea, "CACHE_FILE", Path(temp_dir) / "cache.json"), \
                    patch.object(opensea, "_fetch", return_value=("goat-ranch-club", SAVED_STAGES_JSON)) as fetch:
                stages = opensea.fetch_schedule("robinhood", contract, force=True)
            self.assertEqual(len(stages), 4)
            fetch.assert_called_once_with("robinhood", contract)
        finally:
            opensea._cache.clear()
            opensea._cache.update(original_cache)
            opensea._loaded[0] = original_loaded

    def test_merge_keeps_chain_gating(self):
        chain = [{"key": "signed", "gated": True, "signers": 1},
                 {"key": "public", "gated": False, "price_wei": 0, "start_ts": 1793056200 + 7 * 3600 + 3600 * 0,
                  "end_ts": None, "per_wallet": 1}]
        os_st = opensea.parse_stages(SAMPLE)
        m = opensea.merge(chain, os_st)
        self.assertEqual(m[0]["chain_key"], "signed")
        self.assertFalse(m[0]["snipeable"])
        pub = [x for x in m if x["label"] == "FCFS"][0]
        self.assertEqual(pub["chain_key"], "public")
        self.assertTrue(pub["snipeable"])
        self.assertEqual(pub["price_wei"], 0)
        self.assertEqual(pub["display_price_wei"], 0)
        self.assertTrue(all(x["gated"] and not x["snipeable"] for x in m if x["chain_key"] == "signed"))

    def test_merge_preserves_all_gated_rows_without_public_chain_data(self):
        os_stages = opensea.parse_stages(SAVED_STAGES_JSON)
        merged = opensea.merge([{"key": "allowlist", "gated": True}], os_stages)
        self.assertEqual(len(merged), 4)
        self.assertTrue(all(st["chain_key"] == "allowlist" and st["gated"] and not st["snipeable"]
                            for st in merged[:3]))
        public = merged[-1]
        self.assertIsNone(public["chain_key"])
        self.assertFalse(public["snipeable"])

    def test_draft_panel_renders_all_four_display_phases(self):
        chain = [
            {"key": "allowlist", "gated": True, "price_wei": None, "start_ts": None, "end_ts": None,
             "per_wallet": None},
            {"key": "public", "gated": False, "price_wei": 2 * 10**15,
             "start_ts": 1793620800, "end_ts": 1793728800, "per_wallet": 20},
        ]
        schedule = opensea.merge(chain, opensea.parse_stages(SAVED_STAGES_JSON))
        draft = {
            "contract": "0x" + "12" * 20,
            "chain": "robinhood",
            "price_wei": 2 * 10**15,
            "start_ts": 1793620800,
            "end_ts": 1793728800,
            "per_wallet": 20,
            "seadrop": True,
            "phases": ["allowlist", "public"],
            "phase": "public",
            "schedule": schedule,
            "mint_sig": "mintPublic(address,address,address,uint256)",
            "gas_limit": "auto",
        }
        text = bot.draft_text(draft)
        for label in ("Team &amp; Treasury", "GTD (CCFF00", "WL/FCFS", "PUBLIC: 0 Goats left"):
            self.assertIn(label, text)
        self.assertIn("500 per wallet", text)
        self.assertIn("20 per wallet", text)
        self.assertIn("Mint price: Free", text)
        self.assertIn("0.002 ETH", text)
        self.assertIn("Closes: 2026-11-03 18:00 UTC", text)
        self.assertIn("GMT+5:45", text)
        self.assertTrue(all(st["gated"] and not st["snipeable"] for st in schedule[:3]))
        self.assertTrue(schedule[-1]["snipeable"])

    def test_selecting_gtd_uses_its_display_terms_and_stays_gated(self):
        chain = [
            {"key": "allowlist", "gated": True, "price_wei": None, "start_ts": None,
             "end_ts": None, "per_wallet": None},
            {"key": "public", "gated": False, "price_wei": 2 * 10**15,
             "start_ts": 1793620800, "end_ts": 1793728800, "per_wallet": 20},
        ]
        schedule = opensea.merge(chain, opensea.parse_stages(SAVED_STAGES_JSON))
        draft = {
            "contract": "0x" + "12" * 20,
            "chain": "robinhood",
            "price_wei": 2 * 10**15,
            "start_ts": 1793620800,
            "end_ts": 1793728800,
            "per_wallet": 20,
            "seadrop": True,
            "phase": "signed",
            "phase_idx": 1,
            "schedule": schedule,
            "mint_sig": "mintPublic(address,address,address,uint256)",
            "gas_limit": "auto",
        }

        bot.apply_mint_stage(draft, {
            "chain_key": "signed", "gated": True, "price_wei": 0,
            "start_ts": 100, "end_ts": 200, "per_wallet": 1,
        })
        bot.apply_mint_stage(draft, schedule[1])
        view = bot.draft_phase_view(draft)
        self.assertIn("GTD (CCFF00", view["_selected_stage"]["label"])
        self.assertEqual(view["price_wei"], 0)
        self.assertEqual(view["start_ts"], schedule[1]["display_start_ts"])
        self.assertEqual(view["per_wallet"], 2)
        self.assertEqual(draft["price_wei"], 2 * 10**15)
        self.assertEqual(draft["start_ts"], 1793620800)
        self.assertFalse(bot.draft_phase_snipeable(draft))

        text = bot.draft_text(draft)
        self.assertIn("<b>GTD (CCFF00", text)
        self.assertIn("phase authorization: required", text)
        self.assertIn("missing wallet-specific Merkle proof, phase mint parameters", text)
        self.assertIn("quantity/cost: unavailable until the phase returns valid wallet authorization", text)
        self.assertIn("Mint price: Free", text)
        self.assertIn("Limit: 2 per wallet", text)
        self.assertIn("Opens: 2026-11-02 12:30 UTC", text)
        self.assertIn("Normal only", text)
        self.assertIn("GTD: guaranteed allowlist spot", text)
        self.assertIn("discovered and simulated per wallet before the phase opens", text)

        markup = bot.draft_kb(4, draft)
        buttons = [button for row in markup.inline_keyboard for button in row]
        self.assertTrue(any(button.callback_data == "dsched:4" for button in buttons))
        self.assertTrue(any(button.callback_data == "fm:4" for button in buttons))
        self.assertTrue(any(button.callback_data == "dfm:4:prefire" for button in buttons))
        self.assertTrue(any(button.callback_data == "dfm:4:normal" for button in buttons))
        self.assertTrue(any(button.callback_data == "dfm:4:dual" for button in buttons))
        self.assertFalse(any(button.callback_data.startswith("dee:") for button in buttons))
        self.assertFalse(any("authorization status" in button.text for button in buttons))
        self.assertFalse(any(button.callback_data == "dq:4:20" for button in buttons))

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
                bot.mintapi, "STORE", Path(temp_dir) / "mint_apis.json"):
            bot.mintapi.save(
                draft["contract"],
                "https://mint.example/api/{wallet}?phase={phase}&stage={stage_index}&qty={qty}")
            self.assertTrue(bot.draft_phase_authorized(draft))
            authorized_markup = bot.draft_kb(4, draft)
            authorized_buttons = [button for row in authorized_markup.inline_keyboard for button in row]
            self.assertTrue(any(button.callback_data == "dsched:4" for button in authorized_buttons))
            self.assertTrue(any(button.callback_data == "dq:4:2" for button in authorized_buttons))

        bot.apply_mint_stage(draft, schedule[3])
        draft["phase_idx"] = 3
        self.assertTrue(bot.draft_phase_snipeable(draft))
        public_markup = bot.draft_kb(4, draft)
        public_buttons = [button for row in public_markup.inline_keyboard for button in row]
        self.assertTrue(any(button.callback_data == "dsched:4" for button in public_buttons))
        self.assertEqual(
            {button.callback_data.rsplit(":", 1)[-1] for button in public_buttons
             if button.callback_data and button.callback_data.startswith("dfm:")},
            {"prefire", "normal", "dual"},
        )

    def test_schedule_failure_is_visible_in_panel(self):
        chain = [
            {"key": "public", "gated": False, "price_wei": 0, "start_ts": 1793620800, "per_wallet": 20},
            {"key": "allowlist", "gated": True},
        ]
        draft = {
            "contract": "0x" + "12" * 20,
            "chain": "robinhood",
            "price_wei": 0,
            "start_ts": 1793620800,
            "per_wallet": 20,
            "seadrop": True,
            "phases": ["allowlist", "public"],
            "phase": "public",
            "schedule": opensea.merge(chain, None),
            "schedule_unavailable": True,
            "mint_sig": "mintPublic(address,address,address,uint256)",
            "gas_limit": "auto",
        }
        text = bot.draft_text(draft)
        self.assertIn("schedule unavailable (couldn't read the drop page)", text)
        self.assertIn("public", text)
        self.assertIn("allowlist", text)

    def test_fee_marker_is_escaped_for_telegram_html(self):
        draft = {
            "contract": "0x" + "12" * 20,
            "chain": "ethereum",
            "name": "JPEGZ",
            "price_wei": 0,
            "per_wallet": 2,
            "phase": "public",
            "gas_limit": "auto",
            "speed": "fast",
            "fees": {
                "rows": {
                    "slow": {"label": "Slow", "cost_eth": 0.0003},
                    "medium": {"label": "Medium", "cost_eth": 0.0004},
                    "fast": {"label": "Fast", "cost_eth": 0.0007},
                    "superfast": {"label": "Super Fast", "cost_eth": 0.0018},
                }
            },
            "usd_per_eth": 2000,
        }
        text = bot.draft_text(draft)
        self.assertIn("⚡", text)
        self.assertNotIn("<pre>", text)
        self.assertNotIn("&gt;", text)
        self.assertNotIn(">", text.split("<b>")[0])

    def test_phase_statuses(self):
        now = int(time.time())
        self.assertEqual(bot._phase_status(now - 120, now - 60), "ended")
        self.assertEqual(bot._phase_status(now + 60, now + 120), "upcoming")
        self.assertEqual(bot._phase_status(now - 60, now + 60), "live")
        self.assertEqual(bot._phase_status(None, None), "unknown")

    def test_merge_without_opensea_keeps_chain(self):
        chain = [{"key": "public", "gated": False, "price_wei": 0}]
        m = opensea.merge(chain, None)
        self.assertEqual(m[0]["key"], "public")
        self.assertIsNone(m[0]["label"])

    def test_official_drop_payload_keeps_every_phase(self):
        stages = opensea.stages_from_drop({
            "stages": [
                {"label": "GTD", "stageType": "PRESALE", "stageIndex": 0,
                 "startTime": "2026-11-08T00:00:00Z", "endTime": "2026-11-08T01:00:00Z",
                 "maxPerWallet": 2, "price": {"token": {"unit": "0"}},
                 "allowlistMemberCount": 100, "allowlistMintAllocation": 200},
                {"label": "FCFS", "stageType": "PRESALE", "stageIndex": 1,
                 "startTime": "2026-11-08T01:00:00Z", "maxPerWallet": 1,
                 "price": "0.05", "allowlistMemberCount": 5000},
                {"label": "Public", "stageType": "PUBLIC_SALE", "stageIndex": 2,
                 "startTime": "2026-11-08T02:00:00Z", "maxPerWallet": 3, "price": 0},
            ]
        })
        self.assertEqual([stage["list_mode"] for stage in stages], ["gtd", "fcfs", "public"])
        self.assertEqual(stages[0]["allowlist_allocation"], 200)
        self.assertEqual(stages[1]["price_eth"], 0.05)
        self.assertEqual(stages[2]["index"], 2)

    def test_phase_block_format(self):
        st = {"label": "Reefers Whales", "chain_key": "signed", "gated": True, "price_wei": 0,
              "per_wallet": 18, "start_ts": 1793056200}
        txt = "\n".join(bot.phase_block(st, False))
        self.assertIn("MP: FREE | Limit: 18 per wallet", txt)
        self.assertIn("UTC (", txt)
        self.assertIn("2026-10-27", txt)
        self.assertIn("GMT+5:45", txt)
        self.assertIn("needs allowlist spot", txt)

    def test_saved_drop_classifies_gtd_fcfs_and_public(self):
        stages = opensea.parse_stages(SAVED_STAGES_JSON)
        self.assertEqual([stage["list_mode"] for stage in stages], ["presale", "gtd", "fcfs", "public"])
        public_fcfs = opensea.parse_stages(SAMPLE)
        self.assertEqual(public_fcfs[0]["list_mode"], "gtd")
        self.assertEqual(public_fcfs[1]["list_mode"], "fcfs")
        self.assertEqual(public_fcfs[1]["type"], "PUBLIC_SALE")

    def test_merge_keeps_signed_gtd_separate_from_merkle_fcfs(self):
        chain = [
            {"key": "signed", "gated": True, "signers": 1},
            {"key": "allowlist", "gated": True, "merkle_root": "0x" + "ab" * 32},
            {"key": "public", "gated": False, "price_wei": 0, "start_ts": 300},
        ]
        os_stages = [
            {"label": "GTD", "type": "SIGNED_PRESALE", "index": 1, "start_ts": 100,
             "end_ts": 200, "per_wallet": 2, "price_eth": 0, "list_mode": "gtd"},
            {"label": "WL FCFS", "type": "ALLOWLIST", "index": 2, "start_ts": 200,
             "end_ts": 300, "per_wallet": 1, "price_eth": 0, "list_mode": "fcfs"},
            {"label": "Public", "type": "PUBLIC_SALE", "index": 0, "start_ts": 300,
             "end_ts": None, "per_wallet": 5, "price_eth": 0.01, "list_mode": "public"},
        ]
        merged = opensea.merge(chain, os_stages)
        by_label = {stage["label"]: stage for stage in merged}
        self.assertEqual(by_label["GTD"]["chain_key"], "signed")
        self.assertEqual(by_label["GTD"]["list_mode"], "gtd")
        self.assertFalse(by_label["GTD"]["snipeable"])
        self.assertEqual(by_label["WL FCFS"]["chain_key"], "allowlist")
        self.assertEqual(by_label["WL FCFS"]["list_mode"], "fcfs")
        self.assertTrue(by_label["WL FCFS"]["gated"])
        self.assertEqual(by_label["Public"]["chain_key"], "public")
        self.assertTrue(by_label["Public"]["snipeable"])

    def test_selecting_fcfs_defaults_to_both_shots_until_the_owner_picks(self):
        schedule = opensea.merge(
            [{"key": "allowlist", "gated": True},
             {"key": "public", "gated": False, "price_wei": 0, "start_ts": 1793620800}],
            opensea.parse_stages(SAVED_STAGES_JSON),
        )
        draft = {"phase": "allowlist", "schedule": schedule, "fire_mode": "dual"}
        fcfs = next(stage for stage in schedule if stage["list_mode"] == "fcfs")
        bot.apply_mint_stage(draft, fcfs)
        self.assertEqual(draft["list_mode"], "fcfs")
        self.assertEqual(draft["fire_mode"], "dual")
        self.assertIn("FCFS: first come, first served", "\n".join(bot.phase_block(fcfs, True)))
        draft["fire_mode_picked"] = True
        draft["fire_mode"] = "prefire"
        bot.apply_mint_stage(draft, schedule[1])
        self.assertEqual(draft["fire_mode"], "prefire")
        self.assertEqual(draft["list_mode"], "gtd")


class GatedScheduleTests(unittest.IsolatedAsyncioTestCase):
    async def test_gated_wallet_picker_shows_open_time_simulation_not_eligibility_check(self):
        draft = {
            "chain": "robinhood",
            "phase": "signed",
            "seadrop": True,
            "schedule": [{"chain_key": "signed", "gated": True}],
            "wex": [],
        }
        entries = [(bot.ADMIN_ID, "Wallet 1", "0x" + "44" * 20, 1)]
        ctx = SimpleNamespace(user_data={})
        edit = AsyncMock()
        with patch.dict(bot.DRAFTS, {77: draft}, clear=True), \
                patch.object(bot, "mint_wallets", return_value=entries), \
                patch.object(bot, "_edit_or_send", new=edit):
            await bot.show_wallet_step(123, ctx, 77)
        text, markup = edit.await_args.args[3:5]
        buttons = [button for row in markup.inline_keyboard for button in row]
        self.assertIn("phase authorization: missing wallet-specific project signature", text)
        self.assertFalse(any(button.callback_data == "wchk:77" for button in buttons))
        self.assertTrue(any(button.callback_data == "wgo:77" for button in buttons))

    async def test_legacy_eligibility_report_never_changes_wallet_selection(self):
        wallet = "0x" + "44" * 20
        draft = {
            "contract": "0x" + "12" * 20,
            "chain": "robinhood",
            "schedule": [{"label": "GTD", "chain_key": "signed", "gated": True}],
            "wex": [],
        }
        query = SimpleNamespace(
            answer=AsyncMock(),
            from_user=SimpleNamespace(id=bot.ADMIN_ID, username=None),
            data="dee:77:0",
            message=SimpleNamespace(chat_id=123, message_id=1),
        )
        update = SimpleNamespace(callback_query=query)
        ctx = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        wallets_found = [(bot.ADMIN_ID, "Wallet 1", wallet, 1)]
        probe_result = [( {"label": "Wallet 1", "address": wallet},
                          {"eligible": True, "source": "test", "detail": ""})]
        with patch.dict(bot.DRAFTS, {77: draft}, clear=True), \
                patch.object(bot, "allowed", return_value=True), \
                patch.object(bot, "username_allowed", return_value=False), \
                patch.object(bot, "mint_wallets", return_value=wallets_found), \
                patch("acobot.eligible.probe_fleet", new=AsyncMock(return_value=probe_result)), \
                patch.object(bot, "_safe_send", new=AsyncMock()) as send:
            await bot.on_callback(update, ctx)
        self.assertEqual(draft["wex"], [])
        self.assertNotIn("eligible_addrs", draft)
        self.assertIn("wallet selection is unchanged", send.await_args.args[2])

    async def test_preflight_warnings_do_not_block_schedule_or_filter_wallets(self):
        draft = {
            "contract": "0x" + "12" * 20,
            "chain": "robinhood",
            "price_wei": 0,
            "start_ts": int(time.time()) + 600,
            "seadrop": True,
            "phase": "public",
            "schedule": [{"chain_key": "public", "gated": False, "snipeable": True}],
            "mint_sig": "mintPublic(address,address,address,uint256)",
            "wex": [],
        }
        entries = [(bot.ADMIN_ID, "Wallet 1", "0x" + "44" * 20, 1)]
        ctx = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()), application=object())
        edit = AsyncMock()
        with patch.dict(bot.DRAFTS, {77: draft}, clear=True), \
                patch.object(bot, "mint_wallets", return_value=entries), \
                patch.object(bot, "_draft_checks", new=AsyncMock(return_value=(
                    entries,
                    [{"label": "Wallet 1", "address": entries[0][2], "masked": "0x...4444",
                      "verdict": "fail", "reasons": ["mint simulation reverted: not eligible"]}],
                ))), \
                patch.object(bot, "_edit_or_send", new=edit):
            await bot.run_schedule_check(123, ctx, 77)
        markup = edit.await_args.args[4]
        buttons = [button for row in markup.inline_keyboard for button in row]
        callbacks = {button.callback_data for button in buttons}
        self.assertIn("wconfirm:77", callbacks)
        self.assertFalse(any(callback.startswith("fm:") for callback in callbacks))
        self.assertEqual(draft["wex"], [])
        self.assertIn("Warnings do not change wallet selection", edit.await_args.args[3])

    async def test_gated_phase_can_be_scheduled_while_authorization_is_pending(self):
        draft = {
            "contract": "0x" + "12" * 20,
            "chain": "robinhood",
            "price_wei": 2 * 10**15,
            "start_ts": int(time.time()) + 600,
            "seadrop": True,
            "phase": "signed",
            "phase_idx": 0,
            "schedule": [
                {"chain_key": "signed", "gated": True, "snipeable": False,
                 "display_price_wei": 0, "display_start_ts": 1793622600},
                {"chain_key": "public", "gated": False, "snipeable": True},
            ],
            "mint_sig": "mintPublic(address,address,address,uint256)",
        }
        wallet = (bot.ADMIN_ID, "Wallet 1", "0x" + "44" * 20, 1)
        ctx = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()), application=object())
        with patch.dict(bot.DRAFTS, {77: draft}, clear=True), \
                patch.dict(bot.MINTS, {}, clear=True), \
                patch.object(bot, "_next_id", [1]), \
                patch.object(bot, "mint_wallets", return_value=[wallet]), \
                patch.object(bot.mintapi, "supports_phase", return_value=False), \
                patch.object(bot, "persist"), \
                patch.object(bot, "save_drafts"), \
                patch.object(bot, "start_watcher"), \
                patch.object(bot, "_edit_or_send", new=AsyncMock()):
            await bot.schedule_draft(123, ctx, 77, workflow="force")
            self.assertEqual(len(bot.MINTS), 1)
            scheduled = bot.MINTS[1]
            self.assertTrue(scheduled["gated"])
            self.assertEqual(scheduled["authorization_status"], "waiting")

    async def test_authorized_phase_schedule_uses_selected_stage_window(self):
        from unittest.mock import AsyncMock

        contract = "0x" + "12" * 20
        start = int(time.time()) + 600
        draft = {
            "contract": contract,
            "chain": "robinhood",
            "price_wei": 2 * 10**15,
            "start_ts": start - 600,
            "end_ts": start + 86400,
            "per_wallet": 20,
            "mint_qty": 2,
            "seadrop": True,
            "mint_to": "0x" + "22" * 20,
            "fee_recipient": "0x" + "33" * 20,
            "phase": "signed",
            "phase_idx": 0,
            "schedule": [{
                "chain_key": "signed", "gated": True, "snipeable": False,
                "label": "GTD", "list_mode": "gtd", "index": 2, "display_start_ts": start,
                "display_end_ts": start + 1800, "display_price_wei": 0,
                "display_per_wallet": 2,
            }],
            "mint_sig": "mintPublic(address,address,address,uint256)",
        }
        ctx = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()), application=object())
        old_next = bot._next_id[0]
        try:
            with tempfile.TemporaryDirectory() as temp_dir, \
                    patch.object(bot.mintapi, "STORE", Path(temp_dir) / "mint_apis.json"), \
                    patch.dict(bot.DRAFTS, {77: draft}, clear=True), \
                    patch.dict(bot.MINTS, {}, clear=True), \
                    patch.object(bot, "mint_wallets", return_value=[
                        (bot.ADMIN_ID, "Wallet 1", "0x" + "44" * 20, 1)]), \
                    patch.object(bot, "_split_by_local_eligibility",
                                 new=AsyncMock(return_value=(
                                     [(bot.ADMIN_ID, "Wallet 1", "0x" + "44" * 20, 1)], [], {}))), \
                    patch.object(bot, "persist"), patch.object(bot, "save_drafts"), \
                    patch.object(bot, "start_watcher"):
                bot._next_id[0] = 1
                bot.mintapi.save(
                    contract, "https://mint.example/{wallet}?phase={phase}&stage={stage_index}")
                await bot.schedule_draft(123, ctx, 77)
                record = bot.MINTS[1]
                self.assertEqual(record["start_ts"], start)
                self.assertEqual(record["end_ts"], start + 1800)
                self.assertIsNone(record["price_wei"])
                self.assertEqual(record["display_price_wei"], 0)
                self.assertEqual(record["per_wallet"], 2)
                self.assertTrue(record["gated"])
                self.assertFalse(record["authorization_at_fire"])
                self.assertEqual(record["phase_label"], "GTD")
                self.assertEqual(record["list_mode"], "gtd")
                self.assertEqual(record["stage_index"], 2)
                self.assertEqual(record["fire_mode"], "dual")
        finally:
            bot._next_id[0] = old_next
