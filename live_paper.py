#!/usr/bin/env python3
"""
live_paper.py — integrated paper-trading runner.

Full pipeline, one bar cadence:
    fetch bars -> confluence signal -> factor confirmation -> news overlay
    -> Merton size -> risk gate -> paper order

MODE:
  - If signal_stats.json is present (produced by run_backtest.py), runs in
    TRADE mode: sizes and places paper orders.
  - If absent, runs in OBSERVE mode: computes and logs signals/intents but
    places NO orders. So this is safe to deploy before the backtest exists.

Same signal codepath as the backtest (anti-drift). Fails loud, not silent.
"""

import os
import sys
import json
import time
import logging
from datetime import datetime, timedelta, timezone

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import config
import trend_signals as ts
import factors as fac
import merton
import news_overlay as no
import data_feed as feed
from backtest import _bucket
from risk_manager import RiskManager, NORMAL

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (MarketOrderRequest, StopOrderRequest,
                                      TrailingStopOrderRequest, GetOrdersRequest)
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed


def plan_symbol(direction, score, held_dir, entry_threshold):
    """ENTER / EXIT / FLIP / HOLD / NONE — pure position-decision logic."""
    weak = (direction == 0) or (abs(score) < entry_threshold)
    if held_dir == 0:
        return "NONE" if weak else "ENTER"
    if weak:
        return "EXIT"
    if direction != held_dir:
        return "FLIP"
    return "HOLD"


def cancel_symbol_orders(tc, symbol):
    """Cancel any open (e.g. protective stop) orders for a symbol."""
    try:
        open_orders = tc.get_orders(filter=GetOrdersRequest(
            status=QueryOrderStatus.OPEN, symbols=[symbol]))
        for o in open_orders:
            tc.cancel_order_by_id(o.id)
    except Exception as e:
        log.warning("  %s cancel-orders failed: %s", symbol, e)


def place_entry_with_stop(tc, rm, symbol, approved, price, sig):
    """Submit the entry market order, then a GTC TRAILING stop on the opposite
    side. GTC so protection PERSISTS overnight (a DAY stop expires at 4pm,
    leaving multi-day holds naked). Trails favourable moves; triggers on reversal."""
    side = OrderSide.BUY if approved > 0 else OrderSide.SELL
    tc.submit_order(MarketOrderRequest(symbol=symbol, qty=abs(approved),
                                       side=side, time_in_force=TimeInForce.DAY))
    place_trailing_stop(tc, symbol, abs(approved), approved > 0)
    return side


def place_trailing_stop(tc, symbol, qty, is_long):
    """Place a GTC trailing stop protecting a position (opposite side)."""
    stop_side = OrderSide.SELL if is_long else OrderSide.BUY
    try:
        tc.submit_order(TrailingStopOrderRequest(
            symbol=symbol, qty=abs(int(qty)), side=stop_side,
            time_in_force=TimeInForce.GTC, trail_percent=config.TRAIL_PERCENT))
    except Exception as e:
        log.warning("  %s trailing-stop failed: %s", symbol, e)


def open_order_symbols(tc):
    """Symbols with a currently open order (e.g. a live protective stop)."""
    try:
        return {o.symbol for o in tc.get_orders(
            filter=GetOrdersRequest(status=QueryOrderStatus.OPEN))}
    except Exception as e:
        log.warning("open-orders check failed: %s", e)
        return set()


logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("runner")

OPEN_POLL_SECONDS = 60
CLOSED_POLL_CAP = 900
FETCH_DAYS = 15               # trailing 15m history per symbol for warmup


def _key(*names):
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return None


