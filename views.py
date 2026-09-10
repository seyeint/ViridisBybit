"""
Composite views for the board — the trade card and the journal dialog — and
the text formatting they share with the window. Painted primitives live in
widgets.py. Nothing here talks to the core: a card receives snapshots and
reports clicks through the callbacks it is handed.
"""

import time
from datetime import datetime
from typing import Callable, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QTextEdit,
    QVBoxLayout, QWidget,
)

import theme as T
from cache_engine import InstrumentCache
from trading_core import TradeState, format_value
from widgets import MiniLadder


# ──────────────────────────────────────────────────────────────────
#  Formatting
# ──────────────────────────────────────────────────────────────────

def fmt_px(value: Optional[float], tick: Optional[str]) -> str:
    """Price at tick precision with thousands separators."""
    if value is None:
        return "--"
    if not tick:
        return f"{value:,.6g}"
    s = format_value(value, tick)
    if "." in s:
        whole, frac = s.split(".")
        return f"{int(whole):,}.{frac}"
    return f"{int(s):,}"


def fmt_usd(value: float, decimals: int = 2, signed: bool = True) -> str:
    sign = "−" if value < 0 else ("+" if signed else "")
    return f"{sign}${abs(value):,.{decimals}f}"


def fmt_pct(value: float, decimals: int = 2) -> str:
    sign = "−" if value < 0 else "+"
    return f"{sign}{abs(value) * 100:.{decimals}f}%"


def fmt_r(value: float) -> str:
    sign = "−" if value < 0 else "+"
    return f"{sign}{abs(value):.2f}R"


def fmt_duration(seconds: float) -> str:
    d = max(0.0, seconds)
    if d < 3600:
        return f"{int(d // 60)}m"
    if d < 86400:
        return f"{int(d // 3600)}h {int(d % 3600 // 60)}m"
    return f"{int(d // 86400)}d {int(d % 86400 // 3600)}h"


def age_str(since: Optional[float]) -> str:
    return fmt_duration(time.time() - since) if since else ""


def countdown_str(next_ms) -> str:
    try:
        d = float(next_ms) / 1000.0 - time.time()
    except (TypeError, ValueError):
        return ""
    if d <= 0:
        return "now"
    return f"{int(d // 3600)}h {int(d % 3600 // 60):02d}m"


def span(text, color: str, size: Optional[float] = None, weight: Optional[int] = None) -> str:
    style = f"color:{color};"
    if size:
        style += f" font-size:{size}px;"
    if weight:
        style += f" font-weight:{weight};"
    return f"<span style='{style}'>{text}</span>"


def local_midnight() -> float:
    now = datetime.now()
    return datetime(now.year, now.month, now.day).timestamp()


# ──────────────────────────────────────────────────────────────────
#  Trade card
# ──────────────────────────────────────────────────────────────────

