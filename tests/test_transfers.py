import asyncio, os, shlex, sys, unittest, tempfile
from types import SimpleNamespace
from pathlib import Path
from decimal import Decimal
from unittest.mock import AsyncMock
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from acobot import bot, tokens, wallets, fleet, panels, prices, solana, walletops
from acobot.config import Chain

KEY = "0x" + "11" * 32
KEY2 = "0x" + "22" * 32
KEY3 = "0x" + "33" * 32
ADDR = wallets.address_of(KEY)
CHAIN = Chain(name="ethereum", chain_id=1, rpc=["x"], broadcast_rpc=["x"], priority_fee_gwei=2.0)
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"


class FakeRPC:
    """Answers balances, symbol/decimals and fee calls. Records calls."""
    def __init__(self, eth=10**18, tok=None):
        self.eth = eth; self.tok = tok or {}  # contract lower -> raw balance
        self.sent = []
    def call(self, method, params, url=None):
        if method == "eth_getBalance": return hex(self.eth)
        if method == "eth_getBlockByNumber": return {"baseFeePerGas": hex(10**9)}
        if method == "eth_maxPriorityFeePerGas": return hex(10**9)
        if method == "eth_getTransactionCount": return "0x0"
        raise RuntimeError(method)
    def eth_call(self, to, data):
        sel = data[2:10]
        if sel == tokens.abi.selector_of("symbol()"):
            return "0x" + "20" * 32 + "0" * 0 if False else _str_ret("USDC")
        if sel == tokens.abi.selector_of("decimals()"):
            return "0x" + format(6, "064x")
        if sel == tokens.abi.selector_of("balanceOf(address)"):
            return "0x" + format(self.tok.get(to.lower(), 0), "064x")
        raise RuntimeError("unknown call")


def _str_ret(s):
    b = s.encode()
    return "0x" + format(32, "064x") + format(len(b), "064x") + b.ljust(32, b"\0").hex()


class Bcast:
    def __init__(self): self.raws = []
    def broadcast(self, raw):
        self.raws.append(raw)
        return {"u": "0x" + "ab" * 32}


