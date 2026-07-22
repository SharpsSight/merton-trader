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
import fundamentals as fund
import merton_alloc as alloc
import merton
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
BASE_BAR_MIN = int(list(config.TIMEFRAME_WEIGHTS)[0].replace("min", ""))
# Wall-clock, not a bar count. With FLATTEN_EOD off a position now survives
# session boundaries, and a counter that only advances during RTH silently
# stretches across weekends and holidays.
MAX_HOLD_SEC = (config.MAX_HOLD_CALENDAR_DAYS * 86400
                if getattr(config, "MAX_HOLD_CALENDAR_DAYS", 0) else 0)
OPEN_POLL_SECONDS = 60
CLOSED_POLL_CAP = 900
FETCH_DAYS = 15               # trailing history per symbol for warmup


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
            return True                       # already flat
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


def current_gross(positions: dict) -> float:
    """Sum of |position value| across the book, in dollars."""
    return sum(abs(p["shares"] * p["price"]) for p in positions.values())


def load_entry_times() -> dict:
    """Entry timestamps survive restarts AND session rollovers.

    With multi-day holds an in-memory dict is wrong twice over: a redeploy
    resets every position's max-hold clock to zero, and so does every nightly
    rollover. Both silently let a position run past its cap.
    """
    try:
        with open(config.ENTRY_TIMES_PATH) as f:
            raw = json.load(f)
        return {k: datetime.fromisoformat(v) for k, v in raw.items()}
    except (OSError, ValueError, TypeError):
        return {}


