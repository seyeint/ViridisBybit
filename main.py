"""
Bybit V5 OTOCO Execution Engine — PyQt6 GUI.

Minimal dark terminal interface for bracket order execution
on Bybit linear perpetuals.

Usage:
    source .venv/bin/activate
    python main.py
"""

import sys
import threading
import time
from PyQt6.QtCore import Qt, pyqtSignal, QObject, QStringListModel, QTimer
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QTextEdit,
    QDoubleSpinBox, QCheckBox, QFrame, QCompleter, QMessageBox, QDialog,
)

import config
from trading_core import TradingCore, TradeState
from journal import TradeJournal


# ──────────────────────────────────────────────────────────────────
#  Signal bridge (WebSocket thread → Qt main thread)
# ──────────────────────────────────────────────────────────────────

class SignalBridge(QObject):
    log_signal = pyqtSignal(str, bool)
    trade_signal = pyqtSignal(object)
    cache_done = pyqtSignal(int)
    margin_mode_sync = pyqtSignal(str)
    margin_mode_warning = pyqtSignal(str)
    status_signal = pyqtSignal(str, str)
    balance_signal = pyqtSignal(float)
    execute_done = pyqtSignal(object, str)
    modify_done = pyqtSignal(str, str)
    cancel_done = pyqtSignal(str, str)
    close_done = pyqtSignal(str, str)
    positions_synced = pyqtSignal(dict, dict)
    journal_updated = pyqtSignal()


# ──────────────────────────────────────────────────────────────────
#  Palette
# ──────────────────────────────────────────────────────────────────

BG         = "#111217"
BG_RAISED  = "#16181E"
BG_INPUT   = "#1A1D25"
BORDER     = "#22252E"
BORDER_FCS = "#3A3F4D"
TEXT       = "#C4C8D4"
TEXT_DIM   = "#5C6170"
TEXT_MUTED = "#3D4150"
ACCENT     = "#6E9FFF"
POSITIVE   = "#3FB68B"
NEGATIVE   = "#D9534F"

STYLESHEET = f"""
QMainWindow {{ background-color: {BG}; }}
QWidget {{
    color: {TEXT};
    font-family: 'Menlo', 'Consolas', monospace;
    font-size: 12px;
}}

/* ── Inputs ──────────────────────────────────── */
QLineEdit {{
    background: {BG_INPUT};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 3px;
    padding: 8px 9px;
    font-size: 13px;
    selection-background-color: {ACCENT};
    selection-color: {BG};
}}
QLineEdit:focus {{ border-color: {BORDER_FCS}; }}

QLineEdit#symbol_input {{
    font-size: 14px;
    font-weight: 700;
    color: #FFFFFF;
    letter-spacing: 0.5px;
}}

QDoubleSpinBox {{
    background: {BG_INPUT};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 3px;
    padding: 8px 9px;
    font-size: 13px;
}}
QDoubleSpinBox:focus {{ border-color: {BORDER_FCS}; }}
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
    width: 0; height: 0; border: none;
}}

/* ── Labels ──────────────────────────────────── */
QLabel {{ background: transparent; border: none; padding: 0; margin: 0; }}
QLabel#dim {{ font-size: 10px; color: {TEXT_DIM}; }}
QLabel#muted {{ font-size: 10px; color: {TEXT_MUTED}; letter-spacing: 1px; }}

/* ── Checkboxes ──────────────────────────────── */
QCheckBox {{ font-size: 11px; color: {TEXT_DIM}; spacing: 5px; }}
QCheckBox::indicator {{
    width: 12px; height: 12px;
    border: 1px solid {BORDER_FCS};
    border-radius: 2px;
    background: {BG_INPUT};
}}
QCheckBox::indicator:checked {{
    background: {ACCENT};
    border-color: {ACCENT};
}}

/* ── Buttons ─────────────────────────────────── */
QPushButton {{
    font-size: 12px;
    font-weight: 600;
    border-radius: 3px;
    padding: 9px 14px;
    border: 1px solid {BORDER};
    background: {BG_RAISED};
    color: {TEXT_DIM};
}}
QPushButton:hover {{ border-color: {BORDER_FCS}; color: {TEXT}; }}
QPushButton:pressed {{ background: {BG_INPUT}; }}

/* ── Console ─────────────────────────────────── */
QTextEdit#console {{
    background: {BG};
    color: {TEXT_DIM};
    border: 1px solid {BORDER};
    border-radius: 3px;
    padding: 6px;
    font-size: 10px;
}}

/* ── Autocomplete ────────────────────────────── */
QListView {{
    background: {BG_INPUT};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 3px;
    padding: 2px;
    font-size: 12px;
    font-weight: 600;
    outline: none;
}}
QListView::item {{ padding: 5px 8px; border-radius: 2px; }}
QListView::item:hover {{ background: {BG_RAISED}; color: #FFFFFF; }}
QListView::item:selected {{ background: {BORDER}; color: #FFFFFF; }}

/* ── Scrollbar ───────────────────────────────── */
QScrollBar:vertical {{
    background: {BG}; width: 5px; border: none;
}}
QScrollBar::handle:vertical {{
    background: {BORDER}; border-radius: 2px; min-height: 18px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
"""