class TransferTests(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp())
        wallets.STORE = d / "wallets.json"; wallets.MASTER = d / "master.key"
        tokens.TOKENS_FILE = d / "tokens.json"
        self.old_cfg = bot.CFG
        bot.CFG = SimpleNamespace(chains={"ethereum": CHAIN})
        self.addCleanup(setattr, bot, "CFG", self.old_cfg)
        self.uid = 1
        wallets.add_wallet(self.uid, KEY, chain="ethereum")

    def test_native_plan_and_all(self):
        rpc = FakeRPC(eth=10**18)
        tok = {"kind": "native", "symbol": "ETH", "decimals": 18, "contract": None}
        rows = fleet.plan_transfers(rpc, CHAIN, tok, [("Wallet 2", ADDR)], "0x" + "22" * 20, "all")
        self.assertTrue(rows[0]["ok"])
        self.assertLess(rows[0]["raw"], 10**18)  # all minus gas
        self.assertGreater(rows[0]["raw"], 0)

    def test_single_and_all_fleet_native_transfers_are_mock_broadcast(self):
        wallets.add_wallet(self.uid, KEY2, chain="ethereum")
        wallets.add_wallet(self.uid, KEY3, chain="ethereum")
        main = wallets.main_wallet(self.uid)
        single = bot._select_wallets(self.uid, "Wallet 2", main["address"])
        fleet_wallets = bot._select_wallets(self.uid, "all", main["address"])
        self.assertEqual(len(single), 1)
        self.assertEqual(single[0]["address"], wallets.address_of(KEY2))
        self.assertEqual(len(fleet_wallets), 2)

        token = {"kind": "native", "symbol": "ETH", "decimals": 18, "contract": None}
        senders = [(w["label"], w["address"]) for w in fleet_wallets]
        rows = fleet.plan_transfers(FakeRPC(eth=10**18), CHAIN, token, senders,
                                    main["address"], "all")
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["ok"] and 0 < row["raw"] < 10**18 for row in rows))

        bcast = Bcast()
        result = fleet.execute_transfers(self.uid, rows, token, CHAIN, FakeRPC(eth=10**18), bcast)
        self.assertEqual(len(bcast.raws), 2)
        self.assertTrue(all(row["sent"] for row in result))

    def test_send_wizard_destinations_exclude_source_and_resolve_all(self):
        wallets.add_wallet(self.uid, KEY2, chain="ethereum")
        wallets.add_wallet(self.uid, KEY3, chain="ethereum")
        source = wallets.address_of(KEY2)

        all_destinations = bot._send_wizard_destinations(
            self.uid, "all", "ethereum", {source})
        self.assertEqual({address.lower() for address, _ in all_destinations},
                         {ADDR.lower(), wallets.address_of(KEY3).lower()})

        selected = bot._send_wizard_destinations(
            self.uid, "multi:" + ADDR + "," + wallets.address_of(KEY3),
            "ethereum", {source})
        self.assertEqual([address.lower() for address, _ in selected],
                         [ADDR.lower(), wallets.address_of(KEY3).lower()])
        with self.assertRaisesRegex(ValueError, "same as a source"):
            bot._send_wizard_destinations(self.uid, "multi:" + source,
                                          "ethereum", {source})

    def test_send_wizard_splits_amount_exactly_and_rejects_all_split(self):
        self.assertEqual(bot._send_amounts_per_recipient("0.3", 2, 18),
                         ["0.15", "0.15"])
        self.assertEqual(bot._send_amounts_per_recipient("0.000000003", 2, 9),
                         ["0.000000002", "0.000000001"])
        with self.assertRaisesRegex(ValueError, "cannot be split safely"):
            bot._send_amounts_per_recipient("all", 2, 18)

    def test_send_command_parses_quoted_wallet_label(self):
        parts = shlex.split('/send "Wallet 2" 0.1 native main')
        self.assertEqual(bot._parse_send_args(parts),
                         {"who": "Wallet 2", "amount": "0.1",
                          "token": "native", "to": "main"})

    def test_short_amount_is_skipped(self):
        rpc = FakeRPC(eth=10**17)
        tok = {"kind": "native", "symbol": "ETH", "decimals": 18, "contract": None}
        rows = fleet.plan_transfers(rpc, CHAIN, tok, [("Wallet 2", ADDR)], "0x" + "22" * 20, "1")
        self.assertFalse(rows[0]["ok"])
        self.assertIn("insufficient funds", rows[0]["why"])

    def test_erc20_amount_and_parse(self):
        self.assertEqual(tokens.parse_amount("1.5", 6), 1_500_000)
        with self.assertRaises(ValueError):
            tokens.parse_amount("0", 6)
        with self.assertRaises(ValueError):
            tokens.parse_amount("abc", 6)

    def test_erc20_transfer_calldata_and_signed(self):
        rpc = FakeRPC(eth=10**18, tok={USDC.lower(): 5_000_000})
        tok = {"kind": "erc20", "symbol": "USDC", "decimals": 6, "contract": USDC}
        rows = fleet.plan_transfers(rpc, CHAIN, tok, [("Wallet 2", ADDR)], "0x" + "22" * 20, "2")
        self.assertTrue(rows[0]["ok"]); self.assertEqual(rows[0]["raw"], 2_000_000)
        b = Bcast()
        res = fleet.execute_transfers(self.uid, rows, tok, CHAIN, rpc, b)
        self.assertTrue(res[0]["sent"]); self.assertTrue(res[0]["tx"].startswith("0x"))
        self.assertEqual(len(b.raws), 1)

    def test_no_key_leak_in_text(self):
        rpc = FakeRPC(eth=10**18)
        tok = {"kind": "native", "symbol": "ETH", "decimals": 18, "contract": None}
        rows = fleet.plan_transfers(rpc, CHAIN, tok, [("Wallet 1", ADDR)], "0x" + "22" * 20, "0.1")
        text = "\n".join(panels.transfer_lines("send preview", "main", rows))
        self.assertNotIn(KEY[2:], text)
        self.assertIn(wallets.mask(ADDR), text)

    def test_balance_block_and_usd_format(self):
        snap = {"native_wei": 10**18, "native_usd": 3000.0,
                "tokens": [{"symbol": "USDC", "raw": 2_500_000, "decimals": 6, "usd": 2.5}], "errors": []}
        txt = panels.balance_block("Wallet 1", "0x95e7...637a", "ethereum", snap, "ETH")
        self.assertIn("ETH 1", txt); self.assertIn("USDC 2.5", txt); self.assertIn("$3,000.00", txt)

    def test_token_usd_cache_and_none(self):
        prices._tcache[("ethereum", USDC.lower())] = (__import__("time").time(), 1.0)
        self.assertEqual(prices.token_usd("ethereum", USDC), 1.0)
        self.assertIsNone(prices.token_usd("robinhood", USDC))

    def test_token_sweep_plan_and_exec(self):
        rpc = FakeRPC(tok={USDC.lower(): 7_000_000})
        meta = [{"contract": USDC, "symbol": "USDC", "decimals": 6}]
        rows, notes = fleet.plan_token_sweep(rpc, CHAIN, meta, [("Wallet 2", ADDR)], "0x" + "33" * 20)
        self.assertEqual(len(rows), 1); self.assertEqual(rows[0]["raw"], 7_000_000)
        b = Bcast()
        res = fleet.execute_token_sweep(self.uid, rows, CHAIN, rpc, b)
        self.assertTrue(res[0]["sent"])

    def test_receipt_lines_show_tx(self):
        res = [{"label": "Wallet 1", "masked": "0x1234...abcd", "raw": 10**18, "decimals": 18, "symbol": "ETH",
                "sent": True, "tx": "0x" + "ab" * 32}]
        txt = "\n".join(panels.receipt_lines(res))
        self.assertIn("0x" + "ab" * 32, txt); self.assertIn("summary: 1 sent, 0 failed", txt)

    def test_resolve_unknown_symbol_errors(self):
        rpc = FakeRPC()
        with self.assertRaises(tokens.ValueError if hasattr(tokens, "ValueError") else ValueError):
            tokens.resolve(rpc, "ethereum", "NOPE")


