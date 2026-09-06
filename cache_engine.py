"""
Instrument Symbology Cache Engine.

Downloads the full linear perpetual instrument list from Bybit V5 once,
persists it to a local JSON file, and reloads from disk on subsequent
starts — eliminating REST latency on the critical execution path.

Also fetches base-tier Maintenance Margin Rate (MMR) per symbol from
the risk limit API for accurate safe-leverage calculation.

Cache auto-refreshes when older than CACHE_TTL_SECONDS (default 24h).
"""

import json
import os
import sys
import threading
import time
from typing import Dict, Optional, Callable

from pybit.unified_trading import HTTP

import config


class InstrumentCache:
    """O(1) lookup cache for Bybit linear perpetual instrument rules."""

    def __init__(self, client: HTTP, on_complete: Optional[Callable[[int], None]] = None):
        self._client = client
        self._data: Dict[str, dict] = {}
        self._load(on_complete)

    # ─── Public ────────────────────────────────────────────────────

    def get(self, symbol: str) -> Optional[dict]:
        """
        Return instrument rules for *symbol*, or None if not found.

        Each entry contains:
            tickSize   : str   (price precision step, e.g. "0.10")
            qtyStep    : str   (quantity precision step, e.g. "0.001")
            minQty     : str   (minimum order quantity)
            maxLev     : float (maximum allowed leverage)
            minNotional: str   (minimum notional value in USDT)
            mmr        : float (base-tier maintenance margin rate, e.g. 0.005 = 0.5%)
            tiers      : list  (risk-limit tiers, ascending: {limit, mmr, maxLev})
        """
        return self._data.get(symbol)

    @staticmethod
    def tier_for(rules: dict, notional: float) -> dict:
        """
        The risk-limit tier that applies to a position of *notional* USDT:
        {limit, mmr, maxLev, index, count}. Bybit auto-adjusts the tier with
        position value, raising MMR and lowering max leverage as it grows, so
        sizing must use the tier the trade will actually land in.
        """
        tiers = rules.get("tiers") or []
        if not tiers:
            return {"limit": float("inf"), "mmr": rules.get("mmr", 0.005),
                    "maxLev": rules.get("maxLev", 1.0), "index": 0, "count": 1}
        for i, tier in enumerate(tiers):
            if notional <= tier["limit"]:
                return {**tier, "index": i, "count": len(tiers)}
        return {**tiers[-1], "index": len(tiers) - 1, "count": len(tiers)}

    def refresh(self) -> None:
        """Force a fresh pull from the exchange, ignoring TTL."""
        self._fetch_and_persist()

    @property
    def symbols(self) -> list:
        """Return sorted list of all cached symbol names."""
        return sorted(self._data.keys())

    @property
    def count(self) -> int:
        return len(self._data)

    # ─── Internal ──────────────────────────────────────────────────

    def _load(self, on_complete: Optional[Callable[[int], None]] = None) -> None:
        """Load from disk if exists, and refresh in background if expired or missing."""
        def _bg():
            try:
                self._fetch_and_persist()
                if on_complete:
                    on_complete(len(self._data))
            except Exception:
                if on_complete:
                    on_complete(-1)

        if os.path.exists(config.CACHE_FILE):
            try:
                with open(config.CACHE_FILE, "r") as f:
                    self._data = json.load(f)
            except Exception:
                self._data = {}

            age = time.time() - os.path.getmtime(config.CACHE_FILE)
            # A cache written before tiers were stored is refreshed like an expired one.
            stale_schema = any("tiers" not in v for v in self._data.values())
            if age < config.CACHE_TTL_SECONDS and not stale_schema:
                if on_complete:
                    on_complete(len(self._data))
                return
            else:
                # Expired: update in background, but self._data is populated instantly with stale data
                threading.Thread(target=_bg, daemon=True).start()
                return

        # No cache file exists: fetch in background
        threading.Thread(target=_bg, daemon=True).start()

    def _fetch_and_persist(self) -> None:
        """
        Pull ALL linear instrument pages from Bybit V5 and write to disk.
        Then fetch base-tier MMR from risk limit API for each symbol.
        Uses a local temporary dict for atomic assignment to ensure thread safety.
        """
        temp_data = {}
        cursor = ""

        # ── Step 1: Fetch all instruments ────────────────────────
        for _ in range(50):  # safety bound on pagination
            kwargs = {"category": "linear", "limit": 1000}
            if cursor:
                kwargs["cursor"] = cursor

            res = self._client.get_instruments_info(**kwargs)
            result = res.get("result", {})

            for item in result.get("list", []):
                # Only cache actively trading perpetuals
                if item.get("status") != "Trading":
                    continue

                temp_data[item["symbol"]] = {
                    "tickSize": item["priceFilter"]["tickSize"],
                    "qtyStep": item["lotSizeFilter"]["qtyStep"],
                    "minQty": item["lotSizeFilter"]["minOrderQty"],
                    "maxLev": float(item["leverageFilter"]["maxLeverage"]),
                    "minNotional": item["lotSizeFilter"].get("minNotionalValue", "5"),
                    "mmr": 0.005,  # Default fallback (0.5%), overwritten below
                }

            cursor = result.get("nextPageCursor", "")
            if not cursor:
                break

        # ── Step 2: Fetch ALL risk-limit tiers per symbol ─────────
        #    Bybit raises MMR (and lowers max leverage) as position value
        #    crosses each tier's limit. On most alts tier 1 ends at
        #    $5k–$20k, well inside a $100-risk trade, so the base tier
        #    alone would overstate the liquidation distance.
        rl_cursor = ""
        tiers_by_sym: Dict[str, list] = {}
        for _ in range(50):  # safety bound on pagination
            rl_kwargs = {"category": "linear"}
            if rl_cursor:
                rl_kwargs["cursor"] = rl_cursor

            try:
                rl_res = self._client.get_risk_limit(**rl_kwargs)
                rl_result = rl_res.get("result", {})

                for tier in rl_result.get("list", []):
                    sym = tier.get("symbol", "")
                    if sym not in temp_data:
                        continue
                    try:
                        tiers_by_sym.setdefault(sym, []).append({
                            "limit": float(tier.get("riskLimitValue") or 0),
                            "mmr": float(tier.get("maintenanceMargin") or 0.005),
                            "maxLev": float(tier.get("maxLeverage") or 0)
                                      or temp_data[sym]["maxLev"],
                        })
                    except (TypeError, ValueError):
                        continue

                rl_cursor = rl_result.get("nextPageCursor", "")
                if not rl_cursor:
                    break
            except Exception:
                break  # Risk limit fetch is best-effort

        for sym, tiers in tiers_by_sym.items():
            tiers.sort(key=lambda t: t["limit"])
            temp_data[sym]["tiers"] = tiers
            temp_data[sym]["mmr"] = tiers[0]["mmr"]   # base tier, for display

        # Persist to disk
        try:
            with open(config.CACHE_FILE, "w") as f:
                json.dump(temp_data, f, indent=2)
        except Exception as e:
            sys.stderr.write(f"[!] Warning: Failed to persist symbology cache to disk: {e}\n")

        # Atomic assignment to ensure thread safety
        self._data = temp_data

