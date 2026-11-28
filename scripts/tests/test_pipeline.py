"""Unit tests for scripts/pipeline (pure code; no network)."""
import math
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import common as c  # noqa: E402
from pipeline import scoring  # noqa: E402


def good_item(**kw):
    it = {"node_id": "asia_early_signal", "asset_or_theme": "USD/KRW", "metric_value": "1,337.1",
          "delta": "+0.2%", "priced_in_score": 60, "momentum_score": -5, "tension_score": 30,
          "claim": "USD/KRW at 1,337.1 on Nov 27.", "source_url": "https://finance.yahoo.com/quote/KRW%3DX/",
          "published_at": "2026-11-27"}
    it.update(kw)
    return it


def series(start, step, n, d0=1):
    return [("2026-%02d-%02d" % (1 + (i // 28), 1 + (i % 28)), start + step * i) for i in range(n)]


class ItemValidation(unittest.TestCase):
    def test_good_item(self):
        self.assertEqual(c.validate_item(good_item()), [])

    def test_ranges_and_url(self):
        self.assertTrue(c.validate_item(good_item(priced_in_score=120)))
        self.assertTrue(c.validate_item(good_item(momentum_score=-51)))
        self.assertTrue(c.validate_item(good_item(source_url="http://example.com")))
        self.assertTrue(c.validate_item(good_item(published_at="yesterday")))

    def test_missing_field_and_forbidden(self):
        it = good_item()
        del it["claim"]
        self.assertTrue(c.validate_item(it))
        self.assertTrue(c.validate_item(good_item(source_url="https://x.com/someone/status/1")))

    def test_node(self):
        self.assertTrue(c.validate_node({"node_id": "asia_early_signal", "items": []}, "asia_early_signal"))
        self.assertEqual(c.validate_node({"node_id": "asia_early_signal", "items": [good_item()]}, "asia_early_signal"), [])


class Compass(unittest.TestCase):
    def hist(self):
        h = {k: series(4.0, 0.0, 40) for k in ("US2Y", "US10Y", "SOFR")}
        h["ONRRP"] = series(1.0, 0.0, 40)
        h["DXY"] = series(100.0, 0.0, 40)
        return h

    def test_formula(self):
        h = self.hist()
        last = h["US2Y"][-1][0]
        snap = {"US2Y": {"value": 4.30, "date": last}, "US10Y": {"value": 4.10, "date": last},
                "SOFR": {"value": 4.0, "date": last}, "ONRRP": {"value": 1.0, "date": last},
                "DXY": {"value": 102.0, "date": last}}
        r = scoring.compass(h, snap)
        y = 50 * math.tanh((0.5 * 30 + 0.3 * 10) / 30)
        x = 50 * math.tanh(-(0.6 * 2.0 / 2))
        self.assertAlmostEqual(r["y"], round(y, 1), places=1)
        self.assertAlmostEqual(r["x"], round(x, 1), places=1)
        self.assertEqual(r["quadrant"], "stagflation_defense")
        self.assertTrue(-50 <= r["x"] <= 50 and -50 <= r["y"] <= 50)

    def test_incomplete_returns_none(self):
        h = self.hist()
        del h["DXY"]
        self.assertIsNone(scoring.compass(h, {}))


class Bubbles(unittest.TestCase):
    def test_scores_in_range(self):
        h = [("d%03d" % i, 100 + 5 * math.sin(i / 7.0) + i * 0.1) for i in range(200)]
        s = scoring.bubble_scores(h, "pct", 1)
        self.assertTrue(0 <= s["priced_in_score"] <= 100)
        self.assertTrue(-50 <= s["momentum_score"] <= 50)
        self.assertTrue(0 <= s["tension_score"] <= 100)

    def test_short_history(self):
        self.assertIsNone(scoring.bubble_scores([("d", 1.0)] * 10, "pct", 1))


class Fallback(unittest.TestCase):
    """A node that fails is replaced by its last valid cache, with a WARNING."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = (c.NODES, c.LASTGOOD)
        c.NODES = os.path.join(self.tmp, "nodes")
        c.LASTGOOD = os.path.join(c.NODES, "_lastgood")
        from pipeline import aggregate
        self.agg = aggregate

    def tearDown(self):
        c.NODES, c.LASTGOOD = self.old
        shutil.rmtree(self.tmp)

    def test_lastgood(self):
        for n in c.NODE_IDS:
            doc = {"node_id": n, "status": "ok", "items": [good_item(node_id=n, asset_or_theme="T " + n)]}
            c.write_json(os.path.join(c.NODES, n + ".json"), doc)
        warnings = []
        used, items, returned = self.agg.load_nodes(warnings)
        self.assertEqual(returned, 4)
        self.assertEqual(warnings, [])
        c.write_json(os.path.join(c.NODES, "crypto_leverage.json"), {"node_id": "crypto_leverage", "status": "failed", "items": []})
        warnings = []
        used, items, returned = self.agg.load_nodes(warnings)
        self.assertEqual(returned, 3)
        self.assertTrue(any("crypto_leverage" in w for w in warnings))
        self.assertEqual(used["crypto_leverage"]["source"], "lastgood")
        self.assertTrue(any(it["node_id"] == "crypto_leverage" and it["stale"] for it in items))



class TickerSession(unittest.TestCase):
    """The ticker stamps Yahoo quotes with the KST date; that must not add a fake session."""
    def test_same_close_next_kst_day_is_not_a_session(self):
        from pipeline.prepare import is_new_session
        self.assertFalse(is_new_session({"date": "2026-11-28", "value": 1333.0}, "2026-11-27", 1333.0))   # Saturday KST
        self.assertFalse(is_new_session({"date": "2026-11-27", "value": 104.03}, "2026-11-27", 104.014))  # same exchange date

    def test_new_weekday_value_is_a_session(self):
        from pipeline.prepare import is_new_session
        self.assertTrue(is_new_session({"date": "2026-11-24", "value": 1340.5}, "2026-11-23", 1333.0))
        self.assertTrue(is_new_session({"date": "2026-11-24", "value": 1333.0}, "2026-11-23", 1333.0))   # flat close is still a session

    def test_range_label(self):
        from pipeline.prepare import range_label
        self.assertEqual(range_label([("2025-11-27", 1), ("2026-11-27", 2)]), "1-year")
        self.assertEqual(range_label([("2026-06-14", 1), ("2026-11-27", 2)]), "166-day")


class Ledger(unittest.TestCase):
    def test_entry_from_state(self):
        from pipeline.ledger import entry_from_state
        st = {"generated_at": "2026-11-28T08:41+09:00",
              "compass": {"x": -26.2, "y": -3.8, "snapshot_stamp": "Nov 28 08:40 KST", "as_of_data": "2026-11-27"},
              "regime": {"label": "Safe-Haven Bond Crowding", "quadrant": "safe_haven", "drivers": [{"text": "a"}, {"text": "b"}],
                         "note": {"text": "n", "source_url": "https://example.com"}},
              "bubble_map": {"bubbles": [{"label": "A", "short": "A", "tension": 10, "priced_in": 1, "flow": 2},
                                         {"label": "B", "short": "B", "tension": 50, "priced_in": 3, "flow": 4}]}}
        e = entry_from_state(st)
        self.assertEqual(e["snapshot_stamp"], "Nov 28 08:40 KST")
        self.assertEqual([t["short"] for t in e["top_tension"]], ["B", "A"])
        self.assertEqual(e["drivers"], ["a", "b"])

if __name__ == "__main__":
    unittest.main()