@pytest.mark.parametrize("destination_mode", ["all", "multi"])
def test_send_wizard_destinations_reach_confirmation(monkeypatch, tmp_path, destination_mode):
    monkeypatch.setattr(wallets, "STORE", tmp_path / "send-wallets.json")
    monkeypatch.setattr(wallets, "MASTER", tmp_path / "send-master.key")
    uid = bot.ADMIN_ID
    wallets.add_wallet(uid, KEY, chain="ethereum")
    wallets.add_wallet(uid, KEY2, chain="ethereum")
    wallets.add_wallet(uid, KEY3, chain="ethereum")
    monkeypatch.setattr(bot, "CFG", SimpleNamespace(chains={"ethereum": CHAIN}))
    monkeypatch.setattr(bot, "allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_edit_or_send", AsyncMock())
    monkeypatch.setattr(bot, "RPC", lambda _urls: object())
    monkeypatch.setattr(bot.tkn, "resolve", lambda *_args: {
        "kind": "native", "symbol": "ETH", "decimals": 18,
    })
    planned = []

    def plan(_by_chain, _token, amount, destination, speed):
        planned.append((amount, destination, speed))
        row = {"ok": True, "label": "main", "masked": "0x1234...abcd",
               "raw": 150_000_000_000_000_000, "decimals": 18, "symbol": "ETH"}
        return [{"chain": "ethereum", "token": {"symbol": "ETH"},
                 "rows": [row], "speed": speed}], []

    monkeypatch.setattr(bot, "_plan_groups_sync", plan)
    monkeypatch.setattr(bot, "PENDING", {})
    destinations = ([wallets.address_of(KEY2), wallets.address_of(KEY3)]
                    if destination_mode == "multi" else ["all"])
    destination_pick = ("multi:" + ",".join(destinations)
                        if destination_mode == "multi" else "all")
    query = SimpleNamespace(
        data="snd:gas:fast",
        from_user=SimpleNamespace(id=bot.ADMIN_ID, username=None),
        message=SimpleNamespace(chat_id=bot.ADMIN_ID, message_id=1,
                                chat=SimpleNamespace(type=bot.ChatType.PRIVATE)),
        answer=AsyncMock(),
    )
    update = SimpleNamespace(callback_query=query)
    ctx = SimpleNamespace(
        user_data={"send_wiz": {
            "src": "main", "chain": "ethereum", "token": "native",
            "amount": "0.3",
            "dst": destination_pick,
        }},
        bot=SimpleNamespace(),
    )

    asyncio.run(bot.on_callback(update, ctx))

    assert len(planned) == 2
    assert {amount for amount, _, _ in planned} == {"0.15"}
    assert {destination.lower() for _, destination, _ in planned} == {
        wallets.address_of(KEY2).lower(), wallets.address_of(KEY3).lower()}
    assert all(speed == "fast" for _, _, speed in planned)
    pending = next(iter(bot.PENDING.values()))
    assert len(pending["groups"]) == 2
    assert all(group["speed"] == "fast" for group in pending["groups"])


