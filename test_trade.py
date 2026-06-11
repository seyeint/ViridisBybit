"""
Test Trade Script — Dry-run + optional live execution.

Scenario: BTC LONG
  Entry:  $74,300 (Limit)
  SL:     $73,300 (Market)
  TP:     $75,300 (Limit)
  Risk:   $50
"""

import json
import sys
import traceback

# ── Step 0: Boot config ──────────────────────────────────────────
print("=" * 60)
print("  BYBIT V5 OTOCO — TRADE TEST HARNESS")
print("=" * 60)

import config

print(f"\n[1] CONFIG LOADED")
print(f"    API Key:     {config.API_KEY[:4]}..." if config.API_KEY else "    API Key:     (not set)")
print(f"    Testnet:     {config.USE_TESTNET}")
print(f"    Cache File:  {config.CACHE_FILE}")

# ── Step 1: Test Cache Engine ────────────────────────────────────
print(f"\n{'─' * 60}")
print(f"[2] CACHE ENGINE TEST")

from pybit.unified_trading import HTTP

client = HTTP(
    testnet=config.USE_TESTNET,
    api_key=config.API_KEY,
    api_secret=config.API_SECRET,
)

from cache_engine import InstrumentCache

cache = InstrumentCache(client)
print(f"    Total symbols cached: {cache.count}")

# Check BTC specifically
btc_rules = cache.get("BTCUSDT")
if btc_rules:
    print(f"    BTCUSDT rules:")
    print(f"      tickSize:    {btc_rules['tickSize']}")
    print(f"      qtyStep:     {btc_rules['qtyStep']}")
    print(f"      minQty:      {btc_rules['minQty']}")
    print(f"      maxLeverage: {btc_rules['maxLev']}x")
    print(f"      minNotional: ${btc_rules['minNotional']}")
    mmr_val = btc_rules.get('mmr', 0.005)
    print(f"      MMR (base):  {mmr_val*100:.2f}% ← real value from risk limit API")
else:
    print("    ❌ BTCUSDT NOT FOUND IN CACHE!")
    sys.exit(1)

# ── Step 2: Test Risk Math (pure calculation, no API calls) ──────
print(f"\n{'─' * 60}")
print(f"[3] RISK MATH — DRY RUN")

from trading_core import TradingCore, format_value

# We'll use a standalone TradingCore for the math test
# but suppress WebSocket connection for now
core = TradingCore(
    on_log=lambda msg, err: print(f"    {'❌' if err else '✓'} {msg}"),
    on_trade_update=lambda t: print(f"    📡 Trade state → {t.phase}"),
)

# ── Your scenario ────────────────────────────────────────────────
SYMBOL = "BTCUSDT"
SIDE   = "Buy"       # Long
ENTRY  = 74300.0
SL     = 73300.0
TP     = 75300.0
RISK   = 50.0        # $50 max loss if SL is hit

print(f"\n    Scenario:")
print(f"      Symbol: {SYMBOL}")
print(f"      Side:   {SIDE} (LONG)")
print(f"      Entry:  ${ENTRY:,.1f}")
print(f"      SL:     ${SL:,.1f}")
print(f"      TP:     ${TP:,.1f}")
print(f"      Risk:   ${RISK:.0f}")

try:
    calc = core.calculate_trade(SYMBOL, SIDE, ENTRY, SL, RISK)
    
    notional_str = f"${calc['notional_usd']:,.2f}"
    margin_str = f"${calc['margin_usd']:,.2f}"
    risk_str = f"${RISK:.0f}"
    
    print(f"\n    ⚡ CALCULATION RESULTS:")
    print(f"      SL Distance:      {calc['sl_distance_pct']:.4f}%")
    print(f"      Symbol MMR:       {calc.get('mmr_pct', 0.5):.3f}%")
    print(f"      Total Buffer:     {calc.get('buffer_pct', 0.7):.3f}% (MMR + 0.2% fee cushion)")
    print(f"      Safe Leverage:     {calc['leverage']}x  (max: {calc['max_exchange_lev']}x)")
    print(f"      Position Qty:      {calc['qty']} BTC")
    print(f"      Notional Value:    {notional_str}  (total position value)")
    print(f"      Margin Locked:     {margin_str}  ← actual capital from your balance")
    print(f"      Est. Round-trip Fee: ${calc.get('fee_usd', 0):.2f}  ← maker entry + taker SL, baked into qty")
    print(f"      Risk at SL:        {risk_str}  ← max NET loss if SL triggers (price move + fees)")
    print(f"      Entry (formatted): {calc['entry']}")
    print(f"      SL (formatted):    {calc['sl']}")
    
    # R:R ratio
    reward = abs(TP - ENTRY)
    risk_dist = abs(ENTRY - SL)
    rr = reward / risk_dist if risk_dist > 0 else 0
    print(f"      R:R = 1:{rr:.2f}")
    
except Exception as e:
    print(f"\n    ❌ CALC ERROR: {e}")
    sys.exit(1)

# ── Step 3: Show exact API payload that would be sent ────────────
print(f"\n{'─' * 60}")
print(f"[4] EXACT API PAYLOAD (what /v5/order/create would receive)")

str_tp = format_value(TP, btc_rules['tickSize'])
str_tp_trigger = TradingCore._tp_trigger_price(ENTRY, TP, btc_rules['tickSize'])

