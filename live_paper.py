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

DAILY OPERATION (this rewrite):
  The old runner constructed RiskManager once, loaded signal_stats.json once,
  and selected the universe once -- all at process start. A process that
  survived a market close carried yesterday's start-of-day equity, yesterday's
  mu/sigma, and yesterday's universe into today, forever. It also referenced an
  undefined `now` inside the FLATTEN_EOD branch. All fixed below:

    * ET session date is derived from the Alpaca clock, never from local time.
    * On rollover: RiskManager.new_session(), stats reload, universe refresh.
    * Nightly stats refresh runs run_backtest.py in the pre-open window.
    * Bar staleness is asserted during RTH -- "no signal" and "no data" are
      different states and the old runner could not distinguish them.
    * close_position() polls for qty_available after cancelling resting orders,
      instead of racing the cancel (the PLTR insufficient-qty rejection).
    * Every order event is appended to LIVE_TRADES_PATH. The weekly
      Welch/Levene/KS diagnostics have no input without this; nothing persisted it.
    * Logs go to stdout. `logging` defaults to stderr, which is why Railway was
      painting the entire operating log red.
"""

import os
import sys
import csv
import json
import time
import logging
import subprocess
# stdlib HTTP for Supabase mirroring. Using urllib (not `requests`) means the
# import can NEVER fail -- the old `try: import requests / except: requests=None`
# guard silently no-op'd the ENTIRE dashboard mirror whenever requests wasn't in
# requirements.txt, which is the most likely reason the tables were empty.
import urllib.request
import urllib.error
from datetime import datetime, timezone, time as dtime
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import config
import trend_signals as ts
import factors as fac
import merton
import merton_alloc  # concurrent portfolio allocator + constant-vol targeting
import news_overlay as no
import data_feed as feed
from backtest import _bucket
from risk_manager import RiskManager, NORMAL

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (MarketOrderRequest, TrailingStopOrderRequest,
                                      GetOrdersRequest)
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
from alpaca.data.historical import StockHistoricalDataClient

logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                     format="%(asctime)s | %(levelname)s | %(message)s",
                     datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("runner")

ET = ZoneInfo(config.MARKET_TZ)


def _tf_minutes(tf) -> int:
    """Parse a timeframe key to integer minutes, tolerant of format.

    The old one-liner did ``int(key.replace("min",""))`` which assumes the
    exact string "5min". If config uses "5m", "15m", "30m" (or an int, or
    "5 min"), that raised ValueError at MODULE IMPORT and crash-looped the
    whole process before run() ever executed. Strip to digits instead.
    """
    if isinstance(tf, (int, float)):
        return int(tf)
    digits = "".join(ch for ch in str(tf) if ch.isdigit())
    if not digits:
        raise ValueError(f"cannot parse timeframe minutes from {tf!r}")
    return int(digits)


BASE_BAR_MIN = _tf_minutes(list(config.TIMEFRAME_WEIGHTS)[0])
MAX_HOLD_SEC = config.MAX_HOLD_BARS * BASE_BAR_MIN * 60 if config.MAX_HOLD_BARS else 0
OPEN_POLL_SECONDS = 60
CLOSED_POLL_CAP = 900
FETCH_DAYS = 15  # trailing history per symbol for warmup

# ---------------------------------------------------------------------------
# PATCH: push live status / trades to Supabase so an external dashboard can
# read what this process is actually doing, without scraping Railway logs.
# Set SUPABASE_SERVICE_KEY in Railway's env vars (Project Settings -> API ->
# service_role key in Supabase). Never commit that key to the repo.
# ---------------------------------------------------------------------------
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://nrcwewfcvpbzribeahzn.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
_SUPABASE_WARNED = False


def _supabase_ready():
    """Log ONCE, loudly, if mirroring is off -- so a dead dashboard shows up in
    the logs instead of silently writing zero rows forever (the old bug)."""
    global _SUPABASE_WARNED
    if SUPABASE_KEY:
        return True
    if not _SUPABASE_WARNED:
        log.warning("SUPABASE MIRROR OFF: SUPABASE_SERVICE_KEY is not set. "
                    "Dashboard tables (merton_status/merton_trades) will stay "
                    "EMPTY. Set the service_role key in Railway env vars.")
        _SUPABASE_WARNED = True
    return False


def _supabase_post(table, payload, upsert=False):
    """POST one row via stdlib urllib. CHECKS the HTTP status and logs non-2xx --
    a 401 (bad key), 404 (wrong table) or 400 (schema mismatch) is exactly how
    rows silently never appear even when the key IS set and the import works.
    Never raises: dashboard mirroring must not crash the trader."""
    if not _supabase_ready():
        return
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    if upsert:
        headers["Prefer"] = "resolution=merge-duplicates"
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/{table}",
        data=json.dumps(payload, default=str).encode(),
        headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            if not (200 <= resp.status < 300):
                log.warning("supabase %s -> HTTP %s", table, resp.status)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="ignore")[:200]
        log.warning("supabase %s -> HTTP %s: %s", table, e.code, body)
    except Exception as e:
        log.warning("supabase %s push failed: %s", table, e)


def push_status(**row):
    """Upsert the single live-status row so the dashboard can read current state."""
    _supabase_post("merton_status", {"id": "live", **row}, upsert=True)


def push_trade(**row):
    """Insert one trade/order event, same schema as log_trade's CSV."""
    _supabase_post("merton_trades", row)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _key(*names):
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return None


