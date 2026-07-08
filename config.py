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
TIMEFRAME_WEIGHTS = {"15min": 0.20, "30min": 0.30, "60min": 0.50}
ADX_THRESHOLD = 25.0
ENTRY_THRESHOLD = 0.30        # |confluence score| must exceed this to take a side

# --- Merton sizer ---------------------------------------------------------
# f* = fractional * mu_lcb / (gamma * sigma^2), clipped to [0, MAX_FRACTION]
GAMMA = 3.0                   # CRRA risk aversion (higher = more conservative)
FRACTIONAL = 0.25            # fractional-Kelly style haircut (variance control)
LCB_Z = 1.0                  # z for lower-confidence-bound mu shrinkage
MAX_FRACTION = 0.20          # max fraction of equity in one position

# --- risk manager ---------------------------------------------------------
MAX_GROSS_EXPOSURE = 1.0      # sum |position value| <= this * equity
MAX_POSITIONS = 5
PER_SYMBOL_CAP = 0.20         # max fraction of equity per symbol
DAILY_LOSS_HALT = 0.03        # halt new entries if day PnL <= -3% of start equity
STOP_ATR_MULT = 2.0          # fallback stop distance if no indicator stop
STOP_METHOD = "supertrend"    # 'supertrend' | 'psar' | 'atr'

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

# --- paths ----------------------------------------------------------------
SIGNAL_STATS_PATH = "signal_stats.json"   # produced by backtest, read by runner
