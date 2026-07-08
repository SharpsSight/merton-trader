#!/usr/bin/env python3
"""
live_paper.py — persistent paper-trading runner (foundation build).

WHAT IT DOES NOW:
  - Connects to Alpaca paper account (fails LOUD if creds are bad).
  - Runs a persistent loop through the session (this is what Railway keeps alive).
  - When market is OPEN: heartbeat-logs equity + a slot where signal eval will go.
  - When market is CLOSED: idles until next open, re-checking periodically.
  - Surfaces errors instead of dying silently.

WHAT IT DOES NOT DO YET (by design — these get layered in next):
  - Generate signals (signals/ package)
  - Size positions (sizing/merton.py — needs real mu/sigma from the backtest)
  - Enforce risk limits (risk/manager.py)
  - Place orders

So on day one this is a live, always-on CONNECTION + HEARTBEAT. It will not
trade until the signal/sizer/risk pieces are wired into the marked section below.

RUN:
    local:   python runner/live_paper.py
    Railway: set start command to `python runner/live_paper.py`
             and set ALPACA_API_KEY / ALPACA_SECRET_KEY in Railway env vars.
"""

import os
import sys
import time
import logging
from datetime import datetime, timezone

try:
    from dotenv import load_dotenv
    load_dotenv()  # loads .env locally; harmless on Railway (uses real env vars)
except ImportError:
    pass

from alpaca.trading.client import TradingClient

# ---- config ----------------------------------------------------------------
OPEN_POLL_SECONDS = 60        # heartbeat cadence while market is open
CLOSED_POLL_CAP_SECONDS = 900 # max idle sleep while closed (re-check every 15m)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("runner")


def _key(*names):
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return None


def connect():
    api_key = _key("ALPACA_API_KEY", "APCA_API_KEY_ID")
    secret = _key("ALPACA_SECRET_KEY", "APCA_API_SECRET_KEY")
    if not api_key or not secret:
        log.error("Credentials not found. Set ALPACA_API_KEY / ALPACA_SECRET_KEY.")
        sys.exit(1)
    try:
        tc = TradingClient(api_key, secret, paper=True)
        acct = tc.get_account()
    except Exception as e:
        log.error("Auth/account failed — check keys are PAPER keys: %s", e)
        sys.exit(1)
    log.info("Connected. Paper account %s | equity $%s",
             acct.account_number, f"{float(acct.equity):,.2f}")
    return tc


def run():
    log.info("=== live_paper runner starting ===")
    tc = connect()

    while True:
        try:
            clock = tc.get_clock()

            if clock.is_open:
                acct = tc.get_account()
                log.info("HEARTBEAT | market OPEN | equity $%s | buying_power $%s",
                         f"{float(acct.equity):,.2f}",
                         f"{float(acct.buying_power):,.2f}")

                # ==========================================================
                # SIGNAL / SIZE / RISK / ORDER PIPELINE SLOTS IN HERE.
                #   signals  -> evaluate ORB / VWAP-reversion / momentum
                #   sizing   -> Merton size using per-signal mu/sigma
                #   risk     -> kill switch, daily-loss halt, exposure caps
                #   execute  -> submit paper orders via tc.submit_order(...)
                # Same signal codepath as the backtest (anti-drift discipline).
                # ==========================================================

                time.sleep(OPEN_POLL_SECONDS)

            else:
                now = datetime.now(timezone.utc)
                secs = (clock.next_open - now).total_seconds()
                nap = max(30, min(secs, CLOSED_POLL_CAP_SECONDS))
                log.info("Market CLOSED. Next open %s. Idling %.0fs.",
                         clock.next_open, nap)
                time.sleep(nap)

        except KeyboardInterrupt:
            log.info("Interrupted. Shutting down cleanly.")
            break
        except Exception as e:
            # fail loud, but don't let a transient blip kill the session
            log.error("Loop error (continuing): %s", e)
            time.sleep(30)


if __name__ == "__main__":
    run()
