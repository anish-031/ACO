import json
from acobot import intel, dig, results as resmod


def test_speed_stats_from_results(tmp_path):
    resmod.save_result(tmp_path, 1, {"id": 1, "chain": "robinhood", "rows": [
        {"address": "0xa", "bot_ms": 30.0, "sent_s": 0.03, "receipt_s": 1.1},
        {"address": "0xb", "bot_ms": 34.0, "sent_s": 0.034, "receipt_s": 1.3}]})
    st = intel.speed_stats(tmp_path)
    assert st["averages"]["bot_ms"] == 32.0
    assert st["averages"]["fire_to_receipt_s"] == 1.2
    assert st["mints"][0]["wallets"] == 2
    assert "bot time avg 32.0 ms" in intel.format_speed(st)


def test_speed_stats_empty_is_honest(tmp_path):
    assert "no fire results" in intel.format_speed(intel.speed_stats(tmp_path))


def test_public_mints_mocked_and_failing_source():
    html = '<a href="https://nftcalendar.io/events/digital-humans">Digital Humans mint</a>'
    def fetch(u):
        if "robin" in u:
            raise OSError("down")
        return html
    res = intel.public_mints(fetch)
    assert res["mints"][0]["name"] == "Digital Humans mint"
    assert res["mints"][0]["price"] is None
    assert res["errors"] == ["robin-xp: OSError"]
    assert "sources failing" in intel.format_mints(res)


def test_public_mints_none_found():
    assert "no public mints" in intel.format_mints(intel.public_mints(lambda u: ""))


def test_dig_queries_cover_sources(monkeypatch):
    seen = []
    monkeypatch.setattr(dig, "_ddg", lambda q: (seen.append(q), [])[1])
    hits = dig._search_hits("digital humans")
    joined = " ".join(seen)
    assert "nftcalendar" in joined and "robin-xp" in joined and "nitter" in joined
    urls = [u for u, _ in hits]
    assert "https://opensea.io/collection/digital-humans" in urls
    assert "https://nftcalendar.io/events/" in urls
