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
import data_feed as feed


def _key(*names):
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return None


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

    # pick the top-N most liquid names, then ONE batched fetch for all of them
    universe = feed.select_universe(dc, config.CANDIDATE_POOL, config.UNIVERSE_SIZE)
    if not universe:
        print("Universe selection returned nothing; falling back."); universe = config.UNIVERSE
    print(f"Universe ({len(universe)} by dollar-volume): {', '.join(universe)}\n")

    frames = feed.fetch_bars_batch(dc, universe, args.days)

    all_trades = []
    per_symbol = {}
    tradeable = []
    for sym in universe:
        try:
            df = frames.get(sym)
            if df is None or len(df) < 300:
                print(f"{sym:6s}: insufficient bars, skipping"); continue
            res = bt.run_backtest(df)
            all_trades.extend(res["trades"])
            m = res["metrics"]

            # per-symbol worthiness: is the risk-ADJUSTED edge good enough?
            # ratio = mu_lcb / sigma (confident return per unit of trade risk).
            rets = np.array([t["ret"] for t in res["trades"]])
            if len(rets) >= config.MIN_SYMBOL_TRADES:
                s_mu = float(rets.mean())
                s_sig = float(rets.std(ddof=1))
                s_lcb = s_mu - config.LCB_Z * (s_sig / np.sqrt(len(rets)))
                ratio = s_lcb / s_sig if s_sig > 0 else 0.0
            else:
                s_mu = s_sig = s_lcb = ratio = 0.0
            worthy = ratio >= config.MIN_EDGE_RATIO
            per_symbol[sym] = {**m, "mu": s_mu, "sigma": s_sig,
                               "mu_lcb": s_lcb, "edge_ratio": ratio, "worthy": worthy}
            if worthy:
                tradeable.append(sym)

            flag = "WORTH IT" if worthy else "skip (risk not justified)"
            print(f"{sym:6s}: trades={m['n_trades']:4d} win={m['win_rate']:.2f} "
                  f"avg_ret={m['avg_ret']:+.4f} ratio={ratio:+.3f}  {flag}")
        except Exception as e:
            print(f"{sym:6s}: error {e}")

    if not all_trades:
        print("No trades produced. Check data access / date range."); sys.exit(1)

    pooled = bt._bucket_stats(all_trades)      # pool across universe for sample size
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "days": args.days,
        "universe": universe,
        "tradeable": tradeable,
        "n_trades": len(all_trades),
        "buckets": pooled,
        "per_symbol": per_symbol,
    }
    with open(config.SIGNAL_STATS_PATH, "w") as f:
        json.dump(payload, f, indent=2)

    print(f"\nWrote {config.SIGNAL_STATS_PATH}  ({len(all_trades)} pooled trades, "
          f"{len(tradeable)}/{len(universe)} clear the risk-adjusted bar "
          f"(ratio >= {config.MIN_EDGE_RATIO}))")
    print(f"Tradeable: {', '.join(tradeable) or '(none)'}")
    print("Per-bucket edge (what Merton will size on):")
    for k, v in sorted(pooled.items()):
        lcb = v["mu"] - config.LCB_Z * (v["sigma"] / np.sqrt(v["n"])) if v["n"] > 1 else 0.0
        flag = "SIZES" if lcb > 0 else "-> 0 (LCB not positive)"
        print(f"  {k:9s} mu={v['mu']:+.4f} sigma={v['sigma']:.4f} n={v['n']:4d} "
              f"mu_lcb={lcb:+.4f}  {flag}")


if __name__ == "__main__":
    main()