# ──────────────────────────────────────────────────────────────────
#  Main Window
# ──────────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Viridis")
        self.setMinimumSize(940, 580)
        self.resize(1060, 660)
        self.setStyleSheet(STYLESHEET)

        self._bridge = SignalBridge()
        self._bridge.log_signal.connect(self._append_log)
        self._bridge.trade_signal.connect(self._on_trade_state_changed)
        self._bridge.cache_done.connect(self._on_cache_refreshed)
        self._bridge.margin_mode_sync.connect(self._on_margin_mode_sync)
        self._bridge.margin_mode_warning.connect(self._on_margin_mode_warning)
        self._bridge.status_signal.connect(self._on_status_updated)
        self._bridge.balance_signal.connect(self._on_balance_updated)
        self._bridge.execute_done.connect(self._on_execute_done)
        self._bridge.modify_done.connect(self._on_modify_done)
        self._bridge.cancel_done.connect(self._on_cancel_done)
        self._bridge.close_done.connect(self._on_close_done)
        self._bridge.positions_synced.connect(self._on_positions_synced)
        self._bridge.journal_updated.connect(self._on_journal_updated)

        self._trades: dict[str, TradeState] = {}   # order_id → TradeState
        self._journal = TradeJournal()
        self._journal_dialog = None   # lazily-created history pop-out
        self._reconcile_lock = threading.Lock()  # serialise journal reconciles
        self._is_long = True
        self._equity = None        # cached wallet equity for heatmap
        self._tick_running = False  # guards against overlapping tick threads

        self._build_ui()

        self._core = TradingCore(
            on_log=lambda msg, err: self._bridge.log_signal.emit(msg, err),
            on_trade_update=lambda t: self._bridge.trade_signal.emit(t),
            on_cache_complete=lambda count: self._bridge.cache_done.emit(count),
            on_balance=lambda b: self._bridge.balance_signal.emit(b),
        )

        self._setup_autocomplete()
        self._post_boot()

    # ─────────────────────────────────────────────────────────────
    #  UI
    # ─────────────────────────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Top bar ──────────────────────────────────────────────
        topbar = QWidget()
        topbar.setFixedHeight(40)
        topbar.setStyleSheet(f"background: {BG_RAISED}; border-bottom: 1px solid {BORDER};")
        tb = QHBoxLayout(topbar)
        tb.setContentsMargins(14, 0, 14, 0)

        title = QLabel("VIRIDIS")
        title.setStyleSheet(f"font-size: 12px; font-weight: 800; color: {TEXT_DIM}; letter-spacing: 2px;")
        tb.addWidget(title)
        tb.addStretch()

        self.balance_label = QLabel("--")
        self.balance_label.setStyleSheet(f"font-size: 12px; font-weight: 600; color: {TEXT};")
        tb.addWidget(self.balance_label)

        sep1 = QLabel("")
        sep1.setFixedWidth(12)
        tb.addWidget(sep1)

        self.heatmap_label = QLabel("")
        self.heatmap_label.setTextFormat(Qt.TextFormat.RichText)
        self.heatmap_label.setStyleSheet(f"font-size: 10px;")
        tb.addWidget(self.heatmap_label)

        tb.addStretch()

        self.status_label = QLabel("connecting")
        self.status_label.setStyleSheet(f"font-size: 10px; color: {TEXT_MUTED};")
        tb.addWidget(self.status_label)

        root.addWidget(topbar)

        # ── Main content ─────────────────────────────────────────
        content = QWidget()
        content.setStyleSheet(f"background: {BG};")
        cols = QHBoxLayout(content)
        cols.setContentsMargins(14, 10, 14, 10)
        cols.setSpacing(14)

        # ═══ LEFT — Order Entry ══════════════════════════════════
        left = QVBoxLayout()
        left.setSpacing(5)

        # Symbol + Side
        row1 = QHBoxLayout()
        row1.setSpacing(8)

        sym_col = QVBoxLayout()
        sym_col.setSpacing(1)
        sym_col.addWidget(self._lbl("SYMBOL"))
        self.symbol_input = QLineEdit()
        self.symbol_input.setObjectName("symbol_input")
        self.symbol_input.setPlaceholderText("BTC")
        self.symbol_input.textChanged.connect(self._on_symbol_changed)
        sym_col.addWidget(self.symbol_input)
        row1.addLayout(sym_col, 3)

        side_col = QVBoxLayout()
        side_col.setSpacing(1)
        side_col.addWidget(self._lbl("SIDE"))
        self.side_toggle = QPushButton("LONG")
        self.side_toggle.setMinimumHeight(36)
        self.side_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.side_toggle.clicked.connect(self._toggle_side)
        self._apply_side_style()
        side_col.addWidget(self.side_toggle)
        row1.addLayout(side_col, 1)

        left.addLayout(row1)

        # Info
        self.info_label = QLabel("")
        self.info_label.setObjectName("dim")
        left.addWidget(self.info_label)

        # Live pre-trade price (click → fill entry)
        self.price_btn = QPushButton("")
        self.price_btn.setVisible(False)
        self.price_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.price_btn.setToolTip("Live last price — click to use as entry")
        self.price_btn.setStyleSheet(
            f"text-align: left; font-size: 11px; font-weight: 600; "
            f"color: {ACCENT}; background: transparent; border: none; padding: 1px 0;"
        )
        self.price_btn.clicked.connect(self._use_live_price)
        left.addWidget(self.price_btn)

        # Entry + SL
        row2 = QHBoxLayout()
        row2.setSpacing(8)
        for label, attr, ph in [("ENTRY", "entry_input", "limit price"), ("STOP LOSS", "sl_input", "trigger")]:
            col = QVBoxLayout()
            col.setSpacing(1)
            col.addWidget(self._lbl(label))
            inp = QLineEdit()
            inp.setPlaceholderText(ph)
            setattr(self, attr, inp)
            col.addWidget(inp)
            row2.addLayout(col)
        left.addLayout(row2)

        # TP + Risk
        row3 = QHBoxLayout()
        row3.setSpacing(8)

        tp_col = QVBoxLayout()
        tp_col.setSpacing(1)
        tp_col.addWidget(self._lbl("TAKE PROFIT"))
        self.tp_input = QLineEdit()
        self.tp_input.setPlaceholderText("optional")
        tp_col.addWidget(self.tp_input)
        row3.addLayout(tp_col)

        risk_col = QVBoxLayout()
        risk_col.setSpacing(1)
        risk_col.addWidget(self._lbl("RISK"))
        self.risk_input = QDoubleSpinBox()
        self.risk_input.setRange(1, 1_000_000)
        self.risk_input.setValue(config.DEFAULT_RISK_USD)
        self.risk_input.setPrefix("$ ")
        self.risk_input.setDecimals(2)
        risk_col.addWidget(self.risk_input)
        row3.addLayout(risk_col)

        left.addLayout(row3)

        # Options
        opts = QHBoxLayout()
        self.iso_checkbox = QCheckBox("isolated")
        self.iso_checkbox.setChecked(True)
        self.iso_checkbox.clicked.connect(self._on_margin_checkbox_clicked)
        opts.addWidget(self.iso_checkbox)

        self.strat1_checkbox = QCheckBox("strat1")
        self.strat1_checkbox.setEnabled(False)
        self.strat1_checkbox.setToolTip("Smart trailing SL: tightens at 75% and locks breakeven at 90% of TP journey")
        opts.addWidget(self.strat1_checkbox)

        self.postonly_checkbox = QCheckBox("post-only")
        self.postonly_checkbox.setToolTip(
            "Cancel instead of filling immediately if the entry crosses the book "
            "(guarantees maker entry, so sizing fees match reality)"
        )
        opts.addWidget(self.postonly_checkbox)

        opts.addStretch()
        self.btn_preview = QPushButton("preview")
        self.btn_preview.clicked.connect(self._preview_trade)
        opts.addWidget(self.btn_preview)
        left.addLayout(opts)

        # Wire TP field → strat1 checkbox enabled state
        self.tp_input.textChanged.connect(self._on_tp_changed)

        # Preview
        self.preview_label = QLabel("enter parameters and preview")
        self.preview_label.setObjectName("dim")
        self.preview_label.setWordWrap(True)
        self.preview_label.setMinimumHeight(40)
        self.preview_label.setStyleSheet(
            f"color: {TEXT_DIM}; font-size: 11px; background: {BG_RAISED}; "
            f"border: 1px solid {BORDER}; border-radius: 3px; padding: 8px;"
        )
        left.addWidget(self.preview_label)

        # Execute
        self.btn_execute = QPushButton("EXECUTE LONG")
        self.btn_execute.setMinimumHeight(44)
        self.btn_execute.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_execute.clicked.connect(self._execute_from_toggle)
        self._apply_execute_style()
        left.addWidget(self.btn_execute)

        left.addStretch()
        cols.addLayout(left, 3)

        # Separator
        vsep = QFrame()
        vsep.setFrameShape(QFrame.Shape.VLine)
        vsep.setStyleSheet(f"background: {BORDER}; max-width: 1px;")
        cols.addWidget(vsep)

        # ═══ RIGHT — Management + Logs ═══════════════════════════
        right = QVBoxLayout()
        right.setSpacing(6)

        right.addWidget(self._lbl("ACTIVE TRADES"))

        self._trades_container = QVBoxLayout()
        self._trades_container.setSpacing(3)
        self._trades_widget = QWidget()
        self._trades_widget.setStyleSheet(f"background: transparent;")
        self._trades_widget.setLayout(self._trades_container)
        # Placeholder shown until the first trade-panel rebuild
        placeholder = QLabel("no active trades")
        placeholder.setStyleSheet(
            f"color: {TEXT_MUTED}; font-size: 11px; font-weight: 600; "
            f"background: {BG_RAISED}; border: 1px solid {BORDER}; "
            f"border-radius: 3px; padding: 8px 10px;"
        )
        self._trades_container.addWidget(placeholder)
        right.addWidget(self._trades_widget)

        # Modify
        mod_row = QHBoxLayout()
        mod_row.setSpacing(8)
        for label, attr in [("NEW TP", "new_tp_input"), ("NEW SL", "new_sl_input")]:
            col = QVBoxLayout()
            col.setSpacing(1)
            col.addWidget(self._lbl(label))
            inp = QLineEdit()
            inp.setPlaceholderText("--")
            setattr(self, attr, inp)
            col.addWidget(inp)
            mod_row.addLayout(col)
        right.addLayout(mod_row)

        mgmt = QHBoxLayout()
        mgmt.setSpacing(8)
        self.btn_modify = QPushButton("modify")
        self.btn_modify.clicked.connect(self._modify_brackets)
        self.btn_modify.setEnabled(False)
        self.btn_modify.setStyleSheet(
            f"border: 1px solid {ACCENT}; color: {ACCENT}; background: transparent;"
        )
        mgmt.addWidget(self.btn_modify)

        self.btn_cancel = QPushButton("cancel")
        self.btn_cancel.clicked.connect(self._cancel_trade)
        self.btn_cancel.setEnabled(False)
        mgmt.addWidget(self.btn_cancel)

        self.btn_close = QPushButton("close mkt")
        self.btn_close.clicked.connect(self._close_position)
        self.btn_close.setEnabled(False)
        self.btn_close.setToolTip("Close the selected position immediately at market (reduce-only)")
        self.btn_close.setStyleSheet(
            f"border: 1px solid {NEGATIVE}; color: {NEGATIVE}; background: transparent;"
        )
        mgmt.addWidget(self.btn_close)
        right.addLayout(mgmt)

        # Journal stats
        jsep = QFrame()
        jsep.setFrameShape(QFrame.Shape.HLine)
        jsep.setFixedHeight(1)
        jsep.setStyleSheet(f"background: {BORDER};")
        right.addWidget(jsep)

        jhdr = QHBoxLayout()
        jhdr.addStretch()
        self.btn_journal = QPushButton("history")
        self.btn_journal.setStyleSheet(
            f"font-size: 9px; padding: 3px 8px; color: {TEXT_MUTED}; "
            f"border: 1px solid {BORDER}; background: transparent;"
        )
        self.btn_journal.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_journal.clicked.connect(self._open_journal_dialog)
        jhdr.addWidget(self.btn_journal)
        right.addLayout(jhdr)

        self.journal_label = QLabel("")
        self.journal_label.setTextFormat(Qt.TextFormat.RichText)
        self.journal_label.setWordWrap(True)
        self.journal_label.setStyleSheet(
            f"font-size: 10px; color: {TEXT_DIM}; background: {BG_RAISED}; "
            f"border: 1px solid {BORDER}; border-radius: 3px; padding: 6px 8px;"
        )
        right.addWidget(self.journal_label)
        self._update_journal_stats()

        # Logs
        hsep = QFrame()
        hsep.setFrameShape(QFrame.Shape.HLine)
        hsep.setFixedHeight(1)
        hsep.setStyleSheet(f"background: {BORDER};")
        right.addWidget(hsep)

        log_header = QHBoxLayout()
        log_header.addWidget(self._lbl("LOG"))
        log_header.addStretch()
        self.btn_refresh = QPushButton("refresh cache")
        self.btn_refresh.setStyleSheet(
            f"font-size: 9px; padding: 3px 8px; color: {TEXT_MUTED}; "
            f"border: 1px solid {BORDER}; background: transparent;"
        )
        self.btn_refresh.clicked.connect(self._refresh_cache)
        log_header.addWidget(self.btn_refresh)
        right.addLayout(log_header)

        self.console = QTextEdit()
        self.console.setObjectName("console")
        self.console.setReadOnly(True)
        right.addWidget(self.console, 1)

        cols.addLayout(right, 6)
        root.addWidget(content, 1)

    # ─────────────────────────────────────────────────────────────
    #  Autocomplete
    # ─────────────────────────────────────────────────────────────

    def _set_trading_enabled(self, enabled: bool):
        self.btn_execute.setEnabled(enabled)
        self.btn_preview.setEnabled(enabled)
        if enabled:
            self.symbol_input.setPlaceholderText("BTC")
        else:
            self.symbol_input.setPlaceholderText("loading symbology...")

    def _setup_autocomplete(self):
        all_symbols = self._core.cache.symbols
        symbols = [s for s in all_symbols if s.endswith("USDT") and "-" not in s]

        self._completer_model = QStringListModel(symbols)
        self._completer = QCompleter()
        self._completer.setModel(self._completer_model)
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self._completer.setMaxVisibleItems(10)
        self._completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)

        popup = self._completer.popup()
        # Popup inherits QListView styles from global STYLESHEET
        self.symbol_input.setCompleter(self._completer)
        
        self._set_trading_enabled(len(symbols) > 0)

    # ─────────────────────────────────────────────────────────────
    #  Post-boot
    # ─────────────────────────────────────────────────────────────

    def _post_boot(self):
        bal = self._core.get_wallet_balance()
        if bal is not None:
            self._equity = bal
            self.balance_label.setText(f"${bal:,.2f}")

        # Sync existing positions and pending orders from Bybit
        synced = self._core.sync_existing()
        for t in synced:
            self._trades[t.entry_order_id] = t
        if synced:
            self._update_trade_panel()

        # Render heatmap + journal now that balance + positions are loaded
        self._update_heatmap()
        self._update_journal_stats()

        # Query and sync margin mode + actual fee tier on boot
        def _get_margin_boot():
            try:
                mode = self._core.get_account_margin_mode()
                self._core.account_margin_mode = mode
                self._bridge.margin_mode_sync.emit(mode)
            except Exception:
                pass
            self._core.sync_fee_rates()
        threading.Thread(target=_get_margin_boot, daemon=True).start()

        self._core.connect_websocket()

        # Reconcile the journal against Bybit's closed-PnL history (captures
        # trades closed while the app was off). Off the UI thread — it's network.
        threading.Thread(target=self._reconcile_journal, daemon=True).start()

        # Slow fallback poll (30s) — detects external closes + refreshes balance.
        # Live PnL comes from the public ticker WebSocket in real time.
        self._tick_timer = QTimer()
        self._tick_timer.timeout.connect(self._tick_positions)
        self._tick_timer.start(30_000)

        # UI refresh throttle (500ms max) — prevents ticker from rebuilding
        # widgets hundreds of times per second and crashing Qt.
        self._panel_dirty = False
        self._panel_timer = QTimer()
        self._panel_timer.timeout.connect(self._flush_panel)
        self._panel_timer.start(500)

        # Live pre-trade price refresh (cheap label update off the ticker cache)
        self._price_timer = QTimer()
        self._price_timer.timeout.connect(self._refresh_price)
        self._price_timer.start(500)

        # WS health poll — the status pill reflects actual socket state, not a
        # boot-time assumption. Stays "connecting" until both streams are up.
        self._ws_was_up = False
        self._ws_health_state = None
        self._health_timer = QTimer()
        self._health_timer.timeout.connect(self._check_ws_health)
        self._health_timer.start(5_000)

    def _check_ws_health(self):
        priv, pub = self._core.ws_health()
        state = (priv, pub)
        changed = state != self._ws_health_state
        if not changed and priv and pub:
            return  # healthy steady-state: don't stomp transient statuses
        self._ws_health_state = state

        if priv and pub:
            self._ws_was_up = True
            self._on_status_updated("live", POSITIVE)
            self._append_log("websocket streams healthy", False)
        elif not self._ws_was_up:
            return  # still booting — keep "connecting", core logs any failure
        elif priv:
            # Re-asserted every 5s while degraded so it can't be masked.
            self._on_status_updated("ticker ws down — pnl lags", "#E8A838")
            if changed:
                self._append_log(
                    "public ticker stream lost — live pnl degraded "
                    "(30s REST fallback active)", True)
        else:
            self._on_status_updated("ws down — 30s REST fallback", NEGATIVE)
            if changed:
                self._append_log(
                    "private stream lost — fill events may be delayed "
                    "(30s REST fallback active)", True)

    # ─────────────────────────────────────────────────────────────
    #  Position tick (periodic PnL refresh)
    # ─────────────────────────────────────────────────────────────

    def _tick_positions(self):
        """Slow poll (30s): sync position state, detect external closes, refresh balance."""
        if self._tick_running:
            return
        self._tick_running = True
        def _do():
            try:
                # Refresh margin mode
                try:
                    mode = self._core.get_account_margin_mode()
                    if mode != self._core.account_margin_mode:
                        self._core.account_margin_mode = mode
                        self._bridge.margin_mode_sync.emit(mode)
                except Exception:
                    pass

                # Refresh balance
                bal = self._core.get_wallet_balance()
                if bal is not None:
                    self._bridge.balance_signal.emit(bal)

                # Sync positions
                res = self._core.client.get_positions(
                    category="linear", settleCoin="USDT"
                )
                positions = {p["symbol"]: p for p in res["result"]["list"]
                             if float(p.get("size", "0")) > 0}

                # Collect TP/SL from conditional orders FIRST (before mutating trades)
                tp_sl_map: dict[str, dict] = {}  # symbol → {"tp": ..., "sl": ...}
                for order in self._core._get_open_orders_all():
                    sot = order.get("stopOrderType", "")
                    sym = order.get("symbol", "")
                    trigger = order.get("triggerPrice", "")
                    if sot in ("TakeProfit", "StopLoss", "PartialTakeProfit", "PartialStopLoss") and trigger:
                        if sym not in tp_sl_map:
                            tp_sl_map[sym] = {}
                        if "TakeProfit" in sot:
                            tp_sl_map[sym]["tp"] = trigger
                        elif "StopLoss" in sot:
                            tp_sl_map[sym]["sl"] = trigger

                # Emit synced positions and tp_sl_map to mutate on the main thread safely
                self._bridge.positions_synced.emit(positions, tp_sl_map)

                # Keep the journal current with the exchange (cheap, ~every 30s).
                self._reconcile_journal()

            except Exception as e:
                self._bridge.log_signal.emit(f"position sync error: {e}", True)
            finally:
                self._tick_running = False

        threading.Thread(target=_do, daemon=True).start()

    def _flush_panel(self):
        """Called every 500ms by timer. Only rebuilds widgets if data changed."""
        if not self._panel_dirty:
            return
        self._panel_dirty = False
        self._update_trade_panel()
        self._update_heatmap()
        self._update_journal_stats()

    # ─────────────────────────────────────────────────────────────
    #  Side toggle
    # ─────────────────────────────────────────────────────────────

    def _toggle_side(self):
        self._is_long = not self._is_long
        self._apply_side_style()
        self._apply_execute_style()

    def _apply_side_style(self):
        if self._is_long:
            self.side_toggle.setText("LONG")
            self.side_toggle.setStyleSheet(
                f"background: {POSITIVE}; color: #FFF; font-size: 13px; "
                f"font-weight: 700; border: none; border-radius: 3px; padding: 8px;"
            )
        else:
            self.side_toggle.setText("SHORT")
            self.side_toggle.setStyleSheet(
                f"background: {NEGATIVE}; color: #FFF; font-size: 13px; "
                f"font-weight: 700; border: none; border-radius: 3px; padding: 8px;"
            )

    def _apply_execute_style(self):
        if self._is_long:
            self.btn_execute.setText("EXECUTE LONG")
            self.btn_execute.setStyleSheet(
                f"background: {POSITIVE}; color: #FFF; font-size: 13px; "
                f"font-weight: 700; border: none; border-radius: 3px;"
            )
        else:
            self.btn_execute.setText("EXECUTE SHORT")
            self.btn_execute.setStyleSheet(
                f"background: {NEGATIVE}; color: #FFF; font-size: 13px; "
                f"font-weight: 700; border: none; border-radius: 3px;"
            )

    def _execute_from_toggle(self):
        self._execute("Buy" if self._is_long else "Sell")

    # ─────────────────────────────────────────────────────────────
    #  Symbol changed
    # ─────────────────────────────────────────────────────────────

    def _on_symbol_changed(self, text: str):
        sym = self._normalize_symbol(text)
        rules = self._core.cache.get(sym)
        if rules:
            mmr = rules.get('mmr', 0.005) * 100
            self.info_label.setText(
                f"max {int(rules['maxLev'])}x  |  tick {rules['tickSize']}  |  "
                f"step {rules['qtyStep']}  |  mmr {mmr:.1f}%"
            )
            self._core.watch_symbol(sym)
        else:
            self.info_label.setText("")
        self._refresh_price()

    def _refresh_price(self):
        """Update the live pre-trade price line (500ms timer + symbol change)."""
        sym = self._normalize_symbol(self.symbol_input.text())
        tk = self._core.get_ticker(sym) if self._core.cache.get(sym) else {}
        last = tk.get("lastPrice")
        if not last:
            if self._core.cache.get(sym):
                # No data yet — (re)subscribe; no-op if already subscribed,
                # self-heals subs requested before the public WS was up.
                self._core.watch_symbol(sym)
            self.price_btn.setVisible(False)
            return
        bid, ask = tk.get("bid1Price"), tk.get("ask1Price")
        ba = f"   bid {bid} / ask {ask}" if bid and ask else ""
        self.price_btn.setText(f"live {last}{ba}")
        self.price_btn.setVisible(True)

    def _use_live_price(self):
        sym = self._normalize_symbol(self.symbol_input.text())
        last = self._core.get_ticker(sym).get("lastPrice")
        if last:
            self.entry_input.setText(last)

    def _crossing_ref(self, symbol: str, side: str, entry: float):
        """
        Best-bid/ask the entry limit would cross (None if it rests).
        A buy at/above the ask — or sell at/below the bid — fills immediately
        as taker, breaking the maker-fee assumption baked into sizing.
        """
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
    #  Preview
    # ─────────────────────────────────────────────────────────────

    def _preview_trade(self):
        try:
            symbol, side, entry, sl, tp, risk = self._parse_inputs()
            calc = self._core.calculate_trade(symbol, side, entry, sl, risk)

            rr = ""
            if tp:
                reward = abs(tp - entry)
                risk_dist = abs(entry - sl)
                if risk_dist > 0:
                    rr = f"  |  rr 1:{reward/risk_dist:.2f}"

            tp_str = f"  tp {tp:,.2f}" if tp else ""

            # Warn if TP is on the wrong side of entry (loss-cutting scenario)
            tp_warn = ""
            if tp:
                if (side == "Buy" and tp <= entry) or (side == "Sell" and tp >= entry):
                    tp_warn = "\n⚠ TP is on the loss side of entry"

            cross_warn = ""
            cross_ref = self._crossing_ref(symbol, side, entry)
            if cross_ref is not None:
                verb = "cancelled (post-only)" if self.postonly_checkbox.isChecked() \
                    else "filled immediately as TAKER"
                cross_warn = f"\n⚠ entry crosses the book ({cross_ref:g}) — will be {verb}"

            self.preview_label.setText(
                f"{calc['symbol']}  {calc['side'].lower()}  @  {calc['entry']}\n"
                f"sl {calc['sl']}  ({calc['sl_distance_pct']:.2f}%){tp_str}\n"
                f"lev {calc['leverage']}x  |  qty {calc['qty']}  |  "
                f"notional ${calc['notional_usd']:,.0f}  |  "
                f"margin ${calc['margin_usd']:,.2f}  |  "
                f"risk ${calc['risk_usd']:.0f} (incl ~${calc.get('fee_usd', 0):.2f} fees){rr}{tp_warn}{cross_warn}"
            )

            border_color = NEGATIVE if tp_warn else ("#E8A838" if cross_warn else BORDER_FCS)
            self.preview_label.setStyleSheet(
                f"color: {TEXT}; font-size: 11px; background: {BG_RAISED}; "
                f"border: 1px solid {border_color}; border-radius: 3px; padding: 8px;"
            )
        except Exception as e:
            self.preview_label.setText(str(e))
            self.preview_label.setStyleSheet(
                f"color: {NEGATIVE}; font-size: 11px; background: {BG_RAISED}; "
                f"border: 1px solid {NEGATIVE}; border-radius: 3px; padding: 8px;"
            )

    # ─────────────────────────────────────────────────────────────
    #  Execute
    # ─────────────────────────────────────────────────────────────

    def _execute(self, side_override: str):
        try:
            symbol, _, entry, sl, tp, risk = self._parse_inputs()

            # TP on wrong side of entry — confirm before proceeding
            if tp is not None:
                wrong_side = (
                    (side_override == "Buy" and tp <= entry) or
                    (side_override == "Sell" and tp >= entry)
                )
                if wrong_side:
                    reply = QMessageBox.warning(
                        self, "TP below entry",
                        f"Take Profit ({tp:,.2f}) is on the loss side of Entry ({entry:,.2f}).\n"
                        f"This will lock in a loss. Continue?",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.No,
                    )
                    if reply != QMessageBox.StandardButton.Yes:
                        return

            # Entry crosses the book — make the taker fill (or post-only
            # cancel) a deliberate choice, never an accident.
            is_post_only = self.postonly_checkbox.isChecked()
            cross_ref = self._crossing_ref(symbol, side_override, entry)
            if cross_ref is not None and not is_post_only:
                reply = QMessageBox.warning(
                    self, "Entry crosses the book",
                    f"Entry {entry:,.6g} is through the "
                    f"{'ask' if side_override == 'Buy' else 'bid'} ({cross_ref:,.6g}).\n"
                    f"It will fill immediately as TAKER at ~market — higher fee "
                    f"than sized for, at a price different from your plan.\n\nContinue?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if reply != QMessageBox.StandardButton.Yes:
                    return

            self.btn_execute.setEnabled(False)
            self.btn_preview.setEnabled(False)
            self._bridge.status_signal.emit("executing trade...", ACCENT)

            # Read Qt widgets on main thread before spawning background thread
            is_isolated = self.iso_checkbox.isChecked()
            is_strat1 = self.strat1_checkbox.isChecked()

            def _run():
                try:
                    trade = self._core.execute_bracket(
                        symbol=symbol,
                        side=side_override,
                        entry=entry,
                        sl=sl,
                        tp=tp,
                        risk_usd=risk,
                        isolated=is_isolated,
                        strat1=is_strat1,
                        post_only=is_post_only,
                    )
                    self._bridge.execute_done.emit(trade, "")
                except Exception as e:
                    self._bridge.execute_done.emit(None, str(e))
            
            threading.Thread(target=_run, daemon=True).start()
        except Exception as e:
            self._append_log(f"input error: {e}", True)

    # ─────────────────────────────────────────────────────────────
    #  Modify / Cancel
    # ─────────────────────────────────────────────────────────────

    def _modify_brackets(self):
        trade = self._get_trade_for_symbol()
        if not trade:
            self._append_log("no active trade for this symbol", True)
            return
        try:
            tp_t = self.new_tp_input.text().strip()
            sl_t = self.new_sl_input.text().strip()
            new_tp = float(tp_t) if tp_t else None
            new_sl = float(sl_t) if sl_t else None
            if new_tp is None and new_sl is None:
                self._append_log("enter a new tp or sl", True)
                return
            
            self.btn_modify.setEnabled(False)
            self._bridge.status_signal.emit("modifying brackets...", ACCENT)
            
            def _run():
                try:
                    self._core.modify_brackets(trade, new_tp=new_tp, new_sl=new_sl)
                    self._bridge.modify_done.emit(trade.symbol, "")
                except Exception as e:
                    self._bridge.modify_done.emit(trade.symbol, str(e))
            
            threading.Thread(target=_run, daemon=True).start()

        except Exception as e:
            self._append_log(f"modify error: {e}", True)

    def _cancel_trade(self):
        trade = self._get_trade_for_symbol()
        if not trade:
            self._append_log("no active trade for this symbol", True)
            return
        try:
            self.btn_cancel.setEnabled(False)
            self._bridge.status_signal.emit("cancelling trade...", ACCENT)
            
            def _run():
                try:
                    self._core.cancel_trade(trade)
                    self._bridge.cancel_done.emit(trade.symbol, "")
                except Exception as e:
                    self._bridge.cancel_done.emit(trade.symbol, str(e))
            
            threading.Thread(target=_run, daemon=True).start()
        except Exception as e:
            self._append_log(f"cancel error: {e}", True)

    def _close_position(self):
        trade = self._get_trade_for_symbol()
        if not trade or trade.phase not in (TradeState.PHASE_PARTIAL, TradeState.PHASE_LIVE):
            self._append_log("no live position for this symbol", True)
            return
        qty = trade.qty or trade.cum_exec_qty or "?"
        reply = QMessageBox.warning(
            self, "Market close",
            f"Close {trade.symbol} {trade.side.upper()} ({qty}) at market?\n"
            f"This exits immediately as taker and cancels the TP/SL bracket.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self.btn_close.setEnabled(False)
        self._bridge.status_signal.emit("closing position...", ACCENT)

        def _run():
            try:
                self._core.close_position_market(trade)
                self._bridge.close_done.emit(trade.symbol, "")
            except Exception as e:
                self._bridge.close_done.emit(trade.symbol, str(e))

        threading.Thread(target=_run, daemon=True).start()

    def _on_close_done(self, symbol: str, error_msg: str):
        self.btn_close.setEnabled(True)
        self._bridge.status_signal.emit("live", POSITIVE)
        if error_msg:
            self._append_log(f"close error for {symbol}: {error_msg}", True)
        else:
            self._append_log(f"market close sent for {symbol}", False)

    def _get_trade_for_symbol(self):
        """Find active trade matching the current symbol input."""
        sym = self._normalize_symbol(self.symbol_input.text())
        for t in self._trades.values():
            if t.symbol == sym and t.is_active:
                return t
        return None

    # ─────────────────────────────────────────────────────────────
    #  Cache refresh
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
                return  # TradingCore still initialising, _post_boot will pick this up
            all_symbols = self._core.cache.symbols
            symbols = [s for s in all_symbols if s.endswith("USDT") and "-" not in s]
            self._completer_model.setStringList(symbols)
            self._set_trading_enabled(True)

    # ─────────────────────────────────────────────────────────────
    #  Trade state callback
    # ─────────────────────────────────────────────────────────────

    def _on_trade_state_changed(self, trade: TradeState):
        if trade.entry_order_id in self._trades:
            self._trades[trade.entry_order_id] = trade
        else:
            # Try to match by symbol (e.g. WebSocket update with different order ID)
            matched = False
            for oid, t in self._trades.items():
                if t.symbol == trade.symbol and t.phase in (
                    TradeState.PHASE_PARTIAL,
                    TradeState.PHASE_LIVE,
                ):
                    self._trades[oid] = trade
                    matched = True
                    break
            # New trade (e.g. synced from exchange on boot) — add it
            if not matched:
                self._trades[trade.entry_order_id] = trade

        # Journaling is handled by reconciling against Bybit's closed-PnL
        # (see _reconcile_journal), so closed trades are captured even when the
        # app wasn't running — no live write here.
        self._panel_dirty = True

    def _update_trade_panel(self):
        # Clear existing trade rows
        while self._trades_container.count():
            item = self._trades_container.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        active = [t for t in self._trades.values() if t.is_active]
        closed_recent = [t for t in self._trades.values()
                         if t.phase in (TradeState.PHASE_CLOSED, TradeState.PHASE_CANCELLED)]

        if not active and not closed_recent:
            lbl = QLabel("no active trades")
            lbl.setStyleSheet(
                f"color: {TEXT_MUTED}; font-size: 11px; font-weight: 600; "
                f"background: {BG_RAISED}; border: 1px solid {BORDER}; "
                f"border-radius: 3px; padding: 8px 10px;"
            )
            self._trades_container.addWidget(lbl)
            self.btn_modify.setEnabled(False)
            self.btn_cancel.setEnabled(False)
            self.btn_close.setEnabled(False)
            return

        for t in active:
            # ── Trade card container ──
            card = QWidget()
            card.setCursor(Qt.CursorShape.PointingHandCursor)
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(0, 0, 0, 0)
            card_layout.setSpacing(0)

            # Line 1: symbol, side, phase, entry, pnl, mark
            line1 = f"{t.symbol}  {t.side.lower()}  {t.phase.lower()}"
            if t.fill_price:
                line1 += f"  @  {t.fill_price}"

            bdr = BORDER
            color = TEXT_DIM

            if t.phase in (TradeState.PHASE_PARTIAL, TradeState.PHASE_LIVE) and t.unrealised_pnl is not None:
                pnl = t.unrealised_pnl
                sign = "+" if pnl >= 0 else ""
                color = POSITIVE if pnl >= 0 else NEGATIVE
                bdr = color
                mark = f"  |  mark {t.mark_price:,.4f}" if t.mark_price else ""
                line1 += f"  |  pnl {sign}${pnl:.2f}{mark}"
            elif t.phase == TradeState.PHASE_PENDING:
                line1 += "  (resting)"
            elif t.phase == TradeState.PHASE_PARTIAL:
                filled = t.cum_exec_qty or t.qty or "?"
                total = t.entry_qty or "?"
                line1 += f"  ({filled}/{total} filled)"

            # Line 2: leverage, tp, sl, strat1
            lev_str = f"{t.leverage}x" if t.leverage else "--"
            tp_str = t.take_profit if t.take_profit else "--"
            sl_str = t.stop_loss if t.stop_loss else "--"
            strat1_tag = f"  S1:{t.strat1_phase}" if t.strat1_enabled else ""
            line2 = f"lev {lev_str}  |  tp {tp_str}  |  sl {sl_str}{strat1_tag}"

            btn = QPushButton(f"{line1}\n{line2}")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(
                f"text-align: left; color: {color}; font-size: 11px; "
                f"font-weight: 600; background: {BG_RAISED}; "
                f"border: 1px solid {bdr}; border-radius: 3px; "
                f"border-bottom-left-radius: 0; border-bottom-right-radius: 0; "
                f"padding: 7px 10px;"
            )
            sym = t.symbol
            btn.clicked.connect(lambda checked, s=sym: self._select_trade(s))
            card_layout.addWidget(btn)

            # ── PnL progress bar ──
            bar_html = self._render_pnl_bar(t)
            bar_label = QLabel(bar_html)
            bar_label.setTextFormat(Qt.TextFormat.RichText)
            bar_label.setStyleSheet(
                f"background: {BG_RAISED}; border: 1px solid {bdr}; "
                f"border-top: none; border-radius: 3px; "
                f"border-top-left-radius: 0; border-top-right-radius: 0; "
                f"padding: 3px 10px 5px 10px; font-size: 10px;"
            )
            bar_label.setFixedHeight(22)
            card_layout.addWidget(bar_label)

            self._trades_container.addWidget(card)

        # Show last closed trade
        for t in closed_recent[-1:]:
            rl = f"  |  ${t.pnl:.2f}" if t.pnl is not None else ""
            ct = t.close_type.lower() if t.close_type else "closed"
            lbl = QLabel(f"{t.symbol}  {ct}  @  {t.close_price}{rl}")
            lbl.setStyleSheet(
                f"color: {TEXT_MUTED}; font-size: 10px; font-weight: 600; "
                f"background: {BG_RAISED}; border: 1px solid {BORDER}; "
                f"border-radius: 3px; padding: 5px 10px;"
            )
            self._trades_container.addWidget(lbl)

        has_active = len(active) > 0
        has_pending = any(t.phase in (TradeState.PHASE_PENDING, TradeState.PHASE_PARTIAL) for t in active)
        has_live = any(t.phase in (TradeState.PHASE_PARTIAL, TradeState.PHASE_LIVE) for t in active)
        self.btn_modify.setEnabled(has_active)
        self.btn_cancel.setEnabled(has_pending)
        self.btn_close.setEnabled(has_live)

    def _select_trade(self, symbol: str):
        """Click a trade row → auto-fill the symbol input for modify/cancel."""
        self.symbol_input.setText(symbol.replace("USDT", ""))
        self._append_log(f"selected {symbol}", False)

    @staticmethod
    def _render_pnl_bar(trade: TradeState) -> str:
        """
        Render a terminal-style PnL progress bar as HTML.

        Layout (40 chars total):
          [SL zone ← 20 chars ─ │ ─ 20 chars → TP zone]
          Center │ = entry price (0 PnL).
          Red fills left from center toward SL.
          Green fills right from center toward TP.
          If TP or SL is missing, that half is dimmed out.
        """
        BAR_HALF = 20
        EMPTY = "─"
        FILLED = "█"
        CENTER = "│"

        CLR_RED = NEGATIVE
        CLR_GREEN = POSITIVE
        CLR_DIM = "#252830"
        CLR_CENTER = TEXT_DIM

        entry = trade.fill_price
        mark = trade.mark_price
        has_sl = trade.stop_loss is not None
        has_tp = trade.take_profit is not None

        sl_fill = 0
        tp_fill = 0

        if entry and mark:
            sl = float(trade.stop_loss) if has_sl else None
            tp = float(trade.take_profit) if has_tp else None

            if trade.side == "Buy":
                # Long: SL below entry, TP above entry
                if sl is not None and entry != sl:
                    progress = max(0, min(1, (entry - mark) / (entry - sl)))
                    sl_fill = int(progress * BAR_HALF)
                if tp is not None and tp != entry:
                    progress = max(0, min(1, (mark - entry) / (tp - entry)))
                    tp_fill = int(progress * BAR_HALF)
            else:
                # Short: SL above entry, TP below entry
                if sl is not None and sl != entry:
                    progress = max(0, min(1, (mark - entry) / (sl - entry)))
                    sl_fill = int(progress * BAR_HALF)
                if tp is not None and entry != tp:
                    progress = max(0, min(1, (entry - mark) / (entry - tp)))
                    tp_fill = int(progress * BAR_HALF)

        # ── Build left half (SL zone, fills from right to left) ──
        if has_sl:
            empty_left = BAR_HALF - sl_fill
            left = (
                f"<span style='color:{CLR_DIM};'>{EMPTY * empty_left}</span>"
                f"<span style='color:{CLR_RED};'>{FILLED * sl_fill}</span>"
            )
        else:
            left = f"<span style='color:{CLR_DIM};'>{EMPTY * BAR_HALF}</span>"

        # ── Center marker ──
        center = f"<span style='color:{CLR_CENTER};'>{CENTER}</span>"

        # ── Build right half (TP zone, fills from left to right) ──
        if has_tp:
            empty_right = BAR_HALF - tp_fill
            right = (
                f"<span style='color:{CLR_GREEN};'>{FILLED * tp_fill}</span>"
                f"<span style='color:{CLR_DIM};'>{EMPTY * empty_right}</span>"
            )
        else:
            right = f"<span style='color:{CLR_DIM};'>{EMPTY * BAR_HALF}</span>"

        return (
            f"<span style='font-family: Menlo, Consolas, monospace; "
            f"font-size: 10px; letter-spacing: 0.5px;'>"
            f"{left}{center}{right}</span>"
        )

    # ─────────────────────────────────────────────────────────────
    #  Journal stats
    # ─────────────────────────────────────────────────────────────

    def _update_journal_stats(self):
        """Render journal stats (lifetime + session) in a compact row."""
        s = self._journal.stats()
        sess = self._journal.stats(session_only=True)

        if s["total"] == 0 and sess["total"] == 0:
            self.journal_label.setText(
                f"<span style='color: {TEXT_MUTED};'>JOURNAL  no closed trades</span>"
            )
            return

        # Color the PnL
        pnl_color = POSITIVE if s["total_pnl"] >= 0 else NEGATIVE
        sess_pnl_color = POSITIVE if sess["total_pnl"] >= 0 else NEGATIVE

        self.journal_label.setText(
            f"<span style='font-family: Menlo, monospace; font-size: 9px;'>"
            f"<span style='color: {TEXT_MUTED};'>JOURNAL</span>  "
            f"<span style='color: {TEXT};'>{s['total']} trades</span>  "
            f"<span style='color: {TEXT_DIM};'>WR</span> "
            f"<span style='color: {TEXT};'>{s['win_rate']}%</span>  "
            f"<span style='color: {TEXT_DIM};'>PnL</span> "
            f"<span style='color: {pnl_color};'>${s['total_pnl']:+.2f}</span>  "
            f"<span style='color: {TEXT_DIM};'>avgR</span> "
            f"<span style='color: {TEXT};'>{s['avg_r']}</span>  "
            f"<span style='color: {TEXT_DIM};'>E[</span>"
            f"<span style='color: {pnl_color};'>${s['expectancy']:+.2f}</span>"
            f"<span style='color: {TEXT_DIM};'>]</span>"
            f"<br/>"
            f"<span style='color: {TEXT_MUTED};'>SESSION</span>  "
            f"<span style='color: {TEXT};'>{sess['total']} trades</span>  "
            f"<span style='color: {sess_pnl_color};'>${sess['total_pnl']:+.2f}</span>"
            f"</span>"
        )

    # ─────────────────────────────────────────────────────────────
    #  Strat1 checkbox gate
    # ─────────────────────────────────────────────────────────────

    def _on_tp_changed(self, text: str):
        """Enable strat1 checkbox only when TP field has a value."""
        has_tp = bool(text.strip())
        self.strat1_checkbox.setEnabled(has_tp)
        if not has_tp:
            self.strat1_checkbox.setChecked(False)

    # ─────────────────────────────────────────────────────────────
    #  Portfolio heatmap
    # ─────────────────────────────────────────────────────────────

    def _update_heatmap(self):
        """Render portfolio heat bar: total exposure / equity."""
        live = [t for t in self._trades.values()
                if t.phase in (TradeState.PHASE_PARTIAL, TradeState.PHASE_LIVE) and t.position_value]
        total_exposure = sum(t.position_value for t in live if t.position_value)

        equity = self._equity

        if not equity or equity <= 0:
            self.heatmap_label.setText("")
            return

        pct = (total_exposure / equity) * 100
        BAR_W = 15

        if pct < 50:
            color = POSITIVE
        elif pct < 80:
            color = "#E8A838"  # amber
        else:
            color = NEGATIVE

        filled = min(BAR_W, int((pct / 100) * BAR_W))
        empty = BAR_W - filled

        bar = (
            f"<span style='font-family: Menlo, monospace; font-size: 9px;'>"
            f"<span style='color: {TEXT_MUTED};'>HEAT </span>"
            f"<span style='color: {color};'>{'█' * filled}</span>"
            f"<span style='color: #252830;'>{'░' * empty}</span>"
            f"<span style='color: {color};'> {pct:.0f}%</span>"
            f"</span>"
        )
        self.heatmap_label.setText(bar)

    def _on_status_updated(self, text: str, color_hex: str):
        self.status_label.setText(text)
        self.status_label.setStyleSheet(f"font-size: 10px; color: {color_hex};")

    def _on_balance_updated(self, bal: float):
        self._equity = bal
        self.balance_label.setText(f"${bal:,.2f}")

    def _on_execute_done(self, trade, error_msg: str):
        self.btn_execute.setEnabled(True)
        self.btn_preview.setEnabled(True)
        self._bridge.status_signal.emit("live", POSITIVE)
        if error_msg or trade is None:
            self._append_log(f"execution error: {error_msg or 'unknown error'}", True)
        else:
            self._trades[trade.entry_order_id] = trade
            self._update_trade_panel()
            self._append_log(f"trade executed successfully: {trade.symbol}", False)

    def _on_modify_done(self, symbol: str, error_msg: str):
        self.btn_modify.setEnabled(True)
        self._bridge.status_signal.emit("live", POSITIVE)
        if error_msg:
            self._append_log(f"modify error for {symbol}: {error_msg}", True)
        else:
            self.new_tp_input.clear()
            self.new_sl_input.clear()
            self._append_log(f"brackets successfully modified for {symbol}", False)

    def _on_cancel_done(self, symbol: str, error_msg: str):
        self.btn_cancel.setEnabled(True)
        self._bridge.status_signal.emit("live", POSITIVE)
        if error_msg:
            self._append_log(f"cancel error for {symbol}: {error_msg}", True)
        else:
            self._append_log(f"trade cancelled for {symbol}", False)

    def _on_positions_synced(self, positions: dict, tp_sl_map: dict):
        for t in list(self._trades.values()):
            if t.phase not in (TradeState.PHASE_PARTIAL, TradeState.PHASE_LIVE):
                continue
            pos = positions.get(t.symbol)
            if pos:
                t.unrealised_pnl = float(pos.get("unrealisedPnl", "0"))
                t.mark_price = float(pos.get("markPrice", "0"))
                t.position_value = float(pos.get("positionValue", "0"))
                t.qty = pos.get("size")
                t.leverage = pos.get("leverage", "")
                bracket = tp_sl_map.get(t.symbol, {})
                if "tp" in bracket:
                    t.take_profit = bracket["tp"]
                if "sl" in bracket:
                    t.stop_loss = bracket["sl"]
            elif t.symbol not in positions:
                t.phase = TradeState.PHASE_CLOSED
                t.close_type = "closed externally"
                # Journal is reconciled from Bybit closed-PnL, not written here.

        self._panel_dirty = True

    def _on_margin_mode_sync(self, mode: str):
        is_isolated = (mode == "ISOLATED_MARGIN")
        self.iso_checkbox.blockSignals(True)
        self.iso_checkbox.setChecked(is_isolated)
        self.iso_checkbox.setEnabled(True)  # Re-enable checkbox thread-safely
        self.iso_checkbox.blockSignals(False)

    def _on_margin_mode_warning(self, mode: str):
        self._on_margin_mode_sync(mode)
        
        self.status_label.setText("margin mode locked")
        self.status_label.setStyleSheet(f"font-size: 10px; color: {NEGATIVE};")
        
        # Also show a visual message in the preview panel
        self.preview_label.setText(
            "WARNING: Cannot change margin mode (Isolated/Cross) while you have open positions or pending orders on your Bybit account."
        )
        self.preview_label.setStyleSheet(
            f"color: {NEGATIVE}; font-size: 11px; background: {BG_RAISED}; "
            f"border: 1px solid {NEGATIVE}; border-radius: 3px; padding: 8px;"
        )

    def _on_margin_checkbox_clicked(self, checked: bool):
        # Disable checkbox immediately to prevent double-click race conditions
        self.iso_checkbox.setEnabled(False)
        
        # Notify user we are checking
        self._bridge.status_signal.emit("checking account status...", ACCENT)
        
        def _check():
            try:
                has_active = self._core.has_active_positions_or_orders()
                if has_active:
                    self._bridge.log_signal.emit(
                        "UTA Margin Warning: Cannot switch margin mode while positions or orders are open on the account.", True
                    )
                    self._bridge.margin_mode_warning.emit(self._core.account_margin_mode)
                else:
                    target_mode = "ISOLATED_MARGIN" if checked else "REGULAR_MARGIN"
                    try:
                        self._core.client.set_margin_mode(setMarginMode=target_mode)
                        self._core.account_margin_mode = target_mode
                        self._bridge.log_signal.emit(
                            f"Account margin successfully switched to {'Isolated' if checked else 'Cross'}.", False
                        )
                        self._bridge.margin_mode_sync.emit(target_mode)
                    except Exception as e:
                        self._bridge.log_signal.emit(f"Failed to set margin mode: {e}", True)
                        self._bridge.margin_mode_warning.emit(self._core.account_margin_mode)
            except Exception as e:
                self._bridge.log_signal.emit(f"Error checking margin mode: {e}", True)
                self._bridge.margin_mode_sync.emit(self._core.account_margin_mode)
            finally:
                # Reset status label
                self._bridge.status_signal.emit("live", POSITIVE)
                    
        threading.Thread(target=_check, daemon=True).start()

    # ─────────────────────────────────────────────────────────────
    #  Helpers
    # ─────────────────────────────────────────────────────────────

    def _parse_inputs(self):
        symbol = self._normalize_symbol(self.symbol_input.text())
        if not symbol:
            raise ValueError("enter a symbol")

        side = "Buy" if self._is_long else "Sell"

        entry_t = self.entry_input.text().strip().replace("$", "").replace(",", "")
        sl_t = self.sl_input.text().strip().replace("$", "").replace(",", "")
        if not entry_t or not sl_t:
            raise ValueError("entry and stop loss required")

        entry = float(entry_t)
        sl = float(sl_t)
        risk = self.risk_input.value()

        tp_t = self.tp_input.text().strip().replace("$", "").replace(",", "")
        tp = float(tp_t) if tp_t else None

        return symbol, side, entry, sl, tp, risk

    @staticmethod
    def _normalize_symbol(text: str) -> str:
        """Normalize user input to a Bybit USDT perpetual symbol."""
        sym = text.strip().upper()
        if sym and not sym.endswith("USDT"):
            sym += "USDT"
        return sym

    def _reconcile_journal(self):
        """Pull closed-PnL from Bybit and merge into the journal. Runs off-thread."""
        # Only one reconcile at a time — boot + the 30s tick can both call this,
        # and a long backfill must not race a tick on the has()/ledger logic.
        if not self._reconcile_lock.acquire(blocking=False):
            return
        try:
            last = max((t.get("closed_at") or 0 for t in self._journal.all_trades),
                       default=0)
            start_ms = int(last * 1000) if last else 0
            records = self._core.fetch_closed_pnl(start_ms)
            # Attach R-multiples only to records not already journaled, so a
            # re-fetched boundary trade can't consume a fresh trade's intent.
            for r in records:
                if not self._journal.has(r.get("order_id", "")):
                    self._core.attach_risk_intent(r)
            added = self._journal.reconcile(records)
            if added:
                self._bridge.log_signal.emit(f"journal: +{added} trade(s) from exchange", False)
            self._bridge.journal_updated.emit()
        except Exception as e:
            self._bridge.log_signal.emit(f"journal reconcile failed: {e}", True)
        finally:
            self._reconcile_lock.release()

    def _on_journal_updated(self):
        self._update_journal_stats()
        if self._journal_dialog is not None and self._journal_dialog.isVisible():
            self._journal_dialog.refresh(self._journal.all_trades, self._journal.stats())

    def _open_journal_dialog(self):
        if self._journal_dialog is None:
            self._journal_dialog = JournalDialog(self, STYLESHEET)
        self._journal_dialog.refresh(self._journal.all_trades, self._journal.stats())
        self._journal_dialog.show()
        self._journal_dialog.raise_()
        self._journal_dialog.activateWindow()

    def _append_log(self, msg: str, is_error: bool):
        color = NEGATIVE if is_error else TEXT_DIM
        self.console.append(f"<span style='color:{color};'>{msg}</span>")
        doc = self.console.document()
        if doc.blockCount() > 500:
            cursor = self.console.textCursor()
            cursor.movePosition(cursor.MoveOperation.Start)
            cursor.movePosition(cursor.MoveOperation.Down,
                                cursor.MoveMode.KeepAnchor,
                                doc.blockCount() - 500)
            cursor.removeSelectedText()
        sb = self.console.verticalScrollBar()
        sb.setValue(sb.maximum())

    @staticmethod
    def _lbl(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("muted")
        return lbl


# ──────────────────────────────────────────────────────────────────
#  Journal history pop-out
# ──────────────────────────────────────────────────────────────────

class JournalDialog(QDialog):
    """Read-only monospace ledger of closed trades (newest first)."""

    def __init__(self, parent, stylesheet: str):
        super().__init__(parent)
        self.setWindowTitle("Viridis — Journal")
        self.setMinimumSize(640, 440)
        self.setStyleSheet(stylesheet)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        self._summary = QLabel("")
        self._summary.setTextFormat(Qt.TextFormat.RichText)
        self._summary.setWordWrap(True)
        self._summary.setStyleSheet(
            f"font-size: 11px; color: {TEXT}; background: {BG_RAISED}; "
            f"border: 1px solid {BORDER}; border-radius: 3px; padding: 8px 10px;"
        )
        layout.addWidget(self._summary)

        self._ledger = QTextEdit()
        self._ledger.setReadOnly(True)
        self._ledger.setStyleSheet(
            f"background: {BG}; color: {TEXT_DIM}; border: 1px solid {BORDER}; "
            f"border-radius: 3px; padding: 8px; "
            f"font-family: Menlo, Consolas, monospace; font-size: 11px;"
        )
        layout.addWidget(self._ledger, 1)

    def refresh(self, trades: list, stats: dict):
        pnl_color = POSITIVE if stats.get("total_pnl", 0) >= 0 else NEGATIVE
        self._summary.setText(
            f"<b>{stats['total']}</b> trades&nbsp;&nbsp;&nbsp;"
            f"WR <b>{stats['win_rate']}%</b>&nbsp;&nbsp;&nbsp;"
            f"PnL <span style='color:{pnl_color};'><b>${stats['total_pnl']:+.2f}</b></span>"
            f"&nbsp;&nbsp;&nbsp;avgR <b>{stats['avg_r']}</b>&nbsp;&nbsp;&nbsp;"
            f"E[<span style='color:{pnl_color};'>${stats['expectancy']:+.2f}</span>]"
            f"&nbsp;&nbsp;&nbsp;best <span style='color:{POSITIVE};'>${stats['best']:+.2f}</span>"
            f"&nbsp;&nbsp;&nbsp;worst <span style='color:{NEGATIVE};'>${stats['worst']:+.2f}</span>"
        )

        def esc(s: str) -> str:
            return s.replace(" ", "&nbsp;")

        header = f"{'date':<17}{'symbol':<13}{'side':<6}{'pnl':>12}{'R':>9}"
        rows = [f"<span style='color:{TEXT_MUTED};'>{esc(header)}</span>"]
        for t in sorted(trades, key=lambda x: x.get("closed_at") or 0, reverse=True):
            ts = t.get("closed_at")
            date = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "—"
            sym = (t.get("symbol") or "")[:12]
            side = (t.get("side") or "")[:5]
            pnl = t.get("pnl")
            r = t.get("r_multiple")
            pnl_s = f"${pnl:+.2f}" if pnl is not None else "—"
            r_s = f"{r:+.2f}R" if r is not None else "—"
            c = TEXT_DIM if pnl is None else (POSITIVE if pnl >= 0 else NEGATIVE)
            line = f"{date:<17}{sym:<13}{side:<6}{pnl_s:>12}{r_s:>9}"
            rows.append(f"<span style='color:{c};'>{esc(line)}</span>")

        if len(rows) == 1:
            rows.append(f"<span style='color:{TEXT_MUTED};'>no closed trades yet</span>")
        self._ledger.setHtml("<br/>".join(rows))


# ──────────────────────────────────────────────────────────────────
#  Entry Point
# ──────────────────────────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)

    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(BG))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Base, QColor(BG_INPUT))
    palette.setColor(QPalette.ColorRole.Text, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Button, QColor(BG_RAISED))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(ACCENT))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(BG))
    app.setPalette(palette)

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
