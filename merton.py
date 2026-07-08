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


def merton_fraction(mu: float, sigma: float, n: int,
                    gamma: float = config.GAMMA,
                    fractional: float = config.FRACTIONAL,
                    z: float = config.LCB_Z,
                    max_fraction: float = config.MAX_FRACTION) -> float:
    """
    Return the fraction of equity to allocate (>= 0). Direction is applied
    separately by the caller. Returns 0 when the edge isn't statistically
    positive or inputs are degenerate.
    """
    if n < 2 or sigma <= 0 or not math.isfinite(mu) or not math.isfinite(sigma):
        return 0.0

    mu_lcb = mu - z * (sigma / math.sqrt(n))   # shrink for small-sample uncertainty
    if mu_lcb <= 0:
        return 0.0                              # edge not credibly positive -> flat

    f = fractional * mu_lcb / (gamma * sigma ** 2)
    return max(0.0, min(f, max_fraction))


def size_position(equity: float, price: float, direction: int,
                  stats: dict, confirmation_mult: float = 1.0,
                  params: dict | None = None) -> dict:
    """
    Convert a signal into an order intent.

    equity           : account equity
    price            : current price
    direction        : +1 long / -1 short / 0 flat
    stats            : {'mu':..., 'sigma':..., 'n':...} for this signal bucket
    confirmation_mult: factor multiplier in [0.5,1.0] (attenuates size)

    Returns {'shares', 'fraction', 'notional', 'mu_lcb', 'direction'}.
    """
    params = params or {}
    if direction == 0 or price <= 0 or equity <= 0:
        return _empty(direction)

    mu = float(stats.get("mu", 0.0))
    sigma = float(stats.get("sigma", 0.0))
    n = int(stats.get("n", 0))

    frac = merton_fraction(
        mu, sigma, n,
        gamma=params.get("gamma", config.GAMMA),
        fractional=params.get("fractional", config.FRACTIONAL),
        z=params.get("z", config.LCB_Z),
        max_fraction=params.get("max_fraction", config.MAX_FRACTION),
    )
    frac *= max(0.0, min(1.0, confirmation_mult))   # factor attenuation
    if frac <= 0:
        return _empty(direction)

    notional = frac * equity
    shares = int(notional // price) * direction     # signed; floor to whole shares
    if shares == 0:
        return _empty(direction)

    # recompute mu_lcb for reporting/diagnostics
    mu_lcb = mu - config.LCB_Z * (sigma / math.sqrt(n)) if n >= 2 and sigma > 0 else 0.0
    return {"shares": shares, "fraction": frac, "notional": abs(shares) * price,
            "mu_lcb": mu_lcb, "direction": direction}


def _empty(direction: int) -> dict:
    return {"shares": 0, "fraction": 0.0, "notional": 0.0,
            "mu_lcb": 0.0, "direction": direction}
