"""
fundamentals.py — SEC EDGAR (XBRL) fundamental factor layer.

WHY THIS EXISTS
---------------
Every existing signal in this system (ADX, RSI, Bollinger, MFI, Supertrend, the
whole confluence stack) is a deterministic function of ONE OHLCV series. When
three of them "agree" that is not three pieces of evidence, it is one piece of
evidence wearing three hats. That fake diversification is a large part of why no
bucket has ever cleared MIN_BUCKET_T.

Accounting data is the first genuinely ORTHOGONAL input available here: it is a
different measurement of a different thing, published on a different clock. A
conjunction of "price says up" AND "balance sheet says cheap/profitable" is a
real conjunction in a way that "ADX says up AND RSI says up" is not.

WHAT THIS IS NOT
----------------
This is NOT a daily signal. Financials update quarterly; a 10-Q moves these
numbers four times a year. Reading them every bar returns the same answer every
bar and contributes ZERO daily variation. So fundamentals are used as a SCREEN
and a SIZE MULTIPLIER, never as a direction generator. Direction stays with the
trend layer. See factors.fundamental_confirmation().

It is also NOT evidence of edge. Nothing here has been validated on this
universe at this holding period. It changes what the system is allowed to trade;
whether that improves mu is an empirical question that MIN_BUCKET_T still has to
answer before any funded capital moves.

DATA SOURCE
-----------
SEC EDGAR XBRL "companyfacts" API. Free, no key, authoritative (it is the filing
itself, not a vendor's re-derivation). Quarterly latency is irrelevant at a
weekly refresh.

  ticker -> CIK   https://www.sec.gov/files/company_tickers.json
  facts           https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json

SEC requires a User-Agent identifying you with a real contact address, and rate
limits to 10 requests/second. Set SEC_USER_AGENT in the environment, e.g.
"merton-trader nate@example.com". Requests without it get 403'd.

ETF EXCLUSION (important side effect)
-------------------------------------
An ETF has no us-gaap revenue or assets facts, so it produces no score and is
excluded by eligible(). That is the correct ETF filter for this system: Alpaca's
AssetClass.US_EQUITY includes ETFs, and holding QQQ + SMH + SOXX alongside AVGO,
LRCX, KLAC, MU and ASML is one semiconductor bet expressed eleven times, which
silently destroys the effective sample size the significance gate depends on.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import statistics
import time
import urllib.error
import urllib.request

import config

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

# SEC allows 10 req/s. We stay well under it; the universe is only ~50-100 names
# and this runs once a week.
_REQUEST_SPACING_SEC = 0.15


# ---------------------------------------------------------------------------
# XBRL tag aliases
#
# Filers do not agree on tags. Revenue alone has four common spellings and the
# right one changed with ASC 606. Order matters: first tag with usable data wins.
# ---------------------------------------------------------------------------
TAGS = {
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
    ],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "gross_profit": ["GrossProfit"],
    "cost_of_revenue": [
        "CostOfGoodsAndServicesSold",
        "CostOfRevenue",
        "CostOfGoodsSold",
        "CostOfServices",
    ],
    "cfo": [
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ],
    "capex": [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
    ],
    "operating_income": ["OperatingIncomeLoss"],
    "dna": [
        "DepreciationDepletionAndAmortization",
        "DepreciationAmortizationAndAccretionNet",
        "DepreciationAndAmortization",
    ],
    "interest_expense": ["InterestExpense", "InterestExpenseDebt"],
    "assets": ["Assets"],
    "equity": [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ],
    "cash": [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ],
    "lt_debt": ["LongTermDebtNoncurrent", "LongTermDebt"],
    "st_debt": ["LongTermDebtCurrent", "DebtCurrent", "ShortTermBorrowings"],
    "shares": [
        "CommonStockSharesOutstanding",
        "WeightedAverageNumberOfDilutedSharesOutstanding",
        "WeightedAverageNumberOfSharesOutstandingBasic",
    ],
}

# Factor families. A symbol needs FUND_MIN_FAMILIES of these populated to get a
# score at all -- a composite built from one family is not a composite.
FAMILIES = {
    "value": ["earnings_yield", "fcf_yield"],
    "quality": ["gross_profitability", "roe"],
    "safety": ["neg_net_debt_to_ebitda", "interest_coverage"],
    "growth": ["revenue_growth", "income_growth"],
}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _user_agent() -> str:
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if not ua:
        raise RuntimeError(
            "SEC_USER_AGENT is not set. SEC rejects unidentified clients with "
            "403. Set it to something like 'merton-trader you@example.com'.")
    return ua


def _http_json(url: str, ua: str, retries: int = 3, timeout: int = 30):
    """GET JSON with retry. Returns None on 404 (symbol simply has no filings)."""
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": ua,
                "Accept-Encoding": "gzip, deflate",
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    import gzip
                    raw = gzip.decompress(raw)
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None                      # no such filer; not an error
            last = e
            if e.code in (429, 503):             # throttled -> back off
                time.sleep(2.0 * (attempt + 1))
                continue
            break
        except Exception as e:                   # network/DNS/timeout
            last = e
            time.sleep(1.0 * (attempt + 1))
    if last is not None:
        raise last
    return None


# ---------------------------------------------------------------------------
# XBRL parsing
# ---------------------------------------------------------------------------
def _series(facts: dict, tags, unit: str = "USD"):
    """Deduped, date-sorted fact rows for the first tag that has data.

    Restatement handling: EDGAR keeps every version of a period that was ever
    filed. Two rows can share (start, end) and disagree. We keep the one with
    the latest `filed` date, which is the restated (current) truth. Without this
    you get whichever ordering the JSON happened to use.
    """
    for ns in ("us-gaap", "ifrs-full"):
        node_root = facts.get("facts", {}).get(ns, {})
        for tag in tags:
            node = node_root.get(tag)
            if not node:
                continue
            units = node.get("units", {})
            rows = units.get(unit)
            if rows is None:                     # e.g. "shares", "USD/shares"
                rows = next((v for k, v in units.items()
                             if k.split("/")[0] == unit), None)
            if not rows:
                continue
            best = {}
            for r in rows:
                if r.get("val") is None:
                    continue
                key = (r.get("start"), r.get("end"))
                prev = best.get(key)
                if prev is None or (r.get("filed") or "") > (prev.get("filed") or ""):
                    best[key] = r
            out = sorted(best.values(), key=lambda r: (r.get("end") or ""))
            if out:
                return out
    return []


def _period_days(row: dict):
    s, e = row.get("start"), row.get("end")
    if not s or not e:
        return None
    try:
        return (dt.date.fromisoformat(e) - dt.date.fromisoformat(s)).days
    except ValueError:
        return None


def _stitch_quarters(quarters, n: int = 4):
    """Sum the n most recent NON-OVERLAPPING quarterly periods.

    Non-overlap is the whole point: EDGAR mixes Q1, H1 and 9-month cumulative
    periods in the same tag. Summing blindly double-counts. We walk backwards
    from the newest period and only accept a row whose `end` is at or before the
    previously accepted row's `start`.
    """
    if not quarters:
        return None
    qs = sorted(quarters, key=lambda r: r["end"], reverse=True)
    chosen, cursor = [], None
    for r in qs:
        if cursor is None or (r.get("end") or "") <= cursor:
            chosen.append(r)
            cursor = r.get("start") or ""
        if len(chosen) == n:
            break
    if len(chosen) < n:
        return None
    return sum(float(c["val"]) for c in chosen), chosen[0]["end"]


def _ttm(facts: dict, tags):
    """Trailing-twelve-month value for a FLOW concept (revenue, income, cash).

    Prefers four stitched quarters when they are fresher than the last annual
    period, so the factor updates on 10-Qs rather than lagging a full year.
    """
    rows = _series(facts, tags)
    flows = [r for r in rows if _period_days(r) is not None]
    if not flows:
        return None
    annual = [r for r in flows if 340 <= _period_days(r) <= 400]
    quarterly = [r for r in flows if 60 <= _period_days(r) <= 110]

    stitched = _stitch_quarters(quarterly, 4)
    latest_annual = annual[-1] if annual else None

    if stitched and latest_annual:
        return (stitched[0] if stitched[1] > latest_annual["end"]
                else float(latest_annual["val"]))
    if stitched:
        return stitched[0]
    if latest_annual:
        return float(latest_annual["val"])
    return None


def _instant(facts: dict, tags, unit: str = "USD"):
    """Most recent point-in-time value for a STOCK concept (assets, equity)."""
    rows = _series(facts, tags, unit=unit)
    instants = [r for r in rows if not r.get("start")]
    if not instants:
        return None
    return float(instants[-1]["val"])


def _yoy(facts: dict, tags):
    """Year-over-year growth from the two most recent annual periods."""
    rows = _series(facts, tags)
    annual = [r for r in rows
              if _period_days(r) is not None and 340 <= _period_days(r) <= 400]
    if len(annual) < 2:
        return None
    cur, prev = float(annual[-1]["val"]), float(annual[-2]["val"])
    if prev == 0:
        return None
    # Growth off a negative base is not interpretable as a percentage; a swing
    # from -100 to -50 is an improvement but (cur-prev)/|prev| = +0.5 conflates
    # it with a doubling from a positive base. Drop it rather than mislabel it.
    if prev < 0:
        return None
    return (cur - prev) / abs(prev)


def _shares_outstanding(facts: dict):
    """Shares outstanding, preferring the dei cover-page figure."""
    dei = facts.get("facts", {}).get("dei", {})
    node = dei.get("EntityCommonStockSharesOutstanding")
    if node:
        rows = next((v for k, v in node.get("units", {}).items()
                     if k.startswith("share")), None)
        if rows:
            valid = [r for r in rows if r.get("val")]
            if valid:
                valid.sort(key=lambda r: (r.get("end") or ""))
                return float(valid[-1]["val"])
    return _instant(facts, TAGS["shares"], unit="shares")


# ---------------------------------------------------------------------------
# Per-symbol metrics
# ---------------------------------------------------------------------------
def raw_metrics(facts: dict, price: float | None) -> dict:
    """Compute unnormalised fundamental metrics from one filer's facts.

    Any metric whose inputs are missing or degenerate comes back None rather
    than 0.0 -- a missing gross margin is not a zero gross margin, and scoring
    it as zero would place the company at the cross-sectional median instead of
    excluding it.
    """
    m = {}

    revenue = _ttm(facts, TAGS["revenue"])
    net_income = _ttm(facts, TAGS["net_income"])
    gross_profit = _ttm(facts, TAGS["gross_profit"])
    if gross_profit is None:
        cost = _ttm(facts, TAGS["cost_of_revenue"])
        if revenue is not None and cost is not None:
            gross_profit = revenue - cost
    cfo = _ttm(facts, TAGS["cfo"])
    capex = _ttm(facts, TAGS["capex"])
    op_income = _ttm(facts, TAGS["operating_income"])
    dna = _ttm(facts, TAGS["dna"])
    interest = _ttm(facts, TAGS["interest_expense"])

    assets = _instant(facts, TAGS["assets"])
    equity = _instant(facts, TAGS["equity"])
    cash = _instant(facts, TAGS["cash"])
    lt_debt = _instant(facts, TAGS["lt_debt"]) or 0.0
    st_debt = _instant(facts, TAGS["st_debt"]) or 0.0
    shares = _shares_outstanding(facts)

    market_cap = (price * shares) if (price and shares and shares > 0) else None
    m["market_cap"] = market_cap

    # --- value ------------------------------------------------------------
    if market_cap and market_cap > 0 and net_income is not None:
        m["earnings_yield"] = net_income / market_cap
    if market_cap and market_cap > 0 and cfo is not None and capex is not None:
        m["fcf_yield"] = (cfo - capex) / market_cap

    # --- quality ----------------------------------------------------------
    # Gross profit / total assets. Novy-Marx (2013): one of the few published
    # factors with decades of genuine out-of-sample survival, and it beats
    # earnings-based quality precisely because gross profit sits above the
    # accounting choices that make net income manipulable.
    if assets and assets > 0 and gross_profit is not None:
        m["gross_profitability"] = gross_profit / assets
    if equity and equity > 0 and net_income is not None:
        m["roe"] = net_income / equity

    # --- safety -----------------------------------------------------------
    ebitda = None
    if op_income is not None and dna is not None:
        ebitda = op_income + dna
    if ebitda and ebitda > 0 and cash is not None:
        net_debt = (lt_debt + st_debt) - cash
        # negated: LOW leverage should score HIGH, like every other metric here
        m["neg_net_debt_to_ebitda"] = -(net_debt / ebitda)
    if interest and interest > 0 and op_income is not None:
        m["interest_coverage"] = op_income / interest

    # --- growth -----------------------------------------------------------
    rg = _yoy(facts, TAGS["revenue"])
    if rg is not None:
        m["revenue_growth"] = rg
    ig = _yoy(facts, TAGS["net_income"])
    if ig is not None:
        m["income_growth"] = ig

    return m


# ---------------------------------------------------------------------------
# Cross-sectional scoring
# ---------------------------------------------------------------------------
def _robust_z(values: dict, clip: float = 3.0):
    """Median/MAD z-scores. {sym: value|None} -> {sym: z}.

    Median/MAD rather than mean/stdev because fundamental ratios have brutal
    tails -- one company with near-zero equity produces an ROE of 400 and a
    mean/stdev standardisation would hand it the entire cross-section.
    """
    xs = sorted(v for v in values.values()
                if v is not None and math.isfinite(v))
    if len(xs) < config.FUND_MIN_CROSS_SECTION:
        return {}
    n = len(xs)
    med = xs[n // 2] if n % 2 else 0.5 * (xs[n // 2 - 1] + xs[n // 2])
    devs = sorted(abs(x - med) for x in xs)
    mad = devs[n // 2] if n % 2 else 0.5 * (devs[n // 2 - 1] + devs[n // 2])
    scale = 1.4826 * mad
    if scale <= 0:
        scale = statistics.pstdev(xs) if n > 1 else 0.0
    if scale <= 0:
        return {}
    out = {}
    for sym, v in values.items():
        if v is None or not math.isfinite(v):
            continue
        out[sym] = max(-clip, min(clip, (v - med) / scale))
    return out


def score_cross_section(metrics_by_symbol: dict) -> dict:
    """{sym: raw_metrics} -> {sym: {'score','coverage','families','metrics'}}.

    Scores are cross-sectional by construction: "cheap" only means anything
    relative to the rest of the tradeable universe on the same day. An absolute
    earnings-yield threshold would just be a sector bet.
    """
    metric_names = sorted({k for m in metrics_by_symbol.values() for k in m
                           if k != "market_cap"})
    z_by_metric = {}
    for name in metric_names:
        z_by_metric[name] = _robust_z(
            {s: m.get(name) for s, m in metrics_by_symbol.items()})

    out = {}
    for sym in metrics_by_symbol:
        family_scores, present = {}, 0
        for fam, names in FAMILIES.items():
            zs = [z_by_metric[n][sym] for n in names
                  if n in z_by_metric and sym in z_by_metric[n]]
            if zs:
                family_scores[fam] = sum(zs) / len(zs)
                present += 1
        if present < config.FUND_MIN_FAMILIES:
            continue
        composite = sum(family_scores.values()) / len(family_scores)
        # tanh squash to [-1, 1]: keeps the ordering, bounds the multiplier, and
        # stops a single 3-sigma outlier from dominating the size decision.
        out[sym] = {
            "score": math.tanh(composite / 1.5),
            "coverage": present,
            "families": {k: round(v, 4) for k, v in family_scores.items()},
        }
    return out


# ---------------------------------------------------------------------------
# Fetch + cache
# ---------------------------------------------------------------------------
def load_cik_map(ua: str, cache_path: str | None = None,
                 max_age_days: int = 30) -> dict:
    """{TICKER: zero-padded 10-digit CIK}. Cached; the list barely changes."""
    cache_path = cache_path or os.path.join(config.DATA_DIR, "sec_cik_map.json")
    try:
        age_days = (time.time() - os.path.getmtime(cache_path)) / 86400.0
        if age_days <= max_age_days:
            with open(cache_path) as f:
                return json.load(f)
    except (OSError, ValueError):
        pass

    data = _http_json(SEC_TICKERS_URL, ua)
    if not data:
        raise RuntimeError("SEC company_tickers.json returned nothing")
    out = {}
    for row in data.values():
        ticker = str(row.get("ticker", "")).upper().strip()
        cik = row.get("cik_str")
        if ticker and cik is not None:
            out[ticker] = str(int(cik)).zfill(10)
    try:
        with open(cache_path, "w") as f:
            json.dump(out, f)
    except OSError as e:
        print(f"fundamentals: CIK cache write failed ({e}); continuing")
    return out


def refresh(symbols, price_lookup: dict, path: str | None = None,
            ua: str | None = None, log=None) -> dict:
    """Fetch facts for `symbols`, score them cross-sectionally, write the cache.

    price_lookup: {symbol: last_price}, needed for market-cap-based metrics.
    Symbols with no CIK (ETFs, ADR share classes, trusts) are skipped silently
    -- that absence IS the ETF filter.
    """
    path = path or config.FUND_CACHE_PATH
    ua = ua or _user_agent()
    _log = (log.info if log else print)

    cik_map = load_cik_map(ua)
    metrics, skipped_no_cik, failed = {}, [], []

    for sym in symbols:
        cik = cik_map.get(sym.upper())
        if not cik:
            skipped_no_cik.append(sym)
            continue
        try:
            facts = _http_json(SEC_FACTS_URL.format(cik=cik), ua)
        except Exception as e:
            failed.append((sym, str(e)[:80]))
            continue
        finally:
            time.sleep(_REQUEST_SPACING_SEC)
        if not facts:
            skipped_no_cik.append(sym)
            continue
        m = raw_metrics(facts, price_lookup.get(sym))
        if m:
            metrics[sym] = m

    scores = score_cross_section(metrics)
    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "n_requested": len(list(symbols)),
        "n_scored": len(scores),
        "skipped_no_filings": sorted(skipped_no_cik),
        "failed": failed,
        "scores": scores,
    }
    try:
        with open(path, "w") as f:
            json.dump(payload, f, indent=1)
    except OSError as e:
        _log(f"fundamentals: cache write to {path} failed ({e})")

    _log("FUNDAMENTALS | scored %d/%d | no filings (ETF/trust/ADR): %d | "
         "fetch errors: %d" % (len(scores), len(list(symbols)),
                               len(skipped_no_cik), len(failed)))
    return payload


def load(path: str | None = None):
    path = path or config.FUND_CACHE_PATH
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def is_stale(payload, max_age_days: int | None = None) -> bool:
    if not payload:
        return True
    max_age_days = (config.FUND_REFRESH_DAYS if max_age_days is None
                    else max_age_days)
    try:
        gen = dt.datetime.fromisoformat(payload["generated_at"])
    except (KeyError, ValueError):
        return True
    age = (dt.datetime.now(dt.timezone.utc) - gen).total_seconds() / 86400.0
    return age > max_age_days


def score_for(payload, symbol: str):
    """Composite in [-1, 1], or None if the symbol was never scored."""
    if not payload:
        return None
    row = payload.get("scores", {}).get(symbol)
    return row["score"] if row else None


def eligible(payload) -> set:
    """Symbols with a real fundamental score. This is the ETF/trust filter."""
    if not payload:
        return set()
    return set(payload.get("scores", {}).keys())
