#!/usr/bin/env python3
"""
slice_edge.py — is there a CONDITIONAL edge hiding inside the pooled zero?

    python slice_edge.py --bootstrap 2000

Pooled gross mu across all 7,761 intraday trades is +0.0000 (t = +0.25). That is
an average. An average of zero is consistent with:

    (a) nothing works, everywhere                         <- most likely
    (b) longs work, shorts lose, and they cancel
    (c) the first 30 minutes work, the rest bleeds
    (d) strong-score entries work, weak ones don't

(b), (c) and (d) would each produce a positive-mu subset, and Merton would size
it WITHOUT any bypass. That is the only path to "keep Merton sizing AND get
trades" that does not involve lying to the sizer.

METHOD
------
Reads BACKTEST_TRADES_PATH. Slices on three pre-specified, mechanistically
motivated dimensions -- NOT a fishing expedition:

  direction   long / short          equities drift up; borrow costs and the
                                    uptick rule make shorts structurally different
  time of day open / mid / close    the open is informationally dense, the
                                    midday is thin, the close is auction-driven
  score       b30_50 / b50_70 /     the signal's own confidence, which is the
              b70_100               thing the buckets already condition on

That is 2 x 3 x 3 = 18 cells, plus the 8 marginals. Reporting the best of 26
without correction is exactly how TSLA became "tradeable" at t = 1.59.

The null is max_cells(t) under H0: no gross predictive power anywhere. Trade
returns are sign-flipped BY EXIT DATE -- one Rademacher draw shared across every
symbol and every cell, so cross-sectional correlation and the correlation
between overlapping cells both survive. The GROSS return is flipped and the cost
is subtracted afterwards; flipping the net return would flip the cost with it.

If a cell clears, it must ALSO satisfy the sizer's own gate before it means
anything: n >= MIN_BUCKET_N, t >= MIN_BUCKET_T, mu_lcb > 0.
"""

import os
import sys
import argparse
from itertools import product

import numpy as np
import pandas as pd

import config

ET = "America/New_York"


def tstat(r):
    n = len(r)
    if n < 2:
        return 0.0
    s = r.std(ddof=1)
    return float(r.mean() / (s / np.sqrt(n))) if s > 0 else 0.0


def bucket_of(score):
    a = abs(score)
    return "b30_50" if a < 0.50 else ("b50_70" if a < 0.70 else "b70_100")


def tod_of(ts):
    t = ts.time()
    if t < pd.Timestamp("10:00").time():
        return "open"
    if t < pd.Timestamp("15:00").time():
        return "mid"
    return "close"


def load():
    path = config.BACKTEST_TRADES_PATH
    if not os.path.exists(path):
        print(f"{path} not found. Run run_backtest.py first.")
        sys.exit(1)
    df = pd.read_csv(path)
    df["entry_time"] = pd.to_datetime(df["entry_time"], utc=True).dt.tz_convert(ET)
    df["exit_time"] = pd.to_datetime(df["exit_time"], utc=True).dt.tz_convert(ET)
    df["side"] = np.where(df["direction"] > 0, "long", "short")
    df["tod"] = df["entry_time"].map(tod_of)
    df["bucket"] = df["entry_score"].map(bucket_of)
    df["exit_date"] = df["exit_time"].dt.normalize()
    cost_rt = 2 * (config.SLIPPAGE_BPS + config.SPREAD_BPS) / 1e4
    df["gross"] = df["ret"] + cost_rt
    return df, cost_rt


def build_cells(df):
    cells = {}
    for s in ["long", "short"]:
        cells[f"side={s}"] = df[df.side == s]
    for t in ["open", "mid", "close"]:
        cells[f"tod={t}"] = df[df.tod == t]
    for b in ["b30_50", "b50_70", "b70_100"]:
        cells[f"bucket={b}"] = df[df.bucket == b]
    for s, t, b in product(["long", "short"], ["open", "mid", "close"],
                           ["b30_50", "b50_70", "b70_100"]):
        cells[f"{s[0]}_{t}_{b}"] = df[(df.side == s) & (df.tod == t) & (df.bucket == b)]
    return {k: v for k, v in cells.items() if len(v) >= 30}


def bootstrap(cells, dmap, iters, cost_rt, seed=0):
    rng = np.random.default_rng(seed)
    arrs = [(c["gross"].values,
             c["exit_date"].map(dmap).values.astype(int)) for c in cells.values()]
    out = np.empty(iters)
    for b in range(iters):
        f = rng.choice([-1.0, 1.0], size=len(dmap))
        out[b] = max(tstat(g * f[ix] - cost_rt) for g, ix in arrs)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bootstrap", type=int, default=2000)
    args = ap.parse_args()

    df, cost_rt = load()
    print(f"{len(df):,} trades | {df.exit_date.nunique()} exit dates | "
          f"round-trip cost {cost_rt*1e4:.0f} bp")
    print(f"pooled: net mu={df.ret.mean():+.5f}  gross mu={df.gross.mean():+.5f}  "
          f"t={tstat(df.ret.values):+.2f}\n")

    cells = build_cells(df)
    dates = sorted(df.exit_date.unique())
    dmap = {d: i for i, d in enumerate(dates)}

    rows = []
    for name, c in cells.items():
        r = c["ret"].values
        g = c["gross"].values
        n = len(r)
        se = r.std(ddof=1) / np.sqrt(n)
        mu_lcb = r.mean() - config.LCB_Z * se
        t = tstat(r)
        sizes = (n >= config.MIN_BUCKET_N and t >= config.MIN_BUCKET_T and mu_lcb > 0)
        rows.append((name, n, g.mean(), r.mean(), t, float((r > 0).mean()), sizes))
    rows.sort(key=lambda x: -x[4])

    print(f"{'cell':22s} {'n':>6s} {'gross mu':>10s} {'net mu':>10s} {'t':>7s} "
          f"{'win':>6s}  merton sizes?")
    for name, n, gm, nm, t, w, s in rows:
        print(f"{name:22s} {n:6d} {gm:+10.5f} {nm:+10.5f} {t:+7.2f} {w:6.2f}  "
              f"{'YES' if s else 'no'}")

    obs = rows[0][4]
    null = bootstrap(cells, dmap, args.bootstrap, cost_rt)
    q95 = float(np.quantile(null, 1 - config.SELECTION_ALPHA))
    p = float((null >= obs).mean())

    print(f"\n=== family-wise null over {len(cells)} cells, "
          f"{args.bootstrap} date-blocked sign-flip resamples ===")
    print(f"  observed max t : {obs:+.3f}   ({rows[0][0]})")
    print(f"  null median    : {np.median(null):+.3f}")
    print(f"  null 95th pct  : {q95:+.3f}   <- the bar a single cell must beat")
    print(f"  family-wise p  : {p:.4f}")

    if obs < q95:
        print("\n  VERDICT: no conditional slice survives multiplicity correction.")
        print("  The pooled zero is not hiding a positive subset. It is zero everywhere.")
    else:
        print(f"\n  VERDICT: {rows[0][0]} survives. Its gross mu is real.")
        print("  Next step: restrict the universe/entry rule to this slice, re-run")
        print("  run_backtest.py so signal_stats.json carries ITS mu/sigma, and")
        print("  Merton will size it with no bypass.")


if __name__ == "__main__":
    main()
