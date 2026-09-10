"""
Viridis — PyQt6 board for the Bybit V5 execution core.

Ticket on the left: symbol, side, entry, a stop that accepts a price or a %,
a target that accepts a price, a % or an R multiple, and risk in dollars or
as a share of equity. The preview recomputes on every keystroke and the
ladder shows target, mark, entry, stop and liquidation on one scale.

Positions on the right: one card per active trade with a mini ladder,
liquidation cushion, MFE/MAE and inline actions; a stats strip; the log as a
collapsible drawer. The top bar carries the account-level risk strip.

Background threads never touch widgets: TradingCore → SignalBridge → here.

Usage:
    source .venv/bin/activate
    python main.py
"""

import os
import sys
import threading
import time
from typing import Dict, List, NamedTuple, Optional

from PyQt6.QtCore import QEvent, QObject, QSettings, QStringListModel, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFontMetrics, QIcon, QKeySequence, QPalette, QShortcut
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QCompleter, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPushButton, QScrollArea, QSizePolicy, QTextEdit,
    QVBoxLayout, QWidget,
)

import config
import theme as T
from cache_engine import InstrumentCache
from trading_core import (
    TradeState, TradingCore, breakeven_price, format_value, parse_pct, parse_r,
    ratchet_price, resolve_risk, resolve_stop, resolve_target,
)
from views import (
    JournalDialog, TradeCard, countdown_str, fmt_pct, fmt_px, fmt_r, fmt_usd,
    local_midnight, span,
)
from widgets import EquityCurve, HintLineEdit, Ladder, Meter, RHistogram


# ──────────────────────────────────────────────────────────────────
#  Signal bridge (core threads → Qt main thread)
# ──────────────────────────────────────────────────────────────────

class SignalBridge(QObject):
    log_signal = pyqtSignal(str, bool)
    trade_signal = pyqtSignal(object)
    cache_done = pyqtSignal(int)
    margin_mode_sync = pyqtSignal(str)
    status_signal = pyqtSignal(str, str)
    balance_signal = pyqtSignal(float, object)      # equity, available | None
    execute_done = pyqtSignal(object, str)
    action_done = pyqtSignal(str, str, str)         # card key, action, error
    journal_updated = pyqtSignal()


class Ticket(NamedTuple):
    """A resolved ticket: what calculate_trade and execute_bracket take."""
    symbol: str
    side: str
    entry: float
    sl: float
    tp: Optional[float]
    risk: float
    rules: dict


