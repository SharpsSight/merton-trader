"""
risk_manager.py — the last gate before an order is sent

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

SESSION SEMANTICS (added):
  `start_equity` is the START-OF-SESSION equity, not start-of-process. The
  runner must call `new_session(equity, date)` at every ET session rollover.
  Without that, `daily_halt` silently degrades into a cumulative-drawdown halt
  measured from process launch, which (a) never fires once equity has grown and
  (b) fires permanently after one bad week.

LATCHING (added):
  A halt that un-halts when equity ticks back up is not a halt. Once tripped,
  `daily_halt` stays tripped until the next session. Same for the kill switch,
  which now has an actual setter (previously it was dead code: initialised to
  False and never assigned anywhere in the codebase).
"""

from __future__ import annotations
import logging
import config

log = logging.getLogger("risk")

# news overlay action constants
NORMAL, REDUCE, HALT, FLATTEN = "NORMAL", "REDUCE", "HALT", "FLATTEN"


class RiskManager:
    def __init__(self, start_equity: float, params: dict | None = None,
                 session_date=None):
        p = params or {}
        self.max_gross = p.get("max_gross", config.MAX_GROSS_EXPOSURE)
        self.max_positions = p.get("max_positions", config.MAX_POSITIONS)
        self.per_symbol_cap = p.get("per_symbol_cap", config.PER_SYMBOL_CAP)
        self.daily_loss_halt = p.get("daily_loss_halt", config.DAILY_LOSS_HALT)

        self.kill_switch = False
        self.kill_reason = ""
        self.new_session(start_equity, session_date)

    # ------------------------------------------------------------------
    # session lifecycle
    # ------------------------------------------------------------------
    def new_session(self, equity: float, session_date=None) -> None:
        """Reset all per-session state. MUST be called at every ET rollover."""
        self.start_equity = float(equity)
        self.session_date = session_date
        self.halt_latched = False
        self.halt_low_water = float(equity)
        # a kill switch tripped for a *daily* reason clears; an operator trip does not
        if self.kill_switch and self.kill_reason.startswith("daily:"):
            self.kill_switch, self.kill_reason = False, ""
        log.info("RISK | new session %s | start_equity $%s | kill_switch=%s",
                 session_date, f"{self.start_equity:,.2f}", self.kill_switch)

    def trip_kill_switch(self, reason: str) -> None:
        if not self.kill_switch:
            log.error("RISK | KILL SWITCH TRIPPED: %s", reason)
        self.kill_switch, self.kill_reason = True, reason

    def clear_kill_switch(self) -> None:
        log.warning("RISK | kill switch manually cleared (was: %s)", self.kill_reason)
        self.kill_switch, self.kill_reason = False, ""

    # ------------------------------------------------------------------
    def daily_halt(self, equity: float) -> bool:
        """True if the day's drawdown has EVER breached the halt threshold.

        Latches. Recovering equity does not re-enable entries: the day's risk
        budget is spent. Exits are unaffected (they never route through here).
        """
        self.halt_low_water = min(self.halt_low_water, float(equity))
        if not self.halt_latched:
            if equity <= self.start_equity * (1.0 - self.daily_loss_halt):
                self.halt_latched = True
                log.error("RISK | DAILY LOSS HALT latched | equity $%s vs start $%s "
                          "(-%.2f%%)", f"{equity:,.2f}", f"{self.start_equity:,.2f}",
                          100.0 * (1 - equity / self.start_equity))
        return self.halt_latched

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
            return 0, f"kill_switch({self.kill_reason})"
        if news_action in (HALT, FLATTEN):
            return 0, f"news_{news_action.lower()}"
        if self.daily_halt(equity):
            return 0, "daily_loss_halt"
        if desired_shares == 0:
            return 0, "no_size"

        direction = 1 if desired_shares > 0 else -1

        if (self.max_positions and symbol not in positions
                and len(positions) >= self.max_positions):
            return 0, "max_positions"

        if news_action == REDUCE:
            desired_shares = int(desired_shares * 0.5)
            if desired_shares == 0:
                return 0, "news_reduce_to_zero"

        existing = positions.get(symbol, {}).get("shares", 0) * \
                   positions.get(symbol, {}).get("price", price)
        cap_notional = self.per_symbol_cap * equity
        room_symbol = cap_notional - abs(existing)
        if room_symbol <= 0:
            return 0, "per_symbol_cap"
        max_shares_symbol = int(room_symbol // price)

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
