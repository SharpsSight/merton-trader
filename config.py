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
# True  = rank the whole liquid market by dollar-volume (feed.dynamic_universe).
# False = rank the fixed CANDIDATE_POOL (feed.select_universe).
USE_DYNAMIC_UNIVERSE = True
UNIVERSE = CANDIDATE_POOL[:8]  # fallback if dynamic selection is unavailable
MARKET_PROXY = "SPY"          # used by the volatility circuit-breaker

# --- signal / confluence --------------------------------------------------
# 5-minute base trigger, with 15m and 30m context (higher TF = more weight).
TIMEFRAME_WEIGHTS = {"5min": 0.20, "15min": 0.30, "30min": 0.50}
ADX_THRESHOLD = 25.0
ENTRY_THRESHOLD = 0.15        # |confluence score| must exceed this to take a side
                              # Lowered from 0.30: far more signals cross, so the
                              # book stays populated. More trades, same zero edge.

# --- Merton sizer ---------------------------------------------------------
# f* = fractional * mu_lcb / (gamma * symbol_vol^2), clipped to [0, MAX_FRACTION]
# EDGE comes from the bucket (mu, sigma, n); RISK scaling uses each symbol's
# own realized volatility over the holding horizon -> risk-adjusted sizing.
GAMMA = 3.0                   # CRRA risk aversion (higher = more conservative)
FRACTIONAL = 0.25            # fractional-Kelly style haircut (variance control)
LCB_Z = 1.0                  # z for lower-confidence-bound mu shrinkage
MAX_FRACTION = 0.10          # max fraction of equity in one position
# ~expected holding horizon in 5m bars, used ONLY to scale per-bar vol into
# horizon vol for the sizer's risk denominator.
# CHANGED: was 24 (2 hours). MUST track the real holding period -- symbol_vol is
# per-bar sigma * sqrt(HOLD_BARS) and f* = mu_lcb/(gamma*symbol_vol^2), so
# leaving this at 24 while positions run five sessions understates sigma ~4x and
# therefore overstates the Merton fraction ~16x. 5 sessions * 78 RTH bars = 390.
HOLD_BARS = 390

# --- risk manager ---------------------------------------------------------
MAX_GROSS_EXPOSURE = 1.0      # sum |position value| <= this * equity (no leverage)
MAX_POSITIONS = None          # no count cap: worthiness + gross exposure decide breadth
PER_SYMBOL_CAP = 0.10         # max fraction of equity per symbol

# Floor on entry size, in dollars. When a risk cap shaves an allocation down to
# a token stake, that is not a smaller version of the trade -- it is a full
# round trip of fixed cost on an economically meaningless position. The
# 2026-07-16 log shows a 1-share fill and 5-share exits, all paying 10bps round
# trip for exposure that cannot move the book. Skip rather than shave.
MIN_ENTRY_NOTIONAL = 500.0
MIN_SYMBOL_TRADES = 30        # min backtest trades to judge a symbol's own edge
MIN_EDGE_RATIO = 0.0246       # worthiness bar: mu_lcb / sigma (return per unit risk).
                              # CORRECTED from 0.2551. The old value came from a
                              # bootstrap path that flipped NET returns, which
                              # flips the cost term too and inflated the null
                              # 95th percentile ~10x. Zero symbols cleared at
                              # either value -- the absent edge is real, but the
                              # threshold should still be the right one.
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
# CHANGED: was True (flatten every position before every close).
#
# Cost is FIXED per round trip; signal grows with sqrt(holding time). At 25%
# annualised vol the noise over a 30-minute hold is ~44bps, so a 10bps round
# trip demands a per-trade information ratio of 10/44 = 0.23 just to break even.
# A genuinely good intraday signal runs 0.02-0.05. The system was short by 5-10x
# and no amount of signal work closes that gap.
#
# Stretching the hold to ~5 sessions takes per-trade sigma to ~350bps against
# the same 10bps cost -- required IR drops to ~0.03, which is inside the range a
# real signal can actually reach. This single change buys more than any signal
# improvement available, and it costs nothing.
#
# It also stops the pathology visible in the 2026-07-17 logs, where MAXHOLD
# force-closed AMZN at score -0.87, STX at +0.71 and JNJ at +0.60 -- paying the
# exit cost on precisely the positions the signal liked most.
FLATTEN_EOD = False
FLATTEN_BUFFER_MIN = 10       # minutes before the close to flatten when FLATTEN_EOD

