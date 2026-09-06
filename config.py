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

# Ledger entries older than this are pruned on load. It must exceed your
# longest hold: a live position whose entry is pruned loses its R-multiple
# and its Strat1 restore after a restart.
RISK_LEDGER_MAX_AGE_DAYS: int = int(os.getenv("RISK_LEDGER_MAX_AGE_DAYS", "365"))

# ─── Risk governance (account level) ─────────────────────────────
# Risk on one trade above this share of equity asks for a hard confirm.
MAX_RISK_PCT: float = float(os.getenv("MAX_RISK_PCT", "2.0"))
# Open risk (sum of $ at the stops across active trades) above this share of
# equity asks for a confirm before adding another trade.
MAX_OPEN_RISK_PCT: float = float(os.getenv("MAX_OPEN_RISK_PCT", "6.0"))
# Realised loss today at which the terminal stops taking new trades. 0 = off.
DAILY_LOSS_LIMIT_USD: float = float(os.getenv("DAILY_LOSS_LIMIT_USD", "0"))
# Consecutive losses at which a new trade asks for a confirm. 0 = off.
LOSS_STREAK_CONFIRM: int = int(os.getenv("LOSS_STREAK_CONFIRM", "3"))

# ─── Strat1 ratchet ───────────────────────────────────────────────
# "progress:lockR, …" — at ≥ progress of the entry→TP journey, move the stop
# to lock lockR (in original stop distances): -0.5 = half the stop still at
# risk, 0 = fee-adjusted break-even, +0.5 = break-even plus half an R.
STRAT1_RATCHET: str = os.getenv("STRAT1_RATCHET", "0.75:-0.5,0.90:0")
