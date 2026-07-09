#!/usr/bin/env python3
"""
research_news.py — does abnormal news arrival predict anything?

RESEARCH ONLY. Writes no signal_stats.json, touches no broker, changes nothing
the live runner reads. It answers one question and then gets out of the way.

    python research_news.py --selftest              # validate the machinery first
    python research_news.py --days 120 --bootstrap 1000

METHOD
------
We test a grid of 64 configurations (news window x shock multiple x reaction
threshold x holding horizon x momentum/reversal). Reporting the best cell of 64
without correction is precisely the error that produced TSLA's "edge": the
maximum of many correlated statistics is large under the null by construction.

So the bootstrap is over max_cells(t), not over any single cell. Trade returns
are sign-flipped by EXIT DATE, one Rademacher draw shared across every symbol
and every cell, which preserves both cross-sectional correlation and the
correlation between overlapping configurations. The 95th percentile of that
maximum is the bar. Nothing below it means anything.

CONTROLS
--------
--selftest runs two synthetic datasets before any real data is touched:

  NEGATIVE: random-walk prices, random news. The harness must NOT find edge.
  POSITIVE: prices constructed so the reaction bar genuinely predicts the next
            H bars. The harness MUST find it, at high t, in the momentum cells.

A harness that fails the positive control cannot distinguish "no edge" from
"broken code," and every null result it produces is worthless.
"""

import os
import sys
import csv
import argparse
from datetime import datetime, timedelta, timezone

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import numpy as np
import pandas as pd

import config
import news_signal as ns

BASELINE_BARS = 390          # ~5 RTH sessions of 5-minute bars
MIN_COUNT = 1.0

GRID = {
    "window":       [1, 3],
    "shock_mult":   [2.0, 4.0],
    "react_thresh": [0.000, 0.002],
    "horizon":      [3, 6, 12, 24],
    "mode":         [1, -1],
}


def cells():
    from itertools import product
    keys = list(GRID)
    for combo in product(*(GRID[k] for k in keys)):
        yield dict(zip(keys, combo))


def cell_name(c):
    m = "mom" if c["mode"] == 1 else "rev"
    return (f"w{c['window']}_x{c['shock_mult']:g}_r{c['react_thresh']*1e4:.0f}bp"
            f"_h{c['horizon']}_{m}")


# ---------------------------------------------------------------------------
def _key(*names):
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return None


def _news_records(resp):
    """Normalise NewsSet | dict | list into a flat list of items."""
    if hasattr(resp, "data"):
        resp = resp.data
    items = []
    if isinstance(resp, dict):
        for v in resp.values():
            items.extend(v if isinstance(v, list) else [v])
    elif isinstance(resp, list):
        items = list(resp)
    return items


def _field(item, name):
    return item.get(name) if isinstance(item, dict) else getattr(item, name, None)


def fetch_news(nc, symbols, start, end, slice_days=10):
    """{symbol: sorted np.array of created_at} — paginate by time slice."""
    from alpaca.data.requests import NewsRequest
    out = {s: [] for s in symbols}
    cur = start
    while cur < end:
        stop = min(cur + timedelta(days=slice_days), end)
        req = NewsRequest(symbols=",".join(symbols), start=cur, end=stop,
                          limit=50, include_content=False,
                          exclude_contentless=True, sort="asc")
        try:
            recs = _news_records(nc.get_news(req))
        except Exception as e:
            print(f"  news fetch {cur.date()}..{stop.date()} failed: {e}")
            recs = []
        for it in recs:
            ts = _field(it, "created_at")
            syms = _field(it, "symbols") or []
            if ts is None:
                continue
            for s in syms:
                if s in out:
                    out[s].append(pd.Timestamp(ts).tz_convert("UTC"))
        print(f"  {cur.date()} .. {stop.date()}: {len(recs):5d} articles")
        cur = stop
    return {s: np.sort(np.array(v, dtype="datetime64[ns]")) for s, v in out.items()}


# ---------------------------------------------------------------------------
def build_cell_trades(frames, news, cost_per_side, bar_minutes=5):
    """{cell_name: (rets, gross, exit_date_index)} pooled across symbols."""
    per_cell = {cell_name(c): {"rets": [], "gross": [], "dates": [], "cfg": c}
                for c in cells()}
    bar_td = np.timedelta64(bar_minutes, "m")

    for sym, df in frames.items():
        if df is None or len(df) < BASELINE_BARS + 100:
            continue
        idx = df.index
        starts = idx.values.astype("datetime64[ns]")
        closes = starts + bar_td                       # bar timestamps are bar-START
        nt = news.get(sym)
        if nt is None or len(nt) == 0:
            continue
        bar_ix = ns.actionable_bar_index(closes, nt)
        counts = ns.arrival_counts(len(df), bar_ix)
        if counts.sum() < 5:
            continue

        for c in cells():
            trades = ns.extract_events(
                df, counts, window=c["window"], baseline_bars=BASELINE_BARS,
                min_count=MIN_COUNT, shock_mult=c["shock_mult"],
                react_thresh=c["react_thresh"], horizon=c["horizon"],
                mode=c["mode"], cost_per_side=cost_per_side)
            b = per_cell[cell_name(c)]
            for t in trades:
                b["rets"].append(t["ret"])
                b["gross"].append(t["gross_ret"])
                b["dates"].append(pd.Timestamp(t["exit_time"]).normalize())

    return per_cell


