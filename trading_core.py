"""
Bybit V5 OTOCO Execution Core.

Responsibilities:
  1. Risk-math engine   — SL distance → safe leverage → position qty
  2. Atomic execution   — single REST call with Limit Entry + Limit TP + Market SL
  3. WebSocket tracker  — private order stream fires callbacks on fill / close
  4. Live amendment     — modify TP/SL on pending OR active trades
  5. Cancel             — cancel pending entry (which also kills dormant TP/SL)
"""

import copy
import json
import math
import queue
import threading
import time
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Callable, Dict, List, Optional
from uuid import uuid4

from pybit.unified_trading import HTTP, WebSocket

import config
from cache_engine import InstrumentCache


# ──────────────────────────────────────────────────────────────────
#  Value Formatter — quantises to exchange tick / step rules
# ──────────────────────────────────────────────────────────────────

def format_value(value: float, step_str: str, round_down: bool = False) -> str:
    """
    Round *value* to the nearest valid increment defined by *step_str*.

    For quantities we always round DOWN so we never exceed calculated risk.
    For prices we round to nearest.

    Examples:
        format_value(64951.37, "0.10")         -> "64951.40"
        format_value(0.01437,  "0.001", True)  -> "0.014"
    """
    step = Decimal(step_str)
    val = Decimal(str(value))

    if round_down:
        rounded = (val / step).quantize(Decimal("1"), rounding=ROUND_DOWN) * step
    else:
        rounded = (val / step).quantize(Decimal("1")) * step

    # Derive precision from the step string itself
    if "." in step_str:
        precision = len(step_str.split(".")[1])
    else:
        precision = 0

    return f"{float(rounded):.{precision}f}"


# ──────────────────────────────────────────────────────────────────
#  Trade State — tracks one active bracket lifecycle
# ──────────────────────────────────────────────────────────────────

class TradeState:
    """Immutable-ish snapshot of a tracked trade's lifecycle."""

    PHASE_PENDING = "PENDING"     # Entry limit resting on the book
    PHASE_PARTIAL = "PARTIAL"     # Entry partially filled; remaining qty still resting
    PHASE_LIVE    = "LIVE"        # Entry filled, TP/SL active
    PHASE_CLOSED  = "CLOSED"     # TP or SL triggered, trade done
    PHASE_CANCELLED = "CANCELLED"  # Entry cancelled before fill

    def __init__(self, symbol: str, side: str, entry_order_id: str):
        self.symbol = symbol
        self.side = side
        self.entry_order_id = entry_order_id
        self.order_link_id: Optional[str] = None
        self.phase = self.PHASE_PENDING
        self.entry_price: Optional[float] = None
        self.fill_price: Optional[float] = None
        self.close_type: Optional[str] = None   # "TakeProfit" | "StopLoss" | "Manual"
        self.close_price: Optional[float] = None
        self.pnl: Optional[float] = None
        self.entry_qty: Optional[str] = None
        self.cum_exec_qty: Optional[str] = None
        self.leaves_qty: Optional[str] = None
        # Live PnL fields (updated by ticker + position streams)
        self.unrealised_pnl: Optional[float] = None
        self.mark_price: Optional[float] = None
        # Execution-stream truth: did the entry actually fill maker?
        self.entry_is_maker: Optional[bool] = None
        self.entry_fee_actual: Optional[float] = None
        self.position_value: Optional[float] = None
        self.qty: Optional[str] = None
        self.leverage: Optional[str] = None
        self.take_profit: Optional[str] = None
        self.stop_loss: Optional[str] = None
        # Strat1: smart trailing SL
        self.strat1_enabled: bool = False
        self.strat1_phase: int = 0            # 0=inactive, 1=75% tightened, 2=90% breakeven
        self.original_sl_distance: Optional[float] = None  # abs(entry - sl) at creation
        self.original_tp: Optional[float] = None            # TP value at creation
        # Timestamps
        self.opened_at: Optional[float] = None  # time.time() when trade was created
        self.risk_usd: Optional[float] = None    # dollar risk at SL for R-multiple

    @property
    def is_active(self) -> bool:
        return self.phase in (self.PHASE_PENDING, self.PHASE_PARTIAL, self.PHASE_LIVE)

    def clone(self) -> "TradeState":
        """Return a thread-safe shallow copy of this TradeState."""
        return copy.copy(self)


# ──────────────────────────────────────────────────────────────────
#  Core Engine
# ──────────────────────────────────────────────────────────────────