def test_solana_send_wizard_only_executes_after_confirm(monkeypatch, tmp_path):
    from solders.keypair import Keypair
    from solders.pubkey import Pubkey

    monkeypatch.setattr(wallets, "STORE", tmp_path / "sol-send-wallets.json")
    monkeypatch.setattr(wallets, "MASTER", tmp_path / "sol-send-master.key")
    uid = bot.ADMIN_ID
    signer = Keypair.from_seed(bytes(range(32)))
    sender = wallets.add_wallet(uid, str(signer), chain="solana")[0]
    destination = str(Pubkey.new_unique())
    monkeypatch.setattr(bot, "allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_edit_or_send", AsyncMock())
    monkeypatch.setattr(bot, "PENDING", {})
    execute_calls = []

    def plan(_sender, _destination, _token, amount):
        assert amount == "0.1"
        return {
            "chain": "solana", "kind": "native", "symbol": "SOL",
            "decimals": 9, "amount": 100_000_000, "fee_lamports": 5_000,
            "rent_lamports": 0, "from": sender, "to": destination,
            "amount_text": amount,
        }

    def execute(_plan, _secret, *, confirmed=False):
        execute_calls.append(confirmed)
        return "mock-signature"

    monkeypatch.setattr(solana, "plan_transfer", plan)
    monkeypatch.setattr(solana, "execute_transfer", execute)
    query = SimpleNamespace(
        data="snd:gas:fast",
        from_user=SimpleNamespace(id=uid, username=None),
        message=SimpleNamespace(chat_id=uid, message_id=1,
                                chat=SimpleNamespace(type=bot.ChatType.PRIVATE)),
        answer=AsyncMock(),
    )
    update = SimpleNamespace(callback_query=query)
    ctx = SimpleNamespace(
        user_data={"send_wiz": {
            "src": sender, "chain": "solana", "token": "native",
            "amount": "0.1", "dst": destination,
        }},
        bot=SimpleNamespace(),
    )

    async def run():
        await bot.on_callback(update, ctx)
        assert execute_calls == []
        token = next(iter(bot.PENDING))
        pending = bot.PENDING[token]
        assert pending["kind"] == "sol_tx"
        assert pending["plans"][0]["amount"] == 100_000_000
        monkeypatch.setattr(wallets, "signing_entries", lambda *_args, **_kwargs: [
            (uid, "Wallet 1", sender, str(signer), 1),
        ])
        await bot.run_pending(uid, ctx, uid, token)

    asyncio.run(run())
    assert execute_calls == [True]


