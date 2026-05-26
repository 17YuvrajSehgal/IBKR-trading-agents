# Commands

Reference for running the agents and utilities. All commands assume the venv
is set up and TWS (or IB Gateway) is running on paper port `7497` with the
API enabled.

> **Default port is paper (7497).** Every runner refuses to start on the live
> port (7496 / 4001). To override see [the live-trading note](#switching-to-live-trading).

---

## Single terminal — pick one agent

### Trade without news agent

```powershell
# Single symbol (TSLA) on multi-timeframe technicals
.\.venv\Scripts\python.exe run_regime_agent.py

# Multiple symbols, default 6 names (MU, SNDK, TSLA, NVDA, AMD, QCOM)
.\.venv\Scripts\python.exe run_multi_agent.py

# Full watchlist (238 symbols)
.\.venv\Scripts\python.exe run_multi_agent.py --watchlist watchlist-1.json

# One category from the watchlist
.\.venv\Scripts\python.exe run_multi_agent.py --watchlist watchlist-1.json --category Semiconductors

# Multiple categories
.\.venv\Scripts\python.exe run_multi_agent.py --watchlist watchlist-1.json --category "Semiconductors,Growth Tech"

# Pairs trade (TQQQ vs SQQQ mean reversion)
.\.venv\Scripts\python.exe run_pairs_agent.py
```

### Protect profits on existing positions (no new trades)

The trailing-stop agent watches every open position in the account and
ratchets the stop into profit as price moves favorably. It NEVER opens
new positions — it only closes. Run it alongside any trading agent.

```powershell
# Default — 0.7% initial stop, 0.5% breakeven trigger, 0.5% trail
.\.venv\Scripts\python.exe run_trailing_stop_agent.py

# Tighter trail (locks profit faster, exits sooner)
.\.venv\Scripts\python.exe run_trailing_stop_agent.py `
    --initial-stop-pct 0.005 --breakeven-trigger-pct 0.003 --trail-pct 0.003

# Only manage positions opened DURING this agent's lifetime
# (skip pre-existing positions at startup)
.\.venv\Scripts\python.exe run_trailing_stop_agent.py --manage-only-new

# Run for the trading session then leave positions as-is
.\.venv\Scripts\python.exe run_trailing_stop_agent.py --duration 23400
```

The three trail phases:

| Phase     | When                                          | Stop is                              |
| --------- | --------------------------------------------- | ------------------------------------ |
| INITIAL   | Position just opened                          | Entry ± `initial_stop_pct` (loss)    |
| BREAKEVEN | Favorable move ≥ `breakeven_trigger_pct`      | Forced to entry (no-loss locked)     |
| TRAILING  | Stop has ratcheted above entry (longs)        | Peak × (1 − `trail_pct`) (profit)    |

### Trade with news only (no regime / multi)

```powershell
# Default 10 large caps, full trading
.\.venv\Scripts\python.exe run_news_agent.py

# Narrower universe + tighter risk
.\.venv\Scripts\python.exe run_news_agent.py `
    --symbols TSLA,NVDA,COIN `
    --threshold 3.5 `
    --dollars-per-trade 2000

# Long-only (no shorts even on negative headlines)
.\.venv\Scripts\python.exe run_news_agent.py --shorts off

# Observe-only: classify + log but DO NOT trade
.\.venv\Scripts\python.exe run_news_agent.py --observe
```

---

## Two terminals — trade + observe news in parallel

Each agent needs its own `--client-id` since they share TWS but each connection
requires a unique ID. Give the news observer a high one so it never collides
with the trading agent.

**Terminal 1 — does the trading:**

```powershell
.\.venv\Scripts\python.exe run_multi_agent.py --watchlist watchlist-1.json --category Semiconductors
```

**Terminal 2 — observes news, never trades:**

```powershell
.\.venv\Scripts\python.exe run_news_agent.py `
    --observe `
    --symbols NVDA,AMD,MU,INTC,AVGO,QCOM `
    --client-id 99
```

The news observer subscribes, classifies headlines, and records to JSONL — but
the `--observe` flag means it can't place an order even if the lexicon fires.
Safe to leave running for hours.

---

## Three terminals — trade + trail + observe (recommended live setup)

**Terminal 1 — opens positions:**

```powershell
.\.venv\Scripts\python.exe run_multi_agent.py --watchlist watchlist-1.json --category Semiconductors
```

**Terminal 2 — protects profit on every position:**

```powershell
.\.venv\Scripts\python.exe run_trailing_stop_agent.py
```

**Terminal 3 — watches the news stream without trading:**

```powershell
.\.venv\Scripts\python.exe run_news_agent.py --observe --client-id 99
```

How they cooperate via IBKR position events:

1. Terminal 1 opens a LONG TSLA based on a pullback-to-EMA signal.
2. Terminal 2 sees the new position (positionEvent), starts tracking with
   an initial 0.7% stop, ratchets it as price moves up.
3. Price runs up 1.5%, pulls back 0.5% — Terminal 2's trail triggers, fires
   a market close. Profit is locked.
4. Terminal 1's regime agent detects the external close (positionEvent),
   clears its internal state, and on the next 5-min bar can re-enter if
   the regime + bias still align.

Terminal 3 is purely observational — it never sends orders. Use the JSONL
to identify which headlines coincided with which trades for post-session
review.

---

## Time-bounded runs

Add `--duration <seconds>` to any runner so it auto-stops cleanly and
force-flattens any open position:

```powershell
# Run for 30 minutes then flatten and exit
.\.venv\Scripts\python.exe run_multi_agent.py --duration 1800

# Run news agent in observe mode for a full trading day (6.5 hours)
.\.venv\Scripts\python.exe run_news_agent.py --observe --duration 23400 --client-id 99

# Quick 60-second sanity check
.\.venv\Scripts\python.exe run_regime_agent.py --duration 60
```

---

## Common parameter tweaks

### Multi-agent runner

```powershell
# Conservative: $300 risk per trade, $5k daily cap
.\.venv\Scripts\python.exe run_multi_agent.py `
    --risk-pct 0.001 --max-risk-usd 300 --max-daily-loss 5000

# Aggressive: bigger positions on a smaller universe
.\.venv\Scripts\python.exe run_multi_agent.py `
    --symbols TSLA,NVDA,AMD --dollars-per-leg 10000 --max-notional-per-symbol 100000

# Tighter stops, longer holds
.\.venv\Scripts\python.exe run_multi_agent.py `
    --stop-atr 1.0 --target-atr 3.0 --max-bars-held 48
```

### News agent

```powershell
# Only fire on very strong sentiment
.\.venv\Scripts\python.exe run_news_agent.py --threshold 4.0

# Tighter stops + shorter time-stop (5 min)
.\.venv\Scripts\python.exe run_news_agent.py `
    --stop-pct 0.005 --target-pct 0.010 --time-stop-min 5

# Symmetric shorts (any actionable negative, not just premium-provider)
.\.venv\Scripts\python.exe run_news_agent.py --shorts symmetric
```

---

## Cleanup utilities (no agent running)

```powershell
# See what's open without touching anything
.\.venv\Scripts\python.exe flatten_positions.py --dry-run

# Close everything (cancels any working orders first, then market-closes)
.\.venv\Scripts\python.exe flatten_positions.py

# Close one name
.\.venv\Scripts\python.exe flatten_positions.py --symbol TSLA

# Sanity-check the broker connection + every manager works
.\.venv\Scripts\python.exe smoke_test.py

# Smoke test with a tiny order/cancel cycle
.\.venv\Scripts\python.exe smoke_test.py --place-order
```

---

## Watchlist exploration (no trading)

```powershell
# Print the categories in the watchlist
.\.venv\Scripts\python.exe run_multi_agent.py `
    --watchlist watchlist-1.json --list-categories

# Try loading a single category without going live
# (still connects to TWS — pass duration 1 so it exits immediately after warmup)
.\.venv\Scripts\python.exe run_multi_agent.py `
    --watchlist watchlist-1.json --category Healthcare --duration 1
```

---

## Reading session logs

After any agent run, two artifacts are written:

```
logs/<runner>-YYYY-MM-DDTHH-MM-SS.log     # text log (human-readable)
logs/<runner>-YYYY-MM-DDTHH-MM-SS.jsonl   # structured events (one per line)
```

### List recent sessions

```powershell
Get-ChildItem logs\ | Sort-Object LastWriteTime -Descending | Select-Object -First 10
```

### Inspect the JSONL with PowerShell

```powershell
# All closes with PnL and reason
Get-Content logs\multi-*.jsonl |
  ConvertFrom-Json |
  Where-Object { $_.kind -eq 'close' } |
  Select-Object symbol, pnl, reason, entry, exit

# All would-have-traded headlines from observe mode
Get-Content logs\news-*.jsonl |
  ConvertFrom-Json |
  Where-Object { $_.kind -eq 'would_open' } |
  Select-Object symbol, side, score, headline

# Total realized PnL per symbol
Get-Content logs\multi-*.jsonl |
  ConvertFrom-Json |
  Where-Object { $_.kind -eq 'close' } |
  Group-Object symbol |
  ForEach-Object {
    [pscustomobject]@{
      Symbol = $_.Name
      Trades = $_.Count
      PnL    = ($_.Group | Measure-Object -Property pnl -Sum).Sum
    }
  } | Sort-Object PnL -Descending
```

### Inspect with `jq` (if installed)

```powershell
jq -c 'select(.kind=="close")' logs\multi-*.jsonl
jq -s 'map(select(.kind=="close") | .pnl) | add' logs\multi-*.jsonl
```

---

## Quick client-ID cheat sheet

Default client IDs by runner — change with `--client-id <0-32>` if you see
`Error 326: Client ID already in use`:

| Runner                       | Default |
| ---------------------------- | ------- |
| `run_regime_agent.py`        | 31      |
| `run_multi_agent.py`         | 4       |
| `run_news_agent.py`          | 22      |
| `run_pairs_agent.py`         | 21      |
| `run_trailing_stop_agent.py` | 8       |
| `flatten_positions.py`       | 15      |
| `smoke_test.py`              | 11      |

Any value in `0–32` that's free works. If a previous run hasn't released the
ID yet, just pick a different one; the previous connection will time out on
its own within ~30 seconds.

---

## Switching to live trading

Don't.

If you actually need to:

1. Confirm the strategy has been validated on paper for **at least several weeks**
2. Halve every sizing parameter (`--risk-pct`, `--max-risk-usd`, `--dollars-per-trade`)
3. Pass `--port 7496` for TWS or `--port 4001` for Gateway
4. Use `IBKRConfig.live_trading()` instead of `paper_trading()` if you're writing custom code

The runners deliberately refuse `--port 7496` / `--port 4001` to make this a
two-step decision rather than a one-flag mistake.

---

## Recommended starter sequence

If you're new to the agents, run them in this order to build confidence:

```powershell
# 1. Verify TWS is reachable + connection works
.\.venv\Scripts\python.exe smoke_test.py

# 2. See the watchlist categories
.\.venv\Scripts\python.exe run_multi_agent.py --watchlist watchlist-1.json --list-categories

# 3. Run the news agent in observe mode for 20 minutes — no risk, just see headlines
.\.venv\Scripts\python.exe run_news_agent.py --observe --duration 1200 --client-id 99

# 4. Run the multi-agent on one category for 15 minutes
.\.venv\Scripts\python.exe run_multi_agent.py --watchlist watchlist-1.json --category Semiconductors --duration 900

# 5. Check what happened
Get-Content logs\multi-*.jsonl | ConvertFrom-Json | Where-Object { $_.kind -eq 'close' }

# 6. Clean up if anything is still open
.\.venv\Scripts\python.exe flatten_positions.py --dry-run
.\.venv\Scripts\python.exe flatten_positions.py
```
