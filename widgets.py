"""
Painted widgets for the Viridis board.

  HintLineEdit  — line edit with a dim, right-aligned hint inside the field
  Ladder        — pre-trade price ladder: target / mark / entry / stop / liq
  MiniLadder    — the same levels as a one-line track on a position card
  RHistogram    — distribution of R-multiples
  EquityCurve   — cumulative realised PnL
  Meter         — 3px governance meter

Everything is drawn with QPainter in the shared palette; the widgets hold no
trading logic — the window hands them numbers and strings.
"""

from typing import List, Optional

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen, QPolygonF
from PyQt6.QtWidgets import QLabel, QLineEdit, QSizePolicy, QWidget

import theme as T


def _font(px: int, weight: QFont.Weight = QFont.Weight.Normal, spacing: float = 0.0) -> QFont:
    f = QFont(T.MONO)
    f.setPixelSize(px)
    f.setWeight(weight)
    if spacing:
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing)
    return f


def _c(hex_color: str, alpha: Optional[int] = None) -> QColor:
    col = QColor(hex_color)
    if alpha is not None:
        col.setAlpha(alpha)
    return col


_RIGHT = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
_CENTER = Qt.AlignmentFlag.AlignCenter


# ──────────────────────────────────────────────────────────────────
#  HintLineEdit
# ──────────────────────────────────────────────────────────────────

class HintLineEdit(QLineEdit):
    """QLineEdit with a dim hint drawn inside the field on the right
    (resolved stop price, % of equity, distance from last…)."""

    def __init__(self, placeholder: str = "", parent=None):
        super().__init__(parent)
        self.setPlaceholderText(placeholder)
        self._hint = QLabel("", self)
        self._hint.setStyleSheet(
            f"color: {T.TEXT_DIM}; font-size: 9.5px; background: transparent; border: none;"
        )
        self._hint.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._hint.hide()
        self._hint_text = ""

    def set_hint(self, text: str) -> None:
        self._hint_text = text
        self._hint.setVisible(bool(text))
        self._place()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._place()

    def _place(self) -> None:
        if not self._hint_text:
            self.setTextMargins(0, 0, 0, 0)
            return
        # The hint never takes more than half the field, so typed text stays visible.
        metrics = QFontMetrics(self._hint.font())
        text = metrics.elidedText(self._hint_text, Qt.TextElideMode.ElideLeft,
                                  max(40, int(self.width() * 0.5)))
        self._hint.setText(text)
        self._hint.adjustSize()
        w, h = self._hint.width(), self._hint.height()
        self._hint.move(self.width() - w - 9, (self.height() - h) // 2)
        self.setTextMargins(0, 0, w + 12, 0)


# ──────────────────────────────────────────────────────────────────
#  Ladder
# ──────────────────────────────────────────────────────────────────

class Ladder(QWidget):
    """
    Vertical price ladder. Rows are dicts:
      key      "tp" | "mark" | "entry" | "sl" | "liq"
      label    tag text
      price    float (drives the vertical position)
      price_s  formatted price
      pct_s    signed distance from entry
      usd_s / usd_c   dollars at that level (or a note) and its colour
      r_s / r_c       R at that level (or a note) and its colour
    """

    ROW_H = 18
    DOT = {"tp": T.POSITIVE, "mark": T.ACCENT, "entry": T.WHITE, "sl": T.NEGATIVE, "liq": T.NEGATIVE}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(118)
        self.setMinimumWidth(320)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        self._rows: List[dict] = []
        self._message = "enter entry and stop"

    def clear(self, message: str = "enter entry and stop") -> None:
        self._rows = []
        self._message = message
        self.update()

    def set_rows(self, rows: List[dict]) -> None:
        self._rows = [dict(r) for r in rows if r.get("price")]
        self._message = ""
        self.update()

    def _layout_rows(self, top: float, bottom: float) -> List[dict]:
        rows = sorted(self._rows, key=lambda r: -r["price"])
        prices = [r["price"] for r in rows]
        hi, lo = max(prices), min(prices)
        span = (hi - lo) or 1.0
        # Rows compress a little when the ladder is short, never below 13px.
        natural = (bottom - top) / max(1, len(rows) - 1)
        row_h = 13.0 if natural < 13.0 else self.ROW_H if natural > self.ROW_H else natural
        for r in rows:
            r["y"] = top + (hi - r["price"]) / span * (bottom - top)
        # Push overlapping rows apart, then pull the group back inside.
        for i in range(1, len(rows)):
            if rows[i]["y"] < rows[i - 1]["y"] + row_h:
                rows[i]["y"] = rows[i - 1]["y"] + row_h
        over = rows[-1]["y"] - bottom
        if over > 0:
            for r in rows:
                r["y"] -= over
            for i in range(len(rows) - 2, -1, -1):
                if rows[i]["y"] > rows[i + 1]["y"] - row_h:
                    rows[i]["y"] = rows[i + 1]["y"] - row_h
        return rows

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), _c(T.BG_RAISED))
        p.setPen(QPen(_c(T.BORDER)))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 3, 3)

        if not self._rows:
            p.setPen(_c(T.TEXT_MUTED))
            p.setFont(_font(11))
            p.drawText(self.rect(), _CENTER, self._message)
            return

        top, bottom = 15.0, h - 15.0
        rows = self._layout_rows(top, bottom)
        y = {r["key"]: r["y"] for r in rows}

        x_tag, x_track, x_px, x_pct, x_usd, x_r = 50, 64, 160, 222, 316, w - 8

        # Track and zones
        p.setPen(QPen(_c(T.BORDER_FCS), 1))
        p.drawLine(QPointF(x_track, top - 2), QPointF(x_track, bottom + 2))
        for a, b, col in (("entry", "tp", _c(T.POSITIVE, 140)),
                          ("entry", "sl", _c(T.NEGATIVE, 140)),
                          ("sl", "liq", _c(T.TEXT_MUTED, 170))):
            if a in y and b in y:
                ya, yb = sorted((y[a], y[b]))
                p.fillRect(QRectF(x_track - 2.5, ya, 5, yb - ya), col)

        for r in rows:
            yy = r["y"]
            key = r["key"]
            band = QRectF(0, yy - 9, w, 18)

            p.setFont(_font(9, spacing=1.2))
            p.setPen(_c(T.TEXT_DIM))
            p.drawText(QRectF(0, band.top(), x_tag, 18), _RIGHT, r["label"].upper())

            if key == "liq":
                p.setPen(QPen(_c(T.NEGATIVE), 1))
                p.setBrush(Qt.BrushStyle.NoBrush)
            else:
                if key == "mark":
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(_c(T.ACCENT, 60))
                    p.drawEllipse(QPointF(x_track, yy), 6.5, 6.5)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(_c(self.DOT[key]))
            p.drawEllipse(QPointF(x_track, yy), 3.5, 3.5)

            p.setFont(_font(11, QFont.Weight.Medium if key in ("entry", "mark") else QFont.Weight.Normal))
            p.setPen(_c(T.ACCENT if key == "mark" else T.TEXT))
            p.drawText(QRectF(x_track + 8, band.top(), x_px - x_track - 8, 18), _RIGHT, r["price_s"])

            p.setFont(_font(10))
            p.setPen(_c(T.TEXT_DIM))
            p.drawText(QRectF(x_px + 4, band.top(), x_pct - x_px - 4, 18), _RIGHT, r["pct_s"])
            p.setPen(_c(r.get("usd_c") or T.TEXT_DIM))
            p.drawText(QRectF(x_pct + 4, band.top(), x_usd - x_pct - 4, 18), _RIGHT, r.get("usd_s", ""))
            p.setPen(_c(r.get("r_c") or T.TEXT_DIM))
            p.drawText(QRectF(x_usd + 4, band.top(), x_r - x_usd - 4, 18), _RIGHT, r.get("r_s", ""))