def test_direct_send_uses_selected_evm_wallet_chain(monkeypatch, tmp_path):
    monkeypatch.setattr(wallets, "STORE", tmp_path / "direct-evm-wallets.json")
    monkeypatch.setattr(wallets, "MASTER", tmp_path / "direct-evm-master.key")
    chain = Chain(name="robinhood", chain_id=4663, rpc=["x"],
                  broadcast_rpc=["x"], priority_fee_gwei=2.0)
    monkeypatch.setattr(bot, "CFG", SimpleNamespace(chains={
        "robinhood": chain,
    }))
    monkeypatch.setattr(bot, "PENDING", {})
    captured = []

    def plan(by_chain, token, amount, destination):
        captured.append((by_chain, token, amount, destination))
        return [{"chain": "robinhood", "token": {"symbol": "ETH"},
                 "rows": [{"ok": True, "label": "Wallet 1", "masked": ADDR,
                           "raw": 10**17, "decimals": 18, "symbol": "ETH"}],
                 "speed": "fast"}], []

    monkeypatch.setattr(bot, "_plan_groups_sync", plan)
    wallets.add_wallet(bot.ADMIN_ID, KEY, chain="robinhood")
    destination = "0x" + "44" * 20
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=bot.ADMIN_ID, username=None),
        effective_chat=SimpleNamespace(type=bot.ChatType.PRIVATE),
        message=SimpleNamespace(text=f"/send main 0.1 native {destination}",
                                reply_text=AsyncMock()),
    )
    ctx = SimpleNamespace()

    asyncio.run(bot.cmd_send(update, ctx))

    assert captured[0][0] == {
        "robinhood": [("Wallet 1", ADDR)],
    }
    assert captured[0][2] == "0.1"
    assert next(iter(bot.PENDING.values()))["kind"] == "tx"


def test_direct_solana_send_creates_confirmation_pending(monkeypatch, tmp_path):
    from solders.keypair import Keypair
    from solders.pubkey import Pubkey

    monkeypatch.setattr(wallets, "STORE", tmp_path / "direct-sol-wallets.json")
    monkeypatch.setattr(wallets, "MASTER", tmp_path / "direct-sol-master.key")
    monkeypatch.setattr(bot, "PENDING", {})
    signer = Keypair.from_seed(bytes(range(32)))
    sender = wallets.add_wallet(bot.ADMIN_ID, str(signer), chain="solana")[0]
    destination = str(Pubkey.new_unique())

    def plan(_sender, _destination, _token, _amount):
        return {
            "chain": "solana", "kind": "native", "symbol": "SOL",
            "decimals": 9, "amount": 100_000_000, "fee_lamports": 5_000,
            "rent_lamports": 0, "from": sender, "to": destination,
            "amount_text": "0.1",
        }

    monkeypatch.setattr(solana, "plan_transfer", plan)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=bot.ADMIN_ID, username=None),
        effective_chat=SimpleNamespace(type=bot.ChatType.PRIVATE),
        message=SimpleNamespace(text=f"/send main 0.1 native {destination}",
                                reply_text=AsyncMock()),
    )

    asyncio.run(bot.cmd_send(update, SimpleNamespace()))

    pending = next(iter(bot.PENDING.values()))
    assert pending["kind"] == "sol_tx"
    assert pending["plans"][0]["from"] == sender
    update.message.reply_text.assert_awaited_once()


def test_solana_sweep_token_uses_shared_token_program_aware_transfer(monkeypatch):
    uid = bot.ADMIN_ID
    pending = {
        "kind": "sol_sweep", "owner": uid,
        "plan": {
            "rows": [{"ok": True, "address": ADDR, "label": "Wallet 1",
                      "raw": 1_250_000}],
            "destination": ADDR,
        },
        "token": {"kind": "spl", "mint": "mint", "decimals": 6},
    }
    monkeypatch.setattr(bot, "PENDING", {"test-token": pending})
    monkeypatch.setattr(wallets, "signing_entries", lambda *_args, **_kwargs: [
        (uid, "Wallet 1", ADDR, KEY, 1),
    ])
    monkeypatch.setattr(bot, "_edit_or_send", AsyncMock())
    planned, executed = [], []

    def plan(sender, destination, mint, amount):
        planned.append((sender, destination, mint, amount))
        return {"plan": "token-2022-compatible"}

    def execute(plan_value, secret, *, confirmed=False):
        executed.append((plan_value, secret, confirmed))
        return "mock-sweep-signature"

    monkeypatch.setattr(solana, "plan_transfer", plan)
    monkeypatch.setattr(solana, "execute_transfer", execute)
    monkeypatch.setattr(
        walletops, "execute_solana_sweep",
        lambda _uid, plan_value, asset, destination, *, send_native, send_token,
        confirmed: [
            {"label": "Wallet 1", "ok": True,
             "tx": send_token(ADDR, "source-account", destination,
                              asset["mint"], 1_250_000)}
        ],
    )
    ctx = SimpleNamespace(bot=SimpleNamespace())

    asyncio.run(bot.run_pending(uid, ctx, uid, "test-token"))

    assert planned == [(ADDR, ADDR, "mint", "1.25")]
    assert executed == [({"plan": "token-2022-compatible"}, KEY, True)]


