#!/usr/bin/env python3
"""
run_backtest.py — fetch real history, produce signal_stats.json.

THIS is the mu/sigma unlock. Run it to fetch real IEX bars for the universe, run
the event backtest, pool trades across symbols into score buckets, and write
signal_stats.json — which the live runner reads to size positions.

    python run_backtest.py --days 60
    python run_backtest.py --days 60 --bootstrap 2000

TWO THINGS THIS FILE NOW DOES THAT IT DID NOT BEFORE
----------------------------------------------------
1. PERSISTS EVERY TRADE to BACKTEST_TRADES_PATH. Previously `all_trades` lived
   in memory, got compressed into three bucket moments, and was discarded. You
   cannot bootstrap, cannot audit, and cannot run the Welch/Levene/KS comparison
   against live without the raw sample.

2. BOOTSTRAPS THE NULL DISTRIBUTION OF max(edge_ratio).
   `worthy = ratio >= MIN_EDGE_RATIO` is a threshold applied to 50 test
   statistics, and only the winner survives. Algebraically

       ratio = (t - LCB_Z) / sqrt(n)

   so MIN_EDGE_RATIO = 0.05 at n=68 means t >= 1.41. The expected maximum t of
   ~10-20 effectively independent noise draws is 1.36-1.71. A symbol can clear
   the bar while being exactly what you would expect from the best of 50 coin
   flips. The bootstrap sign-flips trade returns BY EXIT DATE (one Rademacher
   draw shared across all symbols closing that day, so cross-sectional
   correlation is preserved), recomputes every symbol's ratio, and records the
   maximum. The 1-alpha quantile of that distribution is the honest bar.

ON POOLING (deliberate, and correct)
------------------------------------
`_bucket_stats(all_trades)` pools across the WHOLE universe, including the
symbols the screen rejected. This looks like a bug and is not. The pooled mu is
an unbiased estimate of what the signal earns on a randomly chosen large-cap.
The surviving symbol's own mu is the maximum of 50 draws and is upward-biased by
construction. Restricting the buckets to the "tradeable" set would feed the
sizer a selection-biased edge estimate. Do not do it.
"""

import os
import sys
import csv
import json
import argparse
from datetime import datetime, timezone

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import numpy as np
import pandas as pd
import config
import backtest as bt
import data_feed as feed


def _key(*names):
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return None


def edge_ratio(rets: np.ndarray) -> float:
    """ratio = mu_lcb / sigma = mu/sigma - LCB_Z/sqrt(n)."""
    n = len(rets)
    if n < 2:
        return 0.0
    sig = rets.std(ddof=1)
    if sig <= 0:
        return 0.0
    return float(rets.mean() / sig - config.LCB_Z / np.sqrt(n))