class TradeCard(QFrame):
    """
    One active trade. At rest: a header line and the mini ladder. Selected
    (clicked): the detail line and the action buttons unfold; the same
    details show as a tooltip on hover. Reports through `on_select(key)` and
    `on_action(key, action, payload)`; actions are "be", "half", "close",
    "cancel" and "apply" (payload: tp/sl text).
    """

    def __init__(self, key: str, on_select: Callable[[str], None],
                 on_action: Callable[[str, str, Optional[dict]], None]):
        super().__init__()
        self.key = key
        self._on_select = on_select
        self._on_action = on_action
        self._expanded = False
        self.setObjectName("card")
        self.setProperty("stripe", "dim")
        self.setProperty("selected", False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 9, 12, 9)
        lay.setSpacing(6)

        self.line1 = QLabel("")
        self.line1.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(self.line1)

        self.mini = MiniLadder()
        lay.addWidget(self.mini)

        self.details = QLabel("")
        self.details.setTextFormat(Qt.TextFormat.RichText)
        self.details.setWordWrap(True)
        self.details.hide()
        lay.addWidget(self.details)

        self.actions = QWidget()
        acts = QHBoxLayout(self.actions)
        acts.setContentsMargins(0, 0, 0, 0)
        acts.setSpacing(6)
        self.btn_be = self._act("sl → be", "be")
        self.btn_half = self._act("sl → −0.5R", "half")
        self.btn_close = self._act("close mkt", "close", "danger")
        self.btn_cancel = self._act("cancel order", "cancel", "danger")
        self.btn_edit = self._act("edit tp / sl", "edit", "primary")
        self._buttons = [self.btn_be, self.btn_half, self.btn_close, self.btn_cancel, self.btn_edit]
        for b in self._buttons:
            acts.addWidget(b)
        acts.addStretch()
        self.actions.hide()
        lay.addWidget(self.actions)

        self.edit_row = QWidget()
        er = QHBoxLayout(self.edit_row)
        er.setContentsMargins(0, 0, 0, 0)
        er.setSpacing(6)
        self.edit_tp = QLineEdit()
        self.edit_sl = QLineEdit()
        for lbl, edit in (("tp", self.edit_tp), ("sl", self.edit_sl)):
            edit.setObjectName("card_edit")
            edit.setFixedWidth(120)
            edit.setPlaceholderText("price · % · R")
            label = QLabel(lbl)
            label.setObjectName("dim")
            er.addWidget(label)
            er.addWidget(edit)
        er.addWidget(self._act("apply", "apply", "primary"))
        er.addStretch()
        self.edit_row.hide()
        lay.addWidget(self.edit_row)

    def _act(self, text: str, action: str, kind: Optional[str] = None) -> QPushButton:
        b = QPushButton(text)
        b.setObjectName("act")
        if kind:
            b.setProperty("kind", kind)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.clicked.connect(lambda _=False, a=action: self._clicked(a))
        return b

    def _clicked(self, action: str) -> None:
        self._on_select(self.key)
        if action == "edit":
            show = not self.edit_row.isVisible()
            self.edit_row.setVisible(show)
            self.btn_edit.setText("cancel edit" if show else "edit tp / sl")
            if show:
                self.edit_tp.setFocus()
        elif action == "apply":
            self._on_action(self.key, "apply", {"tp": self.edit_tp.text(), "sl": self.edit_sl.text()})
        else:
            self._on_action(self.key, action, None)

    def hide_edit(self) -> None:
        self.edit_row.hide()
        self.btn_edit.setText("edit tp / sl")
        self.edit_tp.clear()
        self.edit_sl.clear()

    def mousePressEvent(self, event) -> None:
        self._on_select(self.key)
        super().mousePressEvent(event)

    def _repolish(self) -> None:
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def set_selected(self, on: bool) -> None:
        """Selection is also expansion: only the selected card shows details and actions."""
        if bool(self.property("selected")) != on:
            self.setProperty("selected", on)
            self._repolish()
        if self._expanded != on:
            self._expanded = on
            self.details.setVisible(on)
            self.actions.setVisible(on)
            if not on:
                self.hide_edit()

    def set_busy(self, on: bool) -> None:
        for b in self._buttons:
            b.setEnabled(not on)

    def update_from(self, t: TradeState, rules: Optional[dict], risk: Optional[float],
                    ratchet: list, ticker: Optional[dict] = None) -> None:
        tick = rules["tickSize"] if rules else None
        long = t.side == "Buy"
        live = t.is_live
        entry = t.fill_price or t.entry_price
        sl = float(t.stop_loss) if t.stop_loss else None
        tp = float(t.take_profit) if t.take_profit else None
        mark = t.mark_price if live else None
        liq = t.liq_price
        liq_est = False
        if liq is None and entry and t.leverage and rules:
            try:
                lev = float(t.leverage)
                qty = float(t.qty or t.entry_qty or 0)
                mmr = InstrumentCache.tier_for(rules, entry * qty)["mmr"]
                liq = entry * (1 - 1 / lev + mmr) if long else entry * (1 + 1 / lev - mmr)
                liq_est = True
            except (TypeError, ValueError, ZeroDivisionError):
                liq = None
        dist = t.original_sl_distance or (abs(entry - sl) if entry and sl else None)
        pnl = t.unrealised_pnl if live else None

        stripe = "dim" if pnl is None else ("pos" if pnl >= 0 else "neg")
        if self.property("stripe") != stripe:
            self.setProperty("stripe", stripe)
            self._repolish()

        def px(v):
            return fmt_px(v, tick) if v is not None else "--"

        def dim(s):
            return span(s, T.TEXT_DIM)

        def v(s):
            return span(s, T.TEXT, weight=500)

        # ── Header: the glance ──
        head = (f"{span(t.symbol, T.WHITE, 12, 600)}&nbsp;&nbsp;"
                f"{span('LONG' if long else 'SHORT', T.POSITIVE if long else T.NEGATIVE, 9, 600)}")
        if t.phase != TradeState.PHASE_LIVE:
            head += f"&nbsp;&nbsp;{span(t.phase.lower(), T.TEXT_DIM, 9, 600)}"
        right = ""
        if live:
            head += f"&nbsp;&nbsp;&nbsp;{px(entry)} {dim('→')} {px(mark)}"
            if t.phase == TradeState.PHASE_PARTIAL:
                head += dim(f" · {t.cum_exec_qty or '?'}/{t.entry_qty or '?'} filled")
            if pnl is not None:
                right = span(fmt_usd(pnl), T.POSITIVE if pnl >= 0 else T.NEGATIVE, 12, 600)
                if risk:
                    right += "&nbsp;&nbsp;" + span(fmt_r(pnl / risk), T.TEXT_DIM, 10)
        else:
            head += f"&nbsp;&nbsp;&nbsp;{dim('resting')} {px(entry)}"
            ref = (ticker or {}).get("ask1Price" if long else "bid1Price")
            if ref and entry:
                gap = abs(float(ref) - entry) / entry
                head += dim(f" · {gap * 100:.2f}% {'below the ask' if long else 'above the bid'}")
            right = span(age_str(t.opened_at), T.TEXT_DIM, 10)
        self.line1.setText(
            f"<table width='100%' cellspacing='0' cellpadding='0'><tr>"
            f"<td>{head}</td><td align='right'>{right}</td></tr></table>"
        )

        self.mini.set_levels(entry, sl, tp, liq, mark, t.mfe_price if live else None)

        # ── Details: unfold on click, tooltip on hover ──
        parts = [f"lev {v((t.leverage or '--') + 'x')}",
                 f"qty {v(t.qty or t.entry_qty or '--')}",
                 f"tp {v(px(tp))}", f"sl {v(px(sl))}"]
        if liq and entry and sl:
            cushion = (sl - liq) / entry if long else (liq - sl) / entry
            ccol = T.NEGATIVE if cushion <= 0 else (T.AMBER if cushion < 0.001 else T.TEXT)
            parts.append(f"liq {v(px(liq))} · {span(f'{cushion * 100:.2f}%', ccol, weight=500)} behind sl"
                         + (" (est)" if liq_est else ""))
        if live and tp and entry and mark and tp != entry:
            prog = (mark - entry) / (tp - entry) if long else (entry - mark) / (entry - tp)
            parts.append(f"{v(f'{prog * 100:.0f}%')} of the way to tp")
        if live and dist and entry:
            if t.mfe_price:
                r = (t.mfe_price - entry) / dist if long else (entry - t.mfe_price) / dist
                parts.append(f"mfe {span(fmt_r(r), T.POSITIVE)}")
            if t.mae_price:
                r = (t.mae_price - entry) / dist if long else (entry - t.mae_price) / dist
                parts.append(f"mae {span(fmt_r(r), T.NEGATIVE)}")
        if t.entry_is_maker is False:
            parts.append(span(f"taker entry · fee ${t.entry_fee_actual or 0:.2f}", T.NEGATIVE))
        if t.strat1_enabled:
            n = len(ratchet)
            if t.strat1_phase >= n:
                parts.append(f"strat1 {v('done')}")
            else:
                nxt = ratchet[t.strat1_phase]
                parts.append(f"strat1 {v(f'{t.strat1_phase}/{n}')} · next at {nxt[0] * 100:.0f}% "
                             f"locks {nxt[1]:+.1f}R")
        if not live:
            parts.append(f"risk {v(fmt_usd(risk, 0, signed=False) if risk else '?')}")
            if tp and entry and dist:
                parts.append(f"rr {v(f'1:{abs(tp - entry) / dist:.2f}')}")
        elif t.opened_at:
            parts.append(f"open {age_str(t.opened_at)}")
        # Items wrap as a whole, never in the middle of "mae −0.22R".
        parts = [p.replace(" ", "&nbsp;") for p in parts]
        self.details.setText(span(" &nbsp;· ".join(parts), T.TEXT_DIM, 10.5))
        self.setToolTip("<span style='font-size:11px;'>" + "<br>".join(parts) + "</span>")

        for b in (self.btn_be, self.btn_half, self.btn_close):
            b.setVisible(live)
        self.btn_cancel.setVisible(t.phase in (TradeState.PHASE_PENDING, TradeState.PHASE_PARTIAL))