class TradingCore:
    """
    Event-driven execution engine.

    Connects to Bybit V5 REST + Private WebSocket.
    Fires user-supplied callbacks on state transitions so the GUI
    can update without polling.
    """

    def __init__(
        self,
        on_log: Optional[Callable[[str, bool], None]] = None,
        on_trade_update: Optional[Callable[[TradeState], None]] = None,
        on_cache_complete: Optional[Callable[[int], None]] = None,
        on_balance: Optional[Callable[[float], None]] = None,
    ):
        # Callbacks
        self._on_log = on_log or (lambda msg, err: print(f"[{'!' if err else '>'}] {msg}"))
        self._on_trade_update = on_trade_update or (lambda _: None)
        self._on_balance = on_balance or (lambda _: None)

        # REST client
        self.client = HTTP(
            testnet=config.USE_TESTNET,
            api_key=config.API_KEY,
            api_secret=config.API_SECRET,
        )

        self.account_margin_mode = "REGULAR_MARGIN"

        # Instrument cache (loads from disk or fetches once)
        self.log("Booting Instrument Cache…")
        
        def handle_cache_done(count):
            if count > 0:
                self.log(f"Cache ready: {count} linear perpetuals indexed.")
            else:
                self.log("Cache download failed.", is_error=True)
            if on_cache_complete:
                on_cache_complete(count)

        self.cache = InstrumentCache(self.client, on_complete=handle_cache_done)

        # Active trades keyed by entry orderId
        self._trades: Dict[str, TradeState] = {}
        self._lock = threading.RLock()

        # WebSocket (private order stream + public ticker)
        self._ws: Optional[WebSocket] = None
        self._ws_public: Optional[WebSocket] = None
        self._ws_connected = False
        self._subscribed_tickers: set = set()
        # Last ticker snapshot per symbol (lastPrice/markPrice/bid1/ask1),
        # merged across delta pushes — feeds the pre-trade price display.
        self._last_ticker: Dict[str, dict] = {}

        # Strat1 rate limiter: symbol → last amendment timestamp
        self._strat1_last_amend: Dict[str, float] = {}

        # Risk ledger: remembers intended $ risk per app-placed trade so the
        # journal can attach R-multiples (matched on symbol + entry price).
        self._risk_ledger_lock = threading.Lock()
        self._risk_ledger: List[dict] = self._load_risk_ledger()

        # Worker queue for asynchronous REST operations (like Strat1 amendments)
        self._queue = queue.Queue()
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker_thread.start()

    def _worker_loop(self):
        """Worker thread to run REST operations without blocking WebSockets."""
        while True:
            try:
                task = self._queue.get()
                if task is None:
                    break
                func, args, kwargs = task
                try:
                    func(*args, **kwargs)
                except Exception as e:
                    self.log(f"Background execution failed: {e}", is_error=True)
            except Exception as e:
                self.log(f"Worker thread error: {e}", is_error=True)
            finally:
                self._queue.task_done()

    def _execute_strat1_amend(
        self, symbol: str, str_sl: str, new_phase: int, trade_entry_order_id: str,
        old_phase: int, old_sl: Optional[str]
    ):
        """Perform actual Strat1 REST API update on the background worker thread."""
        with self._lock:
            trade = self._trades.get(trade_entry_order_id)
            if not trade or trade.phase not in (
                TradeState.PHASE_PARTIAL,
                TradeState.PHASE_LIVE,
            ):
                self.log(f"strat1 [{symbol}]: trade no longer live, skipping amend")
                return
        try:
            open_orders = self._get_open_orders_all(symbol=symbol)
            amended = False
            for order in open_orders:
                sot = order.get("stopOrderType", "")
                if "StopLoss" in sot:
                    self.client.amend_order(
                        category="linear",
                        symbol=symbol,
                        orderId=order["orderId"],
                        triggerPrice=str_sl,
                    )
                    amended = True
                    break

            if not amended:
                raise ValueError(
                    "no existing StopLoss child order found; refusing one-sided "
                    "Partial set_trading_stop add"
                )

            snapshot = None
            link_id = None
            entry_ref = 0.0
            with self._lock:
                trade = self._trades.get(trade_entry_order_id)
                if trade:
                    trade.strat1_phase = new_phase
                    trade.stop_loss = str_sl
                    link_id = trade.order_link_id
                    entry_ref = trade.entry_price or trade.fill_price or 0.0
                    snapshot = trade.clone()
            if snapshot:
                self._on_trade_update(snapshot)
            self._update_risk_intent_phase(symbol, entry_ref, link_id, new_phase)
            self.log(f"strat1 [{symbol}]: SL successfully amended at {str_sl}")

        except Exception as e:
            self.log(f"strat1 amend failed for {symbol}: {e}", is_error=True)
            snapshot = None
            with self._lock:
                trade = self._trades.get(trade_entry_order_id)
                if trade and trade.phase in (
                    TradeState.PHASE_PARTIAL,
                    TradeState.PHASE_LIVE,
                ) and trade.strat1_phase == new_phase:
                    trade.strat1_phase = old_phase
                    trade.stop_loss = old_sl
                    snapshot = trade.clone()
                self._strat1_last_amend.pop(symbol, None)
            if snapshot:
                self._on_trade_update(snapshot)

    def get_account_margin_mode(self) -> str:
        """Query current margin mode from Bybit (REGULAR_MARGIN or ISOLATED_MARGIN)."""
        try:
            res = self.client.get_account_info()
            return res.get("result", {}).get("marginMode", "REGULAR_MARGIN")
        except Exception:
            return "REGULAR_MARGIN"

    def has_active_positions_or_orders(self) -> bool:
        """Check Bybit for any open positions or resting orders on the account."""
        try:
            pos_res = self.client.get_positions(category="linear", settleCoin="USDT")
            for pos in pos_res.get("result", {}).get("list", []):
                if float(pos.get("size", "0")) > 0:
                    return True
            
            if self._get_open_orders_all():
                return True
        except Exception as e:
            self.log(f"Error checking active positions/orders: {e}", is_error=True)
        return False

    # ─── Logging Helper ───────────────────────────────────────────

    def log(self, msg: str, is_error: bool = False):
        self._on_log(msg, is_error)

    @staticmethod
    def _new_order_link_id() -> str:
        """Return a unique Bybit-compatible client order ID (max 36 chars)."""
        return f"vir_{int(time.time() * 1000)}_{uuid4().hex[:8]}"

    def _find_trade_for_order(self, order: dict) -> tuple[Optional[str], Optional[TradeState]]:
        """Find a tracked trade by Bybit orderId or our orderLinkId."""
        oid = order.get("orderId", "")
        link_id = order.get("orderLinkId", "")

        if oid and oid in self._trades:
            return oid, self._trades[oid]
        if link_id and link_id in self._trades:
            return link_id, self._trades[link_id]

        for key, trade in self._trades.items():
            if oid and trade.entry_order_id == oid:
                return key, trade
            if link_id and trade.order_link_id == link_id:
                return key, trade
        return None, None

    def _rekey_trade(self, old_key: Optional[str], order_id: str, trade: TradeState) -> None:
        """Move a pre-registered orderLinkId keyed trade to Bybit's orderId."""
        if not order_id:
            return
        trade.entry_order_id = order_id
        if old_key and old_key != order_id and old_key in self._trades:
            self._trades.pop(old_key, None)
            self._trades[order_id] = trade
        elif order_id not in self._trades:
            self._trades[order_id] = trade

    def _get_open_orders_all(self, symbol: Optional[str] = None) -> List[dict]:
        """Return all open linear orders, following Bybit pagination."""
        orders: List[dict] = []
        cursor = ""
        for _ in range(20):
            kwargs = {"category": "linear", "limit": 50}
            if symbol:
                kwargs["symbol"] = symbol
            else:
                kwargs["settleCoin"] = "USDT"
            if cursor:
                kwargs["cursor"] = cursor

            res = self.client.get_open_orders(**kwargs)
            result = res.get("result", {})
            orders.extend(result.get("list", []))

            cursor = result.get("nextPageCursor", "")
            if not cursor:
                break
        return orders

    # ─── Sync existing positions/orders on boot ──────────────────

    def _restore_strat1(self, trade: TradeState) -> None:
        """
        Re-arm strat1 on a synced trade from the persisted risk ledger.

        Without this, an app restart silently disables trailing on trades
        that were opened with strat1. Matched by orderLinkId when available
        (resting entries), else symbol + entry price (live positions, whose
        synthetic IDs carry no link). Non-destructive: the ledger entry is
        still consumed later by the journal on close.
        """
        entry_ref = trade.fill_price or trade.entry_price or 0.0
        intent = self._peek_risk_intent(trade.symbol, entry_ref,
                                        link_id=trade.order_link_id)
        if not intent:
            return
        trade.risk_usd = intent.get("risk_usd") or trade.risk_usd
        trade.opened_at = intent.get("opened_at") or trade.opened_at
        if not intent.get("strat1"):
            return
        tp, sl, ep = intent.get("tp"), intent.get("sl"), intent.get("entry_price")
        if not tp or not sl or not ep:
            self.log(
                f"strat1 [{trade.symbol}]: ledger entry predates SL/TP persistence — "
                "trailing NOT restored; re-place the trade to re-arm it.",
                is_error=True,
            )
            return
        trade.strat1_enabled = True
        trade.strat1_phase = int(intent.get("strat1_phase") or 0)
        trade.original_tp = float(tp)
        trade.original_sl_distance = abs(float(ep) - float(sl))
        self.log(f"strat1 restored for {trade.symbol} (phase {trade.strat1_phase})")

    def sync_existing(self) -> List[TradeState]:
        """
        Query Bybit for open positions and pending orders.
        Creates TradeState objects so the GUI shows them on startup,
        even if they were placed outside this app or in a previous session.
        """
        synced: List[TradeState] = []

        # 1. Open positions (LIVE trades)
        try:
            res = self.client.get_positions(
                category="linear", settleCoin="USDT"
            )
            for pos in res["result"]["list"]:
                size = float(pos.get("size", "0"))
                if size == 0:
                    continue

                symbol = pos["symbol"]
                side = pos.get("side", "Buy")
                avg_price = float(pos.get("avgPrice", "0"))
                unrealised = float(pos.get("unrealisedPnl", "0"))
                mark = pos.get("markPrice", "0")

                # Use a synthetic order ID for positions we didn't place
                synthetic_id = f"sync_pos_{symbol}"

                trade = TradeState(symbol=symbol, side=side, entry_order_id=synthetic_id)
                trade.phase = TradeState.PHASE_LIVE
                trade.entry_price = avg_price
                trade.fill_price = avg_price
                trade.unrealised_pnl = unrealised
                trade.mark_price = float(mark) if mark else None
                trade.qty = pos.get("size")
                trade.entry_qty = pos.get("size")
                trade.cum_exec_qty = pos.get("size")
                trade.leaves_qty = "0"
                trade.leverage = pos.get("leverage", "")
                trade.position_value = float(pos.get("positionValue", "0")) or None
                tp_val = pos.get("takeProfit", "")
                sl_val = pos.get("stopLoss", "")
                trade.take_profit = tp_val if tp_val and tp_val != "0" else None
                trade.stop_loss = sl_val if sl_val and sl_val != "0" else None

                if str(pos.get("positionIdx", "0")) != "0":
                    self.log(
                        f"{symbol}: hedge-mode position (positionIdx="
                        f"{pos.get('positionIdx')}) — Viridis assumes one-way mode; "
                        "orders on this symbol will fail until the account is "
                        "switched back.",
                        is_error=True,
                    )

                self._restore_strat1(trade)

                with self._lock:
                    self._trades[synthetic_id] = trade
                synced.append(trade)
                self.log(f"synced position: {symbol} {side} {size} @ {avg_price}")

        except Exception as e:
            self.log(f"position sync failed: {e}", is_error=True)

        # 2. Pending orders (resting on the book)
        orders_list = []
        try:
            orders_list = self._get_open_orders_all()
            for order in orders_list:
                status = order.get("orderStatus", "")
                stop_type = order.get("stopOrderType", "")

                # Only sync entry orders, not spawned TP/SL conditionals
                if status in ("New", "PartiallyFilled") and stop_type == "":
                    oid = order["orderId"]
                    symbol = order["symbol"]
                    side = order["side"]
                    price = order.get("price", "0")
                    is_partial = status == "PartiallyFilled"

                    # Skip if we already track this order
                    if oid in self._trades:
                        continue

                    trade = None
                    reused_synced_position = False
                    if is_partial:
                        synthetic_id = f"sync_pos_{symbol}"
                        with self._lock:
                            trade = self._trades.pop(synthetic_id, None)
                        reused_synced_position = trade is not None

                    if trade is None:
                        trade = TradeState(symbol=symbol, side=side, entry_order_id=oid)

                    trade.entry_order_id = oid
                    trade.phase = TradeState.PHASE_PARTIAL if is_partial else TradeState.PHASE_PENDING
                    trade.entry_price = float(price) if price and float(price) > 0 else None
                    trade.order_link_id = order.get("orderLinkId", "") or None
                    trade.entry_qty = order.get("qty")
                    trade.cum_exec_qty = order.get("cumExecQty")
                    trade.leaves_qty = order.get("leavesQty")
                    if is_partial and trade.cum_exec_qty:
                        trade.qty = trade.cum_exec_qty
                    avg_price = order.get("avgPrice") or ""
                    if avg_price and float(avg_price) > 0:
                        trade.fill_price = float(avg_price)

                    # Re-arm strat1 on resting/partial entries too, so an entry
                    # that fills after a restart still gets its trailing logic.
                    if not reused_synced_position:
                        self._restore_strat1(trade)

                    with self._lock:
                        self._trades[oid] = trade
                    if not reused_synced_position:
                        synced.append(trade)
                    phase_msg = "partial" if is_partial else "pending"
                    self.log(f"synced order: {symbol} {side} @ {price} ({phase_msg})")

        except Exception as e:
            self.log(f"order sync failed: {e}", is_error=True)

        # 3. Attach TP/SL from conditional orders to live trades
        #    With tpslMode=Partial, TP/SL are separate orders, not on the position.
        if orders_list:
            try:
                for order in orders_list:
                    sot = order.get("stopOrderType", "")
                    symbol = order.get("symbol", "")
                    trigger = order.get("triggerPrice", "")

                    if sot in ("TakeProfit", "StopLoss", "PartialTakeProfit", "PartialStopLoss") and trigger:
                        for trade in synced:
                            if trade.symbol == symbol and trade.phase in (
                                TradeState.PHASE_PARTIAL,
                                TradeState.PHASE_LIVE,
                            ):
                                if "TakeProfit" in sot:
                                    trade.take_profit = trigger
                                elif "StopLoss" in sot:
                                    trade.stop_loss = trigger
                                break
            except Exception as e:
                self.log(f"tp/sl sync failed: {e}", is_error=True)

        return synced

    # ─── WebSocket Lifecycle ──────────────────────────────────────

    def connect_websocket(self) -> None:
        """Spin up private + public WebSockets in background threads."""
        if self._ws_connected:
            return

        def _connect_private():
            try:
                self._ws = WebSocket(
                    testnet=config.USE_TESTNET,
                    channel_type="private",
                    api_key=config.API_KEY,
                    api_secret=config.API_SECRET,
                )
                self._ws.order_stream(callback=self._handle_order_event)
                self._ws.position_stream(callback=self._handle_position_event)
                self._ws.execution_stream(callback=self._handle_execution_event)
                self._ws.wallet_stream(callback=self._handle_wallet_event)
                self._ws_connected = True
                self.log("private stream connected")
            except Exception as e:
                self.log(f"private ws failed: {e}", is_error=True)

        def _connect_public():
            try:
                self._ws_public = WebSocket(
                    testnet=config.USE_TESTNET,
                    channel_type="linear",
                )
                # Subscribe to tickers for any existing live positions
                with self._lock:
                    symbols = [t.symbol for t in self._trades.values()
                               if t.phase in (TradeState.PHASE_PARTIAL, TradeState.PHASE_LIVE)]
                for sym in symbols:
                    self._subscribe_ticker(sym)
                self.log("ticker stream connected")
            except Exception as e:
                self.log(f"public ws failed: {e}", is_error=True)

        threading.Thread(target=_connect_private, daemon=True).start()
        threading.Thread(target=_connect_public, daemon=True).start()

    def watch_symbol(self, symbol: str) -> None:
        """Subscribe a symbol's ticker for the pre-trade price display."""
        self._subscribe_ticker(symbol)

    def get_ticker(self, symbol: str) -> dict:
        """Last merged ticker snapshot for a symbol ({} if never seen)."""
        with self._lock:
            return dict(self._last_ticker.get(symbol, {}))

    def sync_fee_rates(self) -> None:
        """
        Adopt the account's actual maker/taker rates (fee tier can drift from
        config). Sizing, breakeven, and PnL estimates all read config at call
        time, so overriding here keeps everything honest at once.
        """
        try:
            res = self.client.get_fee_rates(category="linear", symbol="BTCUSDT")
            row = res["result"]["list"][0]
            maker = float(row["makerFeeRate"])
            taker = float(row["takerFeeRate"])
            if abs(maker - config.FEE_MAKER) > 1e-9 or abs(taker - config.FEE_TAKER) > 1e-9:
                self.log(
                    f"fee rates synced from account: maker {maker*100:.4f}% / "
                    f"taker {taker*100:.4f}% (config had {config.FEE_MAKER*100:.4f}% / "
                    f"{config.FEE_TAKER*100:.4f}%)"
                )
                config.FEE_MAKER = maker
                config.FEE_TAKER = taker
        except Exception as e:
            self.log(f"fee rate sync failed (using config defaults): {e}", is_error=True)

    def _handle_wallet_event(self, message: dict) -> None:
        """Private wallet stream → live equity (pushes on fills/funding/transfers)."""
        for acct in message.get("data", []):
            te = acct.get("totalEquity", "")
            if te:
                try:
                    self._on_balance(float(te))
                except ValueError:
                    pass

    def _handle_execution_event(self, message: dict) -> None:
        """
        Private execution stream — ground truth per fill. Used to verify the
        maker assumption on entries: sizing prices the entry leg at the maker
        rate, so a taker fill (marketable limit) means actual risk ran slightly
        above plan. We track actual fees and shout when the assumption broke.
        """
        for ex in message.get("data", []):
            if ex.get("execType", "Trade") != "Trade":
                continue
            taker_entry = None
            with self._lock:
                # Only entry orders are tracked by orderId/orderLinkId, so a
                # match here is an entry fill (TP/SL children have their own ids).
                _, trade = self._find_trade_for_order(ex)
                if not trade:
                    continue
                fee = float(ex.get("execFee", "0") or 0)
                trade.entry_fee_actual = (trade.entry_fee_actual or 0.0) + fee
                maker = bool(ex.get("isMaker"))
                trade.entry_is_maker = maker if trade.entry_is_maker is None \
                    else (trade.entry_is_maker and maker)
                if not maker:
                    planned = 0.0
                    if trade.entry_price and ex.get("execQty"):
                        planned = trade.entry_price * float(ex["execQty"]) * config.FEE_MAKER
                    taker_entry = (trade.symbol, fee, planned)
            if taker_entry:
                sym, fee, planned = taker_entry
                self.log(
                    f"⚠ {sym}: entry filled as TAKER — fee ${fee:.4f} vs "
                    f"~${planned:.4f} planned (maker). Actual risk slightly above plan.",
                    is_error=True,
                )

    def ws_health(self) -> tuple:
        """
        (private_ok, public_ok) — actual socket state, not the boot-time flag.
        pybit auto-reconnects and resubscribes; this just tells the GUI the truth
        while that's happening so "live" is never cosmetic.
        """
        def _ok(ws) -> bool:
            if ws is None:
                return False
            try:
                return bool(ws.is_connected())
            except Exception:
                return False
        return _ok(self._ws), _ok(self._ws_public)

    def _subscribe_ticker(self, symbol: str):
        """Subscribe to a symbol's public ticker for live mark price."""
        with self._lock:
            if symbol in self._subscribed_tickers:
                return
        if self._ws_public is None:
            # Public WS not up yet. Don't mark as subscribed — _connect_public
            # subscribes all active symbols once it connects, and leaving it
            # unmarked lets a later order event retry the subscription.
            return
        try:
            self._ws_public.ticker_stream(
                symbol=symbol,
                callback=self._handle_ticker_event,
            )
            with self._lock:
                self._subscribed_tickers.add(symbol)
        except Exception as e:
            self.log(f"ticker sub failed for {symbol}: {e}", is_error=True)

    def _handle_ticker_event(self, message: dict) -> None:
        """
        Public ticker fires on every price tick.
        Compute PnL locally from mark price + our known position.
        """
        data = message.get("data", {})
        symbol = data.get("symbol", "")
        if not symbol:
            return

        # Merge into the price cache (deltas only carry changed fields).
        with self._lock:
            cached = self._last_ticker.setdefault(symbol, {})
            for k in ("lastPrice", "markPrice", "bid1Price", "ask1Price"):
                v = data.get(k)
                if v:
                    cached[k] = v

        mark_str = data.get("markPrice")
        if not mark_str:
            return
        mark = float(mark_str)

        snapshot = None
        strat1_snapshot = None
        with self._lock:
            for trade in self._trades.values():
                if trade.symbol == symbol and trade.phase in (
                    TradeState.PHASE_PARTIAL,
                    TradeState.PHASE_LIVE,
                ):
                    trade.mark_price = mark
                    if trade.fill_price and trade.qty:
                        qty = float(trade.qty)
                        if trade.side == "Buy":
                            trade.unrealised_pnl = (mark - trade.fill_price) * qty
                        else:
                            trade.unrealised_pnl = (trade.fill_price - mark) * qty
                    snapshot = trade.clone()

                    # ── Strat1: Smart trailing SL ─────────────────
                    if trade.strat1_enabled and trade.strat1_phase < 2:
                        strat1_snapshot = self._evaluate_strat1(trade, mark)
                    break

        if strat1_snapshot:
            self._on_trade_update(strat1_snapshot)
        elif snapshot:
            self._on_trade_update(snapshot)

    def _evaluate_strat1(self, trade: TradeState, mark: float) -> Optional[TradeState]:
        """
        Strat1 smart trailing: auto-tighten SL based on progress toward TP.

        75% of entry→TP journey: SL = entry ± (original_sl_distance / 2)
        90% of entry→TP journey: SL = entry (breakeven)

        Rate-limited to max 1 amendment per 5 seconds per symbol.
        Returns a clone if SL was amended, otherwise None.

        Note: Caller must hold self._lock.
        """
        entry = trade.fill_price
        tp = trade.original_tp
        sl_dist = trade.original_sl_distance

        if not entry or not tp or not sl_dist or tp == entry:
            return

        # Compute progress: 0 = at entry, 1 = at TP
        if trade.side == "Buy":
            progress = (mark - entry) / (tp - entry)
        else:
            progress = (entry - mark) / (entry - tp)

        if progress < 0.75:
            return  # Not in territory yet

        # Rate limit: max 1 amend per 5s per symbol
        now = time.time()
        last = self._strat1_last_amend.get(trade.symbol, 0)
        if now - last < 5:
            return

        rules = self.cache.get(trade.symbol)
        if not rules:
            return
        tick = rules["tickSize"]

        new_phase = trade.strat1_phase
        new_sl = None

        if progress >= 0.90 and trade.strat1_phase < 2:
            # 90%+ → TRUE breakeven: offset SL just past entry to cover the
            # round-trip fees (maker entry + taker market-SL exit), so a stop-out
            # here nets ~0 instead of a small fee-sized loss.
            if trade.side == "Buy":
                new_sl = entry * (1 + config.FEE_MAKER) / (1 - config.FEE_TAKER)
            else:
                new_sl = entry * (1 - config.FEE_MAKER) / (1 + config.FEE_TAKER)
            new_phase = 2
            self.log(f"strat1 [{trade.symbol}]: 90% reached → SL to true breakeven ({new_sl:.6f})")

        elif progress >= 0.75 and trade.strat1_phase < 1:
            # 75%+ → half the original SL distance from entry
            if trade.side == "Buy":
                new_sl = entry - (sl_dist / 2)
            else:
                new_sl = entry + (sl_dist / 2)
            new_phase = 1
            self.log(f"strat1 [{trade.symbol}]: 75% reached → SL tightened to {new_sl:.6f}")

        if new_sl is not None:
            str_sl = format_value(new_sl, tick)
            old_phase = trade.strat1_phase
            old_sl = trade.stop_loss
            
            # Update local state immediately to block redundant ticker triggers
            trade.strat1_phase = new_phase
            trade.stop_loss = str_sl
            self._strat1_last_amend[trade.symbol] = now
            
            # Dispatch network call to the background worker thread queue
            self._queue.put((
                self._execute_strat1_amend,
                (trade.symbol, str_sl, new_phase, trade.entry_order_id, old_phase, old_sl),
                {}
            ))
            return trade.clone()
        return None

    def _handle_order_event(self, message: dict) -> None:
        """
        Bybit pushes order updates here.  We care about two transitions:
          A) Our Entry Limit goes from New/PartiallyFilled → Filled
          B) A spawned TP/SL conditional goes to Filled (trade closed)
          C) Our Entry gets Cancelled / Deactivated
        """
        for order in message.get("data", []):
            oid = order.get("orderId", "")
            status = order.get("orderStatus", "")
            stop_type = order.get("stopOrderType", "")
            symbol = order.get("symbol", "")
            avg_price = order.get("avgPrice") or order.get("price") or "0"
            cum_exec_qty = order.get("cumExecQty", "")
            leaves_qty = order.get("leavesQty", "")

            snapshot = None
            need_ticker_sub = False

            with self._lock:
                key, trade = self._find_trade_for_order(order)

                # --- Case A: Entry changed state ---
                if trade and not stop_type and status in ("PartiallyFilled", "Filled"):
                    self._rekey_trade(key, oid, trade)
                    old_phase = trade.phase
                    trade.entry_qty = order.get("qty") or trade.entry_qty
                    trade.cum_exec_qty = cum_exec_qty or trade.cum_exec_qty
                    trade.leaves_qty = leaves_qty or trade.leaves_qty
                    if avg_price and float(avg_price) > 0:
                        trade.entry_price = trade.entry_price or float(avg_price)
                        trade.fill_price = float(avg_price)
                    if cum_exec_qty and float(cum_exec_qty) > 0:
                        trade.qty = cum_exec_qty

                    if status == "PartiallyFilled":
                        trade.phase = TradeState.PHASE_PARTIAL
                        if old_phase != TradeState.PHASE_PARTIAL:
                            self.log(
                                f"entry partially filled: {symbol} "
                                f"{trade.cum_exec_qty}/{trade.entry_qty} @ {trade.fill_price}"
                            )
                    else:
                        trade.phase = TradeState.PHASE_LIVE
                        trade.leaves_qty = "0"
                        if old_phase != TradeState.PHASE_LIVE:
                            self.log(f"entry filled: {symbol} @ {trade.fill_price}")

                    need_ticker_sub = True
                    snapshot = trade.clone()

                # --- Case B: Entry cancelled / deactivated ---
                elif trade and not stop_type and status in ("Cancelled", "Deactivated"):
                    self._rekey_trade(key, oid, trade)
                    trade.entry_qty = order.get("qty") or trade.entry_qty
                    trade.cum_exec_qty = cum_exec_qty or trade.cum_exec_qty
                    trade.leaves_qty = leaves_qty or "0"
                    filled_qty = float(trade.cum_exec_qty or "0")

                    if filled_qty > 0:
                        trade.phase = TradeState.PHASE_LIVE
                        trade.qty = trade.cum_exec_qty
                        if avg_price and float(avg_price) > 0:
                            trade.entry_price = trade.entry_price or float(avg_price)
                            trade.fill_price = float(avg_price)
                        self.log(f"entry remainder cancelled: {symbol}; live qty {trade.qty}")
                        need_ticker_sub = True
                    else:
                        trade.phase = TradeState.PHASE_CANCELLED
                        self.log(f"entry cancelled: {symbol}")
                    snapshot = trade.clone()

                # --- Case C: TP or SL leg fired (trade closed) ---
                elif stop_type in ("TakeProfit", "StopLoss", "PartialTakeProfit", "PartialStopLoss") and status == "Filled":
                    for trade in self._trades.values():
                        if trade.symbol == symbol and trade.phase in (
                            TradeState.PHASE_PARTIAL,
                            TradeState.PHASE_LIVE,
                        ):
                            trade.phase = TradeState.PHASE_CLOSED
                            trade.close_type = stop_type
                            trade.close_price = float(order.get("avgPrice") or order.get("triggerPrice", 0))
                            self.log(f"trade closed: {symbol} via {stop_type} @ {trade.close_price}")
                            snapshot = trade.clone()
                            # Remember the closing orderId for the exact
                            # journal join (off-thread: it's a disk write).
                            self._queue.put((
                                self._note_closing_order,
                                (symbol, trade.entry_price or 0.0,
                                 trade.order_link_id, oid),
                                {},
                            ))
                            break

            if need_ticker_sub:
                self._subscribe_ticker(symbol)
            if snapshot:
                self._on_trade_update(snapshot)

    def _handle_position_event(self, message: dict) -> None:
        """
        Bybit pushes position updates with unrealised PnL, mark price, etc.
        We match by symbol to our active or recently-closed trades.

        Note: The order event often sets PHASE_CLOSED before this event arrives.
        We still process size=0 events for CLOSED trades to compute final PnL.
        """
        for pos in message.get("data", []):
            symbol = pos.get("symbol", "")
            size = float(pos.get("size", "0"))
            unrealised = pos.get("unrealisedPnl", "0")
            mark = pos.get("markPrice", "0")
            pos_value = pos.get("positionValue", "0")

            snapshot = None
            with self._lock:
                for trade in self._trades.values():
                    if trade.symbol != symbol:
                        continue

                    if trade.phase in (TradeState.PHASE_PARTIAL, TradeState.PHASE_LIVE):
                        trade.unrealised_pnl = float(unrealised)
                        trade.mark_price = float(mark) if mark else None
                        trade.position_value = float(pos_value) if pos_value else None
                        if size > 0:
                            trade.qty = pos.get("size")
                        lev = pos.get("leverage", "")
                        if lev:
                            trade.leverage = lev

                        if size == 0:
                            trade.phase = TradeState.PHASE_CLOSED
                            trade.close_type = trade.close_type or "Position Closed"
                            trade.close_price = trade.mark_price
                            trade.pnl = self._compute_trade_pnl(trade)
                            self.log(f"position closed: {symbol}")

                        snapshot = trade.clone()
                        break

                    elif trade.phase == TradeState.PHASE_CLOSED and size == 0 and trade.pnl is None:
                        trade.close_price = trade.close_price or (float(mark) if mark else None)
                        trade.pnl = self._compute_trade_pnl(trade)
                        self.log(f"realised PnL captured for {symbol}: {trade.pnl}")
                        snapshot = trade.clone()
                        break

            if snapshot:
                self._on_trade_update(snapshot)

    def calculate_trade(
        self, symbol: str, side: str, entry: float, sl: float, risk_usd: float
    ) -> dict:
        """
        Pure math — returns a dict with all computed values.
        Does NOT touch the exchange.  Safe to call for preview.
        """
        rules = self.cache.get(symbol)
        if not rules:
            raise ValueError(f"Symbol {symbol} not found in cache. Try refreshing.")

        # Validate direction
        if side == "Buy" and sl >= entry:
            raise ValueError("LONG: Stop Loss must be below Entry.")
        if side == "Sell" and sl <= entry:
            raise ValueError("SHORT: Stop Loss must be above Entry.")

        distance_pct = abs(entry - sl) / entry
        max_exchange_lev = rules["maxLev"]

        # Real per-symbol MMR from Bybit's risk limit API (base tier)
        # e.g. BTC = 0.005 (0.5%), shitcoins may be 0.02 (2%)
        mmr = rules.get("mmr", 0.005)
        # Add a small cushion for taker fees + slippage (0.2%)
        fee_cushion = 0.002
        total_buffer = mmr + fee_cushion

        # Safe leverage: ensures liquidation distance > SL distance
        # liq_distance = initial_margin - mmr = (1/lev) - mmr
        # We need: (1/lev) - mmr > sl_distance  →  lev < 1/(sl_distance + mmr)
        safe_lev = math.floor(1.0 / (distance_pct + total_buffer))
        target_lev = int(max(1, min(safe_lev, max_exchange_lev)))

        str_entry = format_value(entry, rules["tickSize"])
        str_sl = format_value(sl, rules["tickSize"])

        # Position size from risk. The "risk" you type is the NET loss when
        # stopped — so it must include round-trip fees, not just the price move.
        #   loss_when_stopped = qty*|entry-sl| + entry*qty*maker + sl*qty*taker
        #   (maker entry via limit, taker exit via market SL)
        #   → qty = risk / (|entry-sl| + entry*maker + sl*taker)
        # A naive qty=risk/|entry-sl| ignores the fee term, which grows with
        # notional as the stop tightens, overshooting true risk by ~fee/distance
        # (≈7.5% at a 1% stop, ≈15% at 0.5%, ≈37% at 0.2%).
        qty_step = Decimal(rules["qtyStep"])
        price_dist = Decimal(str(abs(entry - sl)))
        fee_per_unit = (
            Decimal(str(entry)) * Decimal(str(config.FEE_MAKER))
            + Decimal(str(sl)) * Decimal(str(config.FEE_TAKER))
        )
        risk_per_unit = price_dist + fee_per_unit
        raw_qty_dec = Decimal(str(risk_usd)) / risk_per_unit
        min_qty_dec = Decimal(str(rules["minQty"]))
        min_notional_dec = Decimal(str(rules.get("minNotional", "0") or "0"))

        min_notional_qty = Decimal("0")
        if min_notional_dec > 0:
            min_notional_qty = (
                (min_notional_dec / Decimal(str(entry))) / qty_step
            ).quantize(Decimal("1"), rounding=ROUND_UP) * qty_step

        exchange_min_qty = max(min_qty_dec, min_notional_qty)
        if raw_qty_dec < exchange_min_qty:
            min_risk = exchange_min_qty * risk_per_unit
            min_qty_str = format_value(float(exchange_min_qty), rules["qtyStep"])
            raise ValueError(
                f"{symbol} minimum order is {min_qty_str} qty, which risks about "
                f"${float(min_risk):.2f} at this stop. Increase risk or use a tighter stop."
            )

        str_qty = format_value(float(raw_qty_dec), rules["qtyStep"], round_down=True)
        if Decimal(str(str_qty)) < exchange_min_qty:
            min_risk = exchange_min_qty * risk_per_unit
            raise ValueError(
                f"{symbol} qty rounds below exchange minimum. Minimum risk is about "
                f"${float(min_risk):.2f} at this stop."
            )

        fee_usd = round(float(Decimal(str(str_qty)) * fee_per_unit), 2)
        notional_usd = round(float(str_qty) * entry, 2)
        margin_usd = round(notional_usd / target_lev, 2)

        return {
            "symbol": symbol,
            "side": side,
            "entry": str_entry,
            "sl": str_sl,
            "qty": str_qty,
            "leverage": target_lev,
            "max_exchange_lev": int(max_exchange_lev),
            "sl_distance_pct": round(distance_pct * 100, 4),
            "notional_usd": notional_usd,
            "margin_usd": margin_usd,
            "risk_usd": risk_usd,
            "fee_usd": fee_usd,
            "tick_size": rules["tickSize"],
            "mmr_pct": round(mmr * 100, 3),
            "buffer_pct": round(total_buffer * 100, 3),
        }

    # ─── Order Execution ──────────────────────────────────────────

    def execute_bracket(
        self,
        symbol: str,
        side: str,
        entry: float,
        sl: float,
        tp: Optional[float],
        risk_usd: float,
        isolated: bool = True,
        strat1: bool = False,
        post_only: bool = False,
    ) -> TradeState:
        """
        Full atomic execution pipeline:
          1. Calculate risk math
          2. Set margin mode + leverage on the exchange
          3. Fire the OTOCO bracket via /v5/order/create
          4. Register the trade in the state machine
        Returns the TradeState object for GUI tracking.
        """
        rules = self.cache.get(symbol)
        if not rules:
            raise ValueError(f"Symbol {symbol} not in cache.")

        calc = self.calculate_trade(symbol, side, entry, sl, risk_usd)
        target_lev = calc["leverage"]

        self.log(
            f"Calc: {symbol} {side} | Entry={calc['entry']} SL={calc['sl']} | "
            f"SL Dist={calc['sl_distance_pct']}% | Lev={target_lev}x/{calc['max_exchange_lev']}x | "
            f"Qty={calc['qty']} | Notional=${calc['notional_usd']}"
        )

        # ── Step 0: Reject if already tracking a trade on this symbol ──
        #    Done before touching margin/leverage so we never mutate account
        #    state for an order we're about to refuse.
        with self._lock:
            for existing in self._trades.values():
                if existing.symbol == symbol and existing.phase in (
                    TradeState.PHASE_PENDING,
                    TradeState.PHASE_PARTIAL,
                    TradeState.PHASE_LIVE,
                ):
                    raise ValueError(f"Already have an active trade on {symbol}. Cancel or close it first.")

        # ── Step 1: Margin mode (UTA = account-level only) ────────
        target_mode = "ISOLATED_MARGIN" if isolated else "REGULAR_MARGIN"
        try:
            self.client.set_margin_mode(setMarginMode=target_mode)
            self.account_margin_mode = target_mode
            self.log(f"Account margin → {'Isolated' if isolated else 'Cross'}")
        except Exception as e:
            err_str = str(e)
            if "110026" in err_str or "not modified" in err_str.lower():
                self.account_margin_mode = target_mode
            elif "110020" in err_str:
                msg = f"Cannot switch margin mode because open positions or resting orders exist on Bybit account."
                self.log(msg, is_error=True)
                raise ValueError(msg)
            else:
                self.log(f"Margin mode note: {e} (continuing)", is_error=False)

        # ── Step 2: Leverage ─────────────────────────────────────
        try:
            self.client.set_leverage(
                category="linear",
                symbol=symbol,
                buyLeverage=str(target_lev),
                sellLeverage=str(target_lev),
            )
            self.log(f"Leverage set: {target_lev}x")
        except Exception as e:
            if "110043" not in str(e) and "Not modified" not in str(e):
                self.log(f"Leverage note: {e}", is_error=True)

        order_link_id = self._new_order_link_id()

        # ── Step 3: Build OTOCO payload ──────────────────────────
        order_payload = {
            "category": "linear",
            "symbol": symbol,
            "side": side,
            "positionIdx": 0,
            "orderLinkId": order_link_id,
            "orderType": "Limit",
            "qty": calc["qty"],
            "price": calc["entry"],
            "stopLoss": calc["sl"],
            "slOrderType": "Market",
            "slTriggerBy": config.SL_TRIGGER_BY,
            "tpslMode": "Partial",
            # PostOnly = cancel instead of filling immediately if the limit
            # crosses the book — enforces the maker-entry assumption in sizing.
            "timeInForce": "PostOnly" if post_only else "GTC",
        }

        if tp is not None:
            str_tp = format_value(tp, rules["tickSize"])
            str_tp_trigger = self._tp_trigger_price(entry, tp, rules["tickSize"])
            order_payload["takeProfit"] = str_tp_trigger
            order_payload["tpOrderType"] = "Limit"
            order_payload["tpLimitPrice"] = str_tp

        # Register before place_order returns so a fast WebSocket fill can be matched by orderLinkId.
        trade = TradeState(symbol=symbol, side=side, entry_order_id=order_link_id)
        trade.order_link_id = order_link_id
        trade.opened_at = time.time()
        trade.risk_usd = risk_usd
        trade.entry_price = float(calc["entry"])
        trade.original_sl_distance = abs(entry - sl)
        trade.original_tp = tp
        trade.leverage = str(target_lev)
        trade.entry_qty = calc["qty"]
        trade.qty = calc["qty"]
        trade.stop_loss = calc["sl"]
        if tp is not None:
            trade.take_profit = format_value(tp, rules["tickSize"])
        trade.strat1_enabled = strat1 and tp is not None
        if trade.strat1_enabled:
            self.log(f"strat1 enabled for {symbol}")
        with self._lock:
            self._trades[order_link_id] = trade

        # ── Step 4: Fire ─────────────────────────────────────────
        self.log("Dispatching atomic bracket to Bybit matching engine…")
        try:
            response = self.client.place_order(**order_payload)
        except Exception:
            with self._lock:
                self._trades.pop(order_link_id, None)
            raise

        if response["retCode"] != 0:
            with self._lock:
                self._trades.pop(order_link_id, None)
            raise ValueError(f"Bybit rejected: {response['retMsg']}")

        order_id = response["result"]["orderId"]
        self.log(f"bracket placed: {order_id}")

        # Order confirmed placed — now remember the intended risk so the journal
        # can compute this trade's R-multiple when it closes (matched by symbol +
        # entry price). Recorded only on success, so a rejected order can't leave
        # a stale ledger entry that later mis-attributes R to a different trade.
        self._record_risk_intent(symbol, float(calc["entry"]), risk_usd,
                                  trade.opened_at, trade.strat1_enabled,
                                  sl=sl, tp=tp, link_id=order_link_id)

        with self._lock:
            old_key = order_id if order_id in self._trades else order_link_id
            self._rekey_trade(old_key, order_id, trade)
        self._on_trade_update(trade.clone())

        return trade

    # ─── Live TP/SL Amendment ─────────────────────────────────────

    def modify_brackets(
        self,
        trade: TradeState,
        new_tp: Optional[float] = None,
        new_sl: Optional[float] = None,
    ) -> None:
        """
        Amend TP/SL on an active trade.

        Phase A (PENDING):  amend the dormant metadata on the entry order.
        Phase B (LIVE):     find the spawned conditional orders and amend them.
        """
        rules = self.cache.get(trade.symbol)
        if not rules:
            raise ValueError("Symbol not in cache.")

        tick = rules["tickSize"]

        if trade.phase == TradeState.PHASE_PENDING:
            # ── Phase A: Entry hasn't filled yet ─────────────────
            payload = {
                "category": "linear",
                "symbol": trade.symbol,
                "orderId": trade.entry_order_id,
            }
            if new_tp is not None:
                str_tp = format_value(new_tp, tick)
                entry_ref = trade.fill_price or trade.entry_price or new_tp
                str_trigger = self._tp_trigger_price(entry_ref, new_tp, tick)
                payload["takeProfit"] = str_trigger
                payload["tpOrderType"] = "Limit"
                payload["tpLimitPrice"] = str_tp
            if new_sl is not None:
                payload["stopLoss"] = format_value(new_sl, tick)

            self.client.amend_order(**payload)
            self.log(f"Amended dormant brackets on pending entry {trade.entry_order_id}")

        elif trade.phase in (TradeState.PHASE_PARTIAL, TradeState.PHASE_LIVE):
            # ── Phase B: Trade is live, TP/SL are separate orders ─
            open_orders = self._get_open_orders_all(symbol=trade.symbol)
            tp_order = None
            sl_order = None

            for order in open_orders:
                sot = order.get("stopOrderType", "")
                if "TakeProfit" in sot and tp_order is None:
                    tp_order = order
                elif "StopLoss" in sot and sl_order is None:
                    sl_order = order

            wants_tp = new_tp is not None
            wants_sl = new_sl is not None
            missing_tp = wants_tp and tp_order is None
            missing_sl = wants_sl and sl_order is None

            if missing_tp or missing_sl:
                # In Partial mode, Bybit's set_trading_stop is an add API and
                # tpSize/slSize must match. Avoid silently creating unbound legs.
                if tp_order is not None or sl_order is not None:
                    missing = "TP" if missing_tp else "SL"
                    raise ValueError(
                        f"No existing {missing} child order found. Bybit Partial mode "
                        "cannot safely add one missing side while the other side exists."
                    )
                if not (wants_tp and wants_sl):
                    raise ValueError(
                        "Adding a new Partial TP/SL pair requires both new TP and new SL."
                    )

                str_tp = format_value(new_tp, tick)
                str_trigger = self._tp_trigger_price(trade.fill_price, new_tp, tick) if trade.fill_price else str_tp
                str_sl = format_value(new_sl, tick)
                pos_size = self._get_position_size(trade.symbol)
                if float(pos_size) <= 0:
                    raise ValueError("Cannot add TP/SL pair: no open position size found.")
                self.client.set_trading_stop(
                    category="linear",
                    symbol=trade.symbol,
                    takeProfit=str_trigger,
                    stopLoss=str_sl,
                    tpOrderType="Limit",
                    slOrderType="Market",
                    slTriggerBy=config.SL_TRIGGER_BY,
                    tpLimitPrice=str_tp,
                    tpslMode="Partial",
                    tpSize=pos_size,
                    slSize=pos_size,
                    positionIdx=0,
                )
                trade.take_profit = str_tp
                trade.stop_loss = str_sl
                self.log(f"TP/SL pair added → TP {str_tp} (trigger {str_trigger}) | SL {str_sl}")
                return

            if wants_tp:
                str_tp = format_value(new_tp, tick)
                str_trigger = self._tp_trigger_price(trade.fill_price, new_tp, tick) if trade.fill_price else str_tp
                self.client.amend_order(
                    category="linear",
                    symbol=trade.symbol,
                    orderId=tp_order["orderId"],
                    triggerPrice=str_trigger,
                    price=str_tp,
                )
                trade.take_profit = str_tp
                self.log(f"TP trailed → {str_tp} (trigger {str_trigger})")

            if wants_sl:
                str_sl = format_value(new_sl, tick)
                self.client.amend_order(
                    category="linear",
                    symbol=trade.symbol,
                    orderId=sl_order["orderId"],
                    triggerPrice=str_sl,
                )
                trade.stop_loss = str_sl
                self.log(f"SL trailed → {str_sl}")
        else:
            raise ValueError(f"Trade is {trade.phase} — cannot modify.")

    # ─── Market Close (panic button) ──────────────────────────────

    def close_position_market(self, trade: TradeState) -> None:
        """
        Close a live position immediately with a reduce-only market order.

        Bybit clears the Partial-mode TP/SL children when the position hits
        zero; a best-effort sweep runs afterwards to cancel any leftovers so
        a stale conditional can never fire on a future position.
        """
        if trade.phase not in (TradeState.PHASE_PARTIAL, TradeState.PHASE_LIVE):
            raise ValueError("No live position to close.")

        size = self._get_position_size(trade.symbol)
        if float(size) <= 0:
            raise ValueError(f"No open position found for {trade.symbol}.")

        close_side = "Sell" if trade.side == "Buy" else "Buy"
        response = self.client.place_order(
            category="linear",
            symbol=trade.symbol,
            side=close_side,
            orderType="Market",
            qty=size,
            reduceOnly=True,
            positionIdx=0,
        )
        if response["retCode"] != 0:
            raise ValueError(f"Bybit rejected market close: {response['retMsg']}")

        closing_oid = response.get("result", {}).get("orderId", "")
        self._note_closing_order(trade.symbol, trade.entry_price or 0.0,
                                 trade.order_link_id, closing_oid)

        # Label the close on the core's copy (the GUI may hand us a clone) so
        # the position event finalises it as Manual instead of generic.
        with self._lock:
            core_trade = self._trades.get(trade.entry_order_id)
            if core_trade is None:
                for t in self._trades.values():
                    if t.symbol == trade.symbol and t.phase in (
                        TradeState.PHASE_PARTIAL,
                        TradeState.PHASE_LIVE,
                    ):
                        core_trade = t
                        break
            if core_trade:
                core_trade.close_type = "Manual"

        self.log(f"market close dispatched: {trade.symbol} qty {size}")
        self._queue.put((self._sweep_orphan_brackets, (trade.symbol,), {}))

    def _sweep_orphan_brackets(self, symbol: str) -> None:
        """Cancel leftover TP/SL conditionals once a position is flat."""
        time.sleep(1.5)  # let Bybit's own TP/SL clearing run first
        try:
            if float(self._get_position_size(symbol)) > 0:
                return  # position still open — keep its brackets
            for order in self._get_open_orders_all(symbol=symbol):
                if order.get("stopOrderType"):
                    try:
                        self.client.cancel_order(
                            category="linear",
                            symbol=symbol,
                            orderId=order["orderId"],
                        )
                        self.log(
                            f"orphan bracket cancelled: {symbol} "
                            f"{order.get('stopOrderType')}"
                        )
                    except Exception:
                        pass  # already cleared by Bybit
        except Exception as e:
            self.log(f"orphan bracket sweep failed for {symbol}: {e}", is_error=True)

    # ─── Cancel Pending Entry ─────────────────────────────────────

    def cancel_trade(self, trade: TradeState) -> None:
        """Cancel a resting entry order. Partial fills keep the filled position live."""
        if trade.phase not in (TradeState.PHASE_PENDING, TradeState.PHASE_PARTIAL):
            raise ValueError("Can only cancel orders in PENDING or PARTIAL phase.")

        self.client.cancel_order(
            category="linear",
            symbol=trade.symbol,
            orderId=trade.entry_order_id,
        )
        with self._lock:
            filled_qty = float(trade.cum_exec_qty or "0")
            if trade.phase == TradeState.PHASE_PARTIAL and filled_qty > 0:
                trade.phase = TradeState.PHASE_LIVE
                trade.qty = trade.cum_exec_qty
                trade.leaves_qty = "0"
                self.log(f"Cancelled remaining entry {trade.entry_order_id}; live qty {trade.qty}")
            else:
                trade.phase = TradeState.PHASE_CANCELLED
                self.log(f"Cancelled entry {trade.entry_order_id}")
            snapshot = trade.clone()
        self._on_trade_update(snapshot)

    @staticmethod
    def _compute_trade_pnl(trade: TradeState) -> float:
        """
        Compute per-trade realised PnL, net of estimated round-trip fees.

        Entry is a maker limit fill; exit is taker (market SL / manual close)
        unless it closed on the limit TP (maker). Funding is not modelled
        (negligible for short holds). This is an estimate — Bybit's closed-PnL
        is the source of truth — but it keeps the journal honest (fees are a
        meaningful drag on tight stops) without an extra REST call.
        """
        if trade.fill_price and trade.close_price and trade.qty:
            qty = float(trade.qty)
            if trade.side == "Buy":
                gross = (trade.close_price - trade.fill_price) * qty
            else:
                gross = (trade.fill_price - trade.close_price) * qty
            entry_fee = trade.fill_price * qty * config.FEE_MAKER
            exit_maker = "TakeProfit" in (trade.close_type or "")
            exit_rate = config.FEE_MAKER if exit_maker else config.FEE_TAKER
            exit_fee = trade.close_price * qty * exit_rate
            return round(gross - entry_fee - exit_fee, 6)
        return 0.0

    # ─── Query Helpers ────────────────────────────────────────────

    @staticmethod
    def _tp_trigger_price(entry: float, tp: float, tick: str) -> str:
        """Compute the TP trigger at the midpoint between entry and TP (for maker fill)."""
        return format_value((entry + tp) / 2, tick)

    def get_wallet_balance(self) -> Optional[float]:
        """Return total account equity in USD for display."""
        try:
            res = self.client.get_wallet_balance(accountType="UNIFIED")
            account = res["result"]["list"][0]
            total_eq = account.get("totalEquity", "")
            if total_eq:
                return float(total_eq)
        except Exception as e:
            self.log(f"Failed to get wallet balance: {e}", is_error=True)
        return None

    def _get_position_size(self, symbol: str) -> str:
        """Return current position size for a symbol (used by set_trading_stop)."""
        try:
            res = self.client.get_positions(category="linear", symbol=symbol)
            for pos in res["result"]["list"]:
                size = pos.get("size", "0")
                if float(size) > 0:
                    return size
        except Exception as e:
            self.log(f"Failed to get position size for {symbol}: {e}", is_error=True)
        return "0"

    # ─── Risk ledger (for R-multiples on app-placed trades) ───────

    def _load_risk_ledger(self) -> List[dict]:
        """Load the persisted risk ledger, pruning entries older than backfill."""
        try:
            with open(config.RISK_LEDGER_FILE, "r") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return []
        cutoff = time.time() - config.JOURNAL_BACKFILL_DAYS * 86400
        return [e for e in data if (e.get("ts") or 0) >= cutoff]

    def _save_risk_ledger(self) -> None:
        try:
            with open(config.RISK_LEDGER_FILE, "w") as f:
                json.dump(self._risk_ledger, f, indent=2)
        except OSError as e:
            self.log(f"risk ledger save failed: {e}", is_error=True)

    def _record_risk_intent(self, symbol: str, entry_price: float, risk_usd: float,
                            opened_at: Optional[float], strat1: bool,
                            sl: Optional[float] = None, tp: Optional[float] = None,
                            link_id: Optional[str] = None) -> None:
        with self._risk_ledger_lock:
            self._risk_ledger.append({
                "symbol": symbol,
                "entry_price": entry_price,
                "risk_usd": risk_usd,
                "opened_at": opened_at,
                "strat1": bool(strat1),
                "strat1_phase": 0,
                "sl": sl,
                "tp": tp,
                "link_id": link_id,
                "closing_order_ids": [],
                "ts": time.time(),
            })
            self._save_risk_ledger()

    def _match_risk_intent_index(self, symbol: str, entry_price: float,
                                 link_id: Optional[str] = None,
                                 closing_oid: Optional[str] = None) -> int:
        """
        Index of the best ledger match. Precedence: exact closing orderId
        (recorded when the TP/SL child fills or the app market-closes), then
        exact orderLinkId, then symbol + entry price within 0.1% (fallback for
        externally-closed trades). -1 if none. Caller holds the lock.
        """
        if closing_oid:
            for i, e in enumerate(self._risk_ledger):
                if closing_oid in (e.get("closing_order_ids") or []):
                    return i
        if link_id:
            for i, e in enumerate(self._risk_ledger):
                if e.get("link_id") == link_id:
                    return i
        if not entry_price:
            return -1
        best_i, best_diff = -1, None
        for i, e in enumerate(self._risk_ledger):
            if e.get("symbol") != symbol:
                continue
            ep = e.get("entry_price") or 0
            if ep <= 0:
                continue
            diff = abs(ep - entry_price) / ep
            if diff <= 0.001 and (best_diff is None or diff < best_diff):  # within 0.1%
                best_i, best_diff = i, diff
        return best_i

    def _consume_risk_intent(self, symbol: str, entry_price: float,
                             closing_oid: Optional[str] = None) -> Optional[dict]:
        """Find and remove the ledger entry matching this close."""
        with self._risk_ledger_lock:
            i = self._match_risk_intent_index(symbol, entry_price,
                                              closing_oid=closing_oid)
            if i >= 0:
                match = self._risk_ledger.pop(i)
                self._save_risk_ledger()
                return match
        return None

    def _note_closing_order(self, symbol: str, entry_price: float,
                            link_id: Optional[str], closing_oid: str) -> None:
        """
        Tag the ledger entry with the exchange orderId that closed (or partly
        closed) the trade. Bybit's closed-PnL record carries the same orderId,
        so the journal join becomes an exact key lookup instead of a fuzzy
        price match — re-entering the same level can no longer steal a
        different trade's risk intent.
        """
        if not closing_oid:
            return
        with self._risk_ledger_lock:
            i = self._match_risk_intent_index(symbol, entry_price, link_id)
            if i >= 0:
                ids = self._risk_ledger[i].setdefault("closing_order_ids", [])
                if closing_oid not in ids:
                    ids.append(closing_oid)
                    self._save_risk_ledger()

    def _peek_risk_intent(self, symbol: str, entry_price: float,
                          link_id: Optional[str] = None) -> Optional[dict]:
        """Like _consume_risk_intent but non-destructive (for boot restore)."""
        with self._risk_ledger_lock:
            i = self._match_risk_intent_index(symbol, entry_price, link_id)
            return dict(self._risk_ledger[i]) if i >= 0 else None

    def _update_risk_intent_phase(self, symbol: str, entry_price: float,
                                  link_id: Optional[str], phase: int) -> None:
        """Persist strat1 phase progress so a restart re-arms at the right step."""
        with self._risk_ledger_lock:
            i = self._match_risk_intent_index(symbol, entry_price, link_id)
            if i >= 0:
                self._risk_ledger[i]["strat1_phase"] = phase
                self._save_risk_ledger()

    # ─── Journal reconciliation (Bybit closed-PnL = source of truth) ──

    def fetch_closed_pnl(self, start_ms: int) -> List[dict]:
        """
        Return normalised closed-trade records from Bybit since *start_ms*.
        If start_ms is 0/None, backfill JOURNAL_BACKFILL_DAYS. Bybit caps each
        query window at 7 days, so the range is fetched in chunks.
        """
        now_ms = int(time.time() * 1000)
        if not start_ms or start_ms <= 0:
            start_ms = now_ms - config.JOURNAL_BACKFILL_DAYS * 86400 * 1000
        else:
            start_ms -= 1000  # small overlap so a boundary trade isn't missed

        window = 7 * 86400 * 1000
        raw: List[dict] = []
        s = start_ms
        while s < now_ms:
            e = min(s + window, now_ms)
            raw.extend(self._fetch_closed_pnl_window(s, e))
            s = e
        return [self._normalize_closed_pnl(r) for r in raw]

    def _fetch_closed_pnl_window(self, start_ms: int, end_ms: int) -> List[dict]:
        out: List[dict] = []
        cursor = ""
        for _ in range(50):  # safety bound on pagination
            kwargs = {
                "category": "linear",
                "startTime": start_ms,
                "endTime": end_ms,
                "limit": 100,
            }
            if cursor:
                kwargs["cursor"] = cursor
            res = self.client.get_closed_pnl(**kwargs)
            result = res.get("result", {})
            out.extend(result.get("list", []))
            cursor = result.get("nextPageCursor", "")
            if not cursor:
                break
        return out

    def _normalize_closed_pnl(self, r: dict) -> dict:
        """
        Turn a Bybit closed-PnL record into a journal entry. R-multiple is left
        unset here — it's attached separately (attach_risk_intent) and only for
        records that are actually new, so a re-fetched boundary trade can't
        wrongly consume a fresh trade's risk intent.
        """
        entry_p = float(r.get("avgEntryPrice", "0") or 0)
        exit_p = float(r.get("avgExitPrice", "0") or 0)
        pnl = float(r.get("closedPnl", "0") or 0)
        closed_at = (float(r.get("updatedTime", "0") or 0)) / 1000.0

        # Derive the *position* side from the price move vs PnL sign — robust to
        # whether Bybit's `side` field means the position or the closing order.
        if exit_p != entry_p:
            was_long = (exit_p > entry_p) == (pnl >= 0)
            side = "Buy" if was_long else "Sell"
        else:
            side = "Buy" if r.get("side") == "Sell" else "Sell"

        return {
            "order_id": r.get("orderId", ""),
            "symbol": r.get("symbol", ""),
            "side": side,
            "entry_price": entry_p or None,
            "close_price": exit_p or None,
            "qty": r.get("qty"),
            "leverage": r.get("leverage"),
            "pnl": round(pnl, 4),
            "close_type": "closed",
            "strat1_used": False,
            "r_multiple": None,
            "opened_at": None,
            "closed_at": closed_at,
            "duration_sec": None,
        }

    def attach_risk_intent(self, entry: dict) -> None:
        """
        Enrich a NEW journal entry with R-multiple / opened_at / strat1 from the
        risk ledger (matched on symbol + entry price). Consumes the ledger entry.
        No-op if there's no match (backfilled/external trades stay R = None).
        """
        intent = self._consume_risk_intent(entry.get("symbol", ""),
                                           entry.get("entry_price") or 0,
                                           closing_oid=entry.get("order_id") or None)
        if not intent:
            return
        risk_usd = intent.get("risk_usd")
        opened_at = intent.get("opened_at")
        entry["strat1_used"] = bool(intent.get("strat1"))
        entry["opened_at"] = opened_at
        pnl = entry.get("pnl")
        if risk_usd and risk_usd > 0 and pnl is not None:
            entry["r_multiple"] = round(pnl / risk_usd, 2)
        if opened_at and entry.get("closed_at"):
            entry["duration_sec"] = int(entry["closed_at"] - opened_at)
