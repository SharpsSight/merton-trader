#!/usr/bin/env python3
"""
preflight_check.py — validate the full Alpaca paper-trading chain before launch.

Proves, in order:
  1. Credentials load + auth succeeds
  2. Account is a PAPER account, active, and not restricted
  3. Market clock (is it open? when does it open/close?)
  4. IEX historical data is reachable (the free feed you actually trade on)
  5. (optional, --test-order) a single tiny order round-trips: submit -> cancel

Run it BEFORE flipping the live runner on. If any step FAILs, do not launch.

Usage:
    export ALPACA_API_KEY=...        # or APCA_API_KEY_ID
    export ALPACA_SECRET_KEY=...     # or APCA_API_SECRET_KEY
    python3 preflight_check.py
    python3 preflight_check.py --test-order   # also round-trips 1 share of SPY (paper only)
"""

import os
import sys
import argparse
from datetime import datetime, timedelta, timezone

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed

PASS, FAIL, WARN = "\033[92mPASS\033[0m", "\033[91mFAIL\033[0m", "\033[93mWARN\033[0m"


def _key(*names):
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return None


def line(status, msg):
    print(f"  [{status}] {msg}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test-order", action="store_true",
                    help="Round-trip a 1-share SPY market order (paper only).")
    ap.add_argument("--symbol", default="SPY", help="Symbol for data + test-order checks.")
    args = ap.parse_args()

    api_key = _key("ALPACA_API_KEY", "APCA_API_KEY_ID")
    secret = _key("ALPACA_SECRET_KEY", "APCA_API_SECRET_KEY")

    print("\n=== Alpaca paper-trading preflight ===\n")

    # ---- 1. Credentials present -------------------------------------------
    if not api_key or not secret:
        line(FAIL, "Credentials not found. Set ALPACA_API_KEY / ALPACA_SECRET_KEY.")
        sys.exit(1)
    line(PASS, f"Credentials loaded (key ...{api_key[-4:]}).")

    # ---- 2. Auth + account -------------------------------------------------
    try:
        tc = TradingClient(api_key, secret, paper=True)
        acct = tc.get_account()
    except Exception as e:
        line(FAIL, f"Auth/account fetch failed: {e}")
        sys.exit(1)

    line(PASS, f"Auth OK. Account {acct.account_number}.")

    if str(acct.status) != "AccountStatus.ACTIVE":
        line(FAIL, f"Account status is {acct.status}, expected ACTIVE.")
        sys.exit(1)
    line(PASS, "Account status ACTIVE.")

    if acct.trading_blocked or acct.account_blocked:
        line(FAIL, "Account is blocked from trading.")
        sys.exit(1)
    line(PASS, "Trading not blocked.")

    line(PASS, f"Equity ${float(acct.equity):,.2f} | "
               f"Buying power ${float(acct.buying_power):,.2f}")

    # PDT context (informational on paper; matters when you go live)
    dt_count = getattr(acct, "daytrade_count", None)
    pdt = getattr(acct, "pattern_day_trader", False)
    line(WARN if pdt else PASS,
         f"PDT flag={pdt}, daytrade_count={dt_count} "
         f"(paper ignores the $25k rule; live will not).")

    # ---- 3. Market clock ---------------------------------------------------
    try:
        clock = tc.get_clock()
    except Exception as e:
        line(FAIL, f"Clock fetch failed: {e}")
        sys.exit(1)

    if clock.is_open:
        line(PASS, f"Market OPEN. Closes {clock.next_close}.")
    else:
        line(WARN, f"Market CLOSED. Opens {clock.next_open}. "
                   f"Runner will idle until then.")

    # ---- 4. IEX data reachable --------------------------------------------
    try:
        dc = StockHistoricalDataClient(api_key, secret)
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=5)
        req = StockBarsRequest(
            symbol_or_symbols=args.symbol,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
            feed=DataFeed.IEX,
        )
        bars = dc.get_stock_bars(req)
        n = len(bars.data.get(args.symbol, []))
        if n == 0:
            line(FAIL, f"IEX returned 0 bars for {args.symbol}. Data path broken.")
            sys.exit(1)
        last = bars.data[args.symbol][-1]
        line(PASS, f"IEX data OK: {n} daily bars for {args.symbol}, "
                   f"last close ${last.close:.2f} @ {last.timestamp.date()}.")
    except Exception as e:
        line(FAIL, f"IEX data fetch failed: {e}")
        sys.exit(1)

    # ---- 5. Optional order round-trip -------------------------------------
    if args.test_order:
        if not clock.is_open:
            line(WARN, "Market closed; skipping test order (would queue, not fill).")
        else:
            try:
                order = tc.submit_order(MarketOrderRequest(
                    symbol=args.symbol, qty=1, side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                ))
                line(PASS, f"Test order accepted: id={order.id}, status={order.status}.")
                tc.cancel_order_by_id(order.id)
                line(PASS, "Test order cancel requested (paper only).")
            except Exception as e:
                line(FAIL, f"Test order failed: {e}")
                sys.exit(1)

    print("\n=== Preflight complete. If all PASS, you are clear to launch. ===\n")


if __name__ == "__main__":
    main()
