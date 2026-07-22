"""
supabase_mirror.py — push runtime state to Supabase so the system is observable.

WHY THIS EXISTS
---------------
On 2026-07-18 the container lost DNS at 04:16 UTC and stopped. Nobody noticed
for four days, and when the logs were finally read the diagnosis started from
the wrong premise because the only durable record -- two stale rows in Supabase
-- had been written by a version of the code that no longer existed. A system
that sweeps up to a couple of thousand symbols and holds positions for a week
cannot be operated by scrolling a log tail.

So: one row of current state, one row per order event, and a snapshot of the
selection statistics that decided what was tradeable.

DESIGN RULES
------------
1. NEVER raise into the trading loop. Every entry point swallows its own
   exceptions and returns False. A dashboard outage must not stop trading, and
   more importantly must not look like a trading bug.
2. NEVER block for long. Short timeout, no retries on the hot path. A slow
   mirror would stretch the cycle and push bars past the staleness cap.
3. stdlib only (urllib, not requests). The previous mirror guarded `import
   requests` with a try/except and silently degraded to a no-op when the package
   was missing -- and `requests` was never in requirements.txt. It reached
   production as a dependency-by-accident of alpaca-py. No new dependency here
   means no new silent failure mode.
4. Absent credentials = disabled, logged once, not an error. The backtest and
   local runs should not need Supabase keys.

CREDENTIALS (Railway env)
-------------------------
  SUPABASE_URL          https://<project-ref>.supabase.co
  SUPABASE_SERVICE_KEY  service_role key

The service_role key bypasses RLS for the WHOLE project, which is exactly why
the trading tables live in their own project rather than beside the SharpsSight
customer tables. Never commit it; it belongs only in the Railway environment.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

log = logging.getLogger("mirror")

_TIMEOUT_SEC = 6.0
_warned = False


def _creds():
    url = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_KEY") or ""
    return (url, key) if (url and key) else (None, None)


def enabled() -> bool:
    global _warned
    url, key = _creds()
    if url and key:
        return True
    if not _warned:
        log.info("MIRROR | SUPABASE_URL / SUPABASE_SERVICE_KEY not set -- "
                 "state mirroring disabled (trading is unaffected)")
        _warned = True
    return False


def _post(table: str, payload, *, upsert: bool = False) -> bool:
    url, key = _creds()
    if not url:
        return False
    endpoint = f"{url}/rest/v1/{table}"
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        # minimal: do not ship the inserted rows back, we never read them here
        "Prefer": ("resolution=merge-duplicates,return=minimal" if upsert
                   else "return=minimal"),
    }
    body = json.dumps(payload, default=str).encode()
    try:
        req = urllib.request.Request(endpoint, data=body, headers=headers,
                                     method="POST")
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SEC) as r:
            return 200 <= r.status < 300
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()[:300]
        except Exception:
            pass
        log.warning("MIRROR | %s POST failed %s: %s", table, e.code, detail)
        return False
    except Exception as e:
        log.warning("MIRROR | %s POST failed: %s", table, e)
        return False


# ---------------------------------------------------------------------------
def push_status(**fields) -> bool:
    """Upsert the single live-state row (id='live').

    One row, overwritten. This answers 'is it alive, and if it is flat, WHY' --
    which is the question that actually gets asked. The gate_counts blob is the
    important part: a flat book because nothing cleared the edge threshold and a
    flat book because the fundamentals cache is empty look identical from the
    outside and need completely different fixes.
    """
    if not enabled():
        return False
    row = {"id": "live", **{k: v for k, v in fields.items() if v is not None}}
    return _post("merton_status", [row], upsert=True)


def push_trade(**fields) -> bool:
    """Append one order event. Mirrors the local CSV schema."""
    if not enabled():
        return False
    return _post("merton_trades", [{k: v for k, v in fields.items()
                                    if v is not None}])


def push_research(**fields) -> bool:
    """Append one research/selection result.

    The `merton_research` table has existed since the mirror was first designed
    and has never had a single writer -- no module referenced it. The nightly
    selection bootstrap is exactly what it was meant to hold: the measured null
    threshold, the observed maximum, and whether anything cleared.
    """
    if not enabled():
        return False
    return _post("merton_research", [{k: v for k, v in fields.items()
                                      if v is not None}])