def _parse_et(hhmm: str) -> dtime:
    h, m = hhmm.split(":")
    return dtime(int(h), int(m))


def session_date_of(clock):
    """The ET trading date this clock instant belongs to.

    Derived from the broker clock, never from the container's local time nor a
    value cached at __init__. Half days (July 3, day before Thanksgiving,
    Christmas Eve) close at 13:00 ET and are handled for free, because nothing
    here hardcodes 16:00.
    """
    now_et = clock.timestamp.astimezone(ET)
    if clock.is_open:
        return now_et.date()
    return clock.next_open.astimezone(ET).date()


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
        return len(open_orders)
    except Exception as e:
        log.warning("  %s cancel-orders failed: %s", symbol, e)
        return 0


def close_position_safely(tc, symbol) -> bool:
    """Cancel resting orders, WAIT for the broker to release the held quantity,
    then close.

    Alpaca's cancel is asynchronous. The old code cancelled and immediately
    called close_position(), which is how PLTR produced:

        insufficient qty available for order
        (requested: 53, available: 0, held_for_orders: 53)

    The exception aborted that symbol's whole iteration -- including the
    positions.pop() -- and the close only landed on the next 60s cycle. It
    self-healed by luck. Poll instead.
    """
    cancel_symbol_orders(tc, symbol)
    for attempt in range(config.CLOSE_RETRY_ATTEMPTS):
        try:
            p = tc.get_open_position(symbol)
        except Exception:
            return True  # already flat
        try:
            qty = abs(int(float(p.qty)))
            available = abs(int(float(getattr(p, "qty_available", p.qty))))
        except (TypeError, ValueError):
            qty = available = 0
        if available >= qty > 0:
            try:
                tc.close_position(symbol)
                return True
            except Exception as e:
                log.warning("  %s close attempt %d failed: %s",
                            symbol, attempt + 1, e)
        time.sleep(config.CLOSE_RETRY_SLEEP_SEC)
    log.error("  %s CLOSE FAILED after %d attempts -- position still open",
              symbol, config.CLOSE_RETRY_ATTEMPTS)
    return False


def place_trailing_stop(tc, symbol, qty, is_long):
    """Place a GTC trailing stop protecting a position (opposite side)."""
    stop_side = OrderSide.SELL if is_long else OrderSide.BUY
    try:
        tc.submit_order(TrailingStopOrderRequest(
            symbol=symbol, qty=abs(int(qty)), side=stop_side,
            time_in_force=TimeInForce.GTC, trail_percent=config.TRAIL_PERCENT))
    except Exception as e:
        log.warning("  %s trailing-stop failed: %s", symbol, e)


def place_entry_with_stop(tc, rm, symbol, approved, price, sig):
    """Submit the entry market order, then a GTC TRAILING stop on the opposite
    side. GTC so protection PERSISTS overnight (a DAY stop expires at 4pm,
    leaving multi-day holds naked)."""
    side = OrderSide.BUY if approved > 0 else OrderSide.SELL
    tc.submit_order(MarketOrderRequest(symbol=symbol, qty=abs(approved),
                                        side=side, time_in_force=TimeInForce.DAY))
    place_trailing_stop(tc, symbol, abs(approved), approved > 0)
    return side