# ──────────────────────────────────────────────────────────────────
#  Main window
# ──────────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Viridis · TESTNET" if config.USE_TESTNET else "Viridis")
        self.setMinimumSize(1120, 760)
        self.resize(1200, 820)
        self.setStyleSheet(T.STYLESHEET)

        self._bridge = SignalBridge()
        self._bridge.log_signal.connect(self._append_log)
        self._bridge.trade_signal.connect(self._on_trade_state_changed)
        self._bridge.cache_done.connect(self._on_cache_refreshed)
        self._bridge.margin_mode_sync.connect(self._on_margin_mode_sync)
        self._bridge.status_signal.connect(self._on_status_updated)
        self._bridge.balance_signal.connect(self._on_balance_updated)
        self._bridge.execute_done.connect(self._on_execute_done)
        self._bridge.action_done.connect(self._on_action_done)
        self._bridge.journal_updated.connect(self._on_journal_updated)

        self._trades: Dict[str, TradeState] = {}      # key → latest snapshot
        self._cards: Dict[str, TradeCard] = {}
        self._selected_key: Optional[str] = None
        self._journal_dialog = None
        self._is_long = True
        self._equity: Optional[float] = None
        self._available: Optional[float] = None
        self._tick_running = False
        self._cache_ready = False
        self._chips_state: Optional[list] = None
        self._log_count = 0
        self._drawer_open = False
        self._last_price_seen: Optional[str] = None
        self._gov = {"open_risk": 0.0, "unknown": 0, "today_pnl": 0.0, "today_n": 0,
                     "streak": 0, "blocked": False}
        self._warnings: List[tuple] = []     # (level, text, blocking) from the last preview
        self._settings = QSettings("Viridis", "Viridis")

        self._build_ui()

        # Core callbacks arrive on whichever thread produced them; the bridge
        # queues them onto the Qt thread.
        self._core = TradingCore(
            on_log=self._bridge.log_signal.emit,
            on_trade_update=self._bridge.trade_signal.emit,
            on_cache_complete=self._bridge.cache_done.emit,
            on_balance=self._bridge.balance_signal.emit,
            on_margin_mode=self._bridge.margin_mode_sync.emit,
            on_journal=self._bridge.journal_updated.emit,
        )
        self._journal = self._core.journal

        self._setup_autocomplete()
        geo = self._settings.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        self._post_boot()

    # ─────────────────────────────────────────────────────────────
    #  UI construction
    # ─────────────────────────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_topbar())

        content = QWidget()
        content.setStyleSheet(f"background: {T.BG};")
        cols = QHBoxLayout(content)
        cols.setContentsMargins(0, 0, 0, 0)
        cols.setSpacing(0)
        cols.addWidget(self._build_ticket())
        cols.addWidget(self._build_board(), 1)
        root.addWidget(content, 1)

        QShortcut(QKeySequence("Ctrl+Return"), self, self._execute)
        QShortcut(QKeySequence("Ctrl+Enter"), self, self._execute)

    def _build_topbar(self) -> QWidget:
        bar = QWidget()
        bar.setFixedHeight(50)
        bar.setStyleSheet(f"background: {T.BG_RAISED}; border-bottom: 1px solid {T.BORDER};")
        tb = QHBoxLayout(bar)
        tb.setContentsMargins(16, 0, 16, 0)
        tb.setSpacing(16)

        title = QLabel("VIRIDIS")
        title.setStyleSheet(f"font-size: 12px; font-weight: 800; color: {T.TEXT_DIM}; letter-spacing: 3px;")
        tb.addWidget(title)
        if config.USE_TESTNET:
            badge = QLabel("TESTNET")
            badge.setStyleSheet(
                f"font-size: 9px; letter-spacing: 1.5px; padding: 2px 7px; border-radius: 3px; "
                f"border: 1px solid {T.AMBER}; color: {T.AMBER};"
            )
            tb.addWidget(badge)
        tb.addStretch()

        eq = QVBoxLayout()
        eq.setSpacing(1)
        eq.setContentsMargins(0, 7, 0, 7)
        self.balance_label = QLabel("--")
        self.balance_label.setStyleSheet(f"font-size: 13px; font-weight: 600; color: {T.WHITE};")
        self.balance_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        eq.addWidget(self.balance_label)
        sub = QHBoxLayout()
        sub.setSpacing(4)
        sub.addStretch()
        self.avail_label = QLabel("")
        self.avail_label.setObjectName("dim")
        self.avail_label.setStyleSheet(f"font-size: 10px; color: {T.TEXT_DIM};")
        sub.addWidget(self.avail_label)
        self.margin_btn = QPushButton("…")
        self.margin_btn.setObjectName("margin_btn")
        self.margin_btn.setToolTip("Account margin mode — click to switch (needs a flat account)")
        self.margin_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.margin_btn.clicked.connect(self._on_margin_btn_clicked)
        sub.addWidget(self.margin_btn)
        eq.addLayout(sub)
        tb.addLayout(eq)

        gov = QFrame()
        gov.setObjectName("gov")
        gl = QHBoxLayout(gov)
        gl.setContentsMargins(0, 0, 0, 0)
        gl.setSpacing(0)
        self.gov_cells = {}
        cap_open = f"{config.MAX_OPEN_RISK_PCT:g}%"
        day_k = (f"today · limit −${config.DAILY_LOSS_LIMIT_USD:,.0f}"
                 if config.DAILY_LOSS_LIMIT_USD > 0 else "today")
        for name, k in (("open", f"open risk · cap {cap_open}"), ("day", day_k), ("streak", "streak")):
            cell = QWidget()
            cl = QVBoxLayout(cell)
            cl.setContentsMargins(12, 5, 12, 5)
            cl.setSpacing(2)
            kl = QLabel(k.upper())
            kl.setObjectName("muted")
            kl.setStyleSheet(f"font-size: 8px; letter-spacing: 1.4px; color: {T.TEXT_MUTED};")
            vl = QLabel("--")
            vl.setTextFormat(Qt.TextFormat.RichText)
            vl.setStyleSheet("font-size: 11px;")
            meter = Meter()
            cl.addWidget(kl)
            cl.addWidget(vl)
            cl.addWidget(meter)
            if name != "streak":
                cell.setStyleSheet(f"border-right: 1px solid {T.BORDER};")
            gl.addWidget(cell)
            self.gov_cells[name] = (vl, meter)
        tb.addWidget(gov)

        self.status_label = QLabel("connecting")
        self.status_label.setStyleSheet(f"font-size: 10px; color: {T.TEXT_MUTED};")
        tb.addWidget(self.status_label)
        return bar

    def _build_ticket(self) -> QWidget:
        ticket = QFrame()
        ticket.setObjectName("ticket")
        ticket.setFixedWidth(400)
        lay = QVBoxLayout(ticket)
        lay.setContentsMargins(16, 12, 16, 14)
        lay.setSpacing(7)

        # Symbol + side
        row = QHBoxLayout()
        row.setSpacing(8)
        sym_col = QVBoxLayout()
        sym_col.setSpacing(3)
        sym_col.addWidget(self._lbl("SYMBOL"))
        self.symbol_input = QLineEdit()
        self.symbol_input.setObjectName("symbol_input")
        self.symbol_input.setPlaceholderText("BTC")
        self.symbol_input.textChanged.connect(self._on_symbol_changed)
        sym_col.addWidget(self.symbol_input)
        row.addLayout(sym_col, 1)

        side_col = QVBoxLayout()
        side_col.setSpacing(3)
        side_col.addWidget(self._lbl("SIDE"))
        seg = QHBoxLayout()
        seg.setSpacing(0)
        self.btn_long = QPushButton("LONG")
        self.btn_short = QPushButton("SHORT")
        for b, kind in ((self.btn_long, "long"), (self.btn_short, "short")):
            b.setObjectName("side")
            b.setProperty("kind", kind)
            b.setFixedSize(66, 36)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            seg.addWidget(b)
        self.btn_long.clicked.connect(lambda: self._set_side(True))
        self.btn_short.clicked.connect(lambda: self._set_side(False))
        side_col.addLayout(seg)
        row.addLayout(side_col)
        lay.addLayout(row)

        # Recent symbols
        self.chips_layout = QHBoxLayout()
        self.chips_layout.setSpacing(5)
        self.chips_layout.addStretch()
        lay.addLayout(self.chips_layout)

        # Symbol info + live price
        self.info_label = QLabel("")
        self.info_label.setObjectName("dim")
        self.info_label.setTextFormat(Qt.TextFormat.RichText)
        self.info_label.setWordWrap(True)
        lay.addWidget(self.info_label)
        self.price_label = QLabel("")
        self.price_label.setObjectName("dim")
        self.price_label.setTextFormat(Qt.TextFormat.RichText)
        self.price_label.setToolTip("Click the live price to use it as entry")
        self.price_label.linkActivated.connect(lambda _: self._use_live_price())
        lay.addWidget(self.price_label)

        # Entry
        lay.addLayout(self._field("ENTRY · LIMIT", "entry_input", HintLineEdit("limit price")))
        # Stop + target
        row2 = QHBoxLayout()
        row2.setSpacing(8)
        row2.addLayout(self._field("STOP · PRICE OR %", "stop_input", HintLineEdit("73300 or -1.2%")))
        row2.addLayout(self._field("TARGET · PRICE, % OR R", "target_input", HintLineEdit("2R · optional")))
        lay.addLayout(row2)
        # Risk + options
        row3 = QHBoxLayout()
        row3.setSpacing(8)
        risk_edit = HintLineEdit("$ or % of equity")
        risk_edit.setText(f"$ {config.DEFAULT_RISK_USD:g}")
        row3.addLayout(self._field("RISK · $ OR % EQUITY", "risk_input", risk_edit))
        opt_col = QVBoxLayout()
        opt_col.setSpacing(3)
        opt_col.addWidget(self._lbl("OPTIONS"))
        opts = QHBoxLayout()
        opts.setSpacing(12)
        opts.setContentsMargins(0, 8, 0, 0)
        self.postonly_checkbox = QCheckBox("post-only")
        self.postonly_checkbox.setChecked(True)
        self.postonly_checkbox.setToolTip(
            "Cancel instead of filling immediately if the entry crosses the book "
            "(guarantees the maker entry the sizing assumes)"
        )
        self.strat1_checkbox = QCheckBox("strat1")
        self.strat1_checkbox.setEnabled(False)
        self.strat1_checkbox.setToolTip("Ratchet the stop as price approaches the target (needs a target)")
        opts.addWidget(self.postonly_checkbox)
        opts.addWidget(self.strat1_checkbox)
        opts.addStretch()
        opt_col.addLayout(opts)
        row3.addLayout(opt_col)
        lay.addLayout(row3)

        self.ratchet_label = QLabel("")
        self.ratchet_label.setObjectName("sub")
        self.ratchet_label.setTextFormat(Qt.TextFormat.RichText)
        self.ratchet_label.setToolTip("Edit STRAT1_RATCHET in .env — 'progress:lockR, …'")
        self.ratchet_label.hide()
        lay.addWidget(self.ratchet_label)

        # Preview + warnings
        self.preview_label = QLabel("enter entry and stop")
        self.preview_label.setObjectName("preview")
        self.preview_label.setTextFormat(Qt.TextFormat.RichText)
        self.preview_label.setWordWrap(True)
        self.preview_label.setMinimumHeight(84)
        lay.addWidget(self.preview_label)
        self.warnings_label = QLabel("")
        self.warnings_label.setObjectName("warnings")
        self.warnings_label.setTextFormat(Qt.TextFormat.RichText)
        self.warnings_label.setWordWrap(True)
        self.warnings_label.hide()
        lay.addWidget(self.warnings_label)

        # Ladder takes whatever height is left
        self.ladder = Ladder()
        lay.addWidget(self.ladder, 1)

        # Execute
        self.btn_execute = QPushButton("EXECUTE LONG")
        self.btn_execute.setObjectName("execute")
        self.btn_execute.setProperty("kind", "long")
        self.btn_execute.setMinimumHeight(44)
        self.btn_execute.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_execute.clicked.connect(self._execute)
        lay.addWidget(self.btn_execute)
        keys = QLabel(
            f"<span style='color:{T.TEXT_MUTED}; font-size:9.5px;'>"
            f"↩ next &nbsp; ⌘↩ execute &nbsp; esc reset &nbsp; ↑↓ tick &nbsp; ⇧↑↓ ×10</span>"
        )
        keys.setTextFormat(Qt.TextFormat.RichText)
        keys.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(keys)

        # Wiring
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(80)
        self._preview_timer.timeout.connect(self._refresh_preview)
        self._ticket_inputs = [self.entry_input, self.stop_input, self.target_input, self.risk_input]
        for edit in self._ticket_inputs:
            edit.textChanged.connect(self._schedule_preview)
            edit.installEventFilter(self)
        self.symbol_input.installEventFilter(self)
        self.target_input.textChanged.connect(self._on_target_changed)
        self.postonly_checkbox.toggled.connect(self._schedule_preview)
        self.strat1_checkbox.toggled.connect(self._on_strat1_toggled)
        self._apply_side_style()
        return ticket

    def _field(self, label: str, attr: str, edit: QLineEdit) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setSpacing(3)
        col.addWidget(self._lbl(label))
        setattr(self, attr, edit)
        col.addWidget(edit)
        return col

    def _build_board(self) -> QWidget:
        board = QWidget()
        lay = QVBoxLayout(board)
        lay.setContentsMargins(16, 12, 16, 0)
        lay.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(12)
        head.addWidget(self._lbl("POSITIONS"))
        self.board_count = QLabel("")
        self.board_count.setStyleSheet(f"font-size: 9px; letter-spacing: 1.5px; color: {T.TEXT_DIM};")
        head.addWidget(self.board_count)
        head.addStretch()
        hint = QLabel("click a card to select · actions act on that card")
        hint.setStyleSheet(f"font-size: 10px; color: {T.TEXT_MUTED};")
        head.addWidget(hint)
        lay.addLayout(head)

        # Cards scroll when there are more than fit; the strip and drawer stay put.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        scroll.viewport().setAutoFillBackground(False)
        inner = QWidget()
        inner.setAutoFillBackground(False)
        self.cards_layout = QVBoxLayout(inner)
        self.cards_layout.setContentsMargins(0, 0, 0, 0)
        self.cards_layout.setSpacing(8)
        self.placeholder = QLabel("no active trades")
        self.placeholder.setObjectName("placeholder")
        self.cards_layout.addWidget(self.placeholder)
        self.closed_label = QLabel("")
        self.closed_label.setObjectName("closed_row")
        self.closed_label.setTextFormat(Qt.TextFormat.RichText)
        self.closed_label.hide()
        self.cards_layout.addWidget(self.closed_label)
        self.cards_layout.addStretch()
        scroll.setWidget(inner)
        lay.addWidget(scroll, 1)

        # Stats strip
        strip = QFrame()
        strip.setObjectName("strip")
        sl = QHBoxLayout(strip)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(0)
        self.strip_cells = {}
        for name, k in (("today", "today"), ("week", "7 days"), ("life", "lifetime")):
            cell = QWidget()
            cl = QVBoxLayout(cell)
            cl.setContentsMargins(12, 8, 12, 8)
            cl.setSpacing(2)
            kl = QLabel(k.upper())
            kl.setStyleSheet(f"font-size: 8px; letter-spacing: 1.4px; color: {T.TEXT_MUTED};")
            vl = QLabel("--")
            vl.setTextFormat(Qt.TextFormat.RichText)
            vl.setStyleSheet("font-size: 11px;")
            cl.addWidget(kl)
            cl.addWidget(vl)
            cell.setStyleSheet(f"border-right: 1px solid {T.BORDER};")
            cell.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Preferred)
            sl.addWidget(cell)
            self.strip_cells[name] = (kl, vl)
        for name, widget in (("R DISTRIBUTION", RHistogram()), ("EQUITY", EquityCurve())):
            cell = QWidget()
            cl = QVBoxLayout(cell)
            cl.setContentsMargins(12, 8, 12, 6)
            cl.setSpacing(3)
            kl = QLabel(name)
            kl.setStyleSheet(f"font-size: 8px; letter-spacing: 1.4px; color: {T.TEXT_MUTED};")
            cl.addWidget(kl)
            cl.addWidget(widget)
            if name.startswith("R"):
                cell.setStyleSheet(f"border-right: 1px solid {T.BORDER};")
                self.hist = widget
            else:
                self.equity_curve = widget
            sl.addWidget(cell, 1)
        lay.addWidget(strip)
        lay.addStretch()

        # Log drawer
        drawer = QFrame()
        drawer.setObjectName("drawer")
        dl = QVBoxLayout(drawer)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.setSpacing(0)
        dh = QHBoxLayout()
        dh.setSpacing(10)
        self.drawer_btn = QPushButton("▸  LOG")
        self.drawer_btn.setObjectName("drawer_head")
        self.drawer_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.drawer_btn.clicked.connect(lambda: self._set_drawer(not self._drawer_open))
        dh.addWidget(self.drawer_btn)
        self.drawer_last = QLabel("")
        self.drawer_last.setStyleSheet(f"font-size: 10.5px; color: {T.TEXT_DIM};")
        self.drawer_last.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        dh.addWidget(self.drawer_last, 1)
        self.btn_journal = QPushButton("history")
        self.btn_journal.setObjectName("small")
        self.btn_journal.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_journal.clicked.connect(self._open_journal_dialog)
        dh.addWidget(self.btn_journal)
        self.btn_refresh = QPushButton("refresh cache")
        self.btn_refresh.setObjectName("small")
        self.btn_refresh.clicked.connect(self._refresh_cache)
        dh.addWidget(self.btn_refresh)
        dl.addLayout(dh)
        self.console = QTextEdit()
        self.console.setObjectName("console")
        self.console.setReadOnly(True)
        self.console.setFixedHeight(150)
        self.console.hide()
        dl.addWidget(self.console)
        lay.addWidget(drawer)
        return board

    @staticmethod
    def _lbl(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("muted")
        return lbl

    # ─────────────────────────────────────────────────────────────
    #  Autocomplete
    # ─────────────────────────────────────────────────────────────

    def _set_trading_enabled(self, enabled: bool):
        self._cache_ready = enabled
        self.symbol_input.setPlaceholderText("BTC" if enabled else "loading symbology...")
        self._schedule_preview()

    def _usdt_symbols(self) -> List[str]:
        return [s for s in self._core.cache.symbols if s.endswith("USDT") and "-" not in s]

    def _setup_autocomplete(self):
        symbols = self._usdt_symbols()
        self._completer_model = QStringListModel(symbols)
        self._completer = QCompleter()
        self._completer.setModel(self._completer_model)
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self._completer.setMaxVisibleItems(10)
        self._completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.symbol_input.setCompleter(self._completer)
        self._set_trading_enabled(len(symbols) > 0)

    # ─────────────────────────────────────────────────────────────
    #  Post-boot
    # ─────────────────────────────────────────────────────────────

    def _post_boot(self):
        # Account and positions synchronously: the first frame should be right.
        self._core.refresh_account()
        for t in self._core.sync_existing():
            self._trades[t.entry_order_id] = t.clone()
        self._update_trade_panel()
        self._update_governance()
        self._update_stats_strip()
        self._refresh_chips()

        # Fee tier and the journal backfill are network: off the UI thread.
        threading.Thread(target=self._run_logged, args=(self._core.sync_fee_rates,), daemon=True).start()
        threading.Thread(target=self._run_logged, args=(self._core.reconcile_journal,), daemon=True).start()

        self._core.connect_websocket()

        # Slow REST fallback (30s): account, positions, journal.
        self._tick_timer = QTimer()
        self._tick_timer.timeout.connect(self._tick)
        self._tick_timer.start(30_000)

        # UI refresh throttle (500ms max) — the ticker must not repaint
        # cards hundreds of times per second.
        self._panel_dirty = False
        self._panel_timer = QTimer()
        self._panel_timer.timeout.connect(self._flush_panel)
        self._panel_timer.start(500)

        # Live pre-trade price + funding line off the ticker cache
        self._price_timer = QTimer()
        self._price_timer.timeout.connect(self._refresh_price)
        self._price_timer.start(500)

        # WS health poll — the status pill reflects actual socket state.
        self._ws_was_up = False
        self._ws_health_state = None
        self._health_timer = QTimer()
        self._health_timer.timeout.connect(self._check_ws_health)
        self._health_timer.start(5_000)

        self.entry_input.setFocus()

    def _check_ws_health(self):
        priv, pub = self._core.ws_health()
        state = (priv, pub)
        changed = state != self._ws_health_state
        if not changed and priv and pub:
            return
        self._ws_health_state = state

        if priv and pub:
            self._ws_was_up = True
            self._on_status_updated("live", T.POSITIVE)
            self._append_log("websocket streams healthy", False)
        elif not self._ws_was_up:
            return
        elif priv:
            self._on_status_updated("ticker ws down — pnl lags", T.AMBER)
            if changed:
                self._append_log("public ticker stream lost — live pnl degraded "
                                 "(30s REST fallback active)", True)
        else:
            self._on_status_updated("ws down — 30s REST fallback", T.NEGATIVE)
            if changed:
                self._append_log("private stream lost — fill events may be delayed "
                                 "(30s REST fallback active)", True)

    # ─────────────────────────────────────────────────────────────
    #  Position tick (slow poll)
    # ─────────────────────────────────────────────────────────────

    def _run_logged(self, *steps):
        """Run core steps on the calling (background) thread; failures go to the log, not the thread."""
        for step in steps:
            try:
                step()
            except Exception as e:
                self._bridge.log_signal.emit(f"{step.__name__.replace('_', ' ')} failed: {e}", True)

    def _tick(self):
        """Slow REST fallback (30s). The core updates its own state and emits snapshots."""
        if self._tick_running:
            return
        self._tick_running = True

        def _do():
            try:
                self._run_logged(self._core.refresh_account, self._core.refresh_positions,
                                 self._core.reconcile_journal)
            finally:
                self._tick_running = False

        threading.Thread(target=_do, daemon=True).start()

    def _flush_panel(self):
        if not self._panel_dirty:
            return
        self._panel_dirty = False
        self._update_trade_panel()
        self._update_governance()

    # ─────────────────────────────────────────────────────────────
    #  Side
    # ─────────────────────────────────────────────────────────────

    def _set_side(self, is_long: bool):
        if self._is_long == is_long:
            return
        self._is_long = is_long
        self._apply_side_style()
        self._schedule_preview()

    def _apply_side_style(self):
        # Explicit per-button sheets: QPushButton ignores a stylesheet background
        # that arrives via a dynamic-property selector alone.
        base = "font-size: 12px; font-weight: 700; letter-spacing: 1px; padding: 0; border-radius: 0;"
        off = f"{base} background: transparent; color: {T.TEXT_DIM}; border: 1px solid {T.BORDER};"
        for b, on, col in ((self.btn_long, self._is_long, T.POSITIVE),
                           (self.btn_short, not self._is_long, T.NEGATIVE)):
            b.setStyleSheet(f"{base} background: {col}; color: #FFFFFF; border: 1px solid {col};"
                            if on else off)
        kind = "long" if self._is_long else "short"
        col = T.POSITIVE if self._is_long else T.NEGATIVE
        self.btn_execute.setText(f"EXECUTE {kind.upper()}")
        self.btn_execute.setStyleSheet(
            f"QPushButton {{ background: {col}; color: #FFFFFF; font-size: 13px; font-weight: 700; "
            f"letter-spacing: 1.5px; border: none; border-radius: 3px; }}"
            f"QPushButton:disabled {{ background: {T.BORDER}; color: {T.TEXT_MUTED}; }}"
        )

    # ─────────────────────────────────────────────────────────────
    #  Symbol, price line, chips
    # ─────────────────────────────────────────────────────────────

    def _on_symbol_changed(self, text: str):
        sym = self._normalize_symbol(text)
        rules = self._core.cache.get(sym) if hasattr(self, "_core") else None
        if rules:
            self._core.watch_symbol(sym)
        self._last_price_seen = None
        self._refresh_info()
        self._refresh_price()
        self._refresh_chips()
        self._schedule_preview()

    def _refresh_info(self):
        sym = self._normalize_symbol(self.symbol_input.text())
        rules = self._core.cache.get(sym) if sym else None
        self.info_label.setVisible(bool(rules))
        if not rules:
            self.info_label.setText("")
            return
        tier1 = InstrumentCache.tier_for(rules, 0)
        limit = tier1["limit"]
        limit_s = f" to ${limit:,.0f}" if limit and limit != float("inf") else ""
        tk = self._core.get_ticker(sym)
        fund = ""
        if tk.get("fundingRate"):
            try:
                rate = float(tk["fundingRate"])
                # Longs pay a positive rate, shorts a negative one.
                pays_me = (rate < 0 and self._is_long) or (rate > 0 and not self._is_long)
                col = T.POSITIVE if pays_me else (T.NEGATIVE if rate != 0 else T.TEXT)
                fund = (f" &nbsp;·&nbsp; funding {span(fmt_pct(rate, 4), col)}"
                        f" in {countdown_str(tk.get('nextFundingTime'))}")
            except ValueError:
                pass
        mmr = rules.get("mmr", 0.005) * 100
        max_lev, tick_s, step_s = f"{int(rules['maxLev'])}x", rules["tickSize"], rules["qtyStep"]
        self.info_label.setText(
            f"max {span(max_lev, T.TEXT)} &nbsp;·&nbsp; tick {span(tick_s, T.TEXT)}"
            f" &nbsp;·&nbsp; step {span(step_s, T.TEXT)} &nbsp;·&nbsp; "
            f"mmr {span(f'{mmr:.2f}%', T.TEXT)}{limit_s}{fund}"
        )

    def _refresh_price(self):
        """Update the live price line (500ms timer + symbol change) and the ladder's mark."""
        sym = self._normalize_symbol(self.symbol_input.text())
        rules = self._core.cache.get(sym) if sym else None
        tk = self._core.get_ticker(sym) if rules else {}
        last = tk.get("lastPrice")
        if not last:
            if rules:
                self._core.watch_symbol(sym)   # self-heals subs made before the WS was up
            self.price_label.setText("")
            self.price_label.hide()
            return
        self.price_label.show()
        tick = rules["tickSize"]
        bid, ask = tk.get("bid1Price"), tk.get("ask1Price")
        parts = [f"<a href='use' style='color:{T.ACCENT}; text-decoration:none; font-weight:600;'>"
                 f"live {fmt_px(float(last), tick)}</a>"]
        if bid and ask:
            spread = (float(ask) - float(bid)) / float(last) * 1e4
            parts.append(f"{span(fmt_px(float(bid), tick), T.TEXT)} / {span(fmt_px(float(ask), tick), T.TEXT)}")
            parts.append(span(f"{spread:.1f} bps", T.TEXT))
        self.price_label.setToolTip("live · bid / ask · spread — click the price to use it as entry")
        self.price_label.setText(" &nbsp;·&nbsp; ".join(parts))
        if last != self._last_price_seen:
            self._last_price_seen = last
            self._refresh_info()
            self._schedule_preview()

    def _use_live_price(self):
        sym = self._normalize_symbol(self.symbol_input.text())
        rules = self._core.cache.get(sym)
        last = self._core.get_ticker(sym).get("lastPrice")
        if last and rules:
            self.entry_input.setText(format_value(float(last), rules["tickSize"]))
            self.stop_input.setFocus()

    def _refresh_chips(self):
        """Recent symbols: active trades first, then the journal's latest closes."""
        current = self._normalize_symbol(self.symbol_input.text())
        symbols: List[str] = []
        for t in self._trades.values():
            if t.is_active and t.symbol not in symbols:
                symbols.append(t.symbol)
        for s in self._journal.recent_symbols(6):
            if s not in symbols:
                symbols.append(s)
        symbols = symbols[:6]
        wanted = [(s, s == current) for s in symbols]
        if self._chips_state == wanted:
            return
        self._chips_state = wanted
        while self.chips_layout.count() > 1:
            item = self.chips_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for i, (s, on) in enumerate(wanted):
            b = QPushButton(s.replace("USDT", ""))
            b.setObjectName("chip")
            b.setProperty("on", on)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, sym=s: self._pick_symbol(sym))
            self.chips_layout.insertWidget(i, b)

    def _pick_symbol(self, symbol: str):
        self.symbol_input.setText(symbol.replace("USDT", ""))
        if not self.entry_input.text().strip():
            self._use_live_price()
        else:
            self.entry_input.setFocus()

    def _crossing_ref(self, symbol: str, side: str, entry: float):
        """Best bid/ask the entry limit would cross (None if it rests)."""
        tk = self._core.get_ticker(symbol)
        ref = tk.get("ask1Price") if side == "Buy" else tk.get("bid1Price")
        ref = ref or tk.get("lastPrice")
        if not ref:
            return None
        ref = float(ref)
        if (side == "Buy" and entry >= ref) or (side == "Sell" and entry <= ref):
            return ref
        return None

    # ─────────────────────────────────────────────────────────────
    #  Live preview
    # ─────────────────────────────────────────────────────────────

    def _schedule_preview(self, *_):
        if hasattr(self, "_preview_timer"):
            self._preview_timer.start()

    def _on_target_changed(self, text: str):
        has_tp = bool(text.strip())
        self.strat1_checkbox.setEnabled(has_tp)
        if not has_tp:
            self.strat1_checkbox.setChecked(False)

    def _on_strat1_toggled(self, on: bool):
        if on:
            steps = " &nbsp;·&nbsp; ".join(
                f"at {span(f'{p * 100:.0f}%', T.TEXT)} lock {span(f'{r:+.1f}R', T.TEXT)}"
                for p, r in self._core.ratchet
            )
            self.ratchet_label.setText(f"ratchet &nbsp; {steps}")
        self.ratchet_label.setVisible(on)

    def _ticket_values(self) -> Ticket:
        """Resolve the ticket. Raises ValueError with a message for the preview."""
        symbol = self._normalize_symbol(self.symbol_input.text())
        if not symbol:
            raise ValueError("enter a symbol")
        rules = self._core.cache.get(symbol)
        if not rules:
            raise ValueError(f"{symbol} not in cache" if self._cache_ready else "loading symbology…")
        side = "Buy" if self._is_long else "Sell"
        entry_t = self.entry_input.text().strip()
        if not entry_t:
            raise ValueError("enter an entry price")
        try:
            entry = float(entry_t.replace("$", "").replace(",", "").replace(" ", ""))
        except ValueError:
            raise ValueError("entry must be a price") from None
        if entry <= 0:
            raise ValueError("entry must be positive")
        stop_t = self.stop_input.text().strip()
        if not stop_t:
            raise ValueError("enter a stop — a price or a % like 0.8%")
        sl = resolve_stop(stop_t, entry, side)
        target_t = self.target_input.text().strip()
        tp = resolve_target(target_t, entry, sl, side) if target_t else None
        risk = resolve_risk(self.risk_input.text().strip() or "0", self._equity)
        if risk <= 0:
            raise ValueError("enter a risk amount")
        return Ticket(symbol, side, entry, sl, tp, risk, rules)

    def _set_hints(self, entry=None, sl=None, tp=None, risk=None, tick=None, last=None):
        """Each hint shows what the typed form leaves implicit: a % stop shows its
        price, a price stop shows its %, an R target shows its price, and so on."""
        self.entry_input.set_hint(fmt_pct((entry - last) / last) + " vs last"
                                  if entry and last else "")
        if entry and sl:
            typed_pct = parse_pct(self.stop_input.text()) is not None
            self.stop_input.set_hint(fmt_px(sl, tick) if typed_pct
                                     else fmt_pct(-abs(entry - sl) / entry))
        else:
            self.stop_input.set_hint("")
        if entry and tp:
            raw = self.target_input.text()
            typed_rel = parse_pct(raw) is not None or parse_r(raw) is not None
            dist = abs(entry - sl) if sl else 0
            self.target_input.set_hint(
                fmt_px(tp, tick) if typed_rel
                else (f"{abs(tp - entry) / dist:.2f}R" if dist else fmt_pct(abs(tp - entry) / entry)))
        else:
            self.target_input.set_hint("")
        if risk:
            typed_pct = parse_pct(self.risk_input.text()) is not None
            if self._equity:
                self.risk_input.set_hint(fmt_usd(risk, 0, signed=False) if typed_pct
                                         else f"{risk / self._equity * 100:.1f}% eq")
            else:
                self.risk_input.set_hint("" if not typed_pct else fmt_usd(risk, 0, signed=False))
        else:
            self.risk_input.set_hint("")

    def _preview_error(self, message: str):
        # A half-filled ticket is a prompt, not an error.
        prompt = message.startswith("enter ") or message.startswith("loading")
        self.preview_label.setText(span(message, T.TEXT_MUTED) if prompt else message)
        self.preview_label.setProperty("state", "" if prompt else "error")
        self.preview_label.style().unpolish(self.preview_label)
        self.preview_label.style().polish(self.preview_label)
        self.warnings_label.hide()
        self._warnings = []
        self.ladder.clear(message if len(message) < 40 else "fix the ticket")
        self.btn_execute.setEnabled(False)

    def _refresh_preview(self):
        if not hasattr(self, "_core"):
            return
        symbol = self._normalize_symbol(self.symbol_input.text())
        rules = self._core.cache.get(symbol) if symbol else None
        tick = rules["tickSize"] if rules else None
        tk = self._core.get_ticker(symbol) if rules else {}
        last = float(tk["lastPrice"]) if tk.get("lastPrice") else None

        # Hints tolerate a half-filled ticket
        try:
            entry = float(self.entry_input.text().strip().replace(",", "").replace("$", "") or 0) or None
        except ValueError:
            entry = None
        side = "Buy" if self._is_long else "Sell"
        sl = tp = risk = None
        try:
            if entry and self.stop_input.text().strip():
                sl = resolve_stop(self.stop_input.text(), entry, side)
            if entry and sl and self.target_input.text().strip():
                tp = resolve_target(self.target_input.text(), entry, sl, side)
            if self.risk_input.text().strip():
                risk = resolve_risk(self.risk_input.text(), self._equity)
        except ValueError:
            pass
        self._set_hints(entry, sl, tp, risk, tick, last)

        try:
            symbol, side, entry, sl, tp, risk, rules = self._ticket_values()
            calc = self._core.calculate_trade(symbol, side, entry, sl, risk)
        except ValueError as e:
            self._preview_error(str(e))
            return

        self.preview_label.setProperty("state", "")
        self.preview_label.style().unpolish(self.preview_label)
        self.preview_label.style().polish(self.preview_label)

        long = side == "Buy"
        eq = self._equity
        # Margin can never exceed equity, so equity is the conservative fallback
        # when the exchange leaves the available field empty.
        avail = self._available if self._available is not None else eq
        rr = abs(tp - entry) / abs(entry - sl) if tp else None

        def v(s):
            return span(s, T.TEXT, weight=500)

        lev_s = f" &nbsp;·&nbsp; {calc['leverage']}x &nbsp;·&nbsp; qty "
        fee_s = f"${calc['fee_usd']:.2f}"
        cushion_s = f"{calc['cushion_pct']:.2f}%"
        l1 = (f"{span(symbol, T.WHITE, 12, 600)} {span(('long' if long else 'short') + ' @', T.TEXT_DIM)} "
              f"{span(fmt_px(entry, tick), T.TEXT, 12)}{span(lev_s, T.TEXT_DIM)}{v(calc['qty'])}")
        l2 = (f"notional {v(fmt_usd(calc['notional_usd'], 0, signed=False))} · "
              f"margin {v(fmt_usd(calc['margin_usd'], 2, signed=False))}"
              + (f" · {calc['margin_usd'] / avail * 100:.1f}% of avail" if avail else ""))
        l3 = f"risk {v(fmt_usd(risk, 2, signed=False))} incl {v(fee_s)} fees"
        if eq:
            l3 += f" · {v(f'{risk / eq * 100:.1f}%')} eq"
        l3 += f" · rr {v(f'1:{rr:.2f}')}" if rr else f" · {v('no target')}"
        tier = (f" · tier {calc['tier']}/{calc['tier_count']}" if calc["tier_count"] > 1 else "")
        l4 = (f"liq {v(fmt_px(calc['liq_price'], tick))} · cushion {v(cushion_s)}"
              f" · mmr {calc['mmr_pct']:.2f}%{tier}")
        self.preview_label.setText(
            f"<span style='color:{T.TEXT_DIM}'>{l1}<br>{l2}<br>{l3}<br>{l4}</span>"
        )

        # ── Warnings ──
        warns: List[tuple] = []     # (level, text, blocking)
        cross_ref = self._crossing_ref(symbol, side, entry)
        if cross_ref is not None:
            book = "ask" if long else "bid"
            if self.postonly_checkbox.isChecked():
                warns.append(("warn", f"entry crosses the {book} {fmt_px(cross_ref, tick)} · post-only will cancel it", False))
            else:
                qty = float(calc["qty"])
                warns.append(("hard", f"entry crosses the {book} {fmt_px(cross_ref, tick)} · fills as taker, "
                                      f"fee ≈ ${entry * qty * config.FEE_TAKER:.2f} vs "
                                      f"${entry * qty * config.FEE_MAKER:.2f} sized", False))
        if tp is not None and ((long and tp <= entry) or (not long and tp >= entry)):
            warns.append(("hard", "target is on the loss side of entry", False))
        if eq and config.MAX_RISK_PCT > 0 and risk > eq * config.MAX_RISK_PCT / 100:
            warns.append(("hard", f"risk {risk / eq * 100:.1f}% of equity is above the "
                                  f"{config.MAX_RISK_PCT:g}% per-trade cap", False))
        open_after = self._gov["open_risk"] + risk
        if eq and config.MAX_OPEN_RISK_PCT > 0 and open_after > eq * config.MAX_OPEN_RISK_PCT / 100:
            warns.append(("warn", f"open risk would be {fmt_usd(open_after, 0, signed=False)} · "
                                  f"{open_after / eq * 100:.1f}% of equity, above the "
                                  f"{config.MAX_OPEN_RISK_PCT:g}% cap", False))
        limit = config.DAILY_LOSS_LIMIT_USD
        if limit > 0:
            today = self._gov["today_pnl"]
            if self._gov["blocked"]:
                warns.append(("hard", f"daily loss limit reached ({fmt_usd(today)} vs −${limit:,.0f}) · "
                                      "no new trades today", True))
            elif today - risk < -limit:
                warns.append(("warn", f"a stop here would breach today's −${limit:,.0f} limit "
                                      f"(now {fmt_usd(today)})", False))
        if config.LOSS_STREAK_CONFIRM > 0 and self._gov["streak"] >= config.LOSS_STREAK_CONFIRM:
            warns.append(("warn", f"{self._gov['streak']} losses in a row · take a breath", False))
        if calc["tier"] > 1:
            warns.append(("warn", f"notional {fmt_usd(calc['notional_usd'], 0, signed=False)} passes the "
                                  f"tier-1 cap · sized with tier {calc['tier']} mmr {calc['mmr_pct']:.2f}%, "
                                  f"max {calc['max_exchange_lev']}x", False))
        if avail is not None and calc["margin_usd"] > avail:
            warns.append(("hard", f"margin {fmt_usd(calc['margin_usd'], 2, signed=False)} exceeds "
                                  f"available {fmt_usd(avail, 2, signed=False)}", True))
        if any(t.symbol == symbol and t.is_active for t in self._trades.values()):
            warns.append(("hard", f"already active on {symbol} · close or cancel it first", True))
        self._warnings = warns
        if warns:
            self.warnings_label.setText("<br>".join(
                span("▍ " + text, T.NEGATIVE if level == "hard" else T.AMBER) for level, text, _ in warns))
            self.warnings_label.show()
        else:
            self.warnings_label.hide()
        self.btn_execute.setEnabled(not any(b for _, _, b in warns))

        # ── Ladder ──
        qty = float(calc["qty"])

        def pnl_at(price: float, exit_rate: float) -> float:
            gross = (price - entry) * qty if long else (entry - price) * qty
            return gross - entry * qty * config.FEE_MAKER - price * qty * exit_rate

        def pct_s(price: float) -> str:
            return fmt_pct(((price - entry) / entry) * (1 if long else -1))

        rows = []
        if tp:
            val = pnl_at(tp, config.FEE_MAKER)
            col = T.POSITIVE if val >= 0 else T.NEGATIVE
            rows.append({"key": "tp", "label": "target", "price": tp, "price_s": fmt_px(tp, tick),
                         "pct_s": pct_s(tp), "usd_s": fmt_usd(val), "usd_c": col,
                         "r_s": fmt_r(val / risk), "r_c": col})
        if last:
            val = pnl_at(last, config.FEE_TAKER)
            col = T.POSITIVE if val >= 0 else T.NEGATIVE
            rows.append({"key": "mark", "label": "mark", "price": last, "price_s": fmt_px(last, tick),
                         "pct_s": pct_s(last), "usd_s": fmt_usd(val), "usd_c": col,
                         "r_s": fmt_r(val / risk), "r_c": col})
        rows.append({"key": "entry", "label": "entry", "price": entry, "price_s": fmt_px(entry, tick),
                     "pct_s": "", "usd_s": f"qty {calc['qty']}", "usd_c": T.TEXT_DIM, "r_s": "", "r_c": T.TEXT_DIM})
        val = pnl_at(sl, config.FEE_TAKER)
        rows.append({"key": "sl", "label": "stop", "price": sl, "price_s": fmt_px(sl, tick),
                     "pct_s": pct_s(sl), "usd_s": fmt_usd(val), "usd_c": T.NEGATIVE,
                     "r_s": fmt_r(val / risk), "r_c": T.NEGATIVE})
        rows.append({"key": "liq", "label": "liq", "price": calc["liq_price"],
                     "price_s": fmt_px(calc["liq_price"], tick), "pct_s": pct_s(calc["liq_price"]),
                     "usd_s": f"cushion {calc['cushion_pct']:.2f}%", "usd_c": T.TEXT_DIM,
                     "r_s": f"{calc['leverage']}x", "r_c": T.TEXT_MUTED})
        self.ladder.set_rows(rows)

    # ─────────────────────────────────────────────────────────────
    #  Keyboard
    # ─────────────────────────────────────────────────────────────

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.KeyPress and isinstance(obj, QLineEdit):
            key = event.key()
            mods = event.modifiers()
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                with_cmd = bool(mods & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier))
                if with_cmd or obj is self.risk_input:
                    self._execute()
                else:
                    self.focusNextChild()
                return True
            if key == Qt.Key.Key_Escape:
                self._reset_ticket()
                return True
            if key in (Qt.Key.Key_Up, Qt.Key.Key_Down) and obj in (
                    self.entry_input, self.stop_input, self.target_input):
                self._nudge(obj, 1 if key == Qt.Key.Key_Up else -1,
                            bool(mods & Qt.KeyboardModifier.ShiftModifier))
                return True
        return super().eventFilter(obj, event)

    def _nudge(self, edit: QLineEdit, direction: int, big: bool):
        raw = edit.text().strip()
        sym = self._normalize_symbol(self.symbol_input.text())
        rules = self._core.cache.get(sym)
        p, r = parse_pct(raw), parse_r(raw)
        if p is not None:
            n = max(0.0, p * 100 + direction * (0.5 if big else 0.05))
            sign = "-" if raw.startswith("-") else ("+" if raw.startswith("+") else "")
            edit.setText(f"{sign}{n:.2f}%")
        elif r is not None:
            n = max(0.0, r + direction * (1.0 if big else 0.25))
            edit.setText(f"{n:g}R")
        elif rules:
            try:
                value = float(raw.replace(",", "").replace("$", ""))
            except ValueError:
                return
            tick = float(rules["tickSize"])
            value = max(tick, value + direction * tick * (10 if big else 1))
            edit.setText(format_value(value, rules["tickSize"]))

    def _reset_ticket(self):
        for edit in (self.entry_input, self.stop_input, self.target_input):
            edit.clear()
        self.risk_input.setText(f"$ {config.DEFAULT_RISK_USD:g}")
        self.strat1_checkbox.setChecked(False)
        self.entry_input.setFocus()

    # ─────────────────────────────────────────────────────────────
    #  Execute
    # ─────────────────────────────────────────────────────────────

    def _execute(self):
        if not self.btn_execute.isEnabled():
            return
        try:
            symbol, side, entry, sl, tp, risk, rules = self._ticket_values()
            calc = self._core.calculate_trade(symbol, side, entry, sl, risk)
        except ValueError as e:
            self._append_log(f"input error: {e}", True)
            return
        self._refresh_preview()
        if not self.btn_execute.isEnabled():
            return

        tick = rules["tickSize"]
        eq = self._equity
        summary = (
            f"{symbol} {'LONG' if side == 'Buy' else 'SHORT'}  ·  {calc['qty']} @ {fmt_px(entry, tick)}\n"
            f"lev {calc['leverage']}x  ·  margin {fmt_usd(calc['margin_usd'], 2, signed=False)}  ·  "
            f"notional {fmt_usd(calc['notional_usd'], 0, signed=False)}\n"
            f"stop {fmt_px(sl, tick)} on mark  ·  "
            f"{'target ' + fmt_px(tp, tick) + ' limit' if tp else 'no target'}\n"
            f"risk {fmt_usd(risk, 2, signed=False)}"
            + (f"  ·  {risk / eq * 100:.1f}% of equity" if eq else "")
            + f"  ·  open after {fmt_usd(self._gov['open_risk'] + risk, 0, signed=False)}"
        )
        hard = [t for lvl, t, _ in self._warnings if lvl == "hard"]
        soft = [t for lvl, t, _ in self._warnings if lvl == "warn"]

        box = QMessageBox(self)
        box.setWindowTitle("Send to Bybit?")
        box.setText(summary)
        if hard or soft:
            box.setInformativeText("\n".join(["⚠ " + t for t in hard] + ["· " + t for t in soft]))
        yes = box.addButton("send", QMessageBox.ButtonRole.AcceptRole)
        no = box.addButton("back", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(no if hard else yes)
        box.setEscapeButton(no)
        box.exec()
        if box.clickedButton() is not yes:
            return

        self.btn_execute.setEnabled(False)
        self._bridge.status_signal.emit("executing trade...", T.ACCENT)
        is_strat1 = self.strat1_checkbox.isChecked()
        is_post_only = self.postonly_checkbox.isChecked()

        def _run():
            try:
                trade = self._core.execute_bracket(
                    symbol=symbol, side=side, entry=entry, sl=sl, tp=tp, risk_usd=risk,
                    strat1=is_strat1, post_only=is_post_only,
                )
                self._bridge.execute_done.emit(trade, "")
            except Exception as e:
                self._bridge.execute_done.emit(None, str(e))

        threading.Thread(target=_run, daemon=True).start()

    def _on_execute_done(self, trade, error_msg: str):
        self._bridge.status_signal.emit("live", T.POSITIVE)
        if error_msg or trade is None:
            self._append_log(f"execution error: {error_msg or 'unknown error'}", True)
        else:
            self._trades[trade.entry_order_id] = trade.clone()
            self._selected_key = trade.entry_order_id
            self._update_trade_panel()
            self._update_governance()
            self._refresh_chips()
            self._append_log(f"trade executed successfully: {trade.symbol}", False)
        self._schedule_preview()

    # ─────────────────────────────────────────────────────────────
    #  Card actions
    # ─────────────────────────────────────────────────────────────

    def _select_card(self, key: str):
        if key == self._selected_key:
            return
        self._selected_key = key
        for k, card in self._cards.items():
            card.set_selected(k == key)

    def _card_action(self, key: str, action: str, payload: Optional[dict] = None):
        t = self._trades.get(key)
        if not t or not t.is_active:
            self._append_log("that trade is no longer active", True)
            return
        rules = self._core.cache.get(t.symbol)
        tick = rules["tickSize"] if rules else "0.01"
        entry = t.fill_price or t.entry_price
        long = t.side == "Buy"
        mark = t.mark_price

        def stop_ok(new_sl: float) -> bool:
            if mark and ((long and new_sl >= mark) or (not long and new_sl <= mark)):
                self._append_log(f"stop {fmt_px(new_sl, tick)} would trigger immediately "
                                 f"(mark {fmt_px(mark, tick)})", True)
                return False
            return True

        if action == "be":
            if not entry:
                return
            new_sl = breakeven_price(entry, t.side)
            if stop_ok(new_sl):
                self._run_action(key, "stop → break-even",
                                 lambda: self._core.modify_brackets(t, new_sl=new_sl))
        elif action == "half":
            dist = t.original_sl_distance or (abs(entry - float(t.stop_loss)) if entry and t.stop_loss else None)
            if not entry or not dist:
                self._append_log("no stop distance to halve", True)
                return
            new_sl = ratchet_price(entry, dist, t.side, -0.5)
            if stop_ok(new_sl):
                self._run_action(key, "stop → −0.5R",
                                 lambda: self._core.modify_brackets(t, new_sl=new_sl))
        elif action == "close":
            qty = t.qty or t.cum_exec_qty or "?"
            reply = QMessageBox.warning(
                self, "Market close",
                f"Close {t.symbol} {'LONG' if long else 'SHORT'} ({qty}) at market?\n"
                f"This exits immediately as taker and cancels the TP/SL bracket.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self._run_action(key, "market close", lambda: self._core.close_position_market(t))
        elif action == "cancel":
            self._run_action(key, "cancel", lambda: self._core.cancel_trade(t))
        elif action == "apply":
            payload = payload or {}
            tp_t, sl_t = payload.get("tp", "").strip(), payload.get("sl", "").strip()
            if not tp_t and not sl_t:
                self._append_log("enter a new tp or sl", True)
                return
            try:
                ref = entry or 0.0
                cur_sl = float(t.stop_loss) if t.stop_loss else None
                new_sl = resolve_stop(sl_t, ref, t.side) if sl_t else None
                new_tp = resolve_target(tp_t, ref, new_sl or cur_sl or ref, t.side) if tp_t else None
            except ValueError as e:
                self._append_log(f"modify error: {e}", True)
                return
            if new_sl is not None and t.is_live and not stop_ok(new_sl):
                return
            card = self._cards.get(key)
            if card:
                card.hide_edit()
            self._run_action(key, "modify brackets",
                             lambda: self._core.modify_brackets(t, new_tp=new_tp, new_sl=new_sl))

    def _run_action(self, key: str, label: str, fn):
        card = self._cards.get(key)
        if card:
            card.set_busy(True)
        self._bridge.status_signal.emit(f"{label}…", T.ACCENT)

        def _run():
            try:
                fn()
                self._bridge.action_done.emit(key, label, "")
            except Exception as e:
                self._bridge.action_done.emit(key, label, str(e))

        threading.Thread(target=_run, daemon=True).start()

    def _on_action_done(self, key: str, label: str, error_msg: str):
        card = self._cards.get(key)
        if card:
            card.set_busy(False)
        self._bridge.status_signal.emit("live", T.POSITIVE)
        sym = self._trades[key].symbol if key in self._trades else key
        if error_msg:
            self._append_log(f"{label} failed for {sym}: {error_msg}", True)
        else:
            self._append_log(f"{label} done for {sym}", False)
        self._panel_dirty = True

    # ─────────────────────────────────────────────────────────────
    #  Trade state → cards
    # ─────────────────────────────────────────────────────────────

    def _on_trade_state_changed(self, trade: TradeState):
        """A snapshot from the core. Keys change once when Bybit's orderId replaces
        our link id, and a synced position may turn into a partial entry — both
        arrive as a new key for a symbol that already has a live card."""
        if trade.entry_order_id in self._trades:
            self._trades[trade.entry_order_id] = trade
        else:
            stale = next((k for k, t in self._trades.items()
                          if t.symbol == trade.symbol and t.is_live), None)
            if stale is not None:
                del self._trades[stale]
            self._trades[trade.entry_order_id] = trade
        self._panel_dirty = True

    def _update_trade_panel(self):
        active = {k: t for k, t in self._trades.items() if t.is_active}

        for k in list(self._cards):
            if k not in active:
                card = self._cards.pop(k)
                self.cards_layout.removeWidget(card)
                card.deleteLater()
        for k, t in active.items():
            card = self._cards.get(k)
            if card is None:
                card = TradeCard(k, on_select=self._select_card, on_action=self._card_action)
                self._cards[k] = card
                # Before the closed row and the trailing stretch
                self.cards_layout.insertWidget(self.cards_layout.count() - 2, card)
            card.update_from(t, self._core.cache.get(t.symbol), TradingCore.estimate_trade_risk(t),
                             self._core.ratchet,
                             ticker=None if t.is_live else self._core.get_ticker(t.symbol))
        if self._selected_key not in active:
            self._selected_key = next(iter(active), None)
        for k, card in self._cards.items():
            card.set_selected(k == self._selected_key)

        self.placeholder.setVisible(not active)
        n_live = sum(1 for t in active.values() if t.is_live)
        n_pending = len(active) - n_live
        self.board_count.setText(f"{n_live} LIVE · {n_pending} PENDING" if active else "")

        # Last close: this session's, else today's from the journal
        closed = [t for t in self._trades.values()
                  if t.phase in (TradeState.PHASE_CLOSED, TradeState.PHASE_CANCELLED)]
        if closed:
            t = max(closed, key=lambda x: x.closed_at or 0)
            rules = self._core.cache.get(t.symbol)
            tick = rules["tickSize"] if rules else None
            pnl = f" &nbsp;·&nbsp; {span(fmt_usd(t.pnl), T.POSITIVE if t.pnl >= 0 else T.NEGATIVE)}" if t.pnl is not None else ""
            stamp = time.strftime("%H:%M", time.localtime(t.closed_at)) if t.closed_at else "--:--"
            self.closed_label.setText(
                f"{stamp} &nbsp; {span(t.symbol, T.TEXT_DIM, weight=600)} &nbsp; "
                f"{'long' if t.side == 'Buy' else 'short'} · {(t.close_type or 'closed').lower()}"
                f" @ {fmt_px(t.close_price, tick) if t.close_price else '--'}{pnl}"
            )
            self.closed_label.show()
        else:
            today = self._journal.since(local_midnight())
            if today:
                t = today[-1]
                pnl = t.get("pnl") or 0.0
                extra = f" · {fmt_r(t['r_multiple'])}" if t.get("r_multiple") is not None else ""
                mfe = f" &nbsp;·&nbsp; mfe {fmt_r(t['mfe_r'])} before the close" if t.get("mfe_r") is not None else ""
                self.closed_label.setText(
                    f"{time.strftime('%H:%M', time.localtime(t.get('closed_at') or 0))} &nbsp; "
                    f"{span(t.get('symbol', ''), T.TEXT_DIM, weight=600)} &nbsp; "
                    f"{'long' if t.get('side') == 'Buy' else 'short'} · closed &nbsp;·&nbsp; "
                    f"{span(fmt_usd(pnl) + extra, T.POSITIVE if pnl >= 0 else T.NEGATIVE)}{mfe}"
                )
                self.closed_label.show()
            else:
                self.closed_label.hide()

    # ─────────────────────────────────────────────────────────────
    #  Governance strip + stats strip
    # ─────────────────────────────────────────────────────────────

    def _update_governance(self):
        active = [t for t in self._trades.values() if t.is_active]
        risks = [TradingCore.estimate_trade_risk(t) for t in active]
        open_risk = sum(r for r in risks if r)
        unknown = sum(1 for r in risks if r is None)
        today = self._journal.stats(since=local_midnight())
        streak = self._journal.loss_streak()
        limit = config.DAILY_LOSS_LIMIT_USD
        blocked = limit > 0 and today["total_pnl"] <= -limit
        self._gov = {"open_risk": open_risk, "unknown": unknown, "today_pnl": today["total_pnl"],
                     "today_n": today["total"], "streak": streak, "blocked": blocked}
        eq = self._equity

        vl, meter = self.gov_cells["open"]
        pct_s = f" · {open_risk / eq * 100:.1f}% eq" if eq else ""
        unk = f" · {unknown} unsized" if unknown else ""
        vl.setText(f"{span(fmt_usd(open_risk, 0, signed=False), T.WHITE, weight=600)}"
                   f"{span(pct_s + f' · {len(active)} trade' + ('s' if len(active) != 1 else '') + unk, T.TEXT_DIM)}")
        frac = open_risk / (eq * config.MAX_OPEN_RISK_PCT / 100) if eq and config.MAX_OPEN_RISK_PCT > 0 else 0
        meter.set_fraction(frac, None if eq else "off")

        vl, meter = self.gov_cells["day"]
        col = T.NEGATIVE if today["total_pnl"] < 0 else T.POSITIVE
        n = today["total"]
        vl.setText(f"{span(fmt_usd(today['total_pnl']), col, weight=600)}"
                   f"{span(f' · {n} closed', T.TEXT_DIM)}"
                   + (span(f" · {abs(min(0, today['total_pnl'])) / limit * 100:.0f}% used", T.TEXT_DIM) if limit > 0 else ""))
        if limit > 0:
            meter.set_fraction(abs(min(0.0, today["total_pnl"])) / limit)
        else:
            meter.set_fraction(0, "off")

        vl, meter = self.gov_cells["streak"]
        pause = config.LOSS_STREAK_CONFIRM
        vl.setText(f"{span(f'{streak} loss' + ('es' if streak != 1 else ''), T.AMBER if pause and streak >= pause else T.WHITE, weight=600)}"
                   + (span(f" · confirm at {pause}", T.TEXT_DIM) if pause else ""))
        meter.set_fraction(streak / pause if pause else 0, None if pause else "off")

        self._schedule_preview()

    def _update_stats_strip(self):
        j = self._journal
        now = time.time()
        today = j.stats(since=local_midnight())
        week = j.stats(since=now - 7 * 86400)
        life = j.stats()

        def cell(name, header, s, extra=""):
            kl, vl = self.strip_cells[name]
            kl.setText(header.upper())
            if s["total"] == 0:
                vl.setText(span("--", T.TEXT_MUTED))
                return
            col = T.POSITIVE if s["total_pnl"] >= 0 else T.NEGATIVE
            vl.setText(f"{span(fmt_usd(s['total_pnl']), col, weight=600)}"
                       f"{span(extra.replace(' ', '&nbsp;'), T.TEXT_DIM, 10)}")

        def n_trades(s):
            return f"{s['total']} trade{'s' if s['total'] != 1 else ''}"

        cell("today", f"today · {n_trades(today)}", today,
             f" · {fmt_r(today['avg_r'])}" if today["avg_r"] else "")
        cell("week", f"7 days · {n_trades(week)}", week, f" · {week['win_rate']:.0f}% wr")
        cell("life", f"lifetime · {n_trades(life)}" + (f" · E {fmt_usd(life['expectancy'])}" if life["total"] else ""),
             life, f" · {life['win_rate']:.0f}% wr · {fmt_r(life['avg_r'])}")

        trades = sorted(j.all_trades, key=lambda t: t.get("closed_at") or 0)
        self.hist.set_values([t["r_multiple"] for t in trades if t.get("r_multiple") is not None])
        self.equity_curve.set_series([t["pnl"] for t in trades if t.get("pnl") is not None])

    # ─────────────────────────────────────────────────────────────
    #  Status, balance, margin mode
    # ─────────────────────────────────────────────────────────────

    def _on_status_updated(self, text: str, color_hex: str):
        self.status_label.setText(text)
        self.status_label.setStyleSheet(f"font-size: 10px; color: {color_hex};")

    def _on_balance_updated(self, equity: float, available):
        self._equity = equity
        if available is not None:
            self._available = float(available)
        self.balance_label.setText(f"${equity:,.2f}")
        self.avail_label.setText(f"avail ${self._available:,.2f}  ·" if self._available is not None else "")
        self._update_governance()

    def _on_margin_mode_sync(self, mode: str):
        self.margin_btn.setText("isolated" if mode == "ISOLATED_MARGIN" else "cross")
        self.margin_btn.setEnabled(True)

    def _on_margin_btn_clicked(self):
        self.margin_btn.setEnabled(False)
        target_iso = self._core.account_margin_mode != "ISOLATED_MARGIN"
        self._bridge.status_signal.emit("switching margin mode...", T.ACCENT)

        def _switch():
            try:
                self._core.set_margin_mode(isolated=target_iso)
            except Exception as e:
                self._bridge.log_signal.emit(f"margin mode: {e}", True)
            finally:
                self._bridge.margin_mode_sync.emit(self._core.account_margin_mode)
                self._bridge.status_signal.emit("live", T.POSITIVE)

        threading.Thread(target=_switch, daemon=True).start()

    # ─────────────────────────────────────────────────────────────
    #  Cache
    # ─────────────────────────────────────────────────────────────

    def _refresh_cache(self):
        self._append_log("refreshing cache...", False)
        self.btn_refresh.setEnabled(False)

        def _do():
            try:
                self._core.cache.refresh()
                self._bridge.cache_done.emit(self._core.cache.count)
            except Exception as e:
                self._bridge.log_signal.emit(f"cache failed: {e}", True)
                self._bridge.cache_done.emit(-1)

        threading.Thread(target=_do, daemon=True).start()

    def _on_cache_refreshed(self, count: int):
        self.btn_refresh.setEnabled(True)
        if count > 0:
            self._append_log(f"cache: {count} symbols", False)
            if not hasattr(self, "_core"):
                return   # the cache loaded synchronously inside TradingCore.__init__
            self._completer_model.setStringList(self._usdt_symbols())
            self._set_trading_enabled(True)
            self._refresh_info()

    # ─────────────────────────────────────────────────────────────
    #  Journal
    # ─────────────────────────────────────────────────────────────

    def _on_journal_updated(self):
        self._update_stats_strip()
        self._update_governance()
        self._refresh_chips()
        self._panel_dirty = True
        if self._journal_dialog is not None and self._journal_dialog.isVisible():
            self._journal_dialog.refresh(self._journal.all_trades, self._journal.stats())

    def _open_journal_dialog(self):
        if self._journal_dialog is None:
            self._journal_dialog = JournalDialog(self, T.STYLESHEET)
        self._journal_dialog.refresh(self._journal.all_trades, self._journal.stats())
        self._journal_dialog.show()
        self._journal_dialog.raise_()
        self._journal_dialog.activateWindow()

    # ─────────────────────────────────────────────────────────────
    #  Log drawer
    # ─────────────────────────────────────────────────────────────

    def _set_drawer(self, open_: bool):
        self._drawer_open = open_
        self.console.setVisible(open_)
        self.drawer_btn.setText(f"{'▾' if open_ else '▸'}  LOG · {self._log_count}")
        if open_:
            sb = self.console.verticalScrollBar()
            sb.setValue(sb.maximum())

    def _append_log(self, msg: str, is_error: bool):
        color = T.NEGATIVE if is_error else T.TEXT_DIM
        stamp = time.strftime("%H:%M:%S")
        self.console.append(f"<span style='color:{T.TEXT_MUTED};'>{stamp}</span>  "
                            f"<span style='color:{color};'>{msg}</span>")
        doc = self.console.document()
        if doc.blockCount() > 500:
            cursor = self.console.textCursor()
            cursor.movePosition(cursor.MoveOperation.Start)
            cursor.movePosition(cursor.MoveOperation.Down, cursor.MoveMode.KeepAnchor,
                                doc.blockCount() - 500)
            cursor.removeSelectedText()
        sb = self.console.verticalScrollBar()
        sb.setValue(sb.maximum())

        self._log_count += 1
        self.drawer_btn.setText(f"{'▾' if self._drawer_open else '▸'}  LOG · {self._log_count}")
        metrics = QFontMetrics(self.drawer_last.font())
        text = metrics.elidedText(f"{stamp}  {msg}", Qt.TextElideMode.ElideRight,
                                  max(120, self.drawer_last.width() - 8))
        self.drawer_last.setText(text)
        self.drawer_last.setStyleSheet(f"font-size: 10.5px; color: {color};")
        if is_error and not self._drawer_open:
            self._set_drawer(True)

    # ─────────────────────────────────────────────────────────────
    #  Helpers
    # ─────────────────────────────────────────────────────────────

    @staticmethod
    def _normalize_symbol(text: str) -> str:
        sym = text.strip().upper()
        if sym and not sym.endswith("USDT"):
            sym += "USDT"
        return sym

    def closeEvent(self, event):
        self._settings.setValue("geometry", self.saveGeometry())
        super().closeEvent(event)


# ──────────────────────────────────────────────────────────────────
#  Entry point
# ──────────────────────────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)

    # Dock/window icon (present locally via the Viridis.app bundle; optional)
    icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "assets", "icon.png")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))
        try:
            # Qt's windowIcon doesn't reach the macOS Dock — set it natively.
            from AppKit import NSApplication, NSImage
            NSApplication.sharedApplication().setApplicationIconImage_(
                NSImage.alloc().initWithContentsOfFile_(icon_path))
        except ImportError:
            pass

    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(T.BG))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(T.TEXT))
    palette.setColor(QPalette.ColorRole.Base, QColor(T.BG_INPUT))
    palette.setColor(QPalette.ColorRole.Text, QColor(T.TEXT))
    palette.setColor(QPalette.ColorRole.Button, QColor(T.BG_RAISED))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(T.TEXT))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(T.ACCENT))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(T.BG))
    app.setPalette(palette)

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
