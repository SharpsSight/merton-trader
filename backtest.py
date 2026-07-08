"""
backtest.py — event-driven backtest, no look-ahead, pessimistic fills.

Purpose is NOT a pretty equity curve. It is SAMPLE COMPRESSION: run the exact
signal logic over history to estimate, per signal-score bucket, the per-trade
return distribution (mu, sigma, n) that the Merton sizer needs. Without this the
sizer has no edge estimate and correctly sizes everything to zero.

No look-ahead:
  - signal at bar i uses only data through i
  - entry fills at bar i+1 open
  - higher-timeframe context uses the last CLOSED higher-TF bar (merge_asof back)
Pessimistic fills: SLIPPAGE_BPS + SPREAD_BPS applied adversely on entry AND exit.
"""

from __future__ import annotations
import numpy as np
import pandas as pd
import config
import indicators as ind


# --------------------------------------------------------------------------
# Vectorised gated score per timeframe (same indicator math as live signals)
# --------------------------------------------------------------------------
def _score_series(df: pd.DataFrame, adx_threshold: float = config.ADX_THRESHOLD
                  ) -> pd.DataFrame:
    e9, e21 = ind.ema(df["close"], 9), ind.ema(df["close"], 21)
    m = ind.macd(df["close"])
    a = ind.adx(df)
    s = ind.supertrend(df)
    p = ind.parabolic_sar(df)

    votes = (
        np.sign(e9 - e21)
        + np.sign(m["hist"])
        + np.sign(a["plus_di"] - a["minus_di"])
        + s["direction"]
        + p["direction"]
    ) / 5.0
    strength = (a["adx"] / adx_threshold).clip(upper=1.0).fillna(0.0)
    score = (votes * strength).fillna(0.0)
    return pd.DataFrame({"score": score, "st": s["supertrend"]})


def _resample(df, rule):
    agg = {"open": "first", "high": "max", "low": "min", "close": "last"}
    if "volume" in df:
        agg["volume"] = "sum"
    return df.resample(rule, label="right", closed="right").agg(agg).dropna()


def build_signal_frame(df15: pd.DataFrame,
                       weights: dict = None) -> pd.DataFrame:
    """Per-base-bar combined confluence score with no look-ahead. Base timeframe
    is the FIRST key in the weights; the rest are resampled up from it."""
    weights = weights or config.TIMEFRAME_WEIGHTS
    tfs = list(weights.keys())
    base_tf = tfs[0]

    base = _score_series(df15)
    out = df15.join(base[["st"]])
    combined = weights[base_tf] * base["score"].fillna(0)
    wsum = weights[base_tf]

    for tf in tfs[1:]:
        tf_df = _resample(df15, tf)
        sc = _score_series(tf_df)["score"]
        # align last CLOSED higher-TF bar onto each base timestamp (no look-ahead)
        aligned = sc.reindex(out.index, method="ffill").fillna(0)
        combined = combined + weights[tf] * aligned
        wsum += weights[tf]

    out["score"] = combined / wsum if wsum else 0.0
    return out


# --------------------------------------------------------------------------
# Backtest
# --------------------------------------------------------------------------
def run_backtest(df15: pd.DataFrame,
                 entry_threshold: float = config.ENTRY_THRESHOLD) -> dict:
    sf = build_signal_frame(df15)
    cost = (config.SLIPPAGE_BPS + config.SPREAD_BPS) / 1e4  # per side

    trades = []
    pos = 0            # +1/-1/0
    entry_px = entry_score = 0.0
    stop = None
    o = sf["open"].values
    h = sf["high"].values
    lo = sf["low"].values
    score = sf["score"].values
    st = sf["st"].values
    n = len(sf)

    for i in range(1, n - 1):
        if pos == 0:
            if abs(score[i]) >= entry_threshold:
                pos = 1 if score[i] > 0 else -1
                entry_px = o[i + 1] * (1 + pos * cost)     # fill next open, adverse
                entry_score = score[i]
                stop = st[i]                                # supertrend stop at entry
        else:
            exit_now, exit_px = False, None
            # intrabar stop breach
            if stop is not None and not np.isnan(stop):
                if pos == 1 and lo[i] <= stop:
                    exit_now, exit_px = True, stop * (1 - cost)
                elif pos == -1 and h[i] >= stop:
                    exit_now, exit_px = True, stop * (1 + cost)
            # signal flip / decay -> exit next open
            if not exit_now and (np.sign(score[i]) != pos or abs(score[i]) < entry_threshold):
                exit_now, exit_px = True, o[i + 1] * (1 - pos * cost)

            if exit_now:
                ret = pos * (exit_px - entry_px) / entry_px
                trades.append({"entry_score": entry_score, "direction": pos,
                               "ret": ret})
                pos = 0
                stop = None

    return {"trades": trades, "stats": _bucket_stats(trades),
            "metrics": _metrics(trades)}


def _bucket(score: float) -> str:
    """Bucket by |score| band (direction handled separately)."""
    a = abs(score)
    if a < 0.30:
        return "weak"
    if a < 0.50:
        return "b30_50"
    if a < 0.70:
        return "b50_70"
    return "b70_100"


def _bucket_stats(trades: list) -> dict:
    """Per-bucket mu/sigma/n — the payload the Merton sizer consumes."""
    buckets = {}
    for t in trades:
        buckets.setdefault(_bucket(t["entry_score"]), []).append(t["ret"])
    stats = {}
    for k, rets in buckets.items():
        arr = np.array(rets)
        stats[k] = {"mu": float(arr.mean()),
                    "sigma": float(arr.std(ddof=1)) if len(arr) > 1 else 0.0,
                    "n": int(len(arr))}
    return stats


def _metrics(trades: list) -> dict:
    if not trades:
        return {"n_trades": 0, "total_ret": 0.0, "win_rate": 0.0, "avg_ret": 0.0}
    rets = np.array([t["ret"] for t in trades])
    return {"n_trades": len(trades),
            "total_ret": float(rets.sum()),
            "win_rate": float((rets > 0).mean()),
            "avg_ret": float(rets.mean())}
