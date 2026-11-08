import json

import pytest

from acobot import ai, brain

UID = 111
ADMIN = True
ADDR = "0x" + "ab" * 20


def _tool_msg(name, args, call_id="c1"):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


def _say(text):
    return {"role": "assistant", "content": text}


def _script(*msgs):
    """Mock LLM: returns the scripted assistant messages in order, records what it was sent."""
    seen = []
    queue = list(msgs)

    def complete(messages, tools):
        seen.append({"messages": [dict(m) for m in messages], "tools": tools})
        return queue.pop(0)

    complete.seen = seen
    return complete


@pytest.fixture(autouse=True)
def _clean_history():
    ai._HISTORY.clear()
    yield
    ai._HISTORY.clear()


def _providers(calls):
    def mints():
        calls.append("list_mints")
        return [{"id": 3, "contract": ADDR, "chain": "robinhood", "status": "scheduled",
                 "opens": "2026-10-27 20:00 UTC", "price": "0 ETH"}]

    def balances(chain=None):
        calls.append(("balances", chain))
        return {"text": "Wallet 2 0x..ab  ETH 0.123456"}

    def results():
        calls.append("list_results")
        return [{"id": 5, "summary": "failed: out of gas"}]

    def get_result(mid):
        calls.append(("get_result", mid))
        return {"id": mid, "status": "failed", "hint": "pay more gas"}

    return {"list_mints": mints, "balances": balances, "list_results": results, "get_result": get_result,
            "list_wallets": lambda: []}


def test_question_mode_reads_mock_balance_and_answers_plain():
    calls = []
    complete = _script(
        _tool_msg("balances", {"chain": "robinhood"}),
        _say("**wallet 2** has 0.123456 eth on robinhood"),
    )
    out = brain.converse(UID, "how much eth in wallet 2", ADMIN, _providers(calls), "snap", complete)
    assert calls == [("balances", "robinhood")]
    assert out == {"action": "say", "args": {"reply": "wallet 2 has 0.123456 eth on robinhood"}}
    # the tool result (mock balance) was fed back into the model
    tool_msgs = [m for m in complete.seen[1]["messages"] if m["role"] == "tool"]
    assert tool_msgs and "0.123456" in tool_msgs[0]["content"]


def test_question_mode_why_failed_reads_result():
    calls = []
    complete = _script(
        _tool_msg("get_result", {"id": 5}),
        _say("mint 5 failed, it ran out of gas. pay more gas next time."),
    )
    out = brain.converse(UID, "why did mint 5 fail", ADMIN, _providers(calls), "snap", complete)
    assert ("get_result", 5) in calls
    assert "gas" in out["args"]["reply"]


def test_question_mode_read_failure_is_reported_not_invented():
    def boom():
        raise RuntimeError("rpc down /secret/path")

    prov = _providers([])
    prov["balances"] = boom
    complete = _script(_tool_msg("balances", {}), _say("couldnt read balances right now"))
    out = brain.converse(UID, "balances?", ADMIN, prov, "snap", complete)
    tool_msg = [m for m in complete.seen[1]["messages"] if m["role"] == "tool"][0]
    assert "read failed" in tool_msg["content"]
    assert "secret" not in tool_msg["content"]  # exception text never leaks to the model
    assert out["args"]["reply"] == "couldnt read balances right now"


def test_action_mode_send_routes_to_validate_for_confirm():
    # write tools never execute inside the brain: they come back as a validated action
    # so bot.py shows the existing confirm panel.
    complete = _script(_tool_msg("send", {"who": "2", "amount": "0.1", "token": "native", "to": "main"}))
    out = brain.converse(UID, "send 0.1 eth from wallet 2 to main", ADMIN, _providers([]), "snap", complete)
    assert out == {"action": "send", "args": {"who": "2", "amount": "0.1", "token": "native", "to": "main"}}


def test_action_mode_addmint_routes_and_validates_address():
    complete = _script(_tool_msg("addmint", {"contract": ADDR, "chain": "robinhood", "speed": "fast"}))
    out = brain.converse(UID, "mint that on robinhood fast", ADMIN, _providers([]), "snap", complete)
    assert out["action"] == "addmint"
    assert out["args"]["contract"] == ADDR and out["args"]["speed"] == "fast"


def test_bad_write_args_come_back_to_model_not_executed():
    complete = _script(
        _tool_msg("send", {"who": "2", "amount": "0.1", "token": "native", "to": "notanaddress!!"}),
        _say("that destination looks wrong. send me the full 0x address"),
    )
    out = brain.converse(UID, "send to notanaddress", ADMIN, _providers([]), "snap", complete)
    assert out == {"action": "say", "args": {"reply": "that destination looks wrong. send me the full 0x address"}}
    tool_msg = [m for m in complete.seen[1]["messages"] if m["role"] == "tool"][0]
    assert "invalid send args" in tool_msg["content"]


def test_crew_gets_no_write_tools_and_cannot_route_writes():
    tool_names = [t["function"]["name"] for t in brain.tools_for(False)]
    assert "send" not in tool_names and "allow" not in tool_names and "addmint" not in tool_names
    assert "list_mints" in tool_names and "balances" in tool_names
    # even if the model invents a write call, it is refused
    complete = _script(_tool_msg("send", {"who": "2", "amount": "1", "token": "native", "to": "main"}),
                       _say("cant send, admin only"))
    out = brain.converse(UID, "send 1 eth", False, _providers([]), "snap", complete)
    # a write call from a crew member never executes and is refused with a plain message
    assert out["action"] == "say"
    assert "admin only" in out["args"]["reply"]


