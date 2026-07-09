#!/usr/bin/env python3
"""
research_reversion.py — does intraday VWAP reversion have edge?

RESEARCH ONLY. Writes no signal_stats.json, sends no orders.

    python research_reversion.py --selftest
    python research_reversion.py --days 120 --bootstrap 1000

Grid of 64 cells. The statistic is max_cells(t), and the null is built by
sign-flipping trade returns BY EXIT DATE with one Rademacher draw shared across
every symbol and every cell -- so cross-sectional correlation and the
correlation between overlapping configurations both survive into the null.
Reporting the best of 64 without that correction is how TSLA got a "1.59 t-stat
edge" out of fifty coin flips.

COST SENSITIVITY IS NOT OPTIONAL HERE. Intraday reversion measured on 5-minute
bars is substantially bid-ask bounce. The script sweeps the assumed cost and
prints the breakeven. A cell that only clears at 2bps/side has found the spread,
not an edge.

CONTROLS run before any real data is touched:
  NEGATIVE  random walk           -> must find nothing
  POSITIVE  OU process about VWAP -> must find it, in the long+short cells
"""

import os
import sys
import csv
import argparse
from datetime import datetime, timezone, timedelta
from itertools import product

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import numpy as np
import pandas as pd

import config
import reversion_signal as rv
from research_news import tstat, bootstrap_max_t

WARMUP = 8
TRAIL = config.TRAIL_PERCENT / 100.0

GRID = {
    "z_enter":          [1.5, 2.5],
    "z_take":           [0.0, -0.5],
    "z_stop":           [-3.0, -4.0],
    "require_reversal": [True, False],
    "max_hold":         [12, 24],
    "side":             [1, -1],
}


def cells():
    keys = list(GRID)
    for combo in product(*(GRID[k] for k in keys)):
        yield dict(zip(keys, combo))


def cell_name(c):
    return (f"e{c['z_enter']:g}_t{c['z_take']:g}_s{c['z_stop']:g}"
            f"_{'rev' if c['require_reversal'] else 'raw'}"
            f"_h{c['max_hold']}_{'L' if c['side'] == 1 else 'S'}")


def _key(*names):
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return None


def build(frames, cost_per_side):
    per_cell = {cell_name(c): {"rets": [], "gross": [], "dates": [], "cfg": c,
                               "wins": 0, "held": []}
                for c in cells()}
    for sym, df in frames.items():
        if df is None or len(df) < 300:
            continue
        sf = rv.session_zscore(df, warmup=WARMUP)
        for c in cells():
            trades = rv.extract_trades(
                sf, z_enter=c["z_enter"], z_take=c["z_take"], z_stop=c["z_stop"],
                require_reversal=c["require_reversal"], max_hold=c["max_hold"],
                trail=TRAIL, side=c["side"], cost_per_side=cost_per_side)
            b = per_cell[cell_name(c)]
            for t in trades:
                b["rets"].append(t["ret"])
                b["gross"].append(t["gross_ret"])
                b["dates"].append(pd.Timestamp(t["exit_time"]).normalize())
                b["held"].append(t["bars_held"])
    return per_cell


def pack(per_cell, min_trades=30):
    all_dates = sorted({d for v in per_cell.values() for d in v["dates"]})
    dmap = {d: i for i, d in enumerate(all_dates)}
    packed = {}
    for name, v in per_cell.items():
        if len(v["rets"]) < min_trades:
            continue
        packed[name] = {
            "rets": np.asarray(v["rets"]),
            "gross": np.asarray(v["gross"]),
            "held": np.asarray(v["held"]),
            "ix": np.asarray([dmap[d] for d in v["dates"]]),
            "cfg": v["cfg"],
        }
    return packed, len(all_dates)


