"""
data_feed.py — batched market data + dynamic universe selection.

Shared by run_backtest.py and live_paper.py so both use the SAME fetch path
(anti-drift). Batching is the key to scaling past a handful of symbols without
hitting Alpaca's 200-calls/min limit: one request returns bars for ALL symbols.
"""

from __future__ import annotations
from datetime import datetime, timedelta, timezone

from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed

BAR_15M = TimeFrame(15, TimeFrameUnit.Minute)
BAR_DAY = TimeFrame.Day


def fetch_bars_batch(dc, symbols, days, timeframe=BAR_15M) -> dict:
    """
    One request for MANY symbols. Returns {symbol: DataFrame[o,h,l,c,v]}.
    Symbols with no data are simply absent from the result.
    """
    symbols = list(symbols)
    if not symbols:
        return {}
    req = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=timeframe,
        start=datetime.now(timezone.utc) - timedelta(days=days),
        feed=DataFeed.IEX,
    )
    df = dc.get_stock_bars(req).df
    out = {}
    if df is None or len(df) == 0:
        return out
    cols = ["open", "high", "low", "close", "volume"]
    if df.index.nlevels > 1:
        for sym in symbols:
            try:
                sub = df.xs(sym, level=0)
                if len(sub):
                    out[sym] = sub[cols]
            except KeyError:
                continue
    else:                                   # single symbol -> flat index
        out[symbols[0]] = df[cols]
    return out


def select_universe(dc, pool, top_n, lookback_days=10) -> list:
    """
    Rank the candidate pool by recent average DOLLAR-volume (price * shares)
    and return the top_n symbols. Dollar-volume favours 'major companies'
    over penny stocks that print high share counts.
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
