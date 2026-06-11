"""
Configuration loader.
Reads API credentials and app settings from environment / .env file.
"""

import os
import sys
from dotenv import load_dotenv

# Load .env from project root (same directory as this file)
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    load_dotenv(_env_path)
else:
    print(
        "[!] No .env file found. Copy .env.example -> .env and fill in your API keys.",
        file=sys.stderr,
    )

# ─── API Credentials ──────────────────────────────────────────────
API_KEY: str = os.getenv("BYBIT_API_KEY", "")
API_SECRET: str = os.getenv("BYBIT_API_SECRET", "")
USE_TESTNET: bool = os.getenv("BYBIT_TESTNET", "false").lower() == "true"


# Default dollar amount at risk per trade if SL is hit.
DEFAULT_RISK_USD: float = float(os.getenv("DEFAULT_RISK_USD", "100.0"))

# ─── Fees & Triggers ──────────────────────────────────────────────
# Round-trip fee rates (Bybit VIP0 linear perp defaults). Override to match
# your fee tier. Used so "risk" means net loss *including* fees, and so the
# journal/breakeven logic is fee-aware. Maker = limit fills, Taker = market.
FEE_MAKER: float = float(os.getenv("FEE_MAKER_RATE", "0.0002"))   # 0.02%
FEE_TAKER: float = float(os.getenv("FEE_TAKER_RATE", "0.00055"))  # 0.055%

# Price reference that fires the Stop Loss. Liquidation always uses MarkPrice,
# so triggering the SL on MarkPrice guarantees the SL fires before liquidation
# (the leverage cushion remains the room for the market SL to fill). Set to
# "LastPrice" to trigger on last traded price instead.
SL_TRIGGER_BY: str = os.getenv("SL_TRIGGER_BY", "MarkPrice")

# ─── Symbology Cache ──────────────────────────────────────────────
CACHE_FILE: str = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "bybit_symbology.json"
)
CACHE_TTL_SECONDS: int = int(os.getenv("CACHE_TTL_SECONDS", "86400"))  # 24 hours

# ─── Journal ──────────────────────────────────────────────────────
# The journal mirrors Bybit's closed-PnL history (the source of truth), so it
# captures every closed trade — including ones closed while the app was off.
# On a fresh journal, backfill this many days of history.
JOURNAL_BACKFILL_DAYS: int = int(os.getenv("JOURNAL_BACKFILL_DAYS", "30"))

# Sidecar that remembers the intended $ risk per app-placed trade, so the
# journal can compute R-multiples for trades this app opened (matched on
# symbol + entry price). Backfilled/external trades get $ stats but no R.
RISK_LEDGER_FILE: str = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "risk_ledger.json"
)