def save_entry_times(entry_times: dict) -> None:
    try:
        with open(config.ENTRY_TIMES_PATH, "w") as f:
            json.dump({k: v.isoformat() for k, v in entry_times.items()}, f)
    except OSError as e:
        log.warning("entry-times write failed: %s", e)


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

    # Retry rather than sys.exit. On 2026-07-18 at 04:16 UTC the container lost
    # DNS ("Temporary failure in name resolution" on paper-api.alpaca.markets)
    # and never logged again. A transient network fault at boot must not be a
    # terminal condition -- a process that exits here is a process that only
    # comes back if the platform restarts it.
    tc = dc = clock = acct = None
    attempt = 0
    while True:
        try:
            tc = TradingClient(api_key, secret, paper=True)
            dc = StockHistoricalDataClient(api_key, secret)
            clock = tc.get_clock()
            acct = tc.get_account()
            break
        except Exception as e:
            attempt += 1
            wait = min(300, 15 * attempt)
            log.error("Startup connect failed (attempt %d): %s -- retrying in %ds",
                      attempt, e, wait)
            time.sleep(wait)

    session_date = session_date_of(clock)
    stats, universe, tradeable, generated_at = load_stats_and_universe(dc)
    mode = "TRADE" if stats else "OBSERVE"
    equity = float(acct.equity)
    rm = RiskManager(equity, session_date=session_date)
    breaker_cooldown = 0
    halt_announced = False
    # Persisted: positions now live for days, so this must survive both
    # restarts and rollovers. Inherited positions with no record get their clock
    # started at first sighting, which is conservative -- it can only delay a
    # max-hold exit, never trigger one early.
    entry_times = load_entry_times()
    fundamentals = fund.load()

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

    while True:
        try:
            now = datetime.now(timezone.utc)          # defined EVERY iteration
            clock = tc.get_clock()
            today = session_date_of(clock)

            # ---------- session rollover -----------------------------------
            if today != session_date:
                log.info("SESSION ROLLOVER | %s -> %s", session_date, today)
                session_date = today
                acct = tc.get_account()
                equity = float(acct.equity)
                rm.new_session(equity, session_date)    # start_equity + halt latch
                breaker_cooldown = 0
                halt_announced = False
                # entry_times deliberately NOT cleared: a position that survives
                # the rollover must keep its original max-hold clock.
                reconcile(tc, current_positions(tc), session_date)

            # ---------- market closed --------------------------------------
            if not clock.is_open:
                # Weekly, while the market is shut: ~50-100 sequential SEC calls
                # is minutes of wall time and must never run inside a trading
                # cycle. Quarterly data at a weekly refresh has no staleness cost.
                if config.USE_FUNDAMENTAL_GATE and fund.is_stale(fundamentals):
                    try:
                        px = {}
                        try:
                            fr = feed.fetch_bars_batch(dc, universe, 5)
                            px = {k: float(v["close"].iloc[-1])
                                  for k, v in fr.items()
                                  if v is not None and len(v)}
                        except Exception as e:
                            log.warning("FUNDAMENTALS | price fetch failed: %s", e)
                        pool = sorted(set(universe) | set(config.CANDIDATE_POOL))
                        fundamentals = fund.refresh(pool, px, log=log)
                    except Exception as e:
                        log.error("FUNDAMENTALS | refresh failed: %s "
                                  "(keeping previous cache)", e)

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
                    save_entry_times(entry_times)
                    log.info("EOD FLATTEN | closed %d positions | %.0f min to close",
                             len(positions), secs_to_close / 60)
                    time.sleep(OPEN_POLL_SECONDS); continue

            # ---------- halt short-circuit ----------------------------------
            # The halt latches for the session, so once it trips there is no
            # point computing entry signals at all. On 2026-07-17 the runner
            # logged ~25 "blocked: daily_loss_halt" lines every 70 seconds for
            # five hours -- full indicator work on every symbol, discarded at the
            # final gate. Exits and stops still run below; only entries stop.
            entries_halted = rm.daily_halt(equity)
            if entries_halted and not halt_announced:
                log.warning("HALT | daily loss halt latched for %s -- entries "
                            "suppressed for the session. Exits still active.",
                            session_date)
                halt_announced = True

            # ---------- data ------------------------------------------------
            frames = feed.fetch_bars_batch(dc, universe + [config.MARKET_PROXY],
                                           FETCH_DAYS)
            fresh, age = bars_are_fresh(frames, universe, now)
            if not fresh:
                log.error("DATA STALE | newest bar is %.0fs old (cap %ds) -- "
                          "suppressing entries this cycle", age,
                          config.MAX_BAR_STALENESS_SEC)

            protected = open_order_symbols(tc)
            candidates = []          # PASS 1 fills this; PASS 2 allocates it

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

                    # --- manage existing positions (exits ALWAYS run) --------
                    if plan in ("EXIT", "FLIP"):
                        if not close_position_safely(tc, sym):
                            continue                    # still holding; retry next cycle
                        log.info("  [CLOSE] %-5s %s (held %+d) score=%+.2f",
                                 sym, plan, held, sig["score"])
                        log_trade(ts_utc=now.isoformat(), session_date=str(session_date),
                                  symbol=sym, action=plan, direction=held_dir,
                                  shares=-held, price=price, score=sig["score"],
                                  bucket=_bucket(sig["score"]), gate="exit")
                        positions.pop(sym, None)
                        entry_times.pop(sym, None)
                        save_entry_times(entry_times)
                        if plan == "EXIT":
                            continue
                        held_dir = 0                    # FLIP falls through to enter

                    if plan == "HOLD":
                        # hard holding-time cap: exit regardless of signal
                        if MAX_HOLD_SEC and held != 0:
                            t0 = entry_times.get(sym)
                            if t0 is None:
                                entry_times[sym] = now      # inherited; start clock
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
                                    save_entry_times(entry_times)
                                continue
                        if held != 0 and sym not in protected:
                            place_trailing_stop(tc, sym, abs(held), held > 0)
                            log.info("  [PROTECT] %-5s re-armed trailing stop (held %+d)",
                                     sym, held)
                        continue

                    # --- entries: PASS 1, collect candidates -----------------
                    # Do NOT place the order here. Sizing every symbol against
                    # the gross cap in universe order is first-come-first-served:
                    # names early in the loop take 3-12% of equity each, the cap
                    # is exhausted after ~10-15 fills, and every symbol evaluated
                    # afterwards gets int(room_gross // price) = 1 share, then 0.
                    # Alphabetical position silently decides the book, and the
                    # tail of it is one-share noise paying full round-trip cost.
                    # Collect first, allocate jointly below.
                    if sig["direction"] == 0:
                        continue
                    if entries_halted:
                        continue        # latched for the session; see above
                    if sym not in tradeable and not config.PLUMBING_TEST:
                        continue
                    if not fresh:
                        continue                        # no entries on stale data

                    # --- FUNDAMENTAL CONJUNCTION -----------------------------
                    # Hard gate, applied BEFORE sizing. The trend layer has
                    # already chosen a direction; this asks whether an
                    # independent, non-price measurement agrees with it. Note
                    # this also runs under PLUMBING_TEST: plumbing mode bypasses
                    # the Merton EDGE gate, but it must not bypass the universe
                    # composition rule, or ETFs walk straight back in.
                    fscore = fund.score_for(fundamentals, sym)
                    fgate = fac.fundamental_confirmation(fscore, sig["direction"])
                    if not fgate["allow"]:
                        log.debug("  [fund] %-5s blocked: %s (score=%s)",
                                  sym, fgate["reason"],
                                  "None" if fscore is None else f"{fscore:+.3f}")
                        continue

                    conf = fac.confirmation(df, sig["direction"])
                    bucket = _bucket(sig["score"])
                    bstats = (stats or {}).get(bucket, {"mu": 0, "sigma": 0, "n": 0})

                    bar_ret = df["close"].pct_change().dropna()
                    symbol_vol = float(bar_ret.tail(100).std() * (config.HOLD_BARS ** 0.5))

                    # Both confirmation layers multiply into size. Each can only
                    # SHRINK the position (both bounded <= 1.0) -- factors reduce
                    # risk, they never manufacture conviction.
                    combined_mult = conf["multiplier"] * fgate["multiplier"]

                    candidates.append({
                        "symbol": sym, "direction": sig["direction"],
                        "price": price, "score": sig["score"], "bucket": bucket,
                        "mu": float(bstats.get("mu", 0.0)),
                        "sigma": float(bstats.get("sigma", 0.0)),
                        "n": int(bstats.get("n", 0)),
                        "symbol_vol": symbol_vol,
                        "confirmation": combined_mult,
                        "conf_mult": conf["multiplier"],
                        "fund_score": fscore,
                        "sig": sig,
                    })

                except Exception as e:
                    log.warning("  %s error: %s", sym, e)

            # ---------- entries: PASS 2, joint allocation -------------------
            # merton_alloc.allocate_book sizes every qualifying candidate in ONE
            # decision instead of first-come-first-served. It applies the exact
            # same gates as the per-symbol sizer (MIN_BUCKET_N, MIN_BUCKET_T,
            # mu_lcb > 0) -- what changes is only how the risk budget is
            # DISTRIBUTED among names that already qualify, and that the gross
            # cap scales everyone proportionally rather than starving whoever
            # happens to be last in the universe list.
            #
            # This module has been in the repo unused since it was written;
            # nothing imported it, so the pathology it was built to fix was live
            # the whole time.
            if candidates and mode == "TRADE" and not entries_halted:
                try:
                    allocs = alloc.allocate_book(
                        candidates, equity,
                        gross_target=max(0.0, config.MAX_GROSS_EXPOSURE
                                         - current_gross(positions) / max(equity, 1.0)),
                        enforce_gates=not config.PLUMBING_TEST)
                except Exception as e:
                    log.error("ALLOC | allocate_book failed: %s", e)
                    allocs = []

                by_sym = {c["symbol"]: c for c in candidates}

                if config.PLUMBING_TEST:
                    # Plumbing mode bypasses the Merton EDGE gate (mu_lcb is not
                    # positive, so allocate_book returns zeros). Size on the
                    # signal's own |score| instead, then let the SAME joint
                    # normaliser divide the remaining gross budget. This is
                    # confidence-proportional, NOT edge-proportional: loud is not
                    # profitable, and P&L from this mode carries no information
                    # about whether the strategy works.
                    lo = config.ENTRY_THRESHOLD
                    raw = {}
                    for c in candidates:
                        conviction = min(1.0, max(0.0,
                            (abs(c["score"]) - lo) / max(1e-9, 1.0 - lo)))
                        f = (config.PLUMBING_FRACTION_MIN + conviction *
                             (config.PLUMBING_FRACTION - config.PLUMBING_FRACTION_MIN))
                        raw[c["symbol"]] = f * max(0.25, min(1.0, c["confirmation"]))
                    room = max(0.0, config.MAX_GROSS_EXPOSURE
                               - current_gross(positions) / max(equity, 1.0))
                    total = sum(raw.values())
                    k = (room / total) if (total > room and total > 0) else 1.0
                    allocs = []
                    for c in candidates:
                        w = min(raw[c["symbol"]] * k, config.MAX_FRACTION)
                        sh = int((w * equity) // c["price"])
                        allocs.append({"symbol": c["symbol"], "direction": c["direction"],
                                       "fraction": w, "shares": sh * c["direction"],
                                       "bucket_t": 0.0, "mu_lcb": 0.0,
                                       "gate": "PLUMBING_TEST"})

                placed = 0
                for a in sorted(allocs, key=lambda r: -abs(r["fraction"])):
                    if a["shares"] == 0:
                        continue
                    c = by_sym.get(a["symbol"])
                    if c is None:
                        continue
                    sym, price = a["symbol"], c["price"]

                    approved, gate = rm.gate_entry(sym, a["shares"], price,
                                                   positions, equity, action)
                    if approved == 0:
                        log.info("  [gate] %-5s blocked: %s", sym, gate)
                        continue
                    # A cap that shaves an allocation to a token position is not
                    # a smaller version of the trade -- it is a full round trip
                    # of cost on an economically meaningless stake. Skip it.
                    if abs(approved) * price < config.MIN_ENTRY_NOTIONAL:
                        log.info("  [gate] %-5s skipped: notional $%.0f below "
                                 "MIN_ENTRY_NOTIONAL $%.0f (%s)", sym,
                                 abs(approved) * price,
                                 config.MIN_ENTRY_NOTIONAL, gate)
                        continue

                    try:
                        side = place_entry_with_stop(tc, rm, sym, approved,
                                                     price, c["sig"])
                    except Exception as e:
                        log.warning("  %s entry failed: %s", sym, e)
                        continue
                    positions[sym] = {"shares": approved, "price": price}
                    entry_times[sym] = now
                    placed += 1
                    log.info("  [ORDER] %-5s %s %d @ ~%.2f frac=%.3f vol=%.3f "
                             "score=%+.2f fund=%s t=%+.2f",
                             sym, side.value, abs(approved), price,
                             a["fraction"], c["symbol_vol"], c["score"],
                             "n/a" if c["fund_score"] is None
                             else f"{c['fund_score']:+.2f}", a["bucket_t"])
                    log_trade(ts_utc=now.isoformat(),
                              session_date=str(session_date), symbol=sym,
                              action="ENTER", direction=c["direction"],
                              shares=approved, price=price, score=c["score"],
                              bucket=c["bucket"], mu_lcb=a["mu_lcb"],
                              bucket_t=a["bucket_t"], fraction=a["fraction"],
                              symbol_vol=c["symbol_vol"],
                              conf_mult=c["conf_mult"], gate=a.get("gate", gate))
                if placed:
                    save_entry_times(entry_times)
                    log.info("ALLOC | %d candidates -> %d entries | gross now %.1f%%",
                             len(candidates), placed,
                             100.0 * current_gross(positions) / max(equity, 1.0))
            elif candidates:
                log.info("  [obs] %d candidates collected, no entries (mode=%s "
                         "halted=%s)", len(candidates), mode, entries_halted)

            time.sleep(OPEN_POLL_SECONDS)

        except KeyboardInterrupt:
            log.info("Interrupted. Shutting down."); break
        except Exception as e:
            log.error("Loop error (continuing): %s", e)
            time.sleep(30)


if __name__ == "__main__":
    run()