def test_send_group_planner_applies_selected_gas_tier(monkeypatch):
    monkeypatch.setattr(bot, "CFG", SimpleNamespace(chains={"ethereum": CHAIN}))
    monkeypatch.setattr(bot, "RPC", lambda _urls: object())
    monkeypatch.setattr(bot.tkn, "resolve", lambda *_args: {"kind": "native", "symbol": "ETH"})
    calls = []

    def plan(_rpc, _chain, _token, _senders, _destination, _amount, speed):
        calls.append(speed)
        return []

    monkeypatch.setattr(bot.fleet, "plan_transfers", plan)
    groups, notes = bot._plan_groups_sync(
        {"ethereum": [("Wallet 1", ADDR)]}, "native", "0.1",
        "0x" + "22" * 20, "slow")
    assert not notes
    assert calls == ["slow"]
    assert groups[0]["speed"] == "slow"


def _solana_keys():
    from solders.keypair import Keypair
    from solders.pubkey import Pubkey
    sender = Keypair.from_seed(bytes(range(32)))
    destination = Pubkey.new_unique()
    return sender, str(destination)


def test_solana_native_transfer_plan_is_read_only_and_reserves_fee():
    sender, destination = _solana_keys()
    calls = []

    def call(method, params):
        calls.append(method)
        assert method == "getBalance"
        return {"value": 700_000_000}

    plan = solana.plan_transfer(str(sender.pubkey()), destination, "native",
                                "0.5", call=call)
    assert plan["amount"] == 500_000_000
    assert plan["fee_lamports"] == 5_000
    assert calls == ["getBalance"]
    assert "sendTransaction" not in calls


def test_solana_native_transfer_all_keeps_fee_reserve_and_rejects_insufficient():
    sender, destination = _solana_keys()

    def call(_method, _params):
        return {"value": 9_000}

    plan = solana.plan_transfer(str(sender.pubkey()), destination, "sol", "all",
                                call=call)
    assert plan["amount"] == 4_000
    with pytest.raises(ValueError, match="insufficient SOL"):
        solana.plan_transfer(str(sender.pubkey()), destination, "sol", "0.000005",
                             call=call)


def _spl_rpc_fixture(sender, destination, *, existing_ata=False, sol_balance=1_000_000_000,
                     token_program_id=solana.TOKEN_PROGRAM_ID, mint_extensions=None):
    from solders.pubkey import Pubkey
    mint = str(Pubkey.new_unique())
    source_token_account = str(Pubkey.new_unique())
    destination_ata = str(solana._associated_token_address(
        destination, mint, token_program_id))
    calls = []

    def call(method, params, **_kwargs):
        calls.append((method, params))
        if method == "getAccountInfo" and params[0] == mint:
            return {"value": {
                "owner": token_program_id,
                "data": {"parsed": {"info": {"extensions": mint_extensions or []}}},
            }}
        if method == "getAccountInfo" and params[0] == destination_ata:
            if not existing_ata:
                return {"value": None}
            return {"value": {
                "owner": token_program_id,
                "data": {"parsed": {"info": {"mint": mint, "owner": destination}}},
            }}
        if method == "getTokenSupply":
            return {"value": {"decimals": 6}}
        if method == "getTokenAccountsByOwner":
            return {"value": [{
                "pubkey": source_token_account,
                "account": {
                    "owner": token_program_id,
                    "data": {"parsed": {"info": {
                        "mint": mint,
                        "tokenAmount": {"amount": "2500000", "decimals": 6},
                    }}},
                },
            }]}
        if method == "getMinimumBalanceForRentExemption":
            return 2_039_280
        if method == "getBalance":
            return {"value": sol_balance}
        raise AssertionError(f"unexpected RPC call: {method}")

    return mint, source_token_account, destination_ata, calls, call


