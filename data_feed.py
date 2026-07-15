"""
data_feed.py — batched market data + dynamic universe selection.

Shared by run_backtest.py and live_paper.py so both use the SAME fetch path
(anti-drift). Batching is the key to scaling past a handful of symbols without
hitting Alpaca's 200-calls/min limit: one request returns bars for ALL symbols.

TWO CORRECTNESS FIXES (not tuning knobs):

  1. REGULAR TRADING HOURS. Alpaca's bars endpoint has no session filter and
     returns 04:00-20:00 ET. The backtest was therefore generating entries and
     exits on pre-market and after-hours bars -- on IEX, which carries ~2% of
     consolidated volume -- and charging them 5bps of slippage. The live runner
     is gated on `clock.is_open` and CAN NEVER TAKE THOSE TRADES. Every such
     backtest trade is a sample from a system that does not exist.

  2. SPLIT/DIVIDEND ADJUSTMENT. The `adjustment` parameter was unset, so the API
     default of `raw` applied. A split inside the lookback window prints as a
     -50% bar and enters the return distribution as a real trade.

Neither of these is a parameter you tune until the strategy passes. They are
mismatches between what the backtest measures and what the runner can execute.

REMAINING KNOWN BIAS (documented, not fixed here):
  IEX prints are a subset of consolidated prints, so IEX bar lows are >= true
  lows and highs <= true highs. The backtest's trailing stops therefore trigger
  LESS often than a real stop would. Fixing this needs the SIP feed (~$99/mo).

UNIVERSE SELECTION (this file):
  dynamic_universe() builds the candidate set from a RULE, not a hardcoded list:
  active tradable US equities on NYSE/NASDAQ, minus ETFs/leveraged products and
  odd share classes, then ranked POINT-IN-TIME by dollar volume. See the long
  note above dynamic_universe() for the two biases this design avoids and the
  one (IEX thin-tail volume) it cannot until SIP.
"""

from __future__ import annotations
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed, Adjustment

import config

BAR_5M = TimeFrame(5, TimeFrameUnit.Minute)
BAR_15M = TimeFrame(15, TimeFrameUnit.Minute)
BAR_DAY = TimeFrame.Day

_ET = ZoneInfo(config.MARKET_TZ)

_ADJ = {"raw": Adjustment.RAW, "split": Adjustment.SPLIT,
        "dividend": Adjustment.DIVIDEND, "all": Adjustment.ALL}

# --- universe-rule constants ----------------------------------------------
# Common stock lists on NYSE/NASDAQ. The bulk of ETFs and every leveraged/
# inverse product lists on ARCA or BATS, so an exchange whitelist is a cheap,
# maintenance-free ETF filter that Alpaca's asset object does NOT give directly
# (there is no reliable is_etf flag). This is a heuristic, not a guarantee --
# a handful of ETFs list on NASDAQ (e.g. QQQ), which is why we ALSO carry an
# explicit exclude set below.
_ALLOWED_EXCHANGES = {"NYSE", "NASDAQ"}

# Belt-and-suspenders: the highest-dollar-volume names on the whole tape are
# index/leveraged ETFs, so without this they would dominate a volume ranking
# and quietly replace your equity universe with products the signal was never
# built for. Extend as needed; this is not meant to be exhaustive of all ETFs,
# only of the ones liquid enough to crack a top-N dollar-volume screen.
_EXCLUDE_SYMBOLS = {
    "SPY", "QQQ", "IWM", "DIA", "VOO", "VTI", "IVV", "VEA", "VWO", "EFA",
    "EEM", "GLD", "SLV", "TLT", "HYG", "LQD", "XLF", "XLE", "XLK", "XLV",
    "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC", "SMH", "SOXX", "ARKK",
    "TQQQ", "SQQQ", "SOXL", "SOXS", "TNA", "TZA", "SPXL", "SPXS", "UVXY",
    "VXX", "SVXY", "UPRO", "SPXU", "LABU", "LABD", "TSLL", "NVDL", "USO",
    "BITO", "GDX", "GDXJ", "KWEB", "FXI", "VXUS", "BND", "AGG", "SCHD",
}

