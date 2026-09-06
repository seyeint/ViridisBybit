"""
Unit tests for the pure parts of the engine — no network, no Qt.

    python -m unittest discover -s tests

Covers fee-aware sizing, the liquidation cushion (including the risk-limit
tier fix), relative ticket inputs, the Strat1 ratchet, the closed-PnL side
join and the journal helpers.
"""

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config                                   # noqa: E402
import trading_core as tc                       # noqa: E402
from cache_engine import InstrumentCache        # noqa: E402
from journal import TradeJournal                # noqa: E402

MAKER, TAKER = 0.0002, 0.00055

RULES = {
    "BTCUSDT": {
        "tickSize": "0.10", "qtyStep": "0.001", "minQty": "0.001", "maxLev": 100.0,
        "minNotional": "5", "mmr": 0.005,
        "tiers": [{"limit": 300000, "mmr": 0.005, "maxLev": 100},
                  {"limit": 600000, "mmr": 0.01, "maxLev": 50}],
    },
    # A small-cap: tier 1 ends at $5k, MMR climbs fast after that.
    "ALTUSDT": {
        "tickSize": "0.0001", "qtyStep": "1", "minQty": "1", "maxLev": 75.0,
        "minNotional": "5", "mmr": 0.0067,
        "tiers": [{"limit": 5000, "mmr": 0.0067, "maxLev": 75},
                  {"limit": 25000, "mmr": 0.01, "maxLev": 50},
                  {"limit": 50000, "mmr": 0.02, "maxLev": 25}],
    },
    "OLDUSDT": {  # cache entry from before tiers were stored
        "tickSize": "0.01", "qtyStep": "0.1", "minQty": "0.1", "maxLev": 50.0,
        "minNotional": "5", "mmr": 0.01,
    },
}


class StubCache:
    def __init__(self, rules):
        self._rules = rules

    def get(self, symbol):
        return self._rules.get(symbol)


class FakeQueue:
    def __init__(self):
        self.items = []

    def put(self, item):
        self.items.append(item)


def make_core():
    """A TradingCore without __init__ (no REST client, no threads)."""
    core = tc.TradingCore.__new__(tc.TradingCore)
    core.cache = StubCache(RULES)
    core._on_log = lambda *a, **k: None
    core._ratchet = tc.parse_ratchet(config.STRAT1_RATCHET)
    core._strat1_last_amend = {}
    core._queue = FakeQueue()
    return core


class SetFees(unittest.TestCase):
    def setUp(self):
        self._fees = (config.FEE_MAKER, config.FEE_TAKER)
        config.FEE_MAKER, config.FEE_TAKER = MAKER, TAKER

    def tearDown(self):
        config.FEE_MAKER, config.FEE_TAKER = self._fees


class TestSizing(SetFees):
    def test_fee_aware_qty_never_exceeds_risk(self):
        core = make_core()
        calc = core.calculate_trade("BTCUSDT", "Buy", 74300, 73300, 50)
        qty = float(calc["qty"])
        loss_at_stop = qty * (1000 + 74300 * MAKER + 73300 * TAKER)
        self.assertEqual(calc["qty"], "0.047")
        self.assertLessEqual(loss_at_stop, 50.0)
        self.assertGreater(loss_at_stop, 48.5)          # rounding down, not sandbagging
        self.assertAlmostEqual(calc["fee_usd"], round(qty * (74300 * MAKER + 73300 * TAKER), 2))

    def test_leverage_keeps_liquidation_behind_the_stop(self):
        core = make_core()
        calc = core.calculate_trade("BTCUSDT", "Buy", 74300, 73300, 50)
        self.assertEqual(calc["leverage"], 48)
        self.assertLess(calc["liq_price"], 73300)
        self.assertGreaterEqual(calc["cushion_pct"], 0.2)
        self.assertEqual(calc["tier"], 1)

    def test_short_liquidation_sits_above_the_stop(self):
        core = make_core()
        calc = core.calculate_trade("BTCUSDT", "Sell", 74300, 75300, 50)
        self.assertGreater(calc["liq_price"], 75300)
        self.assertGreaterEqual(calc["cushion_pct"], 0.2)

    def test_direction_validation(self):
        core = make_core()
        with self.assertRaises(ValueError):
            core.calculate_trade("BTCUSDT", "Buy", 74300, 74400, 50)
        with self.assertRaises(ValueError):
            core.calculate_trade("BTCUSDT", "Sell", 74300, 74200, 50)

    def test_minimum_size_error_is_explicit(self):
        core = make_core()
        with self.assertRaises(ValueError) as ctx:
            core.calculate_trade("BTCUSDT", "Buy", 74300, 74290, 0.01)
        self.assertIn("minimum", str(ctx.exception))