def open_order_symbols(tc):
    """Symbols with a currently open order (e.g. a live protective stop)."""
    try:
        return {o.symbol for o in tc.get_orders(
            filter=GetOrdersRequest(status=QueryOrderStatus.OPEN))}
    except Exception as e:
        log.warning("open-orders check failed: %s", e)
        return set()


# ---------------------------------------------------------------------------
# live trade log -- the input the diagnostics module needs and never had
# ---------------------------------------------------------------------------
_TRADE_FIELDS = ["ts_utc", "session_date", "symbol", "action", "direction",
                  "shares", "price", "score", "bucket", "mu_lcb", "bucket_t",
                  "fraction", "symbol_vol", "conf_mult", "gate"]


def log_trade(**row):
    """Append one order event. Schema mirrors the backtest trade record so the
    Welch/Levene/KS comparison is apples-to-apples."""
    path = config.LIVE_TRADES_PATH
    exists = os.path.exists(path)
    try:
        with open(path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=_TRADE_FIELDS, extrasaction="ignore")
            if not exists:
                w.writeheader()
            w.writerow(row)
    except Exception as e:
        log.warning("trade-log write failed: %s", e)
    push_trade(**row)  # PATCH: mirror every trade event into Supabase


# ---------------------------------------------------------------------------
# stats / universe lifecycle
# ---------------------------------------------------------------------------
def load_stats_and_universe(dc):
    """Load bucket stats + the universe + the tradeable (worthy) subset."""
    try:
        with open(config.SIGNAL_STATS_PATH) as f:
            payload = json.load(f)
        stats = payload.get("buckets", {})
        universe = payload.get("universe") or feed.select_universe(
            dc, config.CANDIDATE_POOL, config.UNIVERSE_SIZE) or config.UNIVERSE

        # `[] or universe` -> universe. An EMPTY tradeable list means "no symbol
        # cleared MIN_EDGE_RATIO", and the old `or` read that as "field missing,
        # trade everything". The screen inverted exactly when it should have been
        # most restrictive. Distinguish absent (None) from empty ([]).
        t = payload.get("tradeable")
        if t is None:
            tradeable = set(universe)
            log.warning("signal_stats.json has no `tradeable` key -- treating all "
                        "%d universe symbols as eligible", len(universe))
        else:
            tradeable = set(t)
            if not tradeable:
                log.warning("tradeable is EMPTY: no symbol cleared MIN_EDGE_RATIO=%s. "
                            "No entries will be taken. Held positions still exit.",
                            config.MIN_EDGE_RATIO)
        return stats, universe, tradeable, payload.get("generated_at")
    except FileNotFoundError:
        universe = feed.select_universe(
            dc, config.CANDIDATE_POOL, config.UNIVERSE_SIZE) or config.UNIVERSE
        return None, universe, set(universe), None


def stats_are_stale(generated_at, session_date) -> bool:
    if not generated_at:
        return True
    try:
        gen = datetime.fromisoformat(generated_at).astimezone(ET).date()
    except Exception:
        return True
    return gen < session_date


def maybe_refresh_stats(clock, session_date, generated_at) -> bool:
    """Re-run the backtest once per session, in the pre-open window.

    Deliberately NOT run intraday: mid-session mu/sigma changes would mean the
    sizer's inputs differ across bars within one session, which destroys the
    distributional comparison the weekly diagnostics rely on.
    """
    if clock.is_open or not stats_are_stale(generated_at, session_date):
        return False
    now_et = clock.timestamp.astimezone(ET).time()
    if not (_parse_et(config.STATS_REFRESH_START_ET) <= now_et
            <= _parse_et(config.STATS_REFRESH_END_ET)):
        return False

    log.info("STATS | refreshing signal_stats.json for session %s (--days %d)",
             session_date, config.BACKTEST_DAYS)
    try:
        r = subprocess.run(
            [sys.executable, "run_backtest.py", "--days", str(config.BACKTEST_DAYS)],
            capture_output=True, text=True, timeout=1800)
        for line in (r.stdout or "").splitlines():
            log.info("  bt| %s", line)
        if r.returncode != 0:
            log.error("STATS | refresh FAILED rc=%d: %s", r.returncode,
                       (r.stderr or "")[-2000:])
            return False
    except Exception as e:
        log.error("STATS | refresh raised: %s", e)
        return False
    return True