# Fallback so a failed/empty broker pull NEVER leaves the runner with an empty
# universe (which would silently disable all trading). These are stable, liquid,
# unambiguous common stocks; the dynamic pull replaces them whenever it succeeds.
_SEED_UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AVGO", "JPM", "V",
]


def filter_rth(df):
    """Keep only bars that OPEN inside the regular session.

    Alpaca bar timestamps mark the START of the bar, so the last 5-minute RTH
    bar opens at 15:55 and covers 15:55-16:00. Half days (13:00 ET close) leave
    a few extra bars in; harmless, since the runner never trades a closed market.
    """
    if df is None or len(df) == 0:
        return df
    idx = df.index
    if idx.tz is None:
        idx = idx.tz_localize(timezone.utc)
    et = idx.tz_convert(_ET)
    mask = (et.time >= config.RTH_START) & (et.time <= config.RTH_LAST_BAR)
    return df[mask]


def fetch_bars_batch(dc, symbols, days, timeframe=BAR_5M, end=None) -> dict:
    """
    One request for MANY symbols. Returns {symbol: DataFrame[o,h,l,c,v]}.
    Symbols with no data are simply absent from the result.

    `end` lets run_backtest pin the window to a fixed date so two runs are
    comparable. Without it the window slides with wall-clock time and you cannot
    tell a strategy change from a window change.
    """
    symbols = list(symbols)
    if not symbols:
        return {}
    end = end or datetime.now(timezone.utc)
    req = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=timeframe,
        start=end - timedelta(days=days),
        end=end,
        feed=DataFeed.IEX,
        adjustment=_ADJ[config.BAR_ADJUSTMENT],
    )
    df = dc.get_stock_bars(req).df
    out = {}
    if df is None or len(df) == 0:
        return out
    cols = ["open", "high", "low", "close", "volume"]
    # NOTE: TimeFrame has no __eq__ and TimeFrame.Day builds a fresh object on
    # every access, so `timeframe == BAR_DAY` and `timeframe in (BAR_DAY,)` are
    # both False for any caller that does not pass this exact module constant.
    # Getting this wrong silently RTH-filters daily bars (timestamped midnight
    # ET) down to nothing, and select_universe returns []. Check the unit.
    intraday = timeframe.unit in (TimeFrameUnit.Minute, TimeFrameUnit.Hour)

    if df.index.nlevels > 1:
        for sym in symbols:
            try:
                sub = df.xs(sym, level=0)
            except KeyError:
                continue
            if not len(sub):
                continue
            sub = sub[cols]
            if intraday and config.RTH_ONLY:
                sub = filter_rth(sub)
            if len(sub):
                out[sym] = sub
    else:                                   # single symbol -> flat index
        sub = df[cols]
        if intraday and config.RTH_ONLY:
            sub = filter_rth(sub)
        if len(sub):
            out[symbols[0]] = sub
    return out


# ---------------------------------------------------------------------------
def list_tradable_equities(tc) -> list:
    """Rule-based candidate set: active, tradable US common stock on NYSE/NASDAQ.

    Replaces a hardcoded CANDIDATE_POOL. Uses the TRADING client's asset list,
    not the data client. Alpaca's Asset has no reliable is_etf flag, so ETFs are
    excluded heuristically by exchange (ARCA/BATS listings are dropped) plus an
    explicit _EXCLUDE_SYMBOLS set for the liquid ETFs that list on NASDAQ. Odd
    share classes / units / warrants (symbols containing '.' or '/') are dropped.

    Returns a bare symbol list (UNRANKED). dynamic_universe() ranks it.
    """
    try:
        from alpaca.trading.requests import GetAssetsRequest
        from alpaca.trading.enums import AssetClass, AssetStatus
        req = GetAssetsRequest(status=AssetStatus.ACTIVE,
                               asset_class=AssetClass.US_EQUITY)
        assets = tc.get_all_assets(req)
    except Exception as e:
        print(f"list_tradable_equities: asset pull failed ({e}). "
              f"Falling back to seed universe.")
        return list(_SEED_UNIVERSE)

    syms = []
    for a in assets:
        sym = getattr(a, "symbol", "")
        if not sym or not getattr(a, "tradable", False):
            continue
        if "." in sym or "/" in sym:
            continue
        exch = str(getattr(a, "exchange", "") or "")
        # AssetExchange enum stringifies to e.g. 'AssetExchange.NASDAQ'; take tail
        exch = exch.split(".")[-1]
        if exch not in _ALLOWED_EXCHANGES:
            continue
        if sym in _EXCLUDE_SYMBOLS:
            continue
        syms.append(sym)
    if not syms:
        print("list_tradable_equities: rule produced 0 names. Seed fallback.")
        return list(_SEED_UNIVERSE)
    return syms


