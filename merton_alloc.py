"""
Concurrent (portfolio-level) Merton allocation — drop-in addition to merton.py.

Sizes ALL live signals in one bar JOINTLY instead of first-come-first-served.
Discipline is preserved exactly: a name gets nonzero weight ONLY if it clears
MIN_BUCKET_N, MIN_BUCKET_T, and mu_lcb > 0. The only thing that changes vs the
per-symbol sizer is how the risk budget is DISTRIBUTED across names that already
qualify — which is the "allocation within the portfolio" ask.
"""
from __future__ import annotations
import math
import config

# reuse the exact same gated fraction the live sizer uses, but UNCLIPPED so the
# joint normalizer can see the true desired weights before the per-name cap.
def _desired_fraction(mu, sigma, n, symbol_vol, gamma, fractional, z):
    if (n < 2 or sigma <= 0 or symbol_vol <= 0
            or not math.isfinite(mu) or not math.isfinite(symbol_vol)):
        return 0.0, 0.0, 0.0
    se = sigma / math.sqrt(n)
    t = mu / se if se > 0 else 0.0
    if n < config.MIN_BUCKET_N or t < config.MIN_BUCKET_T:   # SAME gate as live
        return 0.0, t, 0.0
    mu_lcb = mu - z * se
    if mu_lcb <= 0:                                          # SAME gate as live
        return 0.0, t, mu_lcb
    g = fractional * mu_lcb / (gamma * symbol_vol ** 2)      # UNCLIPPED desired
    return max(0.0, g), t, mu_lcb


def allocate_book(candidates, equity, *, gross_target=None, gamma=None,
                  fractional=None, z=None, max_fraction=None,
                  concentration=1.0, enforce_gates=True):
    """
    candidates: list of dicts {symbol,direction,price,mu,sigma,n,symbol_vol,confirmation}
    Returns per-name allocations. Budgeting rules:
      1. desired weight g_i from gated Merton (0 unless it clears every gate)
      2. concentration exponent: g_i <- g_i**concentration  (1.0 = pure Merton;
         >1 tilts harder toward the strongest edges; explicit, non-theoretical dial)
      3. if sum(g) > gross_target: scale ALL down proportionally (portfolio budget)
         if sum(g) <= gross_target: leave as-is (never inflate to fill the budget)
      4. per-name MAX_FRACTION cap applied last
    """
    gross_target = config.MAX_GROSS_EXPOSURE if gross_target is None else gross_target
    gamma = config.GAMMA if gamma is None else gamma
    fractional = config.FRACTIONAL if fractional is None else fractional
    z = config.LCB_Z if z is None else z
    max_fraction = config.MAX_FRACTION if max_fraction is None else max_fraction

    rows = []
    for c in candidates:
        conf = max(0.0, min(1.0, c.get("confirmation", 1.0)))
        if enforce_gates:
            g, t, mu_lcb = _desired_fraction(c["mu"], c["sigma"], c["n"],
                                             c["symbol_vol"], gamma, fractional, z)
        else:  # research/shadow only: rank on raw LCB with NO significance gate
            se = c["sigma"]/math.sqrt(c["n"]) if c["n"] >= 2 and c["sigma"] > 0 else 0.0
            t = c["mu"]/se if se > 0 else 0.0
            mu_lcb = c["mu"] - z*se
            g = max(0.0, fractional*mu_lcb/(gamma*c["symbol_vol"]**2)) if (se>0 and c["symbol_vol"]>0) else 0.0
        g *= conf
        if concentration != 1.0 and g > 0:
            g = g ** concentration
        rows.append({**c, "g": g, "bucket_t": t, "mu_lcb": mu_lcb})

    G = sum(r["g"] for r in rows)
    scale = (gross_target / G) if G > gross_target and G > 0 else 1.0

    out = []
    for r in rows:
        w = min(r["g"] * scale, max_fraction)
        notional = w * equity
        shares = int(notional // r["price"]) * r["direction"] if r["price"] > 0 else 0
        out.append({"symbol": r["symbol"], "direction": r["direction"],
                    "fraction": w, "shares": shares, "notional": abs(shares)*r["price"],
                    "bucket_t": r["bucket_t"], "mu_lcb": r["mu_lcb"],
                    "capped": r["g"]*scale > max_fraction,
                    "gate": "alloc" if w > 0 else "flat"})
    return out


# ─────────────────────────────────────────────────────────────────────────────
# CONSTANT-VOLATILITY TARGETING (portfolio-level gross scaling)
#
# Takes the per-name weights from allocate_book and scales the WHOLE book by a
# single factor k so the portfolio's estimated realized vol hits VOL_TARGET.
# Calm tape -> lever up (toward LEVERAGE_CAP). Turbulent tape -> lever down.
#
# Constant-correlation model: sigma_p^2 = sum_i w_i^2 s_i^2
#                                       + rho * sum_{i!=j} w_i w_j s_i s_j
# where s_i is the name's vol over the holding horizon (same symbol_vol the live
# sizer already computes). rho is a single tunable average correlation.
#
# IMPORTANT: this is a RISK controller, not a return generator. It holds vol
# constant; it does nothing to the sign of returns. On zero-edge weights the book
# is zero, sigma_p is zero, and k*0 is still 0 -> it trades nothing. It only
# produces exposure once allocate_book produces nonzero (i.e. gated) weights.
# ─────────────────────────────────────────────────────────────────────────────
import math as _math

# ~2-hour holding blocks per year, for annual<->horizon vol conversion.
# 252 trading days * (6.5h RTH / 2h hold) ~= 252 * 3.25 = 819 blocks/yr.
_BLOCKS_PER_YEAR = 819.0


def _portfolio_sigma(weights, vols, rho):
    """weights signed (long +, short -); vols >= 0; both aligned lists."""
    var = 0.0
    n = len(weights)
    for i in range(n):
        var += (weights[i] * vols[i]) ** 2
    for i in range(n):
        for j in range(n):
            if i != j:
                var += rho * weights[i] * vols[i] * weights[j] * vols[j]
    return _math.sqrt(max(var, 0.0))


def vol_target_scale(allocations, symbol_vols, *, vol_target_annual,
                     leverage_cap, rho, current_gross=None):
    """
    allocations : output rows from allocate_book (need 'fraction','direction')
    symbol_vols : dict symbol -> horizon vol (the same s_i used in sizing)
    Returns (k, sigma_before, sigma_after_capped, gross_after).
    k is the multiplier to apply to every fraction. Gross is hard-capped at
    leverage_cap so vol-targeting can NEVER breach the leverage ceiling in a
    low-vol regime (the one way constant-vol targeting can hurt you).
    """
    active = [a for a in allocations if a["fraction"] > 0]
    if not active:
        return 0.0, 0.0, 0.0, 0.0
    w = [a["fraction"] * a["direction"] for a in active]
    s = [max(symbol_vols.get(a["symbol"], 0.0), 1e-9) for a in active]
    sigma_p = _portfolio_sigma(w, s, rho)
    if sigma_p <= 0:
        return 0.0, 0.0, 0.0, 0.0
    target_horizon = vol_target_annual / _math.sqrt(_BLOCKS_PER_YEAR)
    k = target_horizon / sigma_p
    # cap so gross exposure never exceeds the leverage ceiling
    base_gross = sum(a["fraction"] for a in active)
    if base_gross * k > leverage_cap:
        k = leverage_cap / base_gross
    gross_after = base_gross * k
    return k, sigma_p, sigma_p * k, gross_after