def load_stats_and_universe(dc):
    """Load bucket stats + the universe + the tradeable (worthy) subset the
    backtest chose. Fallback to a fresh selection if the file is missing."""
    try:
        with open(config.SIGNAL_STATS_PATH) as f:
            payload = json.load(f)
        stats = payload.get("buckets", {})
        universe = payload.get("universe") or feed.select_universe(
            dc, config.CANDIDATE_POOL, config.UNIVERSE_SIZE) or config.UNIVERSE
        tradeable = set(payload.get("tradeable") or universe)
        return stats, universe, tradeable
    except FileNotFoundError:
        universe = feed.select_universe(
            dc, config.CANDIDATE_POOL, config.UNIVERSE_SIZE) or config.UNIVERSE
        return None, universe, set(universe)


def current_positions(tc):
    pos = {}
    for p in tc.get_all_positions():
        pos[p.symbol] = {"shares": int(float(p.qty)), "price": float(p.avg_entry_price)}
    return pos


def run():
    api_key = _key("ALPACA_API_KEY", "APCA_API_KEY_ID")
    secret = _key("ALPACA_SECRET_KEY", "APCA_API_SECRET_KEY")
    if not api_key or not secret:
        log.error("Credentials not found."); sys.exit(1)

    try:
        tc = TradingClient(api_key, secret, paper=True)
        dc = StockHistoricalDataClient(api_key, secret)
        acct = tc.get_account()
    except Exception as e:
        log.error("Auth failed (paper keys?): %s", e); sys.exit(1)

    stats, universe, tradeable = load_stats_and_universe(dc)
    mode = "TRADE" if stats else "OBSERVE"
    start_equity = float(acct.equity)
    rm = RiskManager(start_equity)

    log.info("=== live_paper starting | mode=%s | equity $%s | universe=%d | tradeable=%d ===",
             mode, f"{start_equity:,.2f}", len(universe), len(tradeable))
    if mode == "OBSERVE":
        log.info("No signal_stats.json -> OBSERVE mode: logging signals, NOT trading. "
                 "Run run_backtest.py to enable sizing.")

    while True:
        try:
            clock = tc.get_clock()
            if not clock.is_open:
                now = datetime.now(timezone.utc)
                nap = max(30, min((clock.next_open - now).total_seconds(), CLOSED_POLL_CAP))
                log.info("Market CLOSED. Next open %s. Idling %.0fs.", clock.next_open, nap)
                time.sleep(nap); continue

            acct = tc.get_account()
            equity = float(acct.equity)
            positions = current_positions(tc)

            # end-of-day flatten (optional): close everything near the close and
            # skip the rest of the cycle so nothing is carried overnight.
            if config.FLATTEN_EOD:
                secs_to_close = (clock.next_close - now).total_seconds()
                if 0 < secs_to_close <= config.FLATTEN_BUFFER_MIN * 60:
                    for s in list(positions.keys()):
                        cancel_symbol_orders(tc, s)
                        try:
                            tc.close_position(s)
                        except Exception as e:
                            log.warning("  %s EOD close failed: %s", s, e)
                    log.info("EOD FLATTEN | closed %d positions | %.0f min to close",
                             len(positions), secs_to_close / 60)
                    time.sleep(OPEN_POLL_SECONDS); continue

            # ONE batched fetch for the whole universe + market proxy
            frames = feed.fetch_bars_batch(dc, universe + [config.MARKET_PROXY],
                                           FETCH_DAYS)

            # which held positions currently have a live protective (stop) order?
            protected = open_order_symbols(tc)

            # market-level volatility circuit-breaker (macro shocks)
            breaker = False
            try:
                mkt = frames.get(config.MARKET_PROXY)
                if mkt is not None and len(mkt) > 20:
                    ret = mkt["close"].pct_change()
                    recent_move = abs(ret.iloc[-1])
                    atr_ret = ret.abs().rolling(14).mean().iloc[-1]
                    breaker = no.volatility_circuit_breaker(recent_move, atr_ret)
            except Exception as e:
                log.warning("circuit-breaker check failed: %s", e)

            log.info("HEARTBEAT | %s | equity $%s | positions=%d | breaker=%s",
                     mode, f"{equity:,.2f}", len(positions), breaker)

            # news overlay action (market-wide breaker for now)
            action, reason = (no.news_risk_action({}, True) if breaker
                              else (NORMAL, "clear"))

            for sym in universe:
                try:
                    df = frames.get(sym)
                    if df is None or len(df) < 60:
                        continue
                    sig = ts.confluence_signal(df)
                    price = float(df["close"].iloc[-1])

                    held = positions.get(sym, {}).get("shares", 0)
                    held_dir = 1 if held > 0 else (-1 if held < 0 else 0)
                    plan = plan_symbol(sig["direction"], sig["score"], held_dir,
                                       config.ENTRY_THRESHOLD)

                    # --- manage existing positions (exits happen even in TRADE-only) ---
                    if plan in ("EXIT", "FLIP"):
                        cancel_symbol_orders(tc, sym)          # clear protective stop
                        tc.close_position(sym)                 # flatten
                        log.info("  [CLOSE] %-5s %s (held %+d) score=%+.2f",
                                 sym, plan, held, sig["score"])
                        positions.pop(sym, None)
                        if plan == "EXIT":
                            continue                            # done; no re-entry
                        held_dir = 0                            # FLIP falls through to enter

                    if plan == "HOLD":
                        # ensure the held position still has a live protective stop
                        # (e.g. an old DAY stop expired). Re-arm if missing.
                        if held != 0 and sym not in protected:
                            place_trailing_stop(tc, sym, abs(held), held > 0)
                            log.info("  [PROTECT] %-5s re-armed trailing stop (held %+d)",
                                     sym, held)
                        continue

                    # --- entries (ENTER, or the enter-leg of FLIP) ---
                    if sig["direction"] == 0:
                        continue
                    # per-symbol worthiness: only ENTER names whose own edge
                    # cleared the LCB in the backtest. (Held names still exit above.)
                    if sym not in tradeable:
                        continue
                    conf = fac.confirmation(df, sig["direction"])
                    bucket = _bucket(sig["score"])
                    bstats = (stats or {}).get(bucket, {"mu": 0, "sigma": 0, "n": 0})

                    # per-symbol realized vol over the holding horizon -> risk scaling
                    bar_ret = df["close"].pct_change().dropna()
                    symbol_vol = float(bar_ret.tail(100).std() * (config.HOLD_BARS ** 0.5))

                    intent = merton.size_position(equity, price, sig["direction"],
                                                  bstats, symbol_vol, conf["multiplier"])

                    if mode == "OBSERVE" or intent["shares"] == 0:
                        log.info("  [obs] %-5s dir=%+d score=%+.2f conf=%.2f vol=%.3f "
                                 "bucket=%s would_size=%d",
                                 sym, sig["direction"], sig["score"], conf["multiplier"],
                                 symbol_vol, bucket, intent["shares"])
                        continue

                    approved, gate = rm.gate_entry(sym, intent["shares"], price,
                                                   positions, equity, action)
                    if approved == 0:
                        log.info("  [gate] %-5s blocked: %s (news=%s)", sym, gate, reason)
                        continue

                    side = place_entry_with_stop(tc, rm, sym, approved, price, sig)
                    # update running state so portfolio caps bind for later symbols
                    positions[sym] = {"shares": approved, "price": price}
                    log.info("  [ORDER] %-5s %s %d @ ~%.2f frac=%.3f vol=%.3f score=%+.2f",
                             sym, side.value, abs(approved), price,
                             intent["fraction"], symbol_vol, sig["score"])
                except Exception as e:
                    log.warning("  %s error: %s", sym, e)

            time.sleep(OPEN_POLL_SECONDS)

        except KeyboardInterrupt:
            log.info("Interrupted. Shutting down."); break
        except Exception as e:
            log.error("Loop error (continuing): %s", e)
            time.sleep(30)


if __name__ == "__main__":
    run()