def pack(per_cell):
    """Convert to arrays + a shared date index for the block bootstrap."""
    all_dates = sorted({d for v in per_cell.values() for d in v["dates"]})
    dmap = {d: i for i, d in enumerate(all_dates)}
    packed = {}
    for name, v in per_cell.items():
        if len(v["rets"]) < 30:
            continue
        packed[name] = {
            "rets": np.asarray(v["rets"]),
            "gross": np.asarray(v["gross"]),
            "ix": np.asarray([dmap[d] for d in v["dates"]]),
            "cfg": v["cfg"],
        }
    return packed, len(all_dates)


def tstat(r):
    n = len(r)
    if n < 2:
        return 0.0
    s = r.std(ddof=1)
    return float(r.mean() / (s / np.sqrt(n))) if s > 0 else 0.0


def bootstrap_max_t(packed, n_dates, iters, cost_rt=0.0, seed=0):
    """Null distribution of max_cells(t) under H0: no GROSS predictive power.

    The sign flip must be applied to the GROSS return, with the transaction cost
    subtracted afterwards. Flipping the NET return also flips the cost, which is
    a deterministic -cost_rt drift present in every trade. A resample that flips
    most dates positive then manufactures a +cost_rt mean, and with n in the
    thousands that is a t-statistic of +20 or more. The null inflates, the 95th
    percentile explodes, and the test loses all power.

    Verified: on a 50-symbol random walk at 7bps/side, the old form gave a null
    95th percentile of +11.5. It should be near +3.
    """
    rng = np.random.default_rng(seed)
    arrs = [(v["gross"], v["ix"]) for v in packed.values()]
    out = np.empty(iters)
    for b in range(iters):
        f = rng.choice([-1.0, 1.0], size=n_dates)
        out[b] = max(tstat(g * f[ix] - cost_rt) for g, ix in arrs)
    return out


def report(packed, n_dates, iters, cost_rt, label=""):
    if not packed:
        print("  no cell produced >= 30 trades. Nothing to test.")
        return None

    rows = []
    for name, v in packed.items():
        st = ns.pooled_stats(v["rets"])
        rows.append((name, st["n"], st["mu"], v["gross"].mean(), st["t"]))
    rows.sort(key=lambda r: -r[4])

    print(f"\n  {'cell':28s} {'n':>6s} {'net mu':>10s} {'gross mu':>10s} {'t':>8s}")
    for name, n, mu, g, t in rows[:8]:
        print(f"  {name:28s} {n:6d} {mu:+10.5f} {g:+10.5f} {t:+8.2f}")
    if len(rows) > 8:
        print(f"  ... {len(rows)-8} more cells")

    obs = rows[0][4]
    null = bootstrap_max_t(packed, n_dates, iters, cost_rt=cost_rt)
    q95 = float(np.quantile(null, 1 - config.SELECTION_ALPHA))
    p = float((null >= obs).mean())

    print(f"\n  === family-wise null over {len(packed)} cells, "
          f"{iters} date-blocked resamples ===")
    print(f"  observed max t : {obs:+.3f}   ({rows[0][0]})")
    print(f"  null median    : {np.median(null):+.3f}")
    print(f"  null 95th pct  : {q95:+.3f}   <- the honest bar")
    print(f"  family-wise p  : {p:.4f}")
    print(f"  round-trip cost charged: {cost_rt*1e4:.1f} bps")
    if obs < q95:
        print(f"  VERDICT{label}: best cell is INSIDE the noise band. No edge.")
    else:
        print(f"  VERDICT{label}: best cell SURVIVES multiplicity correction.")
    return {"obs": obs, "q95": q95, "p": p, "best": rows[0][0]}