# Legacy intraday cap on the LIVE path. 0 = disabled; the calendar cap governs.
MAX_HOLD_BARS = 0

# The backtest indexes bars, not wall clock, so it needs the SAME cap expressed
# in base bars or it measures a different strategy than the runner executes.
# With RTH_ONLY the base frame is 78 five-minute bars per session, so 5 sessions
# = 390. This MUST stay consistent with MAX_HOLD_CALENDAR_DAYS: a backtest whose
# holding-time distribution differs from live produces mu/sigma for a strategy
# that does not exist, and MIN_BUCKET_T then gates on the wrong distribution.
MAX_HOLD_BARS_BACKTEST = 390

# Maximum WALL-CLOCK days to hold before a forced exit. 7 calendar days is about
# 5 trading sessions. Calendar days rather than a bar count because the position
# now survives session boundaries, and a bar counter that only advances during
# RTH silently stretches over weekends and holidays.
MAX_HOLD_CALENDAR_DAYS = 7



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
PLUMBING_TEST = True
PLUMBING_FRACTION = 0.12      # MAX fraction, at |score|=1.0 (full conviction)
PLUMBING_FRACTION_MIN = 0.03  # MIN fraction, at |score|=ENTRY_THRESHOLD (marginal)
                              # Size scales linearly with the signal's own |score|
                              # between these two. This is confidence-PROPORTIONAL,
                              # not edge-proportional: it concentrates capital where
                              # the signal is loudest, but loud != profitable. The
                              # measured edge is still zero. Real edge-weighting is
                              # the Merton sizer (PLUMBING_TEST=False), which sizes
                              # zero here because mu_lcb <= 0.

# --- fundamental screen (SEC EDGAR) ---------------------------------------
# Fundamentals are a SCREEN and a SIZE MULTIPLIER, never a direction generator.
# Financials update quarterly, so at a daily decision cadence they contribute
# zero daily variation -- they cannot be a signal, only a filter. Direction
# stays with the trend layer.
#
# The value of the screen is ORTHOGONALITY. Every existing input (ADX, RSI,
# Bollinger, MFI, Supertrend) is a transform of the same OHLCV series, so their
# "agreement" is one piece of evidence counted several times. Accounting data is
# a different measurement of a different thing on a different clock, so a
# price/fundamental conjunction is a real conjunction.
USE_FUNDAMENTAL_GATE = True
FUND_REFRESH_DAYS = 7         # refresh cadence; quarterly data, weekly is ample
FUND_MIN_FAMILIES = 3         # of {value, quality, safety, growth} required
FUND_MIN_CROSS_SECTION = 8    # fewer names than this -> no z-scores at all

# CONJUNCTION thresholds, not a blend. A weighted average lets a loud technical
# score override a bad balance sheet, which defeats the entire purpose: the
# point is to require BOTH, so only agreement between two independent sources
# opens a position.
FUND_LONG_MIN = 0.0           # long requires composite score > this
FUND_SHORT_MAX = 0.0          # short requires composite score < this
FUND_REQUIRE_SCORE = True     # unscored symbol (ETF/trust/ADR) -> no entry
FUND_MULT_FLOOR = 0.5         # weakest fundamental agreement still sizes at 50%

# --- daily operation ------------------------------------------------------
MARKET_TZ = "America/New_York"
# CHANGED: was 60. Holding period went from ~24 bars to ~390, so 60 days of
# history yields roughly 12 non-overlapping holding windows per symbol instead
# of ~195. Bucket n collapses below MIN_BUCKET_N=100 and every bucket sizes to
# zero for want of sample, not for want of edge. Longer holds REQUIRE a longer
# window to estimate the same distribution to the same precision.
BACKTEST_DAYS = 400
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
FUND_CACHE_PATH = os.path.join(DATA_DIR, "fundamentals.json")
# Entry timestamps must now survive both restarts and session rollovers: with
# FLATTEN_EOD off a position lives for days, and an in-memory dict silently
# resets its max-hold clock every night (and on every redeploy).
ENTRY_TIMES_PATH = os.path.join(DATA_DIR, "entry_times.json")
