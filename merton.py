"""
merton.py — Merton-share position sizing with lower-confidence-bound shrinkage.

The Merton optimal risky-asset fraction (intraday, r~0, CRRA utility):
    f* = mu / (gamma * sigma^2)

We do NOT use the raw sample mean mu_hat. We use its LOWER confidence bound:
    mu_lcb = mu_hat - z * (sigma / sqrt(n))
so that small samples (uncertain edge) shrink the size, and a signal whose edge
isn't statistically positive gets ZERO size. On random-walk data mu_hat ~ 0 and
mu_lcb < 0 -> size 0, which is the correct "don't trade noise" behavior.

`mu`, `sigma` are per-trade return statistics for a signal bucket, estimated by
the backtest. `n` is the sample count in that bucket.
"""

from __future__ import annotations
import math
import config


def merton_fraction(mu: float, bucket_sigma: float, n: int, symbol_vol: float,
                    gamma: float = config.GAMMA,
                    fractional: float = config.FRACTIONAL,
                    z: float = config.LCB_Z,
                    max_fraction: float = config.MAX_FRACTION) -> float:
    """
    Fraction of equity to allocate (>= 0). Two separate roles:
      - EDGE + confidence: bucket mu, bucket_sigma, n -> mu_lcb (do we trade?)
      - RISK scaling:      symbol_vol -> inverse-variance sizing (how much?)
    So two stocks in the same signal bucket but different volatility get
    DIFFERENT sizes: the more volatile one gets less. Returns 0 when the edge
    isn't statistically positive.
    """
    if (n < 2 or bucket_sigma <= 0 or symbol_vol <= 0
            or not math.isfinite(mu) or not math.isfinite(symbol_vol)):
        return 0.0

    # --- credibility floor, BEFORE the LCB ---------------------------------
    # The LCB alone is a poor gate here: f = fractional*mu_lcb/(gamma*vol^2),
    # which for vol=0.013, gamma=3, fractional=0.25 is f = 493 * mu_lcb. The
    # cap binds at mu_lcb = 2.03 bps, while SE(mu) on a real bucket runs 2.6-9.8
    # bps. So mu_lcb crossing zero by one standard error takes you from flat to
    # ~max leverage. Require the edge to be a real effect first.
    se = bucket_sigma / math.sqrt(n)
    t = mu / se if se > 0 else 0.0
    if n < config.MIN_BUCKET_N or t < config.MIN_BUCKET_T:
        return 0.0

    mu_lcb = mu - z * se                              # edge uncertainty shrink
    if mu_lcb <= 0:
        return 0.0                                     # edge not credible -> flat

    f = fractional * mu_lcb / (gamma * symbol_vol ** 2)  # risk-adjusted by asset vol
    return max(0.0, min(f, max_fraction))


def size_position(equity: float, price: float, direction: int,
                  stats: dict, symbol_vol: float, confirmation_mult: float = 1.0,
                  params: dict | None = None) -> dict:
    """
    Convert a signal into an order intent, risk-adjusted by the symbol's own
    volatility.

    stats       : {'mu','sigma','n'} for this signal bucket (edge estimate)
    symbol_vol  : this symbol's realized return volatility over the holding
                  horizon (the risk denominator)
    """
    params = params or {}
    if direction == 0 or price <= 0 or equity <= 0 or symbol_vol <= 0:
        return _empty(direction)

    mu = float(stats.get("mu", 0.0))
    bucket_sigma = float(stats.get("sigma", 0.0))
    n = int(stats.get("n", 0))

    frac = merton_fraction(
        mu, bucket_sigma, n, symbol_vol,
        gamma=params.get("gamma", config.GAMMA),
        fractional=params.get("fractional", config.FRACTIONAL),
        z=params.get("z", config.LCB_Z),
        max_fraction=params.get("max_fraction", config.MAX_FRACTION),
    )
    frac *= max(0.0, min(1.0, confirmation_mult))
    if frac <= 0:
        return _empty(direction)

    notional = frac * equity
    shares = int(notional // price) * direction
    if shares == 0:
        return _empty(direction)

    se = bucket_sigma / math.sqrt(n) if n >= 2 and bucket_sigma > 0 else 0.0
    mu_lcb = mu - config.LCB_Z * se if se > 0 else 0.0
    return {"shares": shares, "fraction": frac, "notional": abs(shares) * price,
            "mu_lcb": mu_lcb, "bucket_t": (mu / se) if se > 0 else 0.0,
            "direction": direction}


def _empty(direction: int) -> dict:
    return {"shares": 0, "fraction": 0.0, "notional": 0.0,
            "mu_lcb": 0.0, "bucket_t": 0.0, "direction": direction}
