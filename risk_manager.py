"""
risk_manager.py — the last gate before an order is sent.

Enforces, in order:
  1. Kill switch / news action (HALT, FLATTEN, REDUCE)
  2. Daily-loss halt (day PnL <= -DAILY_LOSS_HALT of start-of-day equity)
  3. Max concurrent positions
  4. Per-symbol exposure cap
  5. Gross exposure cap
And computes protective stops from the signal's Supertrend/PSAR levels
(or an ATR fallback).

Sizing (Merton) decides how much you'd LIKE; this decides how much you're
ALLOWED, and where you bail. Two independent jobs, deliberately separated.
"""

from __future__ import annotations
import config

# news overlay action constants
NORMAL, REDUCE, HALT, FLATTEN = "NORMAL", "REDUCE", "HALT", "FLATTEN"


class RiskManager:
    def __init__(self, start_equity: float, params: dict | None = None):
        self.start_equity = float(start_equity)
        p = params or {}
        self.max_gross = p.get("max_gross", config.MAX_GROSS_EXPOSURE)
        self.max_positions = p.get("max_positions", config.MAX_POSITIONS)
        self.per_symbol_cap = p.get("per_symbol_cap", config.PER_SYMBOL_CAP)
        self.daily_loss_halt = p.get("daily_loss_halt", config.DAILY_LOSS_HALT)
        self.kill_switch = False

    # ------------------------------------------------------------------
    def daily_halt(self, equity: float) -> bool:
        """True if the day's drawdown breaches the halt threshold."""
        return equity <= self.start_equity * (1.0 - self.daily_loss_halt)

    def compute_stop(self, price: float, direction: int,
                     signal_stops: dict, atr: float | None = None,
                     method: str = config.STOP_METHOD) -> float:
        """Protective stop price on the correct side of entry."""
        st = signal_stops.get("supertrend")
        ps = signal_stops.get("psar")
        level = {"supertrend": st, "psar": ps}.get(method)

        if level is None and atr:                      # ATR fallback
            level = price - direction * config.STOP_ATR_MULT * atr

        if level is None:
            return None
        # ensure stop is protective (below for long, above for short)
        if direction == 1:
            return min(level, price)
        else:
            return max(level, price)

    def gate_entry(self, symbol: str, desired_shares: int, price: float,
                   positions: dict, equity: float,
                   news_action: str = NORMAL) -> tuple[int, str]:
        """
        Return (approved_shares, reason). approved_shares is signed and may be
        reduced or zeroed. `positions`: {symbol: {'shares':int,'price':float}}.
        """
        if self.kill_switch:
            return 0, "kill_switch"
        if news_action in (HALT, FLATTEN):
            return 0, f"news_{news_action.lower()}"
        if self.daily_halt(equity):
            return 0, "daily_loss_halt"
        if desired_shares == 0:
            return 0, "no_size"

        direction = 1 if desired_shares > 0 else -1

        # max concurrent positions (new symbols only); None disables the cap
        if (self.max_positions and symbol not in positions
                and len(positions) >= self.max_positions):
            return 0, "max_positions"

        # news REDUCE -> halve intended size
        if news_action == REDUCE:
            desired_shares = int(desired_shares * 0.5)
            if desired_shares == 0:
                return 0, "news_reduce_to_zero"

        # per-symbol cap
        existing = positions.get(symbol, {}).get("shares", 0) * \
                   positions.get(symbol, {}).get("price", price)
        cap_notional = self.per_symbol_cap * equity
        room_symbol = cap_notional - abs(existing)
        if room_symbol <= 0:
            return 0, "per_symbol_cap"
        max_shares_symbol = int(room_symbol // price)

        # gross exposure cap
        gross = sum(abs(p["shares"] * p["price"]) for p in positions.values())
        room_gross = self.max_gross * equity - gross
        if room_gross <= 0:
            return 0, "gross_exposure_cap"
        max_shares_gross = int(room_gross // price)

        approved = min(abs(desired_shares), max_shares_symbol, max_shares_gross)
        if approved <= 0:
            return 0, "capped_to_zero"

        reason = "ok" if approved == abs(desired_shares) else "reduced_by_cap"
        return approved * direction, reason
