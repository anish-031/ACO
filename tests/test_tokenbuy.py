import pytest

from acobot import tokenbuy


ADDR = "0x" + "11" * 20


def test_v4_revert_is_reported_when_v3_fallbacks_also_fail(monkeypatch):
    def v4(*_args):
        raise RuntimeError(tokenbuy.V4_QUOTE_UNAVAILABLE)

    def v3(*_args):
        raise RuntimeError("no V3 pool")

    monkeypatch.setattr(tokenbuy, "_prepare_v4", v4)
    monkeypatch.setattr(tokenbuy, "_prepare_v3", v3)
    with pytest.raises(RuntimeError, match="hook-dependent execution cannot be safely quoted"):
        tokenbuy.prepare_buy(ADDR, "0.01", ADDR, object())
