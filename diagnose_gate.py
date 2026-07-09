#!/usr/bin/env python3
"""
diagnose_gate.py — why is would_size 0?

Reads the signal_stats.json the runner is actually using and walks the exact
early-return path inside merton.merton_fraction() for each bucket. No broker
connection, no data fetch. Run it on the box, in the same working directory the
runner uses.

    python diagnose_gate.py
    python diagnose_gate.py --vol 0.013     # override the symbol vol used for f

If this prints a nonzero fraction for a bucket and the runner still shows
would_size=0, the problem is upstream (tradeable set, or the symbol never
produced a signal in that bucket) -- not the sizer.
"""

import sys
import json
import math
import argparse

import config


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vol", type=float, default=0.013,
                    help="symbol_vol to evaluate f at (from the [obs] log lines)")
    ap.add_argument("--path", default=config.SIGNAL_STATS_PATH)
    args = ap.parse_args()

    try:
        payload = json.load(open(args.path))
    except FileNotFoundError:
        print(f"{args.path} NOT FOUND.")
        print("  -> live_paper.py would be in OBSERVE mode: it logs signals and")
        print("     places no orders, ever. Run run_backtest.py first.")
        print(f"  -> config.DATA_DIR = {config.DATA_DIR!r}. If that is not a mounted")
        print("     volume, this file is destroyed on every redeploy.")
        sys.exit(1)

    universe = payload.get("universe", [])
    tradeable = payload.get("tradeable", [])
    print(f"file           : {args.path}")
    print(f"generated_at   : {payload.get('generated_at')}")
    print(f"backtest days  : {payload.get('days')}")
    print(f"pooled trades  : {payload.get('n_trades')}")
    print(f"universe       : {len(universe)} symbols")
    print(f"tradeable      : {len(tradeable)} -> {', '.join(tradeable) or '(NONE)'}")
    if not tradeable:
        print("\n  *** tradeable is EMPTY. No symbol can ENTER, regardless of sizing.")
        print("      Held positions still exit normally.")

    cost_rt = 2 * (config.SLIPPAGE_BPS + config.SPREAD_BPS) / 1e4
    print(f"\nassumed round-trip cost: {cost_rt*1e4:.1f} bps")
    print(f"evaluating f at symbol_vol = {args.vol}\n")
    print("=" * 78)

    denom = config.GAMMA * args.vol ** 2
    slope = config.FRACTIONAL / denom
    cap_at = config.MAX_FRACTION / slope
    print(f"f = FRACTIONAL * mu_lcb / (GAMMA * vol^2) = {slope:.1f} * mu_lcb")
    print(f"MAX_FRACTION={config.MAX_FRACTION} binds at mu_lcb = {cap_at*1e4:.2f} bps")
    print("=" * 78)

    buckets = payload.get("buckets", {})
    if not buckets:
        print("\nNo buckets in payload. Sizer has nothing to read -> everything is 0.")
        sys.exit(1)

    any_sizes = False
    for k in sorted(buckets):
        v = buckets[k]
        mu, sig, n = float(v["mu"]), float(v["sigma"]), int(v["n"])
        print(f"\n[{k}]  mu={mu:+.5f}  sigma={sig:.5f}  n={n}")

        if n < 2 or sig <= 0:
            print("   -> 0.0   BLOCKED: n < 2 or sigma <= 0")
            continue

        se = sig / math.sqrt(n)
        t = mu / se
        gross = mu + cost_rt
        print(f"   SE(mu)      = {se:.6f}  ({se*1e4:.2f} bps)")
        print(f"   t = mu/SE   = {t:+.3f}")
        print(f"   gross mu    = {gross:+.5f}   (mu + round-trip cost)")

        if n < config.MIN_BUCKET_N:
            print(f"   -> 0.0   BLOCKED at merton.py: n={n} < MIN_BUCKET_N={config.MIN_BUCKET_N}")
            continue
        if t < config.MIN_BUCKET_T:
            need = config.MIN_BUCKET_T * se
            print(f"   -> 0.0   BLOCKED at merton.py: t={t:+.3f} < MIN_BUCKET_T={config.MIN_BUCKET_T}")
            print(f"            would need mu >= {need:+.5f} (have {mu:+.5f}); "
                  f"even at ZERO cost gross mu is {gross:+.5f}")
            if gross < need:
                print(f"            *** UNREACHABLE even at zero transaction cost ***")
            continue

        mu_lcb = mu - config.LCB_Z * se
        print(f"   mu_lcb      = {mu_lcb:+.6f}   (mu - {config.LCB_Z}*SE)")
        if mu_lcb <= 0:
            cmax = (gross - config.LCB_Z * se) / 2
            print(f"   -> 0.0   BLOCKED at merton.py: mu_lcb <= 0")
            print(f"            max cost/side that would still size: "
                  f"{(f'{cmax*1e4:.2f} bps' if cmax > 0 else 'IMPOSSIBLE (zero cost is not enough)')}")
            continue

        f = max(0.0, min(config.FRACTIONAL * mu_lcb / denom, config.MAX_FRACTION))
        pinned = " (PINNED AT MAX_FRACTION -- Merton is not differentiating)" \
                 if f >= config.MAX_FRACTION - 1e-9 else ""
        print(f"   -> f = {f:.4f}  = {f*100:.2f}% of equity{pinned}")
        any_sizes = True

    print("\n" + "=" * 78)
    if not any_sizes:
        print("VERDICT: no bucket sizes. The runner will place ZERO entries today.")
        print("         This is the sizer working as designed, not a connection fault.")
    else:
        print("VERDICT: at least one bucket sizes. If the runner still shows")
        print("         would_size=0, the symbol is not in `tradeable`, or its live")
        print("         score never landed in a sizing bucket.")

    ps = payload.get("per_symbol", {})
    if ps:
        rows = sorted(ps.items(), key=lambda kv: -kv[1].get("edge_ratio", 0))[:8]
        print("\nTop symbols by edge_ratio (ratio = t/sqrt(n) - LCB_Z/sqrt(n)):")
        print(f"  {'sym':6s} {'n':>5s} {'mu':>9s} {'sigma':>8s} {'t':>7s} {'ratio':>8s}  worthy")
        for s, d in rows:
            print(f"  {s:6s} {d.get('n_trades',0):5d} {d.get('mu',0):+9.4f} "
                  f"{d.get('sigma',0):8.4f} {d.get('t_stat',0):+7.2f} "
                  f"{d.get('edge_ratio',0):+8.4f}  {d.get('worthy')}")
        print(f"\n  MIN_EDGE_RATIO = {config.MIN_EDGE_RATIO} "
              f"(this is a bar on the MAX of {len(ps)} correlated statistics)")


if __name__ == "__main__":
    main()