def current_positions(tc):
    pos = {}
    for p in tc.get_all_positions():
        pos[p.symbol] = {"shares": int(float(p.qty)), "price": float(p.avg_entry_price)}
    return pos


def reconcile(tc, positions, session_date):
    """Broker is the source of truth. Log inherited state loudly rather than
    silently adopting someone else's book."""
    try:
        acct = tc.get_account()
    except Exception as e:
        log.error("RECONCILE | account fetch failed: %s", e)
        return None
    if getattr(acct, "trading_blocked", False):
        log.error("RECONCILE | trading_blocked=True -- broker has halted this account")
    log.info("RECONCILE | session=%s equity=$%s inherited_positions=%d "
             "daytrade_count=%s pdt=%s shorting=%s",
             session_date, f"{float(acct.equity):,.2f}", len(positions),
             getattr(acct, "daytrade_count", "?"),
             getattr(acct, "pattern_day_trader", "?"),
             getattr(acct, "shorting_enabled", "?"))
    for sym, p in positions.items():
        log.info("  inherited %-5s %+d @ %.2f", sym, p["shares"], p["price"])
    return acct


def bars_are_fresh(frames, universe, now_utc):
    """Age in seconds of the newest bar across the universe.

    A dead websocket or a stalled fetch looks EXACTLY like "no signal fired" if
    you only watch the order flow. It is not the same thing. Distinguish them.
    """
    newest = None
    for sym in universe:
        df = frames.get(sym)
        if df is None or len(df) == 0:
            continue
        ts_ = df.index[-1]
        ts_ = ts_.tz_localize(timezone.utc) if ts_.tzinfo is None else ts_
        newest = ts_ if newest is None else max(newest, ts_)
    if newest is None:
        return False, float("inf")
    age = (now_utc - newest.to_pydatetime()).total_seconds()
    return age <= config.MAX_BAR_STALENESS_SEC, age


