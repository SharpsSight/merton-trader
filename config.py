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
    # --- expansion: 40 more established, liquid S&P large-caps (sector-diverse).
    # Reaches the less-saturated tier below the mega-cap top-50 while staying
    # inside names where IEX bars are dense and the 10bps cost model stays honest.
    "ACN", "LIN", "RTX", "SPGI", "NEE", "PGR", "CB", "ELV", "VRTX", "REGN",
    "PANW", "SNPS", "CDNS", "KLAC", "ADI", "MDLZ", "ADP", "GD", "LMT", "NOC",
    "ITW", "EMR", "ETN", "PH", "MMM", "CL", "KMB", "SYK", "BSX", "CI",
    "ZTS", "BDX", "SO", "DUK", "MMC", "AON", "ICE", "CME", "USB", "PNC",
]
UNIVERSE_SIZE = 100          # trade the top N of the pool by dollar-volume
                             # (was 50; 120-name pool -> ranking still selects)
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
MIN_EDGE_RATIO = 0.0246       # worthiness bar: mu_lcb / sigma (return per unit risk).
                              # Set from the CORRECTED null 95th percentile printed by
                              # run_backtest.py --bootstrap 2000 (2026-07-15 boot),
                              # after the net->gross sign-flip fix. The prior 0.2551
                              # was measured pre-fix and biased ~10x too HIGH, which
                              # would reject genuine marginal candidates.
                              # NOTE: this does NOT unlock trades today -- the observed
                              # max edge ratio was -0.0952 (best symbol still negative,
                              # inside the noise band). This corrected bar only ensures
                              # a real marginal edge is not wrongly rejected WHEN the
                              # wider universe / news / reversion search surfaces one.
                              # Re-measure whenever the universe or cost model changes.
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
PLUMBING_TEST = False        # CUTOVER: real concurrent-Merton + vol-targeting is
                             # now live (see ALLOCATOR block below). Fixed-fraction
                             # plumbing is OFF. With gates intact this trades 0 until
                             # a bucket clears MIN_BUCKET_T -- that is correct, not a
                             # hang. Set True again only to exercise the order path.
PLUMBING_FRACTION = 0.10      # fraction of equity per position when enabled

# --- CONCURRENT ALLOCATOR + CONSTANT-VOLATILITY TARGETING -----------------
# The live runner sizes the whole candidate book JOINTLY each bar (merton_alloc)
# and then scales gross exposure to hold portfolio vol at VOL_TARGET_ANNUAL.
#
# READ THIS: vol targeting is a RISK controller, not a return generator. It holds
# volatility constant; it does not change the SIGN of returns. On a signal with
# mu<=0 it produces a constant-vol path with negative drift -- smoother losing,
# not winning. It converts risk into RETURN only when the buckets feeding it have
# a real (t>=MIN_BUCKET_T) positive edge. None currently do. So today this block
# sizes every name to ZERO, exactly like the bare Merton sizer. It goes aggressive
# automatically the moment slice_edge/reversion produce a qualifying bucket.
VOL_TARGET_ANNUAL = 0.15     # AGGRESSION DIAL. Annualized portfolio vol target.
                             # ~0.10 conservative | ~0.15 S&P-like | 0.25+ aggressive.
                             # Higher = lever up harder in calm tapes (up to the cap).
BOOK_CORRELATION = 0.40      # avg pairwise correlation assumed in the book-vol model
                             # (large-cap intraday co-movement). Raise -> vol-target
                             # treats the book as less diversified -> smaller gross.
CONCENTRATION = 1.0          # 1.0 = pure proportional Merton allocation.
                             # >1.0 tilts harder into the strongest-edge names
                             # (explicit, non-theoretical override; leave at 1.0
                             # unless you deliberately want winner-take-more).
#
# LEVERAGE: the vol-target's gross ceiling IS MAX_GROSS_EXPOSURE (above). It is
# 1.0 = no leverage. To let the vol-targeter lever up, raise MAX_GROSS_EXPOSURE
# ABOVE 1.0 (e.g. 2.0). That is the ONLY knob that lets this trade bigger than
# unlevered -- and on zero-edge signal it amplifies variance, not return. Set it
# with that understood; the RiskManager gross gate reads the same value, so they
# stay consistent.

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