def test_analyze_mint_tool_is_read_only_and_returns_source_aware_phases(monkeypatch):
    from types import SimpleNamespace
    from acobot import bot, mintapi

    detected = {
        "contract": ADDR,
        "name": "Example",
        "standard": "erc721",
        "source": "OpenSea SeaDrop",
        "onchain_read_ts": 100,
        "schedule": [{
            "label": "GTD",
            "chain_key": "signed",
            "index": 2,
            "gated": True,
            "source": "chain+opensea",
            "display_start_ts": 200,
            "display_price_wei": 5,
            "snipeable": False,
        }],
        "schedule_unavailable": False,
    }
    monkeypatch.setattr(bot, "CFG", SimpleNamespace(chains={"robinhood": SimpleNamespace(rpc="unused")}))
    monkeypatch.setattr(bot, "analyze_contract_sync", lambda contract, chain: (detected, None))
    monkeypatch.setattr(mintapi, "supports_phase", lambda contract: True)

    result = bot._brain_providers(UID)["analyze_mint"](contract=ADDR, chain="robinhood")

    assert result["phases"][0]["label"] == "GTD"
    assert result["phases"][0]["stage_index"] == 2
    assert result["phases"][0]["price_wei"] == 5
    assert "simulation still required" in result["phases"][0]["route"]
    assert result["phase_data_limits"]
    assert "analyze_mint" in [tool["function"]["name"] for tool in brain.tools_for(False)]
    assert "analyze_mint" not in brain.WRITE_TOOLS


def test_gated_phase_report_uses_indexed_mint_api_on_ethereum():
    report = brain.mint_phase_report({
        "contract": ADDR,
        "schedule": [{"label": "GTD", "chain_key": "allowlist", "gated": True,
                      "list_mode": "gtd", "source": "chain", "snipeable": False}],
    }, "ethereum")
    assert "Reservoir and Magic Eden" in report["phases"][0]["route"]
    assert report["phases"][0]["list_mode"] == "gtd"


def test_gated_phase_report_says_blocked_without_phase_api_off_ethereum():
    report = brain.mint_phase_report({
        "contract": ADDR,
        "schedule": [{"label": "Allowlist", "chain_key": "allowlist", "gated": True,
                      "source": "chain", "snipeable": False}],
    }, "robinhood")
    assert "blocked" in report["phases"][0]["route"]


def test_phase_report_handles_onchain_only_public_stage_and_formats_times():
    report = brain.mint_phase_report({
        "contract": ADDR,
        "seadrop": True,
        "mint_to": ADDR,
        "fee_recipient": ADDR,
        "stages": [
            {"key": "public", "gated": False, "price_wei": 0, "start_ts": 100},
            {"key": "public", "gated": False, "price_wei": 0, "start_ts": 1793217600},
        ],
    }, "robinhood")
    phase = report["phases"][0]
    assert "supported local route" in phase["route"]
    assert phase["start_utc"] == "1970-01-01 00:01:40 UTC"
    assert phase["start_kathmandu"] == "1970-01-01 05:31:40 UTC+05:30"
    assert report["phases"][1]["start_kathmandu"] == "2026-10-29 01:45:00 UTC+05:45"
    assert report["schedule_status"] == "available"


def test_context_is_last_10_plus_snapshot():
    for i in range(12):
        ai.remember(UID, "user" if i % 2 == 0 else "assistant", f"m{i}")
    complete = _script(_say("ok"))
    brain.converse(UID, "now", ADMIN, _providers([]), "LIVE SNAPSHOT TEXT", complete)
    msgs = complete.seen[0]["messages"]
    assert msgs[0]["role"] == "system" and "LIVE SNAPSHOT TEXT" in msgs[0]["content"]
    history = [m["content"] for m in msgs[1:-1]]
    assert len(history) == 10
    assert history[0] == "m2" and history[-1] == "m11"
    assert msgs[-1] == {"role": "user", "content": "now"}


def test_markdown_is_stripped_from_reply():
    complete = _script(_say("## mints\n- **#3** robinhood, opens 20:00 UTC\n`0xabc`"))
    out = brain.converse(UID, "mints?", ADMIN, _providers([]), "snap", complete)
    reply = out["args"]["reply"]
    assert "#" not in reply.split("\n")[0] and "**" not in reply and "`" not in reply
    assert "- " not in reply


def test_loop_limit_returns_plain_message():
    looping = [_tool_msg("list_mints", {}, call_id=str(i)) for i in range(brain.MAX_ROUNDS + 2)]
    complete = _script(*looping)
    out = brain.converse(UID, "loop", ADMIN, _providers([]), "snap", complete)
    assert out["action"] == "say" and "too many steps" in out["args"]["reply"]


def test_llm_down_raises_ai_error_for_caller_to_show_plain_message():
    def down(messages, tools):
        raise ai.AIError("llm http 500")

    with pytest.raises(ai.AIError):
        brain.converse(UID, "hi", ADMIN, _providers([]), "snap", down)


def test_snapshot_has_no_keys_and_masks_addresses():
    snap = brain.snapshot_text(
        [{"id": 3, "contract": ADDR, "chain": "robinhood", "status": "scheduled", "opens": "2026-10-27 20:00 UTC"}],
        [{"label": "Wallet 1", "masked": "0xab..ab", "chain": "ethereum", "main": True}],
        [])
    assert "scheduled mints" in snap and "Wallet 1 0xab..ab" in snap
    assert "private" not in snap.lower() and "api" not in snap.lower()
