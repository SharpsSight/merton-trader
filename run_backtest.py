#!/usr/bin/env python3
"""
run_backtest.py — fetch real history, produce signal_stats.json.

THIS is the mu/sigma unlock. Run it (locally or on Railway) to fetch real IEX
bars for the universe, run the event backtest, pool trades across symbols into
score buckets, and write signal_stats.json — which the live runner reads to
size positions. Until this file exists, the live runner stays in OBSERVE mode.

    python run_backtest.py --days 60
"""

import os
import sys
import json
import argparse
from datetime import datetime, timedelta, timezone

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import numpy as np
import config
import backtest as bt


def _key(*names):
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return None


def fetch_15m(dc, symbol, days):
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from alpaca.data.enums import DataFeed
    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame(15, TimeFrameUnit.Minute),
        start=datetime.now(timezone.utc) - timedelta(days=days),
        feed=DataFeed.IEX,
    )
    df = dc.get_stock_bars(req).df
    if df is None or len(df) == 0:
        return None
    if "symbol" in df.index.names:            # drop symbol level of MultiIndex
        df = df.xs(symbol, level="symbol")
    return df[["open", "high", "low", "close", "volume"]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    args = ap.parse_args()

    api_key = _key("ALPACA_API_KEY", "APCA_API_KEY_ID")
    secret = _key("ALPACA_SECRET_KEY", "APCA_API_SECRET_KEY")
    if not api_key or not secret:
        print("Credentials not found."); sys.exit(1)

    from alpaca.data.historical import StockHistoricalDataClient
    dc = StockHistoricalDataClient(api_key, secret)

    all_trades = []
    per_symbol = {}
    for sym in config.UNIVERSE:
        try:
            df = fetch_15m(dc, sym, args.days)
            if df is None or len(df) < 300:
                print(f"{sym:6s}: insufficient bars, skipping"); continue
            res = bt.run_backtest(df)
            all_trades.extend(res["trades"])
            per_symbol[sym] = res["metrics"]
            m = res["metrics"]
            print(f"{sym:6s}: trades={m['n_trades']:4d} "
                  f"win={m['win_rate']:.2f} avg_ret={m['avg_ret']:+.4f}")
        except Exception as e:
            print(f"{sym:6s}: error {e}")

    if not all_trades:
        print("No trades produced. Check data access / date range."); sys.exit(1)

    pooled = bt._bucket_stats(all_trades)      # pool across universe for sample size
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "days": args.days,
        "n_trades": len(all_trades),
        "buckets": pooled,
        "per_symbol": per_symbol,
    }
    with open(config.SIGNAL_STATS_PATH, "w") as f:
        json.dump(payload, f, indent=2)

    print(f"\nWrote {config.SIGNAL_STATS_PATH}  ({len(all_trades)} pooled trades)")
    print("Per-bucket edge (what Merton will size on):")
    for k, v in sorted(pooled.items()):
        lcb = v["mu"] - config.LCB_Z * (v["sigma"] / np.sqrt(v["n"])) if v["n"] > 1 else 0.0
        flag = "SIZES" if lcb > 0 else "-> 0 (LCB not positive)"
        print(f"  {k:9s} mu={v['mu']:+.4f} sigma={v['sigma']:.4f} n={v['n']:4d} "
              f"mu_lcb={lcb:+.4f}  {flag}")


if __name__ == "__main__":
    main()