# ──────────────────────────────────────────────────────────────────
#  MiniLadder (position card)
# ──────────────────────────────────────────────────────────────────

class MiniLadder(QWidget):
    """One-line track: liq · stop · entry · target, with the mark as a caret
    and the max favourable excursion as a hollow dot."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(30)
        self._levels: dict = {}

    def set_levels(self, entry: Optional[float], sl: Optional[float], tp: Optional[float],
                   liq: Optional[float] = None, mark: Optional[float] = None,
                   mfe: Optional[float] = None) -> None:
        self._levels = {"entry": entry, "sl": sl, "tp": tp, "liq": liq, "mark": mark, "mfe": mfe}
        self.update()

    def paintEvent(self, event) -> None:
        L = self._levels
        pts = [v for v in L.values() if v]
        if not pts or not L.get("entry"):
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        lo, hi = min(pts), max(pts)
        span = (hi - lo) or 1.0

        def x(v: float) -> float:
            return 8 + (v - lo) / span * (w - 16)

        ty = 15.0
        p.setPen(QPen(_c(T.BORDER), 2))
        p.drawLine(QPointF(4, ty), QPointF(w - 4, ty))
        for a, b, col in (("entry", "tp", _c(T.POSITIVE, 150)),
                          ("entry", "sl", _c(T.NEGATIVE, 150)),
                          ("sl", "liq", _c(T.TEXT_MUTED, 180))):
            if L.get(a) and L.get(b):
                xa, xb = sorted((x(L[a]), x(L[b])))
                p.fillRect(QRectF(xa, ty - 2, xb - xa, 4), col)

        p.setFont(_font(8, spacing=0.6))
        for key, label, col, above in (("liq", "LIQ", T.NEGATIVE, True),
                                       ("sl", "SL", T.NEGATIVE, False),
                                       ("entry", "ENTRY", T.TEXT, False),
                                       ("tp", "TP", T.POSITIVE, False)):
            v = L.get(key)
            if not v:
                continue
            xx = x(v)
            colq = _c(col, 170 if key == "liq" else 255)
            p.setPen(QPen(colq, 1))
            if above:
                p.drawLine(QPointF(xx, ty - 3), QPointF(xx, ty - 9))
                p.drawText(QRectF(xx - 20, 0, 40, 8), _CENTER, label)
            else:
                p.drawLine(QPointF(xx, ty + 3), QPointF(xx, ty + 8))
                p.drawText(QRectF(xx - 24, ty + 9, 48, 9), _CENTER, label)

        if L.get("mfe"):
            p.setPen(QPen(_c(T.TEXT_DIM), 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(x(L["mfe"]), 5), 2.5, 2.5)
        if L.get("mark"):
            xx = x(L["mark"])
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_c(T.ACCENT))
            p.drawPolygon(QPolygonF([QPointF(xx - 4, 3), QPointF(xx + 4, 3), QPointF(xx, 10)]))


# ──────────────────────────────────────────────────────────────────
#  Charts
# ──────────────────────────────────────────────────────────────────

class RHistogram(QWidget):
    """Bars for R-multiples binned at half-R steps from −1 to +2.5 (ends clamp)."""

    CENTERS = [-1.0, -0.5, 0.0, 0.5, 1.0, 1.5, 2.0, 2.5]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(40)
        self.setMinimumWidth(90)
        self._counts = [0] * len(self.CENTERS)

    def set_values(self, rs: List[float]) -> None:
        counts = [0] * len(self.CENTERS)
        for r in rs:
            i = int(round((r + 1.0) / 0.5))
            counts[max(0, min(len(counts) - 1, i))] += 1
        self._counts = counts
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        n = len(self._counts)
        gap = 3.0
        bw = (w - gap * (n - 1)) / n
        base = h - 10.0
        peak = max(self._counts) or 1
        for i, cnt in enumerate(self._counts):
            if not cnt:
                continue
            bh = max(2.0, cnt / peak * (base - 4))
            col = T.NEGATIVE if self.CENTERS[i] < 0 else (T.POSITIVE if self.CENTERS[i] > 0 else T.TEXT_DIM)
            p.fillRect(QRectF(i * (bw + gap), base - bh, bw, bh), _c(col, 230))
        p.setPen(QPen(_c(T.BORDER_FCS), 1))
        p.drawLine(QPointF(0, base + 0.5), QPointF(w, base + 0.5))
        p.setFont(_font(7))
        p.setPen(_c(T.TEXT_MUTED))
        p.drawText(QRectF(0, base + 1, 40, 9), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "−1R")
        p.drawText(QRectF(w - 40, base + 1, 40, 9), _RIGHT, "+2.5R")


class EquityCurve(QWidget):
    """Cumulative realised PnL as a line over an area, zero line dashed."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(40)
        self.setMinimumWidth(100)
        self._cum: List[float] = []

    def set_series(self, pnls: List[float]) -> None:
        total = 0.0
        self._cum = []
        for v in pnls:
            total += v
            self._cum.append(total)
        self.update()

    def paintEvent(self, event) -> None:
        if len(self._cum) < 2:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        lo, hi = min(0.0, *self._cum), max(0.0, *self._cum)
        span = (hi - lo) or 1.0
        n = len(self._cum)

        def x(i: int) -> float:
            return 3 + i / (n - 1) * (w - 6)

        def y(v: float) -> float:
            return 4 + (hi - v) / span * (h - 10)

        pen = QPen(_c(T.BORDER_FCS), 1)
        pen.setStyle(Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.drawLine(QPointF(0, y(0)), QPointF(w, y(0)))

        pts = [QPointF(x(i), y(v)) for i, v in enumerate(self._cum)]
        area = QPolygonF([QPointF(x(0), y(0))] + pts + [QPointF(x(n - 1), y(0))])
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_c(T.ACCENT, 36))
        p.drawPolygon(area)
        p.setPen(QPen(_c(T.ACCENT), 1.4))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPolyline(QPolygonF(pts))
        last = self._cum[-1]
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_c(T.POSITIVE if last >= 0 else T.NEGATIVE))
        p.drawEllipse(pts[-1], 2.6, 2.6)


# ──────────────────────────────────────────────────────────────────
#  Meter
# ──────────────────────────────────────────────────────────────────

class Meter(QWidget):
    """3px bar for the governance strip: fraction of a cap, coloured by level."""

    LEVELS = {"ok": T.POSITIVE, "warn": T.AMBER, "hard": T.NEGATIVE, "off": T.TEXT_MUTED}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(3)
        self._fraction = 0.0
        self._level = "ok"

    def set_fraction(self, fraction: float, level: Optional[str] = None) -> None:
        self._fraction = max(0.0, min(1.0, fraction))
        self._level = level or ("hard" if fraction >= 1 else "warn" if fraction >= 0.75 else "ok")
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(QRectF(0, 0, w, h), _c(T.BORDER))
        if self._fraction > 0:
            p.fillRect(QRectF(0, 0, w * self._fraction, h), _c(self.LEVELS.get(self._level, T.POSITIVE)))
