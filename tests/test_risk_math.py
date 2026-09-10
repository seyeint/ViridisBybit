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
import threading
import time
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


class StubClient:
    """Just enough of pybit's HTTP for the reconciliation and execution paths."""

    def __init__(self, positions=(), orders=(), leverage_error=None):
        self.positions = list(positions)
        self.orders = list(orders)
        self.leverage_error = leverage_error
        self.calls = []

    def get_positions(self, **kw):
        self.calls.append(("get_positions", kw))
        rows = [p for p in self.positions if not kw.get("symbol") or p["symbol"] == kw["symbol"]]
        return {"result": {"list": rows}}

    def get_open_orders(self, **kw):
        self.calls.append(("get_open_orders", kw))
        return {"result": {"list": list(self.orders), "nextPageCursor": ""}}

    def set_leverage(self, **kw):
        self.calls.append(("set_leverage", kw))
        if self.leverage_error:
            raise RuntimeError(self.leverage_error)
        return {"retCode": 0}

    def place_order(self, **kw):
        self.calls.append(("place_order", kw))
        return {"retCode": 0, "result": {"orderId": "oid-1"}}

    def set_margin_mode(self, **kw):
        self.calls.append(("set_margin_mode", kw))
        return {"retCode": 0}


def make_core(client=None):
    """A TradingCore without __init__ (no REST client, no threads, no disk)."""
    core = tc.TradingCore.__new__(tc.TradingCore)
    core.cache = StubCache(RULES)
    core.client = client or StubClient()
    core._on_log = lambda *_: None
    core._on_trade_update = lambda _: None
    core._on_margin_mode = lambda _: None
    core._on_journal = lambda: None
    core._ratchet = tc.parse_ratchet(config.STRAT1_RATCHET)
    core._strat1_last_amend = {}
    core._queue = FakeQueue()
    core._trades = {}
    core._lock = threading.RLock()
    core._risk_ledger_lock = threading.Lock()
    core._risk_ledger = []
    core._save_risk_ledger = lambda: None
    core.account_margin_mode = "ISOLATED_MARGIN"
    return core


def live_trade(symbol="BTCUSDT", side="Buy", entry=100.0, sl="98", tp="104", qty="2", key=None):
    t = tc.TradeState(symbol, side, key or f"oid-{symbol}")
    t.phase = tc.TradeState.PHASE_LIVE
    t.entry_price = t.fill_price = entry
    t.qty = t.entry_qty = t.cum_exec_qty = qty
    t.stop_loss, t.take_profit = sl, tp
    t.leverage = "20"
    t.live_since = time.time() - 60
    t.original_sl_distance = abs(entry - float(sl))
    return t


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

    def test_reconcile_journal_attaches_r_only_to_new_records(self):
        with tempfile.TemporaryDirectory() as d:
            core = make_core()
            core.journal = TradeJournal(os.path.join(d, "j.json"))
            core._reconcile_lock = threading.Lock()
            core._risk_ledger = [{"symbol": "BTCUSDT", "entry_price": 100.0, "risk_usd": 10.0,
                                  "opened_at": 1.0, "strat1": False, "sl": 98.0, "tp": 104.0,
                                  "link_id": "l", "closing_order_ids": ["c-1"], "ts": time.time(),
                                  "mfe": 103.0, "mae": 99.0}]
            record = {"orderId": "c-1", "symbol": "BTCUSDT", "side": "Sell", "avgEntryPrice": "100",
                      "avgExitPrice": "102", "closedPnl": "3.9", "updatedTime": "2000000", "qty": "2"}
            core.fetch_closed_pnl = lambda start_ms: [core._normalize_closed_pnl(record)]
            self.assertEqual(core.reconcile_journal(), 1)
            row = core.journal.all_trades[0]
            self.assertEqual((row["side"], row["r_multiple"], row["mfe_r"], row["mae_r"]), ("Buy", 0.39, 1.5, -0.5))
            self.assertEqual(core._risk_ledger, [])                 # consumed
            self.assertEqual(core.reconcile_journal(), 0)           # deduped on the second pass