# ──────────────────────────────────────────────────────────────────
#  Journal history pop-out
# ──────────────────────────────────────────────────────────────────

class JournalDialog(QDialog):
    """Read-only monospace ledger of closed trades (newest first)."""

    def __init__(self, parent, stylesheet: str):
        super().__init__(parent)
        self.setWindowTitle("Viridis — Journal")
        self.setMinimumSize(760, 460)
        self.setStyleSheet(stylesheet)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        self._summary = QLabel("")
        self._summary.setTextFormat(Qt.TextFormat.RichText)
        self._summary.setWordWrap(True)
        self._summary.setStyleSheet(
            f"font-size: 11px; color: {T.TEXT}; background: {T.BG_RAISED}; "
            f"border: 1px solid {T.BORDER}; border-radius: 3px; padding: 8px 10px;"
        )
        layout.addWidget(self._summary)

        self._ledger = QTextEdit()
        self._ledger.setReadOnly(True)
        self._ledger.setStyleSheet(
            f"background: {T.BG}; color: {T.TEXT_DIM}; border: 1px solid {T.BORDER}; "
            f"border-radius: 3px; padding: 8px; font-family: {T.MONO_CSS}; font-size: 11px;"
        )
        layout.addWidget(self._ledger, 1)

    def refresh(self, trades: list, stats: dict):
        pnl_color = T.POSITIVE if stats.get("total_pnl", 0) >= 0 else T.NEGATIVE
        self._summary.setText(
            f"<b>{stats['total']}</b> trades&nbsp;&nbsp;&nbsp;"
            f"WR <b>{stats['win_rate']}%</b>&nbsp;&nbsp;&nbsp;"
            f"PnL <span style='color:{pnl_color};'><b>${stats['total_pnl']:+.2f}</b></span>"
            f"&nbsp;&nbsp;&nbsp;avgR <b>{stats['avg_r']}</b>&nbsp;&nbsp;&nbsp;"
            f"E[<span style='color:{pnl_color};'>${stats['expectancy']:+.2f}</span>]"
            f"&nbsp;&nbsp;&nbsp;best <span style='color:{T.POSITIVE};'>${stats['best']:+.2f}</span>"
            f"&nbsp;&nbsp;&nbsp;worst <span style='color:{T.NEGATIVE};'>${stats['worst']:+.2f}</span>"
        )

        def esc(s: str) -> str:
            return s.replace(" ", "&nbsp;")

        def rs(v) -> str:
            return f"{v:+.2f}R" if v is not None else "—"

        header = f"{'date':<17}{'symbol':<13}{'side':<6}{'pnl':>12}{'R':>9}{'mfe':>9}{'mae':>9}{'hold':>9}"
        rows = [f"<span style='color:{T.TEXT_MUTED};'>{esc(header)}</span>"]
        for t in sorted(trades, key=lambda x: x.get("closed_at") or 0, reverse=True):
            ts = t.get("closed_at")
            date = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "—"
            sym = (t.get("symbol") or "")[:12]
            side = "long" if t.get("side") == "Buy" else "short"
            pnl = t.get("pnl")
            pnl_s = f"${pnl:+.2f}" if pnl is not None else "—"
            dur = t.get("duration_sec")
            hold = fmt_duration(dur) if dur else "—"
            c = T.TEXT_DIM if pnl is None else (T.POSITIVE if pnl >= 0 else T.NEGATIVE)
            line = (f"{date:<17}{sym:<13}{side:<6}{pnl_s:>12}{rs(t.get('r_multiple')):>9}"
                    f"{rs(t.get('mfe_r')):>9}{rs(t.get('mae_r')):>9}{hold:>9}")
            rows.append(f"<span style='color:{c};'>{esc(line)}</span>")

        if len(rows) == 1:
            rows.append(f"<span style='color:{T.TEXT_MUTED};'>no closed trades yet</span>")
        self._ledger.setHtml("<br/>".join(rows))
