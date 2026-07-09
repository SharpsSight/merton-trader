"""
config.py — single source of truth for all tunable parameters.

Everything the system keys off lives here so the backtest and the live runner
read identical settings (anti-drift discipline).
"""

# --- universe -------------------------------------------------------------
# Candidate pool: liquid S&P 500 large-caps across sectors. The runner ranks
# these by recent dollar-volume at startup and trades the top UNIVERSE_SIZE.
CANDIDATE_POOL = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "GOOG", "TSLA", "AVGO",
    "JPM", "V", "MA", "UNH", "HD", "PG", "XOM", "CVX", "LLY", "ABBV", "MRK",
    "PFE", "KO", "PEP", "COST", "WMT", "DIS", "NFLX", "CRM", "ADBE", "AMD",
    "INTC", "QCOM", "TXN", "CSCO", "ORCL", "IBM", "BAC", "WFC", "GS", "MS",
    "C", "AXP", "BA", "CAT", "DE", "GE", "HON", "UPS", "T", "VZ",
    "TMUS", "CMCSA", "NKE", "MCD", "SBUX", "LOW", "TGT", "PM", "MO", "CVS",
    "TMO", "ABT", "DHR", "BMY", "AMGN", "GILD", "ISRG", "NOW", "INTU", "MU",
    "AMAT", "LRCX", "PYPL", "UBER", "ABNB", "PLTR", "COIN", "SHOP", "F", "GM",
]
UNIVERSE_SIZE = 50            # trade the top N of the pool by dollar-volume
UNIVERSE = CANDIDATE_POOL[:8]  # fallback if dynamic selection is unavailable
MARKET_PROXY = "SPY"          # used by the volatility circuit-breaker

# --- signal / confluence --------------------------------------------------
# 5-minute base trigger, with 15m and 30m context (higher TF = more weight).
TIMEFRAME_WEIGHTS = {"5min": 0.20, "15min": 0.30, "30min": 0.50}
ADX_THRESHOLD = 25.0
ENTRY_THRESHOLD = 0.30        # |confluence score| must exceed this to take a side

# --- Merton sizer ---------------------------------------------------------
# f* = fractional * mu_lcb / (gamma * symbol_vol^2), clipped to [0, MAX_FRACTION]
# EDGE comes from the bucket (mu, sigma, n); RISK scaling uses each symbol's
# own realized volatility over the holding horizon -> risk-adjusted sizing.
GAMMA = 3.0                   # CRRA risk aversion (higher = more conservative)
FRACTIONAL = 0.25            # fractional-Kelly style haircut (variance control)
LCB_Z = 1.0                  # z for lower-confidence-bound mu shrinkage
MAX_FRACTION = 0.10          # max fraction of equity in one position
HOLD_BARS = 24               # ~expected holding horizon (5m bars) for vol scaling

# --- risk manager ---------------------------------------------------------
MAX_GROSS_EXPOSURE = 1.0      # sum |position value| <= this * equity (no leverage)
MAX_POSITIONS = None          # no count cap: worthiness + gross exposure decide breadth
PER_SYMBOL_CAP = 0.10         # max fraction of equity per symbol
MIN_SYMBOL_TRADES = 30        # min backtest trades to judge a symbol's own edge
MIN_EDGE_RATIO = 0.2551       # worthiness bar: mu_lcb / sigma (return per unit risk).
                              # MEASURED, not guessed: this is the 95th percentile of
                              # max(edge_ratio) under the date-blocked sign-flip null,
                              # 2000 resamples, 50 symbols, run 2026-07-09.
                              # The old value of 0.05 was 5x too lenient -- it admitted
                              # TSLA at ratio=0.0721, which is BELOW the null median of
                              # 0.1444 (family-wise p = 0.908).
                              # Re-measure with: run_backtest.py --bootstrap 2000
