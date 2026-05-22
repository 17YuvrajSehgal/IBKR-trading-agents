## Strategies

### TQQQ / SQQQ mean-reversion pairs agent

A statistical pairs-trading agent that trades the joint mispricing between TQQQ (+3× QQQ daily) and SQQQ (−3× QQQ daily).

**Important — this is not arbitrage.** TQQQ and SQQQ are leveraged ETFs that decay over time and re-base daily. There is no risk-free spread between them. The agent implements a short-horizon **mean-reversion** trade on the residual `s_t = ln(TQQQ) + ln(SQQQ)`, which is approximately stationary on short windows. The strategy has positive expected value in choppy markets and **loses money in trending markets** — it has negative skew like most mean-reversion strategies.

#### How the signal works

1. Subscribe to live L1 quotes for TQQQ and SQQQ.
2. Maintain a rolling window of `s_t = ln(TQQQ_mid) + ln(SQQQ_mid)`.
3. Compute `z = (s_t − mean) / std`.
4. **Entry**: `z ≥ +z_enter` → short both (`SHORT_SPREAD`).  `z ≤ −z_enter` → long both (`LONG_SPREAD`).
5. **Exit**: `|z| ≤ z_exit` → take profit (mean reverted).
6. **Stop**: `|z| ≥ z_stop` against the position → cut the loss (structural break).

#### Running it

```powershell
# Default: $5k per leg, z_enter=2.0, paper TWS port 7497, runs until Ctrl-C
.\.venv\Scripts\python.exe run_pairs_agent.py

# Conservative knobs + auto-stop after 10 minutes
.\.venv\Scripts\python.exe run_pairs_agent.py `
    --dollars-per-leg 2000 `
    --z-enter 2.5 `
    --z-exit 0.5 `
    --z-stop 4.0 `
    --lookback 600 `
    --max-daily-loss 250 `
    --duration 600
```

| Flag | Default | Notes |
|---|---|---|
| `--dollars-per-leg` | `5000` | Each leg gets this much $ exposure (dollar-neutral pair). Total notional ≈ 2× |
| `--z-enter` | `2.0` | Higher = fewer, higher-conviction trades |
| `--z-exit` | `0.5` | Lower = wait closer to mean before taking profit |
| `--z-stop` | `4.0` | Adverse cutoff for structural breaks |
| `--lookback` | `300` | Window size in quote samples (roughly 5 min at 1 Hz) |
| `--cooldown` | `30.0` | Seconds to wait after closing before re-entering |
| `--max-daily-loss` | `500` | RiskManager halts trading if session PnL falls below this |
| `--max-total-notional` | `50000` | Hard cap on combined notional |
| `--duration` | `0` | Auto-stop after N seconds; `0` = run forever |
| `--force-after-hours` | off | Allow running outside RTH (paper fills are unreliable then) |

#### Safety features

- Refuses to run on a live-trading port (`7496` / `4001`) — paper only by design.
- Refuses to run outside US regular trading hours unless `--force-after-hours` is set.
- Ctrl-C triggers a clean shutdown that **force-flattens** any open position before disconnecting.
- Every order is gated by `RiskManager` (rate limit, per-symbol & total notional, daily loss kill switch).
- Re-entrancy guard prevents firing duplicate orders on rapid quote bursts.

#### Realistic expectations

- TQQQ/SQQQ are highly liquid and tight (typically 1¢ spread on each). Commissions and crossing the spread are your main costs — about $4 round-trip per position pair at IBKR's default tiered rates.
- The strategy needs many trades to overcome those costs. On a 45-second smoke run, expect to lose a few dollars to noise — that's normal, not a bug.
- Backtest before believing in it. The signal is **not** a guaranteed edge. Most retail attempts at this lose money over time once frictions are accounted for.

#### Files

```
strategies/
├── pairs_signal.py        # Pure-Python z-score signal (PairsSignal, SignalConfig)
└── tqqq_sqqq_agent.py     # Event-driven agent (TqqqSqqqPairsAgent, AgentConfig)
run_pairs_agent.py         # CLI runner with safety gates
```

---
