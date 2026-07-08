# Merton Trader

Automated, signal-driven day-trading system for US equities. Runs locally, paper-first on Alpaca, with statistical validation that live behavior matches backtest expectations before any real capital is committed.

**Current phase:** paper trading. No real capital, no paid data feeds. Cost = $0.

---

## Design principles (the non-negotiables)

1. **Merton sizing is the position-sizing law.** Every position is sized by the Merton share formula, weighting risk/return via empirically derived per-signal `μ` (drift) and `σ` (volatility). No signal trades until it has real `μ`/`σ`.
2. **One signal codepath.** The backtest engine and the live runner import the *same* signal functions. This prevents strategy drift between environments — the #1 way backtests lie.
3. **Validation is distributional, not P&L.** We do not judge the system on short-run profit. We compare the *distribution* of live trades against the backtest's expected distribution (Welch t-test, Levene, KS). This avoids overfitting to noise.
4. **Environments must match.** Slippage calibration and diagnostic baselines are tied to a specific broker + data feed. Switching brokers invalidates the validation chain. Paper → live happens on the *same* broker (Alpaca) before any IBKR migration is considered.
5. **Empirically anchored changes only.** Architecture and platform upgrades are triggered by measured conditions, not preference (see Go-live triggers).

---

## Why backtest at all (the framing that matters)

Backtesting here is **sample compression, not bureaucracy.** The Merton sizer needs empirical `μ` and `σ` *per signal*. Paper trading alone cannot generate enough trades fast enough to estimate those. The backtest compresses months of history into the sample the sizer needs on day one. Without it, the LCB-shrunk `μ` stays ≈ 0 and the system correctly sizes everything to zero — it "runs" but does nothing.

---

## Components

| Component | Role | Status |
|---|---|---|
| Signal strategies | ORB, VWAP reversion, intraday momentum | specced |
| Walk-forward backtest engine | pessimistic fills, produces per-signal `μ`/`σ` | specced |
| Fractional Merton sizer | LCB `μ` shrinkage — small samples auto-shrink size toward 0 | specced |
| Risk manager | kill switch, daily loss halt, exposure caps | specced |
| Live paper runner | shares signal codepath with backtest | specced |
| Weekly diagnostics | Welch t / Levene / KS: live vs. backtest distributions | specced |
| `preflight_check.py` | validates auth + account + clock + IEX data pre-launch | **built** |

---

## Data flow

```
        historical bars (IEX, free)
                 │
                 ▼
        walk-forward backtest ──► per-signal μ, σ  ──►  Merton sizer params
                 │                                           │
                 │ (same signal codepath)                    │
                 ▼                                           ▼
   live paper runner ── signal ──► Merton size ──► risk checks ──► Alpaca paper order
                 │                                                        │
                 ▼                                                        ▼
           trade log ───────────────────────────────►  weekly diagnostics
                                                        (live dist vs backtest dist)
```

---

## Proposed repo structure (build target for tonight)

```
merton-trader/
├── README.md
├── requirements.txt
├── .gitignore
├── preflight_check.py          # DONE — connection/go-no-go gate
├── .env                        # LOCAL ONLY, git-ignored (holds API keys)
├── config.py                   # universe, params, thresholds
├── signals/
│   ├── __init__.py
│   ├── orb.py                  # opening range breakout
│   ├── vwap_reversion.py
│   └── momentum.py
├── backtest/
│   └── engine.py               # walk-forward, pessimistic fills, emits μ/σ
├── sizing/
│   └── merton.py               # fractional Merton + LCB shrinkage
├── risk/
│   └── manager.py              # kill switch, daily loss halt, exposure caps
├── runner/
│   └── live_paper.py           # shares signals/ codepath with backtest
└── diagnostics/
    └── weekly.py               # Welch t / Levene / KS
```

---

## Platform

- **Broker/API:** Alpaca. Paper environment, IEX free data feed.
- **Data caveat:** IEX is ~2–3% of consolidated volume. Signal logic exercises fine, but live paper slippage is calibrated against a partial book. When diagnostics show slippage divergence, rule out the IEX-vs-SIP data gap *before* blaming execution quality.
- **Execution:** local Python on home PC. Machine must stay awake + online 09:30–16:00 ET. Failures must be loud, not silent — a dead process that looks alive corrupts diagnostics.

---

## Rules & triggers

**PDT rule.** A live *margin* account doing >3 day trades per rolling 5 days requires $25k minimum equity. Paper ignores this; live will not.

**Go-live cost triggers** (all post-paper):
- SIP data feed — ~$99/mo on Alpaca
- news/sentiment APIs — optional
- real trading capital

**Platform-upgrade trigger (Alpaca → IBKR):** migrate *only if* realized slippage consistently exceeds the 5 bps/side model in live diagnostics. IBKR SmartRouting fills ~87% at/better than NBBO, but requires a full rewrite + TWS/IB Gateway operational dependencies. Migration resets the validation chain, so the bar is empirical and high.

---

## Open decision points

- **Sizer warm-start:** if real-data backtest still shrinks `μ` ≈ 0 across all signals, decide whether to accept a quiet day-one (honest) or set a shrinkage floor (faster, riskier). Default lean: accept the honest version.
- **Signal expansion:** beyond the three baselines, once the pipeline is validated end-to-end.

---

## Not evidence

Income claims and course funnels from trading content (YouTube, etc.) are marketing, not proof of viability. Conceptual overlap with someone's pitch ≠ implementation equivalence. Only this system's own diagnostics validate this system.