def bootstrap_max_ratio(trades_by_symbol: dict, iters: int, seed: int = 0):
    """Null distribution of max_symbol(edge_ratio) under H0: no edge.

    Sign-flips returns by EXIT DATE, one draw per date shared across symbols, so
    that market-wide co-movement (which is what makes 50 large-caps far fewer
    than 50 independent tests) survives into the null.

    FIX (net->gross): t['ret'] is NET of costs. Sign-flipping NET flips the fixed
    per-trade cost along with the signal, which biases the null upward and sets
    MIN_EDGE_RATIO too high (over-conservative screen). The correct null flips the
    GROSS return and subtracts the cost afterward -- identical to slice_edge.py.
    Under flip=+1 this reproduces the observed net sample exactly, as it must.
    """
    rng = np.random.default_rng(seed)
    dates = sorted({d for v in trades_by_symbol.values() for d in v["dates"]})
    date_ix = {d: i for i, d in enumerate(dates)}
    cost_rt = 2 * (config.SLIPPAGE_BPS + config.SPREAD_BPS) / 1e4   # round-trip cost

    packed = []
    for sym, v in trades_by_symbol.items():
        if len(v["rets"]) >= config.MIN_SYMBOL_TRADES:
            gross = np.asarray(v["rets"], dtype=float) + cost_rt   # reconstruct GROSS
            packed.append((gross,
                           np.asarray([date_ix[d] for d in v["dates"]])))
    if not packed:
        return np.array([])

    out = np.empty(iters)
    for b in range(iters):
        flips = rng.choice([-1.0, 1.0], size=len(dates))
        best = -np.inf
        for gross, ix in packed:
            best = max(best, edge_ratio(gross * flips[ix] - cost_rt))  # flip gross, pay cost
        out[b] = best
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=config.BACKTEST_DAYS)
    ap.add_argument("--bootstrap", type=int, default=config.BOOTSTRAP_ITERS)
    args = ap.parse_args()

    api_key = _key("ALPACA_API_KEY", "APCA_API_KEY_ID")
    secret = _key("ALPACA_SECRET_KEY", "APCA_API_SECRET_KEY")
    if not api_key or not secret:
        print("Credentials not found."); sys.exit(1)

    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.trading.client import TradingClient
    dc = StockHistoricalDataClient(api_key, secret)
    tc = TradingClient(api_key, secret, paper=True)  # asset list for dynamic universe

    if config.USE_DYNAMIC_UNIVERSE:
        # Rule-based candidate set (NYSE/NASDAQ common stock, ETFs excluded),
        # ranked by dollar volume. eval_days=0 -> rank on recent liquidity so the
        # written universe reflects what live can actually trade tomorrow.
        universe = feed.dynamic_universe(dc, tc, config.UNIVERSE_SIZE)
    else:
        universe = feed.select_universe(dc, config.CANDIDATE_POOL, config.UNIVERSE_SIZE)
    if not universe:
        print("Universe selection returned nothing; falling back."); universe = config.UNIVERSE
    print(f"Universe ({len(universe)} by dollar-volume): {', '.join(universe)}\n")

    frames = feed.fetch_bars_batch(dc, universe, args.days)

    all_trades = []
    per_symbol = {}
    trades_by_symbol = {}
    tradeable = []
    for sym in universe:
        try:
            df = frames.get(sym)
            if df is None or len(df) < 300:
                print(f"{sym:6s}: insufficient bars, skipping"); continue
            res = bt.run_backtest(df, flatten_eod=config.FLATTEN_EOD)
            for t in res["trades"]:
                t["symbol"] = sym
            all_trades.extend(res["trades"])
            m = res["metrics"]

            rets = np.array([t["ret"] for t in res["trades"]])
            if len(rets) >= config.MIN_SYMBOL_TRADES:
                s_mu = float(rets.mean())
                s_sig = float(rets.std(ddof=1))
                s_lcb = s_mu - config.LCB_Z * (s_sig / np.sqrt(len(rets)))
                ratio = s_lcb / s_sig if s_sig > 0 else 0.0
                s_t = s_mu / (s_sig / np.sqrt(len(rets))) if s_sig > 0 else 0.0
                trades_by_symbol[sym] = {
                    "rets": rets,
                    "dates": [pd.Timestamp(t["exit_time"]).normalize()
                              for t in res["trades"]],
                }
            else:
                s_mu = s_sig = s_lcb = ratio = s_t = 0.0
            worthy = bool(ratio >= config.MIN_EDGE_RATIO)
            per_symbol[sym] = {**m, "mu": float(s_mu), "sigma": float(s_sig),
                               "mu_lcb": float(s_lcb), "edge_ratio": float(ratio),
                               "t_stat": float(s_t), "worthy": worthy}
            if worthy:
                tradeable.append(sym)

            flag = "WORTH IT" if worthy else "skip (risk not justified)"
            print(f"{sym:6s}: trades={m['n_trades']:4d} win={m['win_rate']:.2f} "
                  f"avg_ret={m['avg_ret']:+.4f} sigma={s_sig:.4f} t={s_t:+.2f} "
                  f"ratio={ratio:+.3f}  {flag}")
        except Exception as e:
            print(f"{sym:6s}: error {e}")

    if not all_trades:
        print("No trades produced. Check data access / date range."); sys.exit(1)

    # ---- persist the raw sample --------------------------------------------
    cols = ["symbol", "entry_time", "exit_time", "direction", "entry_score",
            "bars_held", "exit_kind", "ret"]
    with open(config.BACKTEST_TRADES_PATH, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for t in all_trades:
            w.writerow({**t, "bucket": bt._bucket(t["entry_score"])})
    print(f"\nWrote {config.BACKTEST_TRADES_PATH} ({len(all_trades)} trades)")

    pooled = bt._bucket_stats(all_trades)   # pool across universe -- unbiased, see docstring
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
        json.dump(payload, f, indent=2, default=float)

    print(f"Wrote {config.SIGNAL_STATS_PATH}  ({len(all_trades)} pooled trades, "
          f"{len(tradeable)}/{len(universe)} clear the risk-adjusted bar "
          f"(ratio >= {config.MIN_EDGE_RATIO}))")
    print(f"Tradeable: {', '.join(tradeable) or '(none)'}")

    cost_rt = 2 * (config.SLIPPAGE_BPS + config.SPREAD_BPS) / 1e4
    print("\nPer-bucket edge (what Merton will size on):")
    print(f"  round-trip cost assumption: {cost_rt*1e4:.0f} bps")
    for k, v in sorted(pooled.items()):
        if v["n"] > 1 and v["sigma"] > 0:
            se = v["sigma"] / np.sqrt(v["n"])
            lcb = v["mu"] - config.LCB_Z * se
            t = v["mu"] / se
        else:
            se = lcb = t = 0.0
        gross = v["mu"] + cost_rt
        sized = (v["n"] >= config.MIN_BUCKET_N and t >= config.MIN_BUCKET_T and lcb > 0)
        flag = "SIZES" if sized else (
            f"-> 0 (t={t:+.2f} < {config.MIN_BUCKET_T})" if t < config.MIN_BUCKET_T
            else "-> 0 (LCB not positive)")
        print(f"  {k:9s} mu={v['mu']:+.4f} sigma={v['sigma']:.4f} n={v['n']:4d} "
              f"se={se*1e4:5.2f}bp t={t:+6.2f} mu_lcb={lcb:+.5f} "
              f"gross={gross:+.4f}  {flag}")

    # ---- selection-adjusted null -------------------------------------------
    if args.bootstrap and trades_by_symbol:
        print(f"\n=== Selection-adjusted null for max(edge_ratio) "
              f"({args.bootstrap} date-blocked sign-flip resamples) ===")
        null = bootstrap_max_ratio(trades_by_symbol, args.bootstrap)
        if len(null):
            obs = max((per_symbol[s]["edge_ratio"] for s in trades_by_symbol), default=0.0)
            q = float(np.quantile(null, 1 - config.SELECTION_ALPHA))
            pval = float((null >= obs).mean())
            print(f"  symbols tested          : {len(trades_by_symbol)}")
            print(f"  observed max(edge_ratio): {obs:+.4f}")
            print(f"  null median             : {np.median(null):+.4f}")
            print(f"  null {100*(1-config.SELECTION_ALPHA):.0f}th pct (the honest bar)"
                  f": {q:+.4f}   [MIN_EDGE_RATIO is currently {config.MIN_EDGE_RATIO}]")
            print(f"  family-wise p-value     : {pval:.4f}")
            if obs < q:
                print("  VERDICT: the best symbol is INSIDE the noise band. "
                      "No symbol has demonstrated edge. Do not trade this.")
            else:
                print("  VERDICT: the best symbol survives the multiplicity "
                      "correction. Set MIN_EDGE_RATIO to the quantile above.")

    # --- exit-rule comparison on the tradeable set (signals built once) -------
    sfs = {}
    for sym in tradeable:
        df = frames.get(sym)
        if df is not None and len(df) >= 300:
            sfs[sym] = bt.build_signal_frame(df)

    variants = {
        "current (2.5% blend)": dict(trail=0.025, sensitive=False),
        "tight (1.5% blend)":   dict(trail=0.015, sensitive=False),
        "sensitive (2.5% 5m)":  dict(trail=0.025, sensitive=True),
        "both (1.5% 5m)":       dict(trail=0.015, sensitive=True),
    }

    def evaluate(trail, sensitive):
        rows = []
        for sf in sfs.values():
            for t in bt._simulate(sf, config.ENTRY_THRESHOLD, config.FLATTEN_EOD,
                                  trail, sensitive):
                rows.append((pd.Timestamp(t["exit_time"]).normalize(), t["ret"]))
        if not rows:
            return None
        r = np.array([x[1] for x in rows])
        daily = pd.Series([x[1] for x in rows],
                          index=[x[0] for x in rows]).groupby(level=0).sum()
        d = daily.values
        return {
            "n": len(r), "total": float(r.sum()), "avg": float(r.mean()),
            "sharpe": float(r.mean() / r.std(ddof=1)) if r.std(ddof=1) > 0 else 0.0,
            "win": float((r > 0).mean()),
            "worst_day": float(d.min()) if len(d) else 0.0,
            "pct_down_days": float((d < 0).mean()) if len(d) else 0.0,
        }

    if not sfs:
        print("\n(no tradeable symbols -- skipping exit-rule comparison)")
        return

    print("\n=== Exit-rule comparison (tradeable set) ===")
    print("NOTE: this is an in-sample comparison on the ONE symbol that won a "
          "50-way search.\n      Treat differences under ~1 se as noise.")
    print(f"{'variant':22s} {'trades':>6s} {'total':>8s} {'risk-adj':>9s} "
          f"{'win':>5s} {'worstday':>9s} {'down-days':>9s}")
    results = {}
    for name, p in variants.items():
        res = evaluate(p["trail"], p["sensitive"])
        results[name] = res
        if res:
            print(f"{name:22s} {res['n']:6d} {res['total']:+8.2f} {res['sharpe']:+9.3f} "
                  f"{res['win']:5.2f} {res['worst_day']:+9.3f} {res['pct_down_days']:9.2f}")

    valid = {k: v for k, v in results.items() if v}
    if valid:
        best_ret = max(valid, key=lambda k: valid[k]["sharpe"])
        best_pain = max(valid, key=lambda k: valid[k]["worst_day"])  # least-negative
        print(f"\n-> Best risk-adjusted return: {best_ret}")
        print(f"-> Smallest worst-day drawdown: {best_pain}")
        print(f"-> Currently running: trail={config.TRAIL_PERCENT}% "
              f"sensitive_exit={config.SENSITIVE_EXIT}")


if __name__ == "__main__":
    main()