def report(packed, n_dates, iters, cost_per_side, label=""):
    if not packed:
        print("  no cell produced enough trades. Nothing to test.")
        return None

    rows = []
    for name, v in packed.items():
        r = v["rets"]
        n = len(r)
        sd = r.std(ddof=1)
        rows.append((name, n, r.mean(), v["gross"].mean(), tstat(r),
                     float((r > 0).mean()), float(v["held"].mean())))
    rows.sort(key=lambda x: -x[4])

    print(f"\n  {'cell':30s} {'n':>6s} {'net mu':>10s} {'gross mu':>10s} "
          f"{'t':>7s} {'win':>6s} {'bars':>6s}")
    for name, n, mu, g, t, w, hb in rows[:8]:
        print(f"  {name:30s} {n:6d} {mu:+10.5f} {g:+10.5f} {t:+7.2f} "
              f"{w:6.2f} {hb:6.1f}")
    if len(rows) > 8:
        print(f"  ... {len(rows)-8} more cells")

    obs = rows[0][4]
    null = bootstrap_max_t(packed, n_dates, iters)
    q95 = float(np.quantile(null, 1 - config.SELECTION_ALPHA))
    p = float((null >= obs).mean())

    print(f"\n  === family-wise null over {len(packed)} cells, "
          f"{iters} date-blocked resamples ===")
    print(f"  observed max t : {obs:+.3f}   ({rows[0][0]})")
    print(f"  null median    : {np.median(null):+.3f}")
    print(f"  null 95th pct  : {q95:+.3f}   <- a single cell needs to beat THIS")
    print(f"  family-wise p  : {p:.4f}")
    print(f"  cost charged   : {cost_per_side*1e4:.1f} bps/side")
    if obs < q95:
        print(f"  VERDICT{label}: best cell is INSIDE the noise band. No edge.")
    else:
        print(f"  VERDICT{label}: best cell SURVIVES multiplicity correction.")
    return {"obs": obs, "q95": q95, "p": p, "best": rows[0][0], "rows": rows}


def cost_sweep(frames, iters):
    """The number that decides whether a reversion 'edge' is really the spread."""
    print(f"\n{'='*74}\nCOST SENSITIVITY — where does the edge die?\n{'='*74}")
    print(f"  {'bps/side':>9s} {'best cell':>32s} {'max t':>8s} {'null 95th':>10s} {'p':>8s}")
    for bps in [0.0, 1.0, 3.0, 5.0, 7.0, 10.0]:
        packed, nd = pack(build(frames, bps / 1e4))
        if not packed:
            print(f"  {bps:9.1f}  (no cells)")
            continue
        obs = max(tstat(v["rets"]) for v in packed.values())
        best = max(packed, key=lambda k: tstat(packed[k]["rets"]))
        null = bootstrap_max_t(packed, nd, iters, seed=5)
        q95 = float(np.quantile(null, 0.95))
        p = float((null >= obs).mean())
        flag = " <- edge only exists below real spreads" if (p < 0.05 and bps <= 1.0) else ""
        print(f"  {bps:9.1f} {best:>32s} {obs:+8.2f} {q95:+10.2f} {p:8.4f}{flag}")


