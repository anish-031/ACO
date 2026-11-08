from acobot import gasguard


def _history(values):
    return {"baseFeePerGas": [hex(value) for value in values]}


def test_analyze_uses_median_baseline_and_current_fee():
    result = gasguard.analyze(_history([10, 12, 11, 13, 14, 30]))
    assert result["state"] == "ok"
    assert result["current_gwei"] == 30 / 1e9
    assert result["baseline_gwei"] == 12 / 1e9
    assert result["ratio"] == 30 / 12
    assert result["trend_up"] is True


def test_analyze_reports_unknown_with_too_little_history():
    result = gasguard.analyze(_history([1, 2, 3, 4]))
    assert result == {"state": "unknown", "current_gwei": None,
                      "baseline_gwei": None, "ratio": None}


def test_verdict_warns_for_material_spike():
    result = gasguard.verdict("ethereum", {
        "state": "ok", "current_gwei": 100, "baseline_gwei": 20,
        "ratio": 5, "trend_up": True,
    }, native_usd=3000)
    assert result["level"] == "warn"
    assert "5.0x" in result["line"]
    assert "rising" in result["line"]


def test_verdict_does_not_warn_for_cheap_spike_or_missing_price():
    cheap = gasguard.verdict("robinhood", {
        "state": "ok", "current_gwei": 1, "baseline_gwei": 0.1,
        "ratio": 10, "trend_up": False,
    }, native_usd=1)
    no_price = gasguard.verdict("ethereum", {
        "state": "ok", "current_gwei": 100, "baseline_gwei": 1,
        "ratio": 100, "trend_up": True,
    }, native_usd=None)
    assert cheap["level"] == "ok"
    assert "cheap" in cheap["line"]
    assert no_price["level"] == "ok"
    assert "usd n/a" in no_price["line"]


def test_check_turns_rpc_failure_into_unknown():
    class BrokenRPC:
        def call(self, *_args):
            raise OSError("private endpoint detail")

    result = gasguard.check(BrokenRPC(), "ethereum", native_usd=3000)
    assert result == {"level": "unknown", "line": "gas check unavailable right now",
                      "analysis": None}


def test_warning_text_includes_open_time_and_disclaimer():
    result = gasguard.warning_text("drop", "20:00 UTC", {"line": "gas normal"})
    assert "gas warning: drop" in result
    assert "opens 20:00 UTC" in result
    assert "still pays whatever gas" in result
