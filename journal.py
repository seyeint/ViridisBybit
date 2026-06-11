"""
Trade Journal — JSON-backed, mirrors Bybit's closed-PnL history.

The exchange is the source of truth: every closed trade is reconciled from
Bybit's closed-PnL records (deduped by the closing order ID), so the journal
captures trades closed while the app was off or placed elsewhere — and PnL is
Bybit's own fee-inclusive realised number, not a local estimate.

Trades this app placed also carry an R-multiple (the closing core attaches the
intended $ risk before handing records here); backfilled/external trades get
full $ stats but no R.
"""

import json
import os
import sys
import threading
import time
from typing import Any, Dict, List


DEFAULT_PATH = os.path.join(os.path.dirname(__file__), "trade_journal.json")


class TradeJournal:
    """Append-only trade journal, deduped by closing order ID."""

    def __init__(self, path: str = DEFAULT_PATH):
        self._path = path
        self._trades: List[Dict[str, Any]] = []
        self._seen: set = set()           # order_id keys already journaled
        self._lock = threading.Lock()     # reconcile (bg thread) vs stats (UI thread)
        self._session_start = time.time()
        self._load()

    # ─── Persistence ─────────────────────────────────────────────

    @staticmethod
    def _key(entry: Dict[str, Any], index: int) -> str:
        """Stable dedup key. Falls back to a synthetic key for legacy rows."""
        oid = entry.get("order_id")
        if oid:
            return str(oid)
        return f"legacy_{index}_{entry.get('symbol','')}_{entry.get('closed_at','')}"

    def _load(self):
        if not os.path.exists(self._path):
            self._trades = []
            return
        try:
            with open(self._path, "r") as f:
                self._trades = json.load(f)
        except json.JSONDecodeError:
            # Corrupted JSON: preserve it before resetting, so the next
            # _save() doesn't silently overwrite the whole history.
            try:
                backup = self._path + ".corrupted"
                os.replace(self._path, backup)
                sys.stderr.write(f"[!] Journal corrupted; backed up to {backup}\n")
            except OSError:
                pass
            self._trades = []
        except IOError:
            self._trades = []

        # Backfill dedup keys for everything already on disk.
        for i, entry in enumerate(self._trades):
            self._seen.add(self._key(entry, i))

    def _save(self):
        try:
            with open(self._path, "w") as f:
                json.dump(self._trades, f, indent=2)
        except IOError as e:
            sys.stderr.write(f"[!] Warning: Failed to save journal: {e}\n")

    # ─── Reconciliation ──────────────────────────────────────────

    def has(self, order_id: str) -> bool:
        """True if a record with this closing order ID is already journaled."""
        with self._lock:
            return bool(order_id) and str(order_id) in self._seen

    def reconcile(self, entries: List[Dict[str, Any]]) -> int:
        """
        Merge normalised closed-trade records (from the exchange) into the
        journal. New records — by closing order ID — are appended; known ones
        are skipped. Returns the count newly added.
        """
        added = 0
        with self._lock:
            for entry in entries:
                key = self._key(entry, -1)
                if key in self._seen:
                    continue
                self._seen.add(key)
                self._trades.append(entry)
                added += 1
            if added:
                # Keep chronological by close time for a readable ledger.
                self._trades.sort(key=lambda t: t.get("closed_at") or 0)
                self._save()
        return added

    # ─── Statistics ───────────────────────────────────────────────

    @property
    def all_trades(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._trades)

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._trades)

    def stats(self, session_only: bool = False) -> Dict[str, Any]:
        """Compute summary statistics."""
        with self._lock:
            trades = list(self._trades)
        if session_only:
            trades = [t for t in trades
                      if (t.get("closed_at") or 0) >= self._session_start]

        if not trades:
            return {
                "total": 0, "wins": 0, "losses": 0,
                "win_rate": 0, "avg_pnl": 0, "total_pnl": 0,
                "avg_r": 0, "expectancy": 0,
                "best": 0, "worst": 0,
            }

        pnls = [t["pnl"] for t in trades if t.get("pnl") is not None]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]   # flat trades (0) are neutral, not losses
        r_vals = [t["r_multiple"] for t in trades if t.get("r_multiple") is not None]

        decided = len(wins) + len(losses)
        win_rate = len(wins) / decided if decided else 0
        avg_win = sum(wins) / len(wins) if wins else 0
        avg_loss = abs(sum(losses) / len(losses)) if losses else 0
        expectancy = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)

        return {
            "total": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(win_rate * 100, 1),
            "avg_pnl": round(sum(pnls) / len(pnls), 2) if pnls else 0,
            "total_pnl": round(sum(pnls), 2) if pnls else 0,
            "avg_r": round(sum(r_vals) / len(r_vals), 2) if r_vals else 0,
            "expectancy": round(expectancy, 2),
            "best": round(max(pnls), 2) if pnls else 0,
            "worst": round(min(pnls), 2) if pnls else 0,
        }
