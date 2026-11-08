"""Two-shot (pre-fire + normal, same nonce) against a fake chain that enforces nonce rules.

FakeChain: one account nonce, a mempool keyed by nonce, EIP-1559 replacement (>=10% bump on both fees),
and inclusion mines the pending nonce once. Same-nonce losers get the node error text.
"""
import unittest, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import rlp
from eth_account import Account
from acobot import sniper, results as R
from acobot.sniper import _sign_pair, bump_fees, lift_over_sent, is_dual, DUAL

KEY = "0x" + "11" * 32
ADDR = Account.from_key(KEY).address
CFG = type("C", (), {"chain_id": 1})()


def decode(raw):
    f = rlp.decode(bytes.fromhex(raw[2:])[1:])   # type-2 envelope: drop the 0x02 byte
    i = lambda x: int.from_bytes(x, "big") if x else 0
    return {"nonce": i(f[1]), "maxPriorityFeePerGas": i(f[2]), "maxFeePerGas": i(f[3])}


class FakeChain:
    def __init__(self, nonce=7):
        self.nonce = nonce
        self.mempool = {}     # nonce -> {"raw", "maxFee", "prio", "tag"}
        self.mined = []       # [(nonce, tag)]

    def accept(self, raw, tag):
        d = decode(raw)
        n = d["nonce"]
        if n < self.nonce:
            raise RuntimeError("nonce too low")
        cur = self.mempool.get(n)
        if cur and (d["maxFeePerGas"] < cur["maxFee"] * 1.1 or d["maxPriorityFeePerGas"] < cur["prio"] * 1.1):
            raise RuntimeError("replacement transaction underpriced")
        self.mempool[n] = {"raw": raw, "maxFee": d["maxFeePerGas"], "prio": d["maxPriorityFeePerGas"], "tag": tag}
        return "0x" + tag.encode().hex().ljust(64, "0")[:64]

    def include(self):
        """Mine the pending nonce once. Any same-nonce loser never mines."""
        if self.nonce not in self.mempool:
            return None
        tx = self.mempool.pop(self.nonce)
        self.mined.append((self.nonce, tx["tag"]))
        self.nonce += 1
        self.mempool.clear()
        return tx["tag"]


class FakeRpc:
    """Stands in for the broadcast RPC: every broadcast goes to the fake chain's mempool.
    The shot tag is read from the raw tx's fee shape (pre-fire vs normal) via the test's own map."""
    def __init__(self, chain, tags):
        self.chain = chain
        self.tags = tags          # raw hex -> tag

    def broadcast(self, raw):
        return self.chain.accept(raw, self.tags.get(raw, "?"))


def entry(nonce):
    return {"label": "w", "address": ADDR, "priv": KEY, "nonce": nonce,
            "to": "0x" + "22" * 20, "value": 0, "data": "0x1249c58b"}


def fees(chain_cfg=CFG):
    return _sign_pair(entry(7), chain_cfg, "fast", 10 * 10**9, 2 * 10**9, 10**9, 250000)


class DualFireTests(unittest.TestCase):
    def test_pair_same_nonce_and_bump(self):
        pre_raw, pre_info, raw, info = fees()
        dp, dn = decode(pre_raw), decode(raw)
        self.assertEqual(dp["nonce"], 7)
        self.assertEqual(dn["nonce"], 7)
        self.assertGreaterEqual(dn["maxFeePerGas"], int(dp["maxFeePerGas"] * 1.12))
        self.assertGreaterEqual(dn["maxPriorityFeePerGas"], int(dp["maxPriorityFeePerGas"] * 1.12))
        self.assertNotEqual(pre_raw, raw)

    def test_bump_clears_replacement_rule(self):
        p, m = bump_fees(3 * 10**9, 60 * 10**9)
        self.assertGreaterEqual(p, int(3 * 10**9 * 1.1) + 1)
        self.assertGreaterEqual(m, int(60 * 10**9 * 1.1) + 1)

    def test_lift_over_sent_bids_above_sent_prefire(self):
        pre_raw, pre_info, raw, info = fees()
        t = {**entry(7), "raw": pre_raw, "info": pre_info}
        lifted = lift_over_sent(t, pre_info, CFG)
        self.assertGreater(lifted["info"]["max_wei"], pre_info["max_wei"])
        self.assertGreater(lifted["info"]["prio_wei"], pre_info["prio_wei"])

    def test_fake_chain_pre_then_normal_one_mined(self):
        chain = FakeChain(nonce=7)
        pre_raw, pre_info, raw, info = fees()
        rpc = FakeRpc(chain, {pre_raw: "prefire", raw: "normal"})
        # pre-fire shot lands in the mempool first
        pre = sniper.fire([{**entry(7), "raw": pre_raw, "label": "w", "address": ADDR}], rpc, shot="prefire")
        self.assertIsInstance(pre["results"][0]["rpc"], str)
        # normal shot replaces it (same nonce, bumped fees): accepted
        norm = sniper.fire([{**entry(7), "raw": raw, "label": "w", "address": ADDR}], rpc, shot="normal")
        self.assertIsInstance(norm["results"][0]["rpc"], str)
        self.assertEqual(chain.include(), "normal")  # one winner, nonce advances once
        self.assertEqual(chain.nonce, 8)
        self.assertEqual(len(chain.mined), 1)

    def test_fake_chain_loser_is_same_nonce_reject_not_failure(self):
        chain = FakeChain(nonce=7)
        pre_raw, _, raw, _ = fees()
        rpc = FakeRpc(chain, {pre_raw: "prefire", raw: "normal"})
        chain.accept(raw, "normal")
        chain.include()                     # nonce 7 is now mined
        out = sniper.fire([{**entry(7), "raw": pre_raw, "label": "w", "address": ADDR}], rpc, shot="prefire")
        err = out["results"][0]["rpc"]
        self.assertIsInstance(err, dict)
        self.assertTrue(R.is_same_nonce_reject(err["error"]))

    def test_same_nonce_reject_detector(self):
        self.assertTrue(R.is_same_nonce_reject("nonce too low"))
        self.assertTrue(R.is_same_nonce_reject("replacement transaction underpriced"))
        self.assertTrue(R.is_same_nonce_reject("already known"))
        self.assertFalse(R.is_same_nonce_reject("insufficient funds for gas"))

    def test_collapse_gas_only_from_included(self):
        rows = [
            {"label": "w", "address": "0xA", "shot": "prefire", "status": "superseded", "gas_eth": None, "gas_usd": None},
            {"label": "w", "address": "0xA", "shot": "normal", "status": "minted", "gas_eth": 0.001,
             "gas_usd": 2.5, "tx_hash": "0xn", "receipt_s": 1.2},
        ]
        w = R.collapse_wallets(rows)[0]
        self.assertEqual(w["status"], "minted")
        self.assertEqual(w["landed_shot"], "normal")
        self.assertEqual(w["gas_eth"], 0.001)
        self.assertEqual(w["gas_usd"], 2.5)

    def test_dual_flag(self):
        self.assertTrue(is_dual({"fire_mode": DUAL}))
        self.assertFalse(is_dual({"fire_mode": "normal"}))
        self.assertFalse(is_dual({}))


if __name__ == "__main__":
    unittest.main()
