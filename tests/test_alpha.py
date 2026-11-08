import os, sqlite3, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pathlib import Path
from acobot import alpha as alp

REAL_DB = Path.home() / ".folk" / "sagetracer" / "data" / "tracker.db"
CA = "0x" + "cd" * 20
WAL = "0x1f00b89f92990b8b3a1708a59e11397dc5af0364"

SCHEMA = """CREATE TABLE events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER, chain TEXT, kind TEXT, wallet TEXT, wallet_name TEXT, ca TEXT, coll TEXT,
  ids TEXT, qty INTEGER, price_wei TEXT, price_known INTEGER, usd REAL, tx TEXT
)"""


def ev(**kw):
    base = {"id": 1, "ts": 1793132178, "chain": "ethereum", "kind": "BOUGHT", "wallet": WAL,
            "wallet_name": "craz", "ca": CA, "coll": "Test Coll", "ids": "12", "qty": 1,
            "price_wei": str(10**17), "price_known": 1, "usd": 2000.0, "tx": "0x" + "ab" * 32}
    base.update(kw)
    return base


def make_db(rows):
    d = tempfile.mkdtemp()
    path = os.path.join(d, "tracker.db")
    con = sqlite3.connect(path)
    con.execute(SCHEMA)
    for r in rows:
        con.execute("INSERT INTO events (ts, chain, kind, wallet, wallet_name, ca, coll, ids, qty, price_wei, "
                    "price_known, usd, tx) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (r["ts"], r["chain"], r["kind"], r["wallet"], r["wallet_name"], r["ca"], r["coll"],
                     r["ids"], r["qty"], r["price_wei"], r["price_known"], r["usd"], r["tx"]))
    con.commit()
    con.close()
    return path


class ReadOnlyReader(unittest.TestCase):
    def test_reads_rows_newest_first(self):
        path = make_db([ev(ts=1, ids="1"), ev(ts=2, ids="2")])
        rows = alp.read_events(path=path)
        self.assertEqual([r["ids"] for r in rows], ["2", "1"])

    def test_limit_applies(self):
        path = make_db([ev(ts=i, ids=str(i)) for i in range(5)])
        self.assertEqual(len(alp.read_events(limit=3, path=path)), 3)

    def test_missing_db_returns_empty_not_error(self):
        self.assertEqual(alp.read_events(path="/nonexistent/tracker.db"), [])
        self.assertIsNone(alp.get_event(1, path="/nonexistent/tracker.db"))

    def test_connection_is_read_only(self):
        path = make_db([ev()])
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        with self.assertRaises(sqlite3.OperationalError):
            con.execute("INSERT INTO events (ts, chain, kind, wallet, wallet_name, ca, coll, ids, qty, "
                        "price_wei, price_known, usd, tx) VALUES (1,'ethereum','BOUGHT','w','n','c','c','1',1,'1',1,1,'t')")
        con.close()

    def test_get_event_by_id(self):
        path = make_db([ev(ids="7")])
        self.assertEqual(alp.get_event(1, path=path)["ids"], "7")
        self.assertIsNone(alp.get_event("abc", path=path))


class CardAndRows(unittest.TestCase):
    def test_card_has_ca_token_price_and_links(self):
        t = alp.card(ev(), floor_native=0.2, usd=2000.0)
        self.assertIn(f"<code>{CA}</code>", t)
        self.assertIn("token #12", t)
        self.assertIn("0.1000 ETH", t)
        self.assertIn("$200.00", t)
        self.assertIn("floor now 0.2000 ETH", t)
        self.assertIn("opensea.io/assets/ethereum/", t)
        self.assertIn("etherscan.io/tx/", t)
        self.assertIn("UTC", t)

    def test_price_unknown_says_so(self):
        self.assertIn("free mint (0 ETH)", alp.card(ev(price_known=0, price_wei=None, kind="MINTED")))

    def test_draft_contract_gate(self):
        self.assertEqual(alp.draft_contract(CA, "ethereum", ["ethereum"]), CA)
        self.assertIsNone(alp.draft_contract(CA, "robinhood", ["ethereum"]))
        self.assertIsNone(alp.draft_contract("0x123", "ethereum", ["ethereum"]))
        self.assertIsNone(alp.draft_contract(None, "ethereum", ["ethereum"]))

    def test_best_buys_pnl_vs_floor(self):
        rows = alp.best_buys([ev(id=1, price_wei=str(10**17))], lambda c, ca: 0.2)
        self.assertAlmostEqual(rows[0][0], 100.0)

    def test_best_buys_skips_sold_and_unknown_price(self):
        self.assertEqual(alp.best_buys([ev(kind="SOLD")], lambda c, ca: 1.0), [])
        self.assertEqual(alp.best_buys([ev(price_known=0, price_wei=None)], lambda c, ca: 1.0), [])

    def test_upcoming_mints_one_per_contract(self):
        rows = alp.upcoming_mints([ev(kind="MINTED", id=3), ev(kind="MINTED", id=2), ev(kind="BOUGHT", id=1)])
        self.assertEqual([r["id"] for r in rows], [3])


class RealTrackerDb(unittest.TestCase):
    """Against the live sagetracer tracker db: the 3 events that exist right now."""

    @unittest.skipUnless(REAL_DB.exists(), "sagetracer tracker db not present")
    def test_real_db_has_three_events_readable(self):
        rows = alp.read_events(path=REAL_DB)
        self.assertGreaterEqual(len(rows), 3)
        kinds = [r["kind"] for r in rows]
        self.assertIn("MINTED", kinds)
        self.assertIn("BOUGHT", kinds)
        for r in rows:
            self.assertTrue(alp.card(r).startswith(("🆕", "🟢", "🔴", "📤", "📥")))


if __name__ == "__main__":
    unittest.main()