# ---------------------------------------------------------------------------
def run():
    api_key = _key("ALPACA_API_KEY", "APCA_API_KEY_ID")
    secret = _key("ALPACA_SECRET_KEY", "APCA_API_SECRET_KEY")
    if not api_key or not secret:
        log.error("Credentials not found."); sys.exit(1)

    try:
        tc = TradingClient(api_key, secret, paper=True)
        dc = StockHistoricalDataClient(api_key, secret)
        clock = tc.get_clock()
        acct = tc.get_account()
    except Exception as e:
        log.error("Auth failed (paper keys?): %s", e); sys.exit(1)

    session_date = session_date_of(clock)
    stats, universe, tradeable, generated_at = load_stats_and_universe(dc)
    mode = "TRADE" if stats else "OBSERVE"
    equity = float(acct.equity)
    rm = RiskManager(equity, session_date=session_date)
    breaker_cooldown = 0
    # in-memory only. With FLATTEN_EOD the book never survives a session, so a
    # process restart cannot orphan an entry time for long. Inherited positions
    # get their clock started at first sighting, which is conservative: it can
    # only delay a max-hold exit, never trigger one early.
    entry_times = {}

    log.info("=== live_paper starting | mode=%s | equity $%s | universe=%d "
             "| tradeable=%d | session=%s | stats_generated=%s ===",
             mode, f"{equity:,.2f}", len(universe), len(tradeable),
             session_date, generated_at)
    if mode == "OBSERVE":
        log.info("No signal_stats.json -> OBSERVE mode: logging signals, NOT trading.")
    if config.PLUMBING_TEST:
        log.warning("=" * 78)
        log.warning("PLUMBING_TEST ENABLED. Merton sizing is BYPASSED. Every signal is")
        log.warning("sized at a fixed %.1f%% of equity regardless of mu, sigma, or n.",
                     config.PLUMBING_FRACTION * 100)
        log.warning("P&L from this mode carries NO information about edge. It exists to")
        log.warning("exercise the order path and populate %s.", config.LIVE_TRADES_PATH)
        log.warning("The `tradeable` screen is ALSO bypassed: every symbol in the")
        log.warning("universe can enter, not just the %d that cleared MIN_EDGE_RATIO.",
                     len(tradeable))
        log.warning("Risk caps still bind: PER_SYMBOL_CAP=%.0f%%, MAX_GROSS=%.0f%%.",
                     config.PER_SYMBOL_CAP * 100, config.MAX_GROSS_EXPOSURE * 100)
        log.warning("If this account is funded, KILL THE PROCESS NOW.")
        log.warning("=" * 78)
    reconcile(tc, current_positions(tc), session_date)

    # PATCH: push an initial status row immediately at startup, so the
    # dashboard shows something even before the first HEARTBEAT cycle.
    push_status(mode=mode, session_date=str(session_date), equity=equity,
                positions_count=0, universe_size=len(universe),
                tradeable_size=len(tradeable), breaker=False,
                halt_latched=False, bar_age_sec=0,
                stats_generated_at=generated_at)

    while True:
        try:
            now = datetime.now(timezone.utc)  # defined EVERY iteration
            clock = tc.get_clock()
            today = session_date_of(clock)

            # ---------- session rollover -----------------------------------
            if today != session_date:
                log.info("SESSION ROLLOVER | %s -> %s", session_date, today)
                session_date = today
                acct = tc.get_account()
                equity = float(acct.equity)
                rm.new_session(equity, session_date)  # start_equity + halt latch
                breaker_cooldown = 0
                entry_times.clear()
                reconcile(tc, current_positions(tc), session_date)

            # ---------- market closed --------------------------------------
            if not clock.is_open:
                if maybe_refresh_stats(clock, session_date, generated_at):
                    stats, universe, tradeable, generated_at = \
                        load_stats_and_universe(dc)
                    mode = "TRADE" if stats else "OBSERVE"
                    log.info("STATS | reloaded | mode=%s universe=%d tradeable=%d "
                             "generated=%s", mode, len(universe), len(tradeable),
                             generated_at)
                nap = max(30, min((clock.next_open - now).total_seconds(),
                                   CLOSED_POLL_CAP))
                log.info("Market CLOSED. Next open %s. Idling %.0fs.",
                         clock.next_open, nap)
                time.sleep(nap); continue

            if stats and stats_are_stale(generated_at, session_date):
                log.warning("STATS | signal_stats.json generated %s, session is %s "
                            "-- sizing on stale mu/sigma", generated_at, session_date)

            acct = tc.get_account()
            equity = float(acct.equity)
            positions = current_positions(tc)

            # ---------- optional EOD flatten -------------------------------
            if config.FLATTEN_EOD:
                secs_to_close = (clock.next_close - now).total_seconds()
                if 0 < secs_to_close <= config.FLATTEN_BUFFER_MIN * 60:
                    for s in list(positions.keys()):
                        close_position_safely(tc, s)
                    entry_times.clear()
                    log.info("EOD FLATTEN | closed %d positions | %.0f min to close",
                             len(positions), secs_to_close / 60)
                    time.sleep(OPEN_POLL_SECONDS); continue

            # ---------- data ------------------------------------------------
            frames = feed.fetch_bars_batch(dc, universe + [config.MARKET_PROXY],
                                            FETCH_DAYS)
            fresh, age = bars_are_fresh(frames, universe, now)
            if not fresh:
                log.error("DATA STALE | newest bar is %.0fs old (cap %ds) -- "
                          "suppressing entries this cycle", age,
                          config.MAX_BAR_STALENESS_SEC)

            protected = open_order_symbols(tc)

            # ---------- volatility circuit breaker (with hysteresis) --------
            breaker_now = False
            try:
                mkt = frames.get(config.MARKET_PROXY)
                if mkt is not None and len(mkt) > 20:
                    ret = mkt["close"].pct_change()
                    recent_move = abs(ret.iloc[-1])
                    atr_ret = ret.abs().rolling(14).mean().iloc[-1]
                    breaker_now = no.volatility_circuit_breaker(recent_move, atr_ret)
            except Exception as e:
                log.warning("circuit-breaker check failed: %s", e)

            # A breaker that clears on the next 60s heartbeat is a status flag,
            # not a circuit breaker. Hold it hot for a cooldown window.
            if breaker_now:
                breaker_cooldown = config.BREAKER_COOLDOWN_CYCLES
            elif breaker_cooldown > 0:
                breaker_cooldown -= 1
            breaker = breaker_now or breaker_cooldown > 0

            log.info("HEARTBEAT | %s | equity $%s | positions=%d | breaker=%s%s "
                     "| halt=%s | bar_age=%.0fs",
                     mode, f"{equity:,.2f}", len(positions), breaker,
                     f" (cooldown {breaker_cooldown})" if breaker_cooldown else "",
                     rm.halt_latched, age)

            # PATCH: mirror the heartbeat into Supabase so the dashboard's
            # "Merton Trader — Live Status" panel reflects reality in near
            # real time (this loop ticks roughly every OPEN_POLL_SECONDS).
            push_status(mode=mode, session_date=str(session_date), equity=equity,
                        positions_count=len(positions), universe_size=len(universe),
                        tradeable_size=len(tradeable), breaker=bool(breaker),
                        halt_latched=bool(rm.halt_latched), bar_age_sec=age,
                        stats_generated_at=generated_at)

            action, reason = (no.news_risk_action({}, True) if breaker
                               else (NORMAL, "clear"))

            # ═══════════════════════════════════════════════════════════════
            # PASS 1 — manage open positions (exits/flips/holds run IMMEDIATELY,
            # unchanged) and COLLECT entry candidates for joint sizing.
            # Risk-reducing actions must not wait on the allocator, so they stay
            # per-symbol and eager here. Only ENTRIES are deferred to Pass 2.
            # ═══════════════════════════════════════════════════════════════
            entry_candidates = []
            symbol_vols = {}
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

                    # --- manage existing positions (exits ALWAYS run) --------
                    if plan in ("EXIT", "FLIP"):
                        if not close_position_safely(tc, sym):
                            continue  # still holding; retry next cycle
                        log.info("  [CLOSE] %-5s %s (held %+d) score=%+.2f",
                                 sym, plan, held, sig["score"])
                        log_trade(ts_utc=now.isoformat(), session_date=str(session_date),
                                   symbol=sym, action=plan, direction=held_dir,
                                   shares=-held, price=price, score=sig["score"],
                                   bucket=_bucket(sig["score"]), gate="exit")
                        positions.pop(sym, None)
                        entry_times.pop(sym, None)
                        if plan == "EXIT":
                            continue
                        held_dir = 0  # FLIP falls through to enter

                    if plan == "HOLD":
                        # hard holding-time cap: exit regardless of signal
                        if MAX_HOLD_SEC and held != 0:
                            t0 = entry_times.get(sym)
                            if t0 is None:
                                entry_times[sym] = now  # inherited; start clock
                            elif (now - t0).total_seconds() >= MAX_HOLD_SEC:
                                if close_position_safely(tc, sym):
                                    log.info("  [MAXHOLD] %-5s closed after %.0f min "
                                             "(cap %d bars) score=%+.2f", sym,
                                             (now - t0).total_seconds() / 60,
                                             config.MAX_HOLD_BARS, sig["score"])
                                    log_trade(ts_utc=now.isoformat(),
                                               session_date=str(session_date), symbol=sym,
                                               action="MAXHOLD", direction=held_dir,
                                               shares=-held, price=price,
                                               score=sig["score"],
                                               bucket=_bucket(sig["score"]), gate="max_hold")
                                    positions.pop(sym, None)
                                    entry_times.pop(sym, None)
                                continue
                        if held != 0 and sym not in protected:
                            place_trailing_stop(tc, sym, abs(held), held > 0)
                            log.info("  [PROTECT] %-5s re-armed trailing stop (held %+d)",
                                     sym, held)
                        continue

                    # --- collect entry candidate (sizing deferred to Pass 2) --
                    if sig["direction"] == 0:
                        continue
                    if sym not in tradeable:       # worthiness screen (PLUMBING removed)
                        continue
                    if not fresh:
                        continue                    # no entries on stale data
                    if mode == "OBSERVE":
                        continue                    # no stats to size on -> log-only mode

                    conf = fac.confirmation(df, sig["direction"])
                    bucket = _bucket(sig["score"])
                    bstats = (stats or {}).get(bucket, {"mu": 0, "sigma": 0, "n": 0})
                    bar_ret = df["close"].pct_change().dropna()
                    symbol_vol = float(bar_ret.tail(100).std() * (config.HOLD_BARS ** 0.5))
                    symbol_vols[sym] = symbol_vol
                    entry_candidates.append({
                        "symbol": sym, "direction": sig["direction"], "price": price,
                        "mu": float(bstats.get("mu", 0.0)),
                        "sigma": float(bstats.get("sigma", 0.0)),
                        "n": int(bstats.get("n", 0)), "symbol_vol": symbol_vol,
                        "confirmation": conf["multiplier"],
                        "score": sig["score"], "bucket": bucket, "sig": sig})
                except Exception as e:
                    log.warning("  %s pass1 error: %s", sym, e)

            # ═══════════════════════════════════════════════════════════════
            # ALLOCATE — concurrent Merton across the whole candidate book,
            # then constant-volatility scaling to VOL_TARGET_ANNUAL (capped at
            # the leverage ceiling MAX_GROSS_EXPOSURE). Gates are intact: a name
            # gets weight ONLY if its bucket clears MIN_BUCKET_T / MIN_BUCKET_N /
            # mu_lcb>0. Zero-edge book -> zero exposure, by design.
            # ═══════════════════════════════════════════════════════════════
            placements = []
            if entry_candidates and not breaker and action == NORMAL:
                alloc = merton_alloc.allocate_book(
                    entry_candidates, equity,
                    gross_target=config.MAX_GROSS_EXPOSURE,
                    gamma=config.GAMMA, fractional=config.FRACTIONAL,
                    z=config.LCB_Z, max_fraction=config.PER_SYMBOL_CAP,
                    concentration=getattr(config, "CONCENTRATION", 1.0))
                k, sig0, sig1, gross = merton_alloc.vol_target_scale(
                    alloc, symbol_vols,
                    vol_target_annual=config.VOL_TARGET_ANNUAL,
                    leverage_cap=config.MAX_GROSS_EXPOSURE,
                    rho=config.BOOK_CORRELATION)
                gated = sum(1 for a in alloc if a["fraction"] > 0)
                log.info("ALLOC | cands=%d gated=%d book_sigma=%.2f%%(horizon) "
                         "k=%.2f target=%.0f%% gross=%.1f%%",
                         len(entry_candidates), gated, sig0 * 100, k,
                         config.VOL_TARGET_ANNUAL * 100, gross * 100)
                if gated == 0:
                    log.info("  [obs] no bucket cleared MIN_BUCKET_T=%.1f -- 0 entries "
                             "(this is correct on zero-edge signal, NOT a bug)",
                             config.MIN_BUCKET_T)
                cand_by_sym = {c["symbol"]: c for c in entry_candidates}
                for a in alloc:
                    if a["fraction"] <= 0:
                        continue
                    c = cand_by_sym[a["symbol"]]
                    frac = a["fraction"] * k                       # vol-targeted weight
                    shares = int((frac * equity) // c["price"]) * a["direction"]
                    if shares != 0:
                        placements.append((a["symbol"], shares, frac, c, a))

            # ═══════════════════════════════════════════════════════════════
            # PASS 2 — place the jointly-sized entries. Per-symbol/gross caps in
            # the RiskManager still bind as a final backstop.
            # ═══════════════════════════════════════════════════════════════
            for sym, shares, frac, c, a in placements:
                try:
                    approved, gate = rm.gate_entry(sym, shares, c["price"],
                                                    positions, equity, action)
                    if approved == 0:
                        log.info("  [gate] %-5s blocked: %s (news=%s)", sym, gate, reason)
                        continue
                    side = place_entry_with_stop(tc, rm, sym, approved, c["price"], c["sig"])
                    positions[sym] = {"shares": approved, "price": c["price"]}
                    entry_times[sym] = now
                    log.info("  [ORDER] %-5s %s %d @ ~%.2f frac=%.3f vol=%.3f "
                             "score=%+.2f t=%+.2f",
                             sym, side.value, abs(approved), c["price"], frac,
                             c["symbol_vol"], c["score"], a["bucket_t"])
                    log_trade(ts_utc=now.isoformat(), session_date=str(session_date),
                               symbol=sym, action="ENTER", direction=a["direction"],
                               shares=approved, price=c["price"], score=c["score"],
                               bucket=c["bucket"], mu_lcb=a["mu_lcb"],
                               bucket_t=a["bucket_t"], fraction=frac,
                               symbol_vol=c["symbol_vol"], conf_mult=c["confirmation"],
                               gate=gate)
                except Exception as e:
                    log.warning("  %s place error: %s", sym, e)

            time.sleep(OPEN_POLL_SECONDS)

        except KeyboardInterrupt:
            log.info("Interrupted. Shutting down."); break
        except Exception as e:
            log.error("Loop error (continuing): %s", e)
            time.sleep(30)


if __name__ == "__main__":
    run()
