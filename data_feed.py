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
    intraday = timeframe not in (BAR_DAY,)

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


def select_universe(dc, pool, top_n, lookback_days=10) -> list:
    """
    Rank the candidate pool by recent average DOLLAR-volume (price * shares)
    and return the top_n symbols.

    NOTE: this lookback overlaps the backtest's evaluation window, so the
    universe is partly chosen using the data it is then evaluated on. A name
    that spiked on news lands in the universe precisely because it had unusual
    price action in the sample. Mild, but it is one reason `tradeable` is
    unstable across deploys.
    """
    frames = fetch_bars_batch(dc, pool, lookback_days, BAR_DAY)
    ranked = []
    for sym, df in frames.items():
        if df is None or len(df) == 0:
            continue
        dollar_vol = float((df["close"] * df["volume"]).mean())
        ranked.append((sym, dollar_vol))
    ranked.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in ranked[:top_n]]
