# Patch notes — daily-continuity fixes + statistical corrections

Six files changed. Drop-in replacements; no other file needs to move.

---

## 1. `live_paper.py` — the daily loop

| # | Bug | Consequence | Fix |
|---|---|---|---|
| 1 | `RiskManager` built once, before the `while` loop | `start_equity` frozen at process launch. `daily_halt` silently became a *cumulative* drawdown halt from launch: never fires once equity grows, fires permanently after one bad week | ET session date from the Alpaca clock; `rm.new_session()` on rollover |
| 2 | `now` referenced inside `if config.FLATTEN_EOD:` but only assigned inside `if not clock.is_open:` | `NameError` on the first open cycle after a cold start, caught by the outer handler → "Loop error (continuing)" every 30s, forever. Masked today only because `FLATTEN_EOD=False` | `now` assigned at the top of every iteration |
| 3 | `cancel_symbol_orders()` then immediate `close_position()` | Alpaca's cancel is async. Produced the PLTR rejection: `held_for_orders: 53, available: 0`. The exception aborted the symbol's whole iteration *including* `positions.pop()` | `close_position_safely()` polls `qty_available` with backoff before closing |
| 4 | `breaker` recomputed per cycle from `ret.iloc[-1]` | Flapped `True→False` in 70s, three times in your log. A breaker that self-clears on the next heartbeat is a status flag | `BREAKER_COOLDOWN_CYCLES` hysteresis |
| 5 | `rm.kill_switch` initialised `False`, **never assigned anywhere in the codebase** | Dead code. The first gate in `gate_entry` could never fire | `trip_kill_switch()` / `clear_kill_switch()` |
| 6 | `stats`, `universe`, `tradeable` loaded once at startup | μ/σ and the dollar-volume universe frozen at deploy time, forever | `maybe_refresh_stats()` re-runs `run_backtest.py` once per session in the 08:00–09:20 ET pre-open window, then reloads |
| 7 | No distinction between "no signal" and "no data" | A stalled feed looks identical to selectivity. This is the failure mode that lets a dead system look healthy for a week | `bars_are_fresh()`; entries suppressed and `DATA STALE` logged past `MAX_BAR_STALENESS_SEC` |
| 8 | Nothing persisted live fills | The Welch/Levene/KS diagnostics have **no input**. The entire validation plan had no data source | `log_trade()` → `LIVE_TRADES_PATH`, schema mirroring the backtest trade record |
| 9 | `logging.basicConfig` defaults to stderr | Railway painted every INFO line red, which trains you to ignore the log | `stream=sys.stdout` |
| 10 | Startup silently adopted 7 inherited positions | No assertion, no acknowledgement | `reconcile()` logs equity, inherited positions, `daytrade_count`, `pattern_day_trader`, `trading_blocked` |

Half-day handling (Jul 3, Thanksgiving eve, Christmas Eve — 13:00 ET closes) is free: nothing hardcodes 16:00.

## 2. `risk_manager.py`

- `new_session(equity, date)` resets `start_equity` and the halt latch.
- **`daily_halt` now latches.** It previously returned `equity <= start*(1-0.03)` — a pure function of *current* equity. Drop 3%, recover 1%, and entries resumed. The day's risk budget was spent.
- Kill switch has a setter. A `daily:`-prefixed trip clears at rollover; an operator trip does not.
- Exits never route through `gate_entry`, so none of this can trap you in a position.

## 3. `backtest.py` — two look-ahead bugs, both in your favour

```python
# BEFORE
hwm = max(hwm, h[i]) ...        # raise HWM with THIS bar's high
if lo[i] <= lvl:                # then test THIS bar's low against it
    exit_px = lvl * (1 - cost)  # and fill exactly at the level
```

**(a)** You cannot know a bar's high before its low. Raising the water mark first ratchets the stop up before the drawdown is measured against it — a free win on every bar of every trade.

**(b)** `exit_px = lvl` assumes the stop always fills at the level. A bar that *gaps through* fills at the open. With `FLATTEN_EOD = False` you hold overnight, so every gap-down hits this path.

Both fixed: HWM updates *after* the stop test; fills use `min(o[i], lvl)` for longs, `max(o[i], lvl)` for shorts. Trades now also carry `entry_time`, `bars_held`, `exit_kind`.

**The backtest was already negative. These fixes make it worse.**

## 4. `merton.py` — a credibility floor

`f = FRACTIONAL · μ_lcb / (GAMMA · symbol_vol²)`. At `symbol_vol = 0.013`:

> **f = 493 × μ_lcb**, and `MAX_FRACTION = 0.10` binds at **μ_lcb = 2.03 bps**.

