"""
trend_signals.py — multi-timeframe trend confluence.

Turns the indicator engine into ONE signal per symbol:
    direction ∈ {+1 long, -1 short, 0 flat}
    score     ∈ [-1, +1]   (strength/confidence of the confluence)
    stops     : Supertrend + PSAR levels for the risk manager

Design decisions (deliberate, not incidental):
  1. The four trend-followers (EMA, MACD, Supertrend, PSAR) are correlated,
     so they are AVERAGED into a raw score rather than treated as independent.
  2. ADX GATES the score: in weak-trend/chop (low ADX) the score is scaled
     toward zero, so confluence among lagging indicators can't drag you into
     a whipsaw. This is loss-avoidance at the signal level.
  3. Higher timeframes are weighted more — the 1h trend defines context; the
     15m is only the trigger. A 15m long against a 1h downtrend is penalized.

The `score` is the quantity the backtest estimates mu/sigma on, which the
Merton sizer then turns into position size. Bigger, more reliable edge ->
bigger size; uncertain edge -> LCB shrinkage sizes it down.
"""

from __future__ import annotations
import pandas as pd
import indicators as ind

# timeframe weights (higher TF = more weight = trend context)
DEFAULT_WEIGHTS = {"15min": 0.20, "30min": 0.30, "60min": 0.50}
ADX_THRESHOLD = 25.0      # below this, trend is "weak" -> score scaled down
ENTRY_THRESHOLD = 0.30    # |combined score| must exceed this to take a side


def resample_ohlc(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Resample a base-timeframe OHLC frame up to a higher timeframe."""
    agg = {"open": "first", "high": "max", "low": "min", "close": "last"}
    if "volume" in df.columns:
        agg["volume"] = "sum"
    return df.resample(rule, label="right", closed="right").agg(agg).dropna()


def timeframe_signal(df: pd.DataFrame, adx_threshold: float = ADX_THRESHOLD) -> dict:
    """Compute the ADX-gated confluence score for a single timeframe."""
    if len(df) < 40:  # need enough bars for the slow indicators to warm up
        return {"score": 0.0, "votes": {}, "adx": 0.0, "strength": 0.0,
                "st_stop": None, "psar_stop": None, "insufficient_data": True}

    e_fast, e_slow = ind.ema(df["close"], 9), ind.ema(df["close"], 21)
    m = ind.macd(df["close"])
    a = ind.adx(df)
    s = ind.supertrend(df)
    p = ind.parabolic_sar(df)
    i = -1

    votes = {
        "ema":        _sign(e_fast.iloc[i] - e_slow.iloc[i]),
        "macd":       _sign(m["hist"].iloc[i]),
        "adx_dir":    _sign(a["plus_di"].iloc[i] - a["minus_di"].iloc[i]),
        "supertrend": int(s["direction"].iloc[i]),
        "psar":       int(p["direction"].iloc[i]),
    }
    raw = sum(votes.values()) / len(votes)          # [-1, 1]

    adx_val = float(a["adx"].iloc[i])
    strength = min(adx_val / adx_threshold, 1.0)     # 0..1, ADX gate
    score = raw * strength                            # chop -> shrinks to ~0

    return {
        "score": float(score),
        "votes": votes,
        "adx": adx_val,
        "strength": float(strength),
        "st_stop": float(s["supertrend"].iloc[i]),
        "psar_stop": float(p["sar"].iloc[i]),
        "insufficient_data": False,
    }


def confluence_signal(base_df: pd.DataFrame,
                      weights: dict = None,
                      entry_threshold: float = ENTRY_THRESHOLD,
                      adx_threshold: float = ADX_THRESHOLD) -> dict:
    """
    Full multi-timeframe signal from a single base (15min) OHLC feed.

    Returns:
        direction : +1 long / -1 short / 0 flat
        score     : weighted, ADX-gated confluence in [-1, 1]
        per_tf    : the per-timeframe breakdown (for diagnostics)
        stops     : {'supertrend', 'psar'} from the ENTRY (base) timeframe
    """
    weights = weights or DEFAULT_WEIGHTS
    frames = {"15min": base_df}
    frames["30min"] = resample_ohlc(base_df, "30min")
    frames["60min"] = resample_ohlc(base_df, "60min")

    per_tf, combined, wsum = {}, 0.0, 0.0
    for tf, w in weights.items():
        sig = timeframe_signal(frames[tf], adx_threshold)
        per_tf[tf] = sig
        combined += w * sig["score"]
        wsum += w
    combined = combined / wsum if wsum else 0.0

    if combined >= entry_threshold:
        direction = 1
    elif combined <= -entry_threshold:
        direction = -1
    else:
        direction = 0

    entry = per_tf["15min"]
    return {
        "direction": direction,
        "score": float(combined),
        "per_tf": per_tf,
        "stops": {"supertrend": entry["st_stop"], "psar": entry["psar_stop"]},
    }


def _sign(x) -> int:
    if x > 0:
        return 1
    if x < 0:
        return -1
    return 0