class TestFormatValue(unittest.TestCase):
    def test_rounding(self):
        self.assertEqual(tc.format_value(64951.37, "0.10"), "64951.40")
        self.assertEqual(tc.format_value(0.01437, "0.001", round_down=True), "0.014")
        self.assertEqual(tc.format_value(0.0199999, "0.0001"), "0.0200")


class TestExecution(SetFees):
    def test_payload_carries_the_partial_bracket(self):
        core = make_core()
        calc = core.calculate_trade("BTCUSDT", "Buy", 74300, 73300, 50)
        p = tc.TradingCore.build_bracket_payload(calc, 75300, True, "link-1")
        self.assertEqual((p["orderType"], p["tpslMode"], p["timeInForce"]), ("Limit", "Partial", "PostOnly"))
        self.assertEqual((p["stopLoss"], p["slOrderType"], p["slTriggerBy"]), ("73300.00", "Market", config.SL_TRIGGER_BY))
        self.assertEqual((p["takeProfit"], p["tpLimitPrice"], p["tpOrderType"]), ("74800.00", "75300.00", "Limit"))
        self.assertEqual(p["orderLinkId"], "link-1")
        self.assertNotIn("takeProfit", tc.TradingCore.build_bracket_payload(calc, None, False, "x"))

    def test_execute_registers_the_trade_and_sets_leverage_first(self):
        client = StubClient()
        core = make_core(client)
        trade = core.execute_bracket("BTCUSDT", "Buy", 74300, 73300, 75300, 50, post_only=True)
        names = [c[0] for c in client.calls]
        self.assertEqual(names, ["set_leverage", "place_order"])
        self.assertEqual(client.calls[0][1]["buyLeverage"], "48")
        self.assertEqual(trade.entry_order_id, "oid-1")
        self.assertIn("oid-1", core._trades)
        self.assertEqual(trade.take_profit, "75300.00")
        self.assertEqual(len(core._risk_ledger), 1)

    def test_execute_aborts_when_leverage_cannot_be_set(self):
        """Sending the order anyway would use whatever leverage the symbol last had."""
        client = StubClient(leverage_error="110012 leverage exceeds limit")
        core = make_core(client)
        with self.assertRaises(ValueError):
            core.execute_bracket("BTCUSDT", "Buy", 74300, 73300, 75300, 50)
        self.assertEqual([c[0] for c in client.calls], ["set_leverage"])
        self.assertEqual(core._trades, {})

    def test_execute_refuses_a_second_trade_on_the_symbol(self):
        core = make_core()
        core._trades["x"] = live_trade("BTCUSDT")
        with self.assertRaises(ValueError):
            core.execute_bracket("BTCUSDT", "Buy", 74300, 73300, 75300, 50)

    def test_set_margin_mode_refuses_with_open_positions(self):
        client = StubClient(positions=[{"symbol": "BTCUSDT", "size": "1"}])
        core = make_core(client)
        with self.assertRaises(ValueError):
            core.set_margin_mode(isolated=False)
        self.assertNotIn("set_margin_mode", [c[0] for c in client.calls])
        core.client = StubClient()
        self.assertEqual(core.set_margin_mode(isolated=False), "REGULAR_MARGIN")