# ---------------------------------------------------------------------------
def synth(kind, seed=1, n_days=120, bars=78):
    """Synthetic bars + news. kind='null' or 'planted'."""
    rng = np.random.default_rng(seed)
    n = n_days * bars
    idx = pd.date_range("2025-01-02 09:30", periods=n, freq="5min", tz="UTC")
    sigma = 0.0012
    r = rng.normal(0, sigma, n)

    # news on ~1.5% of bars, clustered
    news_bar = np.sort(rng.choice(n - 40, size=int(n * 0.015), replace=False))

    if kind == "planted":
        # after each news bar i: reaction at i, then genuine drift over i+1..i+12
        for i in news_bar:
            if i < 5 or i > n - 40:
                continue
            react = rng.normal(0, 6 * sigma)
            r[i] += react
            drift = np.sign(react) * 3.0 * sigma        # real, exploitable drift
            r[i + 1:i + 13] += drift

    close = 100 * np.exp(np.cumsum(r))
    op = np.concatenate([[100.0], close[:-1]])
    df = pd.DataFrame({"open": op, "high": np.maximum(op, close) * 1.001,
                       "low": np.minimum(op, close) * 0.999, "close": close,
                       "volume": 1e6}, index=idx)
    news_times = idx.values[news_bar] + np.timedelta64(1, "m")
    return {"SYNTH": df}, {"SYNTH": np.sort(news_times.astype("datetime64[ns]"))}


def selftest(iters):
    cost = (config.SLIPPAGE_BPS + config.SPREAD_BPS) / 1e4
    for kind, expect in [("null", "NO edge"), ("planted", "edge FOUND")]:
        print(f"\n{'='*74}\nSELFTEST [{kind}] — harness must report: {expect}\n{'='*74}")
        frames, news = synth(kind)
        pc = build_cell_trades(frames, news, cost)
        packed, nd = pack(pc)
        res = report(packed, nd, iters, 2 * cost, label=f" [{kind}]")
        if res is None:
            print("  *** SELFTEST INCONCLUSIVE ***"); return False
        ok = (res["p"] > 0.05) if kind == "null" else (res["p"] < 0.05)
        print(f"  -> {'PASS' if ok else '*** FAIL ***'}")
        if not ok:
            return False
    print("\nBoth controls passed. The harness can detect edge and does not "
          "hallucinate it.\n")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--bootstrap", type=int, default=1000)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--extra-spread-bps", type=float, default=3.0,
                    help="added half-spread per side; news bars are wide")
    args = ap.parse_args()

    if args.selftest:
        sys.exit(0 if selftest(args.bootstrap) else 1)

    if not selftest(min(args.bootstrap, 400)):
        print("Controls failed. Refusing to run on real data.")
        sys.exit(1)

    api_key = _key("ALPACA_API_KEY", "APCA_API_KEY_ID")
    secret = _key("ALPACA_SECRET_KEY", "APCA_API_SECRET_KEY")
    if not api_key or not secret:
        print("Credentials not found."); sys.exit(1)

    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.historical.news import NewsClient
    import data_feed as feed

    dc = StockHistoricalDataClient(api_key, secret)
    nc = NewsClient(api_key, secret)

    universe = feed.select_universe(dc, config.CANDIDATE_POOL, config.UNIVERSE_SIZE) \
        or config.UNIVERSE
    print(f"Universe ({len(universe)}): {', '.join(universe)}\n")

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)
    print(f"Fetching bars {start.date()} .. {end.date()} (RTH only)")
    frames = feed.fetch_bars_batch(dc, universe, args.days)
    print(f"  {len(frames)} symbols, "
          f"{sum(len(d) for d in frames.values()):,} bars\n")

    print("Fetching news")
    news = fetch_news(nc, universe, start, end)
    tot = sum(len(v) for v in news.values())
    print(f"  {tot:,} symbol-headline pairs "
          f"({np.median([len(v) for v in news.values()]):.0f} median per symbol)\n")
    if tot < 500:
        print("Too few headlines to test anything. Increase --days."); sys.exit(1)

    cost = (config.SLIPPAGE_BPS + config.SPREAD_BPS + args.extra_spread_bps) / 1e4
    print(f"Cost charged: {cost*1e4:.1f} bps/side "
          f"({config.SLIPPAGE_BPS}+{config.SPREAD_BPS}+{args.extra_spread_bps} extra "
          f"for wide news-bar spreads)")

    per_cell = build_cell_trades(frames, news, cost)
    packed, n_dates = pack(per_cell)
    print(f"\n{len(packed)}/{len(list(cells()))} cells produced >= 30 trades "
          f"across {n_dates} exit dates")
    res = report(packed, n_dates, args.bootstrap, 2 * cost)

    if res:
        path = os.path.join(config.DATA_DIR, "news_research.csv")
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["cell", "n", "net_mu", "gross_mu", "t"])
            for name, v in packed.items():
                st = ns.pooled_stats(v["rets"])
                w.writerow([name, st["n"], f"{st['mu']:.6f}",
                            f"{v['gross'].mean():.6f}", f"{st['t']:.3f}"])
        print(f"\nWrote {path}")


if __name__ == "__main__":
    main()