class TestRiskLimitTiers(SetFees):
    def test_tier_for_picks_by_notional(self):
        rules = RULES["ALTUSDT"]
        self.assertEqual(InstrumentCache.tier_for(rules, 4000)["index"], 0)
        self.assertEqual(InstrumentCache.tier_for(rules, 5000)["index"], 0)
        self.assertEqual(InstrumentCache.tier_for(rules, 5001)["index"], 1)
        self.assertEqual(InstrumentCache.tier_for(rules, 10 ** 9)["index"], 2)

    def test_tier_for_falls_back_to_base_mmr(self):
        tier = InstrumentCache.tier_for(RULES["OLDUSDT"], 10 ** 9)
        self.assertEqual(tier["mmr"], 0.01)
        self.assertEqual(tier["count"], 1)

    def test_sizing_uses_the_tier_the_notional_lands_in(self):
        """A $100 risk at a 0.5% stop is ~$17k notional on this alt: tier 2.
        Base-tier sizing would have put liquidation INSIDE the stop."""
        core = make_core()
        calc = core.calculate_trade("ALTUSDT", "Buy", 0.02, 0.0199, 100)
        self.assertGreater(calc["notional_usd"], 5000)
        self.assertEqual(calc["tier"], 2)
        self.assertEqual(calc["mmr_pct"], 1.0)
        self.assertEqual(calc["leverage"], 50)          # tier-2 max, below the 58x the formula allows
        self.assertLess(calc["liq_price"], 0.0199)
        self.assertGreaterEqual(calc["cushion_pct"], 0.2)

        # The old behaviour, for the record: base MMR → 73x → liquidation above the stop.
        naive_lev = int(1 / (0.005 + 0.0067 + 0.002))
        naive_liq_at_real_mmr = 0.02 * (1 - 1 / naive_lev + 0.01)
        self.assertGreater(naive_liq_at_real_mmr, 0.0199)


class TestRelativeInputs(SetFees):
    def test_stop_as_percent_or_price(self):
        self.assertAlmostEqual(tc.resolve_stop("-1.35%", 74300, "Buy"), 74300 * (1 - 0.0135))
        self.assertAlmostEqual(tc.resolve_stop("1.35%", 74300, "Buy"), 74300 * (1 - 0.0135))
        self.assertAlmostEqual(tc.resolve_stop("1%", 100, "Sell"), 101)
        self.assertEqual(tc.resolve_stop("73,300", 74300, "Buy"), 73300)
        with self.assertRaises(ValueError):
            tc.resolve_stop("abc", 74300, "Buy")

    def test_target_as_r_percent_or_price(self):
        self.assertEqual(tc.resolve_target("2R", 100, 99, "Buy"), 102)
        self.assertEqual(tc.resolve_target("2r", 100, 101, "Sell"), 98)
        self.assertAlmostEqual(tc.resolve_target("+3%", 100, 99, "Buy"), 103)
        self.assertAlmostEqual(tc.resolve_target("3%", 100, 101, "Sell"), 97)
        self.assertEqual(tc.resolve_target("$105", 100, 99, "Buy"), 105)
        with self.assertRaises(ValueError):
            tc.resolve_target("2R", 100, 100, "Buy")

    def test_risk_as_dollars_or_share_of_equity(self):
        self.assertEqual(tc.resolve_risk("$ 50", None), 50)
        self.assertEqual(tc.resolve_risk("1%", 5000), 50)
        with self.assertRaises(ValueError):
            tc.resolve_risk("1%", None)


class TestRatchet(SetFees):
    def test_ratchet_prices(self):
        be = tc.breakeven_price(100, "Buy")
        self.assertAlmostEqual(be, 100 * (1 + MAKER) / (1 - TAKER))
        self.assertEqual(tc.ratchet_price(100, 2, "Buy", -0.5), 99)
        self.assertAlmostEqual(tc.ratchet_price(100, 2, "Buy", 0), be)
        self.assertAlmostEqual(tc.ratchet_price(100, 2, "Buy", 0.5), be + 1)
        self.assertEqual(tc.ratchet_price(100, 2, "Sell", -0.5), 101)
        self.assertAlmostEqual(tc.ratchet_price(100, 2, "Sell", 0.5), tc.breakeven_price(100, "Sell") - 1)

    def test_parse_ratchet(self):
        self.assertEqual(tc.parse_ratchet("0.9:0.5, 0.5:-0.5, junk, 1.5:2"), [(0.5, -0.5), (0.9, 0.5)])
        self.assertEqual(tc.parse_ratchet(""), [(0.75, -0.5), (0.90, 0.0)])

    def _trade(self):
        t = tc.TradeState("BTCUSDT", "Buy", "oid")
        t.phase = tc.TradeState.PHASE_LIVE
        t.fill_price = 100.0
        t.original_tp = 110.0
        t.original_sl_distance = 2.0
        t.stop_loss = "98.0"
        t.strat1_enabled = True
        return t

    def test_progression_and_skip_ahead(self):
        core = make_core()
        core._ratchet = [(0.75, -0.5), (0.90, 0.0)]
        t = self._trade()
        self.assertIsNone(core._evaluate_strat1(t, 105.0))          # 50%: nothing
        self.assertIsNotNone(core._evaluate_strat1(t, 107.5))       # 75%: half distance
        self.assertEqual(t.strat1_phase, 1)
        self.assertEqual(t.stop_loss, "99.00")
        core._strat1_last_amend.clear()
        self.assertIsNotNone(core._evaluate_strat1(t, 109.0))       # 90%: break-even
        self.assertEqual(t.strat1_phase, 2)
        self.assertAlmostEqual(float(t.stop_loss), tc.breakeven_price(100, "Buy"), places=1)
        self.assertEqual(len(core._queue.items), 2)

        t2 = self._trade()
        core._strat1_last_amend.clear()
        core._evaluate_strat1(t2, 109.5)                             # fast move: straight to phase 2
        self.assertEqual(t2.strat1_phase, 2)

    def test_never_loosens_a_tighter_manual_stop(self):
        core = make_core()
        core._ratchet = [(0.75, -0.5), (0.90, 0.0)]
        t = self._trade()
        t.stop_loss = "99.50"
        self.assertIsNone(core._evaluate_strat1(t, 107.5))
        self.assertEqual(t.strat1_phase, 1)                         # phase recorded, no amend
        self.assertEqual(t.stop_loss, "99.50")
        self.assertEqual(core._queue.items, [])

    def test_never_crosses_the_mark(self):
        core = make_core()
        core._ratchet = [(0.5, 3.0)]                                # a lock past the target
        t = self._trade()
        self.assertIsNone(core._evaluate_strat1(t, 106.0))
        self.assertEqual(t.strat1_phase, 1)
        self.assertEqual(t.stop_loss, "98.0")
        self.assertEqual(core._queue.items, [])