class TestReconciliation(SetFees):
    POS = {"symbol": "BTCUSDT", "size": "2", "avgPrice": "100", "unrealisedPnl": "6", "markPrice": "103",
           "leverage": "20", "liqPrice": "95.5", "side": "Buy", "takeProfit": "0", "stopLoss": "0"}
    CHILDREN = [{"symbol": "BTCUSDT", "stopOrderType": "PartialTakeProfit", "triggerPrice": "102"},
                {"symbol": "BTCUSDT", "stopOrderType": "PartialStopLoss", "triggerPrice": "97.5"},
                {"symbol": "BTCUSDT", "stopOrderType": "", "triggerPrice": ""}]

    def test_bracket_map(self):
        self.assertEqual(tc.TradingCore._bracket_map(self.CHILDREN), {"BTCUSDT": {"tp": "102", "sl": "97.5"}})

    def test_refresh_positions_updates_live_fields_and_bracket(self):
        core = make_core(StubClient(positions=[self.POS], orders=self.CHILDREN))
        t = live_trade("BTCUSDT")
        core._trades[t.entry_order_id] = t
        core.refresh_positions()
        self.assertEqual((t.mark_price, t.unrealised_pnl, t.liq_price), (103.0, 6.0, 95.5))
        self.assertEqual((t.take_profit, t.stop_loss), ("102", "97.5"))
        self.assertEqual(t.mfe_price, 103.0)
        self.assertTrue(t.is_live)

    def test_refresh_positions_closes_a_vanished_position_but_spares_a_fresh_fill(self):
        core = make_core(StubClient(positions=[], orders=[]))
        old = live_trade("BTCUSDT", key="old")
        fresh = live_trade("ETHUSDT", key="fresh")
        fresh.live_since = time.time()
        core._trades.update({"old": old, "fresh": fresh})
        core.refresh_positions()
        self.assertEqual(old.phase, tc.TradeState.PHASE_CLOSED)
        self.assertEqual(old.close_type, "closed externally")
        self.assertTrue(fresh.is_live)

    def test_sync_existing_rebuilds_live_and_pending_trades(self):
        resting = {"orderId": "o-2", "symbol": "ETHUSDT", "side": "Sell", "orderStatus": "New",
                   "price": "3900", "qty": "1.2", "cumExecQty": "0", "avgPrice": "", "orderLinkId": "vir_x",
                   "stopOrderType": ""}
        core = make_core(StubClient(positions=[self.POS], orders=self.CHILDREN + [resting]))
        active = core.sync_existing()
        by_symbol = {t.symbol: t for t in active}
        self.assertEqual(set(by_symbol), {"BTCUSDT", "ETHUSDT"})
        btc, eth = by_symbol["BTCUSDT"], by_symbol["ETHUSDT"]
        self.assertEqual((btc.phase, btc.fill_price, btc.qty, btc.liq_price), ("LIVE", 100.0, "2", 95.5))
        self.assertEqual((btc.take_profit, btc.stop_loss), ("102", "97.5"))
        self.assertEqual((eth.phase, eth.entry_price, eth.order_link_id), ("PENDING", 3900.0, "vir_x"))

    def test_position_event_closes_and_prices_the_trade(self):
        core = make_core()
        t = live_trade("BTCUSDT")
        core._trades[t.entry_order_id] = t
        core._handle_position_event({"data": [{**self.POS, "size": "0", "markPrice": "104"}]})
        self.assertEqual(t.phase, tc.TradeState.PHASE_CLOSED)
        self.assertEqual(t.close_price, 104.0)
        self.assertAlmostEqual(t.pnl, (104 - 100) * 2 - 100 * 2 * MAKER - 104 * 2 * TAKER, places=6)
        self.assertEqual(core._queue.items[0][0], core._update_risk_intent)

    def test_order_event_fill_sets_live_since_and_tp_child_closes(self):
        core = make_core()
        t = tc.TradeState("BTCUSDT", "Buy", "link-1")
        t.order_link_id = "link-1"
        t.entry_price = 100.0
        core._trades["link-1"] = t
        core._subscribe_ticker = lambda _s: None
        core._handle_order_event({"data": [{"orderId": "oid-9", "orderLinkId": "link-1", "orderStatus": "Filled",
                                            "stopOrderType": "", "symbol": "BTCUSDT", "avgPrice": "100.5",
                                            "cumExecQty": "2", "qty": "2"}]})
        self.assertEqual((t.phase, t.fill_price, t.qty), ("LIVE", 100.5, "2"))
        self.assertIsNotNone(t.live_since)
        self.assertIn("oid-9", core._trades)
        core._handle_order_event({"data": [{"orderId": "tp-1", "orderStatus": "Filled", "symbol": "BTCUSDT",
                                            "stopOrderType": "PartialTakeProfit", "avgPrice": "104"}]})
        self.assertEqual((t.phase, t.close_type, t.close_price), ("CLOSED", "PartialTakeProfit", 104.0))
        self.assertEqual(core._queue.items[-1][0], core._note_closing_order)


if __name__ == "__main__":
    unittest.main()