def test_solana_spl_preview_accounts_for_ata_rent():
    sender, destination = _solana_keys()
    mint, source_token_account, destination_ata, calls, call = _spl_rpc_fixture(
        str(sender.pubkey()), destination)
    plan = solana.plan_transfer(str(sender.pubkey()), destination, mint, "1.25",
                                call=call)
    assert plan["amount"] == 1_250_000
    assert plan["token_accounts"] == [{
        "address": source_token_account, "amount": 1_250_000,
    }]
    assert plan["destination_ata"] == destination_ata
    assert plan["create_ata"]
    assert plan["rent_lamports"] == 2_039_280
    assert not any(method == "sendTransaction" for method, _ in calls)


def test_solana_spl_preview_existing_ata_needs_no_rent():
    sender, destination = _solana_keys()
    _mint, _source, _ata, calls, call = _spl_rpc_fixture(
        str(sender.pubkey()), destination, existing_ata=True)
    plan = solana.plan_transfer(str(sender.pubkey()), destination, _mint, "1",
                                call=call)
    assert not plan["create_ata"]
    assert plan["rent_lamports"] == 0
    assert not any(method == "getMinimumBalanceForRentExemption"
                   for method, _ in calls)


def test_solana_token_2022_balance_is_read():
    sender, _destination = _solana_keys()

    def call(method, params, **_kwargs):
        assert method == "getTokenAccountsByOwner"
        assert params[0] == str(sender.pubkey())
        return {"value": [{
            "pubkey": "associated-token-account",
            "account": {
                "owner": solana.TOKEN_2022_PROGRAM_ID,
                "data": {"parsed": {"info": {
                    "tokenAmount": {"amount": "2500000", "decimals": 6},
                }}},
            },
        }]}

    balance = solana.get_token_balance(
        str(sender.pubkey()), "Token2022Mint111111111111111111111111111",
        call=call)
    assert balance["raw"] == 2_500_000
    assert balance["decimals"] == 6
    assert balance["token_program_id"] == solana.TOKEN_2022_PROGRAM_ID


def test_solana_token_2022_send_uses_its_program_and_ata_rent():
    from solders.hash import Hash
    sender, destination = _solana_keys()
    mint, source_account, destination_ata, calls, fixture_call = _spl_rpc_fixture(
        str(sender.pubkey()), destination,
        token_program_id=solana.TOKEN_2022_PROGRAM_ID,
        mint_extensions=[{"extension": "metadataPointer", "state": {}}],
    )
    broadcast_calls = []

    def call(method, params, **kwargs):
        if method == "getLatestBlockhash":
            return {"value": {"blockhash": str(Hash.from_bytes(bytes(32)))}}
        if method == "sendTransaction":
            broadcast_calls.append(params)
            return "token-2022-signature"
        return fixture_call(method, params, **kwargs)

    plan = solana.plan_transfer(
        str(sender.pubkey()), destination, mint, "1.25", call=call)
    assert plan["token_program_id"] == solana.TOKEN_2022_PROGRAM_ID
    assert plan["destination_ata"] == destination_ata
    assert plan["token_accounts"] == [{"address": source_account, "amount": 1_250_000}]
    rent_call = next(params for method, params in calls
                     if method == "getMinimumBalanceForRentExemption")
    assert rent_call == [170]

    signature = solana.execute_transfer(
        plan, str(sender), confirmed=True, call=call)
    assert signature == "token-2022-signature"
    assert len(broadcast_calls) == 1
    from solders.transaction import Transaction
    import base64
    tx = Transaction.from_bytes(base64.b64decode(broadcast_calls[0][0]))
    assert str(tx.message.account_keys[tx.message.instructions[-1].program_id_index]) == \
        solana.TOKEN_2022_PROGRAM_ID


@pytest.mark.parametrize("extension", ["transferFeeConfig", "transferHook",
                                       "nonTransferable", "confidentialTransferMint"])