def rank_by_dollar_volume(dc, pool, top_n, lookback_days=10, end=None,
                          eval_days=0) -> list:
    """Rank `pool` by average dollar-volume (price*shares) and return top_n.

    POINT-IN-TIME: the ranking window ENDS at (end - eval_days), i.e. strictly
    BEFORE the backtest's evaluation window begins. This is the fix for the
    selection-on-outcome bias the old select_universe carried: previously the
    10-day ranking lookback overlapped the eval window, so a name that spiked on
    news during the sample entered the universe BECAUSE of the very move it was
    then scored on. Pass eval_days = the backtest --days so selection and
    evaluation never share a single bar. For live selection pass eval_days=0.
    """
    end = end or datetime.now(timezone.utc)
    rank_end = end - timedelta(days=eval_days)
    frames = fetch_bars_batch(dc, pool, lookback_days, BAR_DAY, end=rank_end)
    ranked = []
    for sym, df in frames.items():
        if df is None or len(df) == 0:
            continue
        dollar_vol = float((df["close"] * df["volume"]).mean())
        if dollar_vol <= 0:
            continue
        ranked.append((sym, dollar_vol))
    ranked.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in ranked[:top_n]]


def dynamic_universe(dc, tc, top_n, lookback_days=10, end=None,
                     eval_days=0) -> list:
    """Volume-driven universe with NO hardcoded candidate list.

        1. list_tradable_equities(tc)  -> rule-based candidate set (NYSE/NASDAQ
           common stock, ETFs/leveraged excluded).
        2. rank_by_dollar_volume(...)  -> point-in-time top_n by dollar volume.

    WHAT THIS BUYS: no maintained CANDIDATE_POOL, and the universe adapts as
    liquidity shifts -- while staying STABLE within one backtest run (it is
    selected once per run_backtest, then written into signal_stats.json, exactly
    like before). It does NOT re-select intraday; that would churn the symbol set
    within a session and break the live-vs-backtest distributional comparison the
    same way an intraday mu/sigma refresh would.

    TWO BIASES THIS DESIGN AVOIDS:
      - listless != junk: the exchange rule + exclude set keep SPY/QQQ/TQQQ and
        other high-volume ETFs OUT, so the ranking returns the most liquid
        STOCKS, not the most liquid products.
      - point-in-time: selection window ends before the eval window (eval_days),
        so names are not chosen on the same price action they are scored on.

    THE BIAS IT CANNOT AVOID (yet):
      IEX volume is ~2-3% of consolidated. Near the rank-`top_n` cutoff this is a
      thin, noisy proxy for true liquidity and borderline names will shuffle run
      to run. Mega-caps are unaffected; the tail is approximate until SIP.

    RUNTIME: the candidate set is large (thousands of symbols), so step 2 fetches
    many daily bars. Fine for the nightly pre-open refresh; heavy for a mid-
    session redeploy. Prefer running universe selection in the pre-open window.
    """
    pool = list_tradable_equities(tc)
    universe = rank_by_dollar_volume(dc, pool, top_n,
                                     lookback_days=lookback_days,
                                     end=end, eval_days=eval_days)
    return universe or list(_SEED_UNIVERSE)


def select_universe(dc, pool, top_n, lookback_days=10, end=None,
                    eval_days=0) -> list:
    """Backward-compatible ranking of a GIVEN pool by dollar-volume.

    Retained so existing callers that pass config.CANDIDATE_POOL keep working.
    Now supports point-in-time selection via end/eval_days (see
    rank_by_dollar_volume). New code should prefer dynamic_universe(), which
    also builds the candidate set from a rule instead of a hardcoded list.
    """
    return rank_by_dollar_volume(dc, pool, top_n, lookback_days=lookback_days,
                                 end=end, eval_days=eval_days)
