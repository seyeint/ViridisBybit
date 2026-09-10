"""
CLI dry run: price a trade through the engine, print exactly what would be
sent, and optionally fire it.

Everything goes through TradingCore, so the numbers and the payload are the
ones the window would produce — nothing is re-derived here.

Scenario: BTC LONG · entry $74,300 · SL $73,300 · TP $75,300 · risk $50
"""

import json
import sys

import config
from trading_core import TradingCore

SYMBOL = "BTCUSDT"
SIDE = "Buy"
ENTRY = 74300.0
SL = 73300.0
TP = 75300.0
RISK = 50.0
POST_ONLY = True

print("=" * 60)
print("  VIRIDIS — TRADE DRY RUN")
print("=" * 60)
print(f"    API key:  {config.API_KEY[:4] + '…' if config.API_KEY else '(not set)'}")
print(f"    Testnet:  {config.USE_TESTNET}")

core = TradingCore(
    on_log=lambda msg, err: print(f"    {'!' if err else '>'} {msg}"),
    on_trade_update=lambda t: print(f"    state → {t.symbol} {t.phase}"),
)
core.sync_fee_rates()

rules = core.cache.get(SYMBOL)
if not rules:
    print(f"    {SYMBOL} not in cache — wait for the download and rerun")
    sys.exit(1)
tiers = rules.get("tiers") or []
print(f"\n    {SYMBOL}: tick {rules['tickSize']} · step {rules['qtyStep']} · "
      f"max {rules['maxLev']:g}x · {len(tiers)} risk-limit tiers")

# ── Risk math ─────────────────────────────────────────────────────
try:
    calc = core.calculate_trade(SYMBOL, SIDE, ENTRY, SL, RISK)
except ValueError as e:
    print(f"\n    sizing error: {e}")
    sys.exit(1)

rr = abs(TP - ENTRY) / abs(ENTRY - SL)
print(f"\n    Entry {calc['entry']}  SL {calc['sl']}  TP {TP}  ({calc['sl_distance_pct']}% stop, rr 1:{rr:.2f})")
print(f"    Qty {calc['qty']}  ·  notional ${calc['notional_usd']:,.2f}  ·  "
      f"lev {calc['leverage']}x / max {calc['max_exchange_lev']}x  ·  margin ${calc['margin_usd']:,.2f}")
print(f"    Risk ${RISK:.2f} net incl ≈${calc['fee_usd']:.2f} fees")
print(f"    Tier {calc['tier']}/{calc['tier_count']} · mmr {calc['mmr_pct']:.3f}% · "
      f"liq ≈ {calc['liq_price']:.6g} · cushion {calc['cushion_pct']:.2f}% behind the stop")

# ── Payload ───────────────────────────────────────────────────────
payload = core.build_bracket_payload(calc, TP, POST_ONLY, order_link_id="dry_run")
print("\n    /v5/order/create would receive:")
print(json.dumps(payload, indent=4))

# ── Account (read-only) ───────────────────────────────────────────
print(f"\n    Margin mode: {core.get_account_margin_mode()}")
snap = core.get_wallet_snapshot()
if snap["equity"] is not None:
    avail = snap["available"]
    print(f"    Equity ${snap['equity']:,.2f}  ·  available "
          f"{'$' + format(avail, ',.2f') if avail is not None else 'unknown'}")
    if avail is not None:
        print("    Margin ok" if calc["margin_usd"] <= avail
              else f"    INSUFFICIENT MARGIN (need ${calc['margin_usd'] - avail:,.2f} more)")

active = [t for t in core.sync_existing() if t.symbol == SYMBOL]
if active:
    print(f"    Already active on {SYMBOL}: {active[0].phase} — execute_bracket will refuse")

# ── Fire or abort ─────────────────────────────────────────────────
print("\n    Type FIRE to send this bracket to Bybit, anything else to abort.")
if input("    >>> ").strip() != "FIRE":
    print("    Aborted. Nothing was sent.")
    sys.exit(0)

try:
    trade = core.execute_bracket(SYMBOL, SIDE, ENTRY, SL, TP, RISK, post_only=POST_ONLY)
    print(f"\n    Placed: {trade.entry_order_id} — resting with its bracket, in the ledger for R.")
    print(f"    Monitor: https://www.bybit.com/trade/usdt/{SYMBOL}")
except Exception as e:
    print(f"\n    EXECUTION FAILED: {e}")
    sys.exit(1)
