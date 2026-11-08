import json
from acobot import brain, dig, ai

ADDR = "0x" + "d6" * 20
SD = "0xd6889961584a42dc5d8196bd82a4c6d0f2b684c5"


def _fake_search(monkeypatch, hits, pages=None):
    monkeypatch.setattr(dig, "_search_hits", lambda name: hits)
    monkeypatch.setattr(dig, "_page_text", lambda url: (pages or {}).get(url, ""))


def test_dig_verifies_contract_from_sources(monkeypatch):
    hits = [("https://nftcalendar.io/event/digital-human", "DIGITAL HUMAN robinhood mint"),
            ("https://x.com/IThinkItsArt", "official"),
            ("https://opensea.io/collection/digital-human-x", "")]
    _fake_search(monkeypatch, hits, {"https://nftcalendar.io/event/digital-human": f"mint at {SD} on robinhood"})
    seen = []

    def detect(contract, rpc):
        seen.append(contract)
        return {"price_wei": 0, "start_ts": 1793217600, "per_wallet": 1, "phases": ["public"],
                "seadrop": True, "name": "Digital Human"}
    res = dig.dig("DIGITAL HUMAN", ["ethereum", "robinhood"], detect, lambda cn: cn)
    assert res["verified"]["contract"] == SD
    assert res["verified"]["chain"] == "robinhood"  # chain hint from source text
    assert res["x_handles"][0] == "IThinkItsArt"
    assert res["opensea_slugs"] == ["digital-human-x"]
    assert seen[0] == SD
    txt = dig.format_for_user(res)
    assert "verified on-chain" in txt and "IThinkItsArt" in txt


def test_dig_never_guesses_when_no_address(monkeypatch):
    _fake_search(monkeypatch, [("https://x.com/SoulsClub", "souls club nft"),
                               ("https://opensea.io/collection/souls-club", "")])
    res = dig.dig("SoulsClub", ["robinhood"], lambda c, r: (_ for _ in ()).throw(AssertionError("no call")), lambda cn: cn)
    assert res["verified"] is None
    assert res["x_handles"] == ["SoulsClub"]
    assert res["opensea_slugs"] == ["souls-club"]
    txt = dig.format_for_user(res)
    assert "no contract address found" in txt


def test_dig_skips_address_without_code(monkeypatch):
    _fake_search(monkeypatch, [("https://nftcalendar.io/x", f"{ADDR}")])

    def detect(c, r):
        raise RuntimeError("no contract")
    res = dig.dig("X", ["robinhood"], detect, lambda cn: cn)
    assert res["verified"] is None


def test_brain_dig_tool_runs_and_returns_proposal(monkeypatch):
    ai._HISTORY.clear()
    proposal = {"contract": SD, "chain": "robinhood", "price_wei": 0, "start_ts": 1793217600,
                "per_wallet": 1, "phases": ["public"], "seadrop": True, "name": "Digital Human"}
    calls = []

    def dig_fn(name):
        calls.append(name)
        return {"verified": proposal, "name": name}
    prov = {"dig_project": dig_fn}
    seq = [{"role": "assistant", "content": "", "tool_calls": [
        {"id": "1", "type": "function", "function": {"name": "dig_project", "arguments": json.dumps({"name": "DIGITAL HUMAN"})}}]},
        {"role": "assistant", "content": "found it, want it on the schedule?"}]
    out = brain.converse(1, "add DIGITAL HUMAN", True, prov, "snap", lambda m, t: seq.pop(0))
    assert calls == ["DIGITAL HUMAN"]
    assert out["action"] == "say" and out["args"]["proposal"]["contract"] == SD


def test_brain_dig_crew_sees_tool_but_no_write(monkeypatch):
    names = [t["function"]["name"] for t in brain.tools_for(False)]
    assert "dig_project" in names
    assert "addmint" not in names
    assert "addmint" in [t["function"]["name"] for t in brain.tools_for(True)]


def test_brain_no_proposal_when_not_verified():
    ai._HISTORY.clear()
    seq = [{"role": "assistant", "content": "", "tool_calls": [
        {"id": "1", "type": "function", "function": {"name": "dig_project", "arguments": json.dumps({"name": "x"})}}]},
        {"role": "assistant", "content": "found the x account, no contract yet."}]
    out = brain.converse(1, "add x", True, {"dig_project": lambda n: {"x_handles": ["x"], "verified": None}},
                         "snap", lambda m, t: seq.pop(0))
    assert "proposal" not in out["args"]