# ---------------------------------------------------------------------------
def synth(kind, seed=2, n_days=90, bars=78, n_sym=6):
    """kind: 'null' = random walk. 'planted' = OU reversion about a drifting VWAP."""
    rng = np.random.default_rng(seed)
    frames = {}
    n = n_days * bars
    idx = pd.date_range("2025-01-02 14:30", periods=n, freq="5min", tz="UTC")
    idx = pd.DatetimeIndex([t + pd.Timedelta(days=(i // bars))
                            for i, t in enumerate(idx[:n])])  # placeholder
    # rebuild a clean per-session index: `bars` bars per day, one day apart
    days = pd.date_range("2025-01-02", periods=n_days, freq="B", tz="UTC")
    stamps = []
    for d in days:
        stamps.extend(pd.date_range(d + pd.Timedelta(hours=14, minutes=30),
                                    periods=bars, freq="5min"))
    idx = pd.DatetimeIndex(stamps)

    sigma = 0.0011
    for s in range(n_sym):
        px = np.empty(n)
        p = 100.0
        for d in range(n_days):
            anchor = p
            for b in range(bars):
                i = d * bars + b
                if kind == "planted":
                    # OU pull back toward the session anchor
                    pull = -0.06 * (p / anchor - 1.0)
                    p *= np.exp(pull + rng.normal(0, sigma))
                else:
                    p *= np.exp(rng.normal(0, sigma))
                px[i] = p
        op = np.concatenate([[px[0]], px[:-1]])
        frames[f"S{s}"] = pd.DataFrame(
            {"open": op, "high": np.maximum(op, px) * 1.0004,
             "low": np.minimum(op, px) * 0.9996, "close": px,
             "volume": rng.lognormal(11, 0.3, n)}, index=idx)
    return frames


def selftest(iters):
    cost = (config.SLIPPAGE_BPS + config.SPREAD_BPS) / 1e4
    for kind, expect in [("null", "NO edge"), ("planted", "edge FOUND")]:
        print(f"\n{'='*74}\nSELFTEST [{kind}] — harness must report: {expect}\n{'='*74}")
        packed, nd = pack(build(synth(kind), cost))
        res = report(packed, nd, iters, cost, label=f" [{kind}]")
        if res is None:
            print("  *** INCONCLUSIVE ***"); return False
        ok = (res["p"] > 0.05) if kind == "null" else (res["p"] < 0.05)
        print(f"  -> {'PASS' if ok else '*** FAIL ***'}")
        if not ok:
            return False
    print("\nBoth controls passed. The harness detects planted reversion and does "
          "not invent it.\n")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--bootstrap", type=int, default=1000)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--extra-spread-bps", type=float, default=3.0,
                    help="reversion entries take liquidity when it is scarce")
    args = ap.parse_args()

    if args.selftest:
        sys.exit(0 if selftest(args.bootstrap) else 1)
    if not selftest(min(args.bootstrap, 400)):
        print("Controls failed. Refusing to run on real data."); sys.exit(1)

    api_key = _key("ALPACA_API_KEY", "APCA_API_KEY_ID")
    secret = _key("ALPACA_SECRET_KEY", "APCA_API_SECRET_KEY")
    if not api_key or not secret:
        print("Credentials not found."); sys.exit(1)

    from alpaca.data.historical import StockHistoricalDataClient
    import data_feed as feed
    dc = StockHistoricalDataClient(api_key, secret)

    universe = feed.select_universe(dc, config.CANDIDATE_POOL,
                                    config.UNIVERSE_SIZE) or config.UNIVERSE
    print(f"Universe ({len(universe)}): {', '.join(universe)}\n")
    frames = feed.fetch_bars_batch(dc, universe, args.days)
    print(f"{len(frames)} symbols, {sum(len(d) for d in frames.values()):,} RTH bars")

    cost = (config.SLIPPAGE_BPS + config.SPREAD_BPS + args.extra_spread_bps) / 1e4
    print(f"Cost charged: {cost*1e4:.1f} bps/side "
          f"({config.SLIPPAGE_BPS}+{config.SPREAD_BPS}+{args.extra_spread_bps})")

    packed, n_dates = pack(build(frames, cost))
    print(f"\n{len(packed)}/{len(list(cells()))} cells produced >= 30 trades "
          f"across {n_dates} exit dates")
    res = report(packed, n_dates, args.bootstrap, cost)

    cost_sweep(frames, max(300, args.bootstrap // 3))

    if res:
        path = os.path.join(config.DATA_DIR, "reversion_research.csv")
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["cell", "n", "net_mu", "gross_mu", "t", "win_rate", "avg_bars"])
            for row in res["rows"]:
                w.writerow([row[0], row[1], f"{row[2]:.6f}", f"{row[3]:.6f}",
                            f"{row[4]:.3f}", f"{row[5]:.3f}", f"{row[6]:.1f}"])
        print(f"\nWrote {path}")


if __name__ == "__main__":
    main()