payload = {
    "category": "linear",
    "symbol": SYMBOL,
    "side": SIDE,
    "positionIdx": 0,
    "orderType": "Limit",
    "qty": calc['qty'],
    "price": calc['entry'],
    "stopLoss": calc['sl'],
    "slOrderType": "Market",
    "slTriggerBy": config.SL_TRIGGER_BY,
    "takeProfit": str_tp_trigger,
    "tpOrderType": "Limit",
    "tpLimitPrice": str_tp,
    "tpslMode": "Partial",
    "timeInForce": "GTC",
}

print(f"\n{json.dumps(payload, indent=4)}")

# ── Step 4: Test leverage/margin setting ─────────────────────────
print(f"\n{'─' * 60}")
print(f"[5] LEVERAGE & MARGIN TEST")

target_lev = calc['leverage']

# Report account margin mode (UTA = account-level only)
try:
    acct_info = client.get_account_info()
    margin_mode = acct_info['result'].get('marginMode', 'unknown')
    uta_status = acct_info['result'].get('unifiedMarginStatus', '?')
    print(f"    Account margin mode: {margin_mode} (UTA status: {uta_status})")
    print(f"    Note: UTA accounts can't mix isolated/cross per-symbol")
except Exception as e:
    print(f"    Account info: {e}")

# Test leverage set
try:
    res = client.set_leverage(
        category="linear",
        symbol=SYMBOL,
        buyLeverage=str(target_lev),
        sellLeverage=str(target_lev),
    )
    print(f"    Leverage → {target_lev}x: retCode={res['retCode']} ({res['retMsg']})")
except Exception as e:
    if "110043" in str(e) or "Not modified" in str(e):
        print(f"    Leverage already at {target_lev}x — OK")
    else:
        print(f"    Leverage error: {e}")

# ── Step 5: Check wallet balance ─────────────────────────────────
print(f"\n{'─' * 60}")
print(f"[6] WALLET BALANCE")

try:
    bal_res = client.get_wallet_balance(accountType="UNIFIED")
    account = bal_res['result']['list'][0]
    
    # Show total account equity
    total_equity = account.get('totalEquity', '0')
    available_balance = account.get('totalAvailableBalance', '0')
    print(f"    Account Total Equity:  ${float(total_equity or 0):,.2f}")
    print(f"    Available Balance:     ${float(available_balance or 0):,.2f}")
    
    # Show individual coin balances
    for coin in account.get('coin', []):
        eq = coin.get('equity', '')
        if eq and float(eq) > 0:
            print(f"    {coin['coin']}: equity={eq}  available={coin.get('availableToWithdraw', '0')}")
    
    avail = float(available_balance or 0)
    margin_required = calc['notional_usd'] / target_lev
    print(f"    Margin needed:  ${margin_required:,.2f} ({target_lev}x leverage)")
    
    if avail >= margin_required:
        print(f"    ✅ Sufficient margin")
    else:
        print(f"    ❌ INSUFFICIENT MARGIN (need ${margin_required - avail:,.2f} more)")
except Exception as e:
    print(f"    Balance check failed: {e}")
    traceback.print_exc()

# ── Step 6: Check for any existing BTC positions ─────────────────
print(f"\n{'─' * 60}")
print(f"[7] EXISTING POSITIONS & ORDERS FOR {SYMBOL}")

try:
    positions = client.get_positions(category="linear", symbol=SYMBOL)
    for pos in positions['result']['list']:
        size = float(pos.get('size', 0))
        if size > 0:
            print(f"    ⚠ EXISTING POSITION: {pos['side']} {pos['size']} @ {pos['avgPrice']} | uPnL: {pos['unrealisedPnl']}")
        else:
            print(f"    No open position for {SYMBOL}")
except Exception as e:
    print(f"    Position check: {e}")

try:
    orders = client.get_open_orders(category="linear", symbol=SYMBOL)
    if orders['result']['list']:
        print(f"    Open orders:")
        for o in orders['result']['list']:
            print(f"      {o['side']} {o['orderType']} qty={o['qty']} price={o.get('price','—')} status={o['orderStatus']} stopType={o.get('stopOrderType','—')}")
    else:
        print(f"    No open orders for {SYMBOL}")
except Exception as e:
    print(f"    Orders check: {e}")

# ── Step 7: FIRE or ABORT ────────────────────────────────────────
print(f"\n{'─' * 60}")
print(f"[8] EXECUTION DECISION")
print(f"\n    Everything above looks correct?")
print(f"    Type 'FIRE' to send the order to Bybit, anything else to abort.")

answer = input("\n    >>> ").strip()

if answer == "FIRE":
    print(f"\n    🚀 Dispatching atomic bracket to Bybit matching engine...")
    try:
        response = client.place_order(**payload)
        print(f"\n    Response:")
        print(f"    {json.dumps(response, indent=4)}")
        
        if response['retCode'] == 0:
            oid = response['result']['orderId']
            print(f"\n    ✅ SUCCESS! OrderId: {oid}")
            print(f"    Your Entry Limit + OCO bracket is now resting on the book.")
            print(f"    Monitor via: https://www.bybit.com/trade/usdt/{SYMBOL}")
        else:
            print(f"\n    ❌ REJECTED: {response['retMsg']}")
    except Exception as e:
        print(f"\n    ❌ EXECUTION FAILED: {e}")
else:
    print(f"\n    Aborted. No orders were sent.")

print(f"\n{'=' * 60}")
print(f"  TEST COMPLETE")
print(f"{'=' * 60}")