DAILY_LOSS_HALT = 0.03        # halt new entries if day PnL <= -3% of start equity
TRAIL_PERCENT = 2.5          # trailing-stop distance (%) — locks gains, cuts losers
SENSITIVE_EXIT = False        # True = exit on fast (5m) flip; False = on blended flip
STOP_ATR_MULT = 2.0          # fallback stop distance if no indicator stop
STOP_METHOD = "supertrend"    # 'supertrend' | 'psar' | 'atr'

# --- data feed ------------------------------------------------------------
# Alpaca's bars endpoint returns 04:00-20:00 ET and has no session filter. The
# live runner only trades while clock.is_open, so any backtest trade opened or
# closed outside RTH is a sample from a system that cannot exist. RTH_ONLY makes
# the backtest measure what the runner can actually execute.
RTH_ONLY = True
import datetime as _dt
RTH_START = _dt.time(9, 30)     # first bar opens 09:30
RTH_LAST_BAR = _dt.time(15, 55)  # last 5-min bar opens 15:55, covers 15:55-16:00

# 'raw' (the API default that was silently in effect) leaves splits unadjusted:
# a 2:1 split prints as a -50% bar and enters the return distribution as a trade.
BAR_ADJUSTMENT = "all"          # raw | split | dividend | all

# --- execution model (pessimistic) ---------------------------------------
SLIPPAGE_BPS = 5.0            # per side, matches the diagnostics baseline
SPREAD_BPS = 2.0             # assumed half-spread cost per side

# --- news risk overlay ----------------------------------------------------
NEWS_LOOKBACK_MIN = 30        # window for news-velocity
NEWS_VELOCITY_HALT = 5        # >= this many articles in window -> elevated
CIRCUIT_BREAKER_ATR_MULT = 3.0  # market move > this * ATR -> macro shock
HIGH_IMPACT_KEYWORDS = [
    "halt", "halted", "investigation", "sec ", "lawsuit", "bankruptcy",
    "downgrade", "guidance", "recall", "fraud", "probe", "default",
    "acquisition", "merger", "earnings", "fda", "tariff", "sanction",
]

# --- overnight handling ---------------------------------------------------
FLATTEN_EOD = True            # True = close all positions before the close.
                              # Every dollar the system has ever made came from
                              # overnight gaps (Jul 8: 3 longs, +$958). Every
                              # same-day round trip it opened AND closed lost
                              # money (0 for 6, -$137). Turning this on removes
                              # the gap exposure -- which means it also removes
                              # the only source of P&L the system has shown.
                              # That is the point: it isolates the signal.
FLATTEN_BUFFER_MIN = 10       # minutes before the close to flatten when FLATTEN_EOD

# Maximum bars to hold a position before exiting at the next open, regardless of
# signal. 24 five-minute bars = 2 hours. Caps the tail of the holding-time
# distribution so mu/sigma are estimated over a horizon the runner can honour.
MAX_HOLD_BARS = 24

# --- selection-bias control ----------------------------------------------
# MIN_EDGE_RATIO is a THRESHOLD ON THE MAXIMUM OF 50 CORRELATED TEST STATISTICS.
# Algebraically ratio = (t - LCB_Z)/sqrt(n), so ratio >= 0.05 is t >= 1 + 0.05*sqrt(n)
# -- about t >= 1.41 at n=68. The expected max t of ~10-20 effectively independent
# noise draws is 1.36-1.71. A symbol can therefore "clear the bar" while being
# indistinguishable from the best of 50 coin flips.
#
# run_backtest.py --bootstrap now estimates the null distribution of
# max(edge_ratio) by sign-flipping trades within each symbol. Set this from that
# output (the empirical 95th percentile), not by intuition.
BOOTSTRAP_ITERS = 2000        # 0 disables; ~30s for 2000 on a 4k-trade sample
SELECTION_ALPHA = 0.05        # one-sided FWER target for the max-statistic null

