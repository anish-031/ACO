from acobot import cawatch
import pytest


@pytest.fixture(autouse=True)
def isolate_config(tmp_path, monkeypatch):
    monkeypatch.setattr(cawatch, "CONFIG_FILE", str(tmp_path / "ca_watchers.json"))


def test_extract_evm():
    evm, sol = cawatch.extract_cas("gm mint is live 0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168 lfg")
    assert evm == ["0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"]
    assert sol == []


def test_extract_sol():
    sol_ca = "7EYnhzNwNSJnaLXmvBbgDfgyyaDUBfcJNqzEGmDwpump"
    evm, sol = cawatch.extract_cas(f"new token {sol_ca} ape")
    assert sol == [sol_ca]
    assert evm == []


def test_dedup_and_noise():
    evm, sol = cawatch.extract_cas("0xAaa0000000000000000000000000000000000001 0xAaa0000000000000000000000000000000000001 https://x.com/a")
    assert len(evm) == 1
    assert all("http" not in s for s in sol)


def test_add_list_remove():
    s = cawatch.add_source("x", "jackbutcher_test", "manual")
    assert any(x["name"] == "jackbutcher_test" for x in cawatch.list_sources())
    assert cawatch.remove_source("jackbutcher_test")
    assert not any(x["name"] == "jackbutcher_test" for x in cawatch.list_sources())


def test_auto_needs_cap():
    try:
        cawatch.add_source("x", "nocap_test", "auto", cap_eth=0)
        assert False
    except Exception:
        pass
    assert not any(x["name"] == "nocap_test" for x in cawatch.list_sources())


def test_cap_ceiling():
    assert not cawatch.cap_ok({"cap_eth": 1.0}, 0.5)  # above hard ceiling
    assert cawatch.cap_ok({"cap_eth": 0.02}, 0.02)


def test_seen_dedup():
    cawatch.add_source("x", "seen_test", "manual")
    assert cawatch.is_new("x", "seen_test", "0x" + "a" * 40)
    cawatch.mark_seen("x", "seen_test", "0x" + "a" * 40)
    assert not cawatch.is_new("x", "seen_test", "0x" + "a" * 40)
    cawatch.remove_source("seen_test")
