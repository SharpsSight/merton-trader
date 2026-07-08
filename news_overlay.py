"""
news_overlay.py — news as a RISK OVERLAY (not a directional alpha signal).

Two detectors, because macro shocks and company news arrive differently:

  1. Per-symbol news (Alpaca/Benzinga feed): velocity (how much news, how fast)
     + high-impact keyword hits (halt, SEC, guidance, M&A, recall, tariff...).
  2. Market volatility circuit-breaker: a sudden move in the market proxy vs
     its own ATR — this catches geopolitical/environmental/macro shocks that
     hit the tape BEFORE any ticker-tagged headline exists.

Output is a defensive ACTION consumed by the risk manager:
    NORMAL   -> trade as usual
    REDUCE   -> halve intended size
    HALT     -> no new entries
    FLATTEN  -> close exposure (reserved for the most severe breaker)

Deliberately NOT fed into the Merton sizer: news signals are sparse and
event-driven, their mu/sigma can't be estimated as cleanly as TA, and naive
inclusion would poison the sizing edge. It gates risk; it does not size.
"""

from __future__ import annotations
from datetime import datetime, timezone, timedelta
import config
from risk_manager import NORMAL, REDUCE, HALT, FLATTEN

_POS = {"beats", "surge", "record", "upgrade", "approval", "raises", "wins"}
_NEG = {"miss", "plunge", "cuts", "downgrade", "probe", "lawsuit", "recall",
        "halt", "fraud", "bankruptcy", "default", "warning", "slump"}


# --------------------------------------------------------------------------
# Pure assessment (testable without any live feed)
# --------------------------------------------------------------------------
def assess_symbol_news(articles: list[dict], now: datetime | None = None,
                       lookback_min: int = config.NEWS_LOOKBACK_MIN) -> dict:
    """
    articles: [{'headline': str, 'created_at': datetime}, ...]
    Returns {velocity, impact_hits, sentiment, matched}.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=lookback_min)
    recent = [a for a in articles if a.get("created_at") and a["created_at"] >= cutoff]

    matched, sent = [], 0
    for a in recent:
        h = (a.get("headline") or "").lower()
        for kw in config.HIGH_IMPACT_KEYWORDS:
            if kw.strip() in h:
                matched.append(kw.strip())
        sent += sum(w in h for w in _POS) - sum(w in h for w in _NEG)

    return {"velocity": len(recent), "impact_hits": len(matched),
            "sentiment": sent, "matched": sorted(set(matched))}


def volatility_circuit_breaker(recent_move: float, atr: float,
                               mult: float = config.CIRCUIT_BREAKER_ATR_MULT
                               ) -> bool:
    """
    recent_move: magnitude of the market proxy's recent return (abs, in same
                 units as atr, e.g. fractional return).
    atr:         the proxy's ATR over the same return basis.
    True -> a macro-scale shock is underway.
    """
    if atr <= 0:
        return False
    return abs(recent_move) >= mult * atr


def news_risk_action(symbol_assessment: dict, breaker_tripped: bool) -> tuple[str, str]:
    """Combine detectors into a single action + reason."""
    if breaker_tripped:
        return HALT, "volatility_circuit_breaker"

    v = symbol_assessment.get("velocity", 0)
    hits = symbol_assessment.get("impact_hits", 0)

    if hits >= 1 and v >= config.NEWS_VELOCITY_HALT:
        return HALT, "high_impact_news_burst"
    if hits >= 1:
        return REDUCE, "high_impact_keyword"
    if v >= config.NEWS_VELOCITY_HALT:
        return REDUCE, "elevated_news_velocity"
    return NORMAL, "clear"


# --------------------------------------------------------------------------
# Live fetch wrapper (needs keys; verify shape against your SDK version)
# --------------------------------------------------------------------------
def fetch_recent_news(api_key: str, secret: str, symbols, lookback_min: int = 60
                      ) -> dict:
    """
    Returns {symbol: [ {headline, created_at}, ... ]}.
    Uses Alpaca's free NewsClient. Wrapped defensively so a feed hiccup
    degrades to 'no news' rather than crashing the runner.
    """
    out = {s: [] for s in symbols}
    try:
        from alpaca.data.historical.news import NewsClient
        from alpaca.data.requests import NewsRequest
        client = NewsClient(api_key, secret)
        start = datetime.now(timezone.utc) - timedelta(minutes=lookback_min)
        resp = client.get_news(NewsRequest(symbols=",".join(symbols), start=start))
        for art in getattr(resp, "data", {}).get("news", []) or getattr(resp, "news", []):
            created = getattr(art, "created_at", None)
            head = getattr(art, "headline", "")
            for s in getattr(art, "symbols", []) or symbols:
                if s in out:
                    out[s].append({"headline": head, "created_at": created})
    except Exception as e:  # feed unavailable -> treat as no news, log upstream
        out["_error"] = str(e)
    return out