# --- bucket credibility floor --------------------------------------------
# Merton's f = FRACTIONAL * mu_lcb / (GAMMA * symbol_vol^2). With symbol_vol ~ 0.013
# that is f = 493 * mu_lcb, so MAX_FRACTION binds at mu_lcb = 2.03 BASIS POINTS
# while the standard error on bucket mu is 2.6-9.8 bps. The sizer's entire
# dynamic range is smaller than its own estimation noise: it is a step function
# between 0 and MAX_FRACTION, and GAMMA/FRACTIONAL do nothing.
#
# MIN_BUCKET_T requires the bucket's edge to be statistically real before any
# size is taken, independent of how the LCB happens to land.
MIN_BUCKET_T = 2.0            # require mu / SE(mu) >= this before sizing at all
MIN_BUCKET_N = 100            # and this many trades in the bucket

# --- PLUMBING TEST (paper only) -------------------------------------------
# Bypasses Merton and sizes every signal at a small FIXED fraction of equity.
#
# WHAT THIS IS FOR: exercising the full order path -- entry, trailing stop,
# exit, flip, reconcile, trade log -- so the live/backtest distribution
# comparison has an input, and so plumbing bugs surface before real capital.
#
# WHAT THIS IS NOT: evidence of edge. Sizing here is arbitrary and has no
# relationship to mu, sigma, or n. Resulting P&L carries ZERO information about
# whether the strategy works. At zero transaction cost no bucket in the current
# backtest reaches t = 2.0; b30_50 (70% of the sample) has t = 0.00 gross.
# Do not promote a good week here into a live deployment.
#
# NEVER set this True against a funded account.
PLUMBING_TEST = False
PLUMBING_FRACTION = 0.10      # fraction of equity per position when enabled

# --- daily operation ------------------------------------------------------
MARKET_TZ = "America/New_York"
BACKTEST_DAYS = 60            # --days passed to the nightly stats refresh
STATS_REFRESH_START_ET = "08:00"   # nightly refresh window (pre-open)
STATS_REFRESH_END_ET = "09:20"
MAX_BAR_STALENESS_SEC = 900   # during RTH, no fresh bar in this long -> no entries
BREAKER_COOLDOWN_CYCLES = 5   # volatility breaker stays hot this many cycles
CLOSE_RETRY_ATTEMPTS = 5      # poll cycles waiting for cancelled orders to release qty
CLOSE_RETRY_SLEEP_SEC = 2.0

# --- paths ----------------------------------------------------------------
# NOTE: Railway containers have an EPHEMERAL filesystem. Anything written here is
# lost on redeploy. Mount a volume and point DATA_DIR at it, otherwise the live
# trade log (which the Welch/Levene/KS diagnostics consume) is destroyed every
# time you push.
import os
import sys as _sys

_requested = os.environ.get("DATA_DIR", ".")


def _resolve_data_dir(path: str) -> str:
    """Create DATA_DIR, or fall back to cwd rather than crashing the process.

    Setting DATA_DIR=/data does not create /data. If the Railway volume is not
    mounted (or is mounted at a different path), open() raises FileNotFoundError
    on the first write, the container dies, Railway restarts it, and the whole
    backtest re-runs -- forever. A missing volume should degrade, not crash.
    """
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".write_probe")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        return path
    except OSError as e:
        print(f"WARNING: DATA_DIR={path!r} is not writable ({e}). "
              f"Falling back to '.'. Persistent files WILL be lost on redeploy: "
              f"check that the Railway volume is mounted at {path!r}.",
              file=_sys.stderr, flush=True)
        return "."


DATA_DIR = _resolve_data_dir(_requested)
SIGNAL_STATS_PATH = os.path.join(DATA_DIR, "signal_stats.json")  # backtest -> runner
BACKTEST_TRADES_PATH = os.path.join(DATA_DIR, "backtest_trades.csv")
LIVE_TRADES_PATH = os.path.join(DATA_DIR, "live_trades.csv")