def test_solana_token_2022_rejects_extensions_without_supported_transfer_semantics(extension):
    sender, destination = _solana_keys()
    mint, _source, _ata, _calls, call = _spl_rpc_fixture(
        str(sender.pubkey()), destination,
        token_program_id=solana.TOKEN_2022_PROGRAM_ID,
        mint_extensions=[{"extension": extension, "state": {}}],
    )
    with pytest.raises(ValueError, match="needs special transfer handling"):
        solana.plan_transfer(str(sender.pubkey()), destination, mint, "1",
                             call=call)


def test_solana_spl_preview_requires_sol_for_fee_and_ata_rent():
    sender, destination = _solana_keys()
    mint, _source, _ata, _calls, call = _spl_rpc_fixture(
        str(sender.pubkey()), destination, sol_balance=2_000_000)
    with pytest.raises(ValueError, match="fee and recipient token-account rent"):
        solana.plan_transfer(str(sender.pubkey()), destination, mint, "1",
                             call=call)


def test_solana_spl_execute_builds_and_broadcasts_confirmed_transfer():
    from solders.hash import Hash
    sender, destination = _solana_keys()
    mint, _source, _ata, _calls, fixture_call = _spl_rpc_fixture(
        str(sender.pubkey()), destination)
    broadcast_calls = []

    def call(method, params, **kwargs):
        if method == "getLatestBlockhash":
            return {"value": {"blockhash": str(Hash.from_bytes(bytes(32)))}}
        if method == "sendTransaction":
            broadcast_calls.append((method, params))
            return "spl-mock-signature"
        return fixture_call(method, params, **kwargs)

    plan = solana.plan_transfer(str(sender.pubkey()), destination, mint, "1.25",
                                call=call)
    signature = solana.execute_transfer(
        plan, str(sender), confirmed=True, call=call)
    assert signature == "spl-mock-signature"
    assert len(broadcast_calls) == 1
    assert broadcast_calls[0][1][1]["skipPreflight"] is False


def test_solana_execute_requires_confirmation_and_matching_key():
    sender, destination = _solana_keys()
    from solders.keypair import Keypair
    other = Keypair.from_seed(bytes(reversed(range(32))))
    calls = []
    with pytest.raises(PermissionError, match="final confirmation"):
        solana.execute_transfer(
            {"chain": "solana"}, str(sender), call=lambda *args: calls.append(args))
    assert not calls
    with pytest.raises(ValueError, match="wallet/key mismatch"):
        solana.execute_transfer(
            {"chain": "solana", "from": str(sender.pubkey())},
            str(other),
            confirmed=True, call=lambda *args: calls.append(args))
    assert not calls


def test_solana_stale_plan_does_not_broadcast():
    sender, destination = _solana_keys()
    initial = {"value": 700_000_000}
    calls = []

    def call(method, _params):
        calls.append(method)
        if method == "getBalance":
            return initial
        raise AssertionError(f"unexpected RPC call: {method}")

    plan = solana.plan_transfer(str(sender.pubkey()), destination, "native",
                                "0.5", call=call)
    initial["value"] = 400_000_000
    with pytest.raises(ValueError, match="balance changed since preview"):
        solana.execute_transfer(plan, str(sender), confirmed=True, call=call)
    assert "sendTransaction" not in calls


def test_solana_execute_broadcasts_only_after_confirmed_plan(monkeypatch):
    sender, destination = _solana_keys()
    sent = []

    def call(method, _params):
        if method == "getBalance":
            return {"value": 700_000_000}
        if method == "getLatestBlockhash":
            from solders.hash import Hash
            return {"value": {"blockhash": str(Hash.from_bytes(bytes(32)))}}
        if method == "sendTransaction":
            sent.append(method)
            return "5igature"
        raise AssertionError(f"unexpected RPC call: {method}")

    plan = solana.plan_transfer(str(sender.pubkey()), destination, "native",
                                "0.5", call=call)
    signature = solana.execute_transfer(
        plan, str(sender), confirmed=True, call=call)
    assert signature == "5igature"
    assert sent == ["sendTransaction"]


if __name__ == "__main__":
    unittest.main()