| bucket | SE(μ) | × the entire 0→cap range |
|---|---|---|
| b30_50 | 2.59 bps | 1.3× |
| b50_70 | 7.32 bps | 3.6× |
| b70_100 | 9.79 bps | 4.8× |

Estimation noise is up to **five times the sizer's full dynamic range**. `GAMMA` and `FRACTIONAL` are inert; `MAX_FRACTION` sets every position. There is no risk-adjusted ramp — it's a coin flip between 0% and 10%.

Added `MIN_BUCKET_T = 2.0` and `MIN_BUCKET_N = 100`: the bucket's edge must be a real effect before *any* size is taken, independent of where the LCB happens to land.

Note this does **not** repair the step function — it prevents noise from stepping onto it. Fixing the ramp properly means sizing on a per-trade Sharpe with a horizon-matched σ, not `mu_per_trade / vol_over_24_bars²`. Those are different horizons and Merton wants them to be the same one. Separate job.

## 5. `run_backtest.py`

- **Persists every trade** to `backtest_trades.csv`. Previously `all_trades` was compressed into three bucket moments and discarded — no bootstrap, no audit, no live-vs-backtest comparison possible.
- **`--bootstrap N`**: null distribution of `max(edge_ratio)` by sign-flipping trade returns **by exit date** (one Rademacher draw shared across symbols closing that day, so market-wide co-movement survives into the null — that's what makes 50 large-caps far fewer than 50 independent tests). Prints the honest threshold and a family-wise p-value.
- Prints per-symbol σ and t alongside `ratio`, and per-bucket `gross = μ + round-trip cost`.
- **Pooling left alone.** See below.

## 6. `config.py`, `requirements.txt`

`MARKET_TZ`, `BACKTEST_DAYS`, `STATS_REFRESH_*`, `MAX_BAR_STALENESS_SEC`, `BREAKER_COOLDOWN_CYCLES`, `CLOSE_RETRY_*`, `MIN_BUCKET_T`, `MIN_BUCKET_N`, `BOOTSTRAP_ITERS`, `SELECTION_ALPHA`, `DATA_DIR`. Added `tzdata` (zoneinfo fails on slim images without it) and `scipy`.

---

## The pooling is not a bug — do not "fix" it

```python
pooled = bt._bucket_stats(all_trades)   # pool across universe for sample size
```

I told you last turn this was the bug. It isn't, and correcting myself matters more than the code.

The pooled μ is an **unbiased** estimate of what your signal earns on a randomly chosen large-cap. TSLA's `μ = +0.0036` is the **maximum of 50 draws** and is upward-biased by construction. Feeding the sizer TSLA's own μ would replace an honest estimate with a selection-biased one.

Restricting buckets to TSLA's 68 trades produces this:

| bucket n | μ_lcb | f |
|---|---|---|
| 20 | −0.00057 | 0.0% |
| 25 | −0.00013 | 0.0% |
| **30** | **+0.00020** | **9.8% of equity** |

Five trades of sample size separates flat from near-max leverage on a t = 1.59 symbol. That was the "fix."

---

## Deploy

```bash
# Railway start command — unchanged shape, but now the runner refreshes itself
python run_backtest.py --days 60 --bootstrap 2000 && python live_paper.py
```

Mount a volume and set `DATA_DIR` to it. Railway's filesystem is ephemeral: without a volume, `live_trades.csv` — the only input your diagnostics will ever have — is destroyed on every push.

**Read the bootstrap block before anything else.** If it prints

```
VERDICT: the best symbol is INSIDE the noise band.
```

then `tradeable` is empty, `mode=OBSERVE`, and the system logs signals without trading. That is the correct outcome, not a failure to launch.

---

## What these patches do not fix

Nothing here creates edge. The pooled buckets say:

| bucket | μ | t | gross (μ + 14 bps cost) |
|---|---|---|---|
| b30_50 | −0.0014 | **−5.41** | +0.0000 |
| b50_70 | −0.0002 | −0.27 | +0.0012 |
| b70_100 | −0.0008 | −0.82 | +0.0006 |

**b30_50 is 70% of the sample and has precisely zero gross edge at t = −5.4.** The other two buckets carry 6–12 bps gross against 14 bps of cost. Cost sensitivity for the best bucket:

| cost/side | net μ | μ_lcb | trades? |
|---|---|---|---|
| 1 bp | +0.00100 | +0.00027 | yes |
| 2 bps | +0.00080 | +0.00007 | barely |
| **3 bps** | +0.00060 | −0.00013 | **no** |
| 7 bps (current) | −0.00020 | −0.00093 | no |

You need sub-2bps all-in per side on market orders. Not reachable on Alpaca.

The scaffold is genuinely good and will outlive these signals. Swap the signals.