class TestJournalJoin(SetFees):
    def test_side_comes_from_the_closing_order(self):
        core = make_core()
        # Long closed by a Sell, exited a hair above entry, net negative after fees:
        # the price/PnL heuristic would call this a short.
        rec = core._normalize_closed_pnl({
            "orderId": "abc", "symbol": "BTCUSDT", "side": "Sell",
            "avgEntryPrice": "100", "avgExitPrice": "100.01", "closedPnl": "-0.05",
            "updatedTime": "1700000000000", "qty": "1", "leverage": "10",
        })
        self.assertEqual(rec["side"], "Buy")
        rec = core._normalize_closed_pnl({"side": "Buy", "avgEntryPrice": "100",
                                          "avgExitPrice": "99", "closedPnl": "1", "updatedTime": "0"})
        self.assertEqual(rec["side"], "Sell")
        # Fallback when the field is missing
        rec = core._normalize_closed_pnl({"avgEntryPrice": "100", "avgExitPrice": "110",
                                          "closedPnl": "10", "updatedTime": "0"})
        self.assertEqual(rec["side"], "Buy")

    def test_estimate_trade_risk(self):
        t = tc.TradeState("BTCUSDT", "Buy", "x")
        t.fill_price, t.qty, t.stop_loss = 100.0, "2", "98"
        self.assertAlmostEqual(tc.TradingCore.estimate_trade_risk(t),
                               4 + 100 * 2 * MAKER + 98 * 2 * TAKER)
        t.risk_usd = 50
        self.assertEqual(tc.TradingCore.estimate_trade_risk(t), 50)
        self.assertIsNone(tc.TradingCore.estimate_trade_risk(tc.TradeState("X", "Buy", "y")))

    def test_liq_inside_stop_alarm(self):
        t = tc.TradeState("BTCUSDT", "Buy", "x")
        t.stop_loss, t.liq_price = "98", 97.0
        self.assertIsNone(tc.TradingCore._liq_inside_stop(t))
        t.liq_price = 98.5
        self.assertIn("inside the stop", tc.TradingCore._liq_inside_stop(t))
        self.assertIsNone(tc.TradingCore._liq_inside_stop(t))   # fires once


class TestJournal(unittest.TestCase):
    def test_streak_recent_symbols_and_windows(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "j.json")
            rows = [
                {"order_id": "1", "symbol": "ETHUSDT", "pnl": 10.0, "closed_at": 100},
                {"order_id": "2", "symbol": "BTCUSDT", "pnl": -5.0, "closed_at": 200},
                {"order_id": "3", "symbol": "SOLUSDT", "pnl": 0.0, "closed_at": 300},
                {"order_id": "4", "symbol": "BTCUSDT", "pnl": -7.0, "closed_at": 400},
            ]
            with open(path, "w") as f:
                json.dump(rows, f)
            j = TradeJournal(path)
            self.assertEqual(j.loss_streak(), 2)                  # flat trade skipped
            self.assertEqual(j.recent_symbols(2), ["BTCUSDT", "SOLUSDT"])
            self.assertEqual(j.stats(since=250)["total"], 2)
            self.assertEqual(j.stats(since=250)["total_pnl"], -7.0)
            self.assertEqual(j.reconcile([{"order_id": "4"}, {"order_id": "5", "pnl": 1, "closed_at": 500}]), 1)
            self.assertEqual(j.loss_streak(), 0)


class TestFormatValue(unittest.TestCase):
    def test_rounding(self):
        self.assertEqual(tc.format_value(64951.37, "0.10"), "64951.40")
        self.assertEqual(tc.format_value(0.01437, "0.001", round_down=True), "0.014")
        self.assertEqual(tc.format_value(0.0199999, "0.0001"), "0.0200")


if __name__ == "__main__":
    unittest.main()
