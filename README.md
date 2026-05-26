# IBKR Trading Agents

A production-style async Python interface to **Interactive Brokers TWS / IB Gateway**, built on top of [`ib_async`](https://github.com/ib-api-reloaded/ib_async).

It gives you clean, typed building blocks for connecting, fetching account and position data, streaming L1 quotes and news, placing/managing orders, and gating everything through a pluggable risk layer — so you can focus on strategy rather than plumbing.

---

## Table of contents

1. [Prerequisites](#prerequisites)
2. [Setting up TWS / IB Gateway](#setting-up-tws--ib-gateway)
3. [Installation](#installation)
4. [Smoke test](#smoke-test)
5. [Quick start](#quick-start)
6. [Package layout](#package-layout)
7. [Common pitfalls](#common-pitfalls)

---

## Prerequisites

- **Python 3.10+** (developed against 3.14).
- **An IBKR account** — paper or live. Paper trading is free; create one at [interactivebrokers.com](https://www.interactivebrokers.com/) → *Account Management* → *Paper Trading Account*.
- **Trader Workstation (TWS)** or **IB Gateway** installed and logged in. Download from the [IBKR client portal](https://www.interactivebrokers.com/en/trading/tws.php).

> TWS is the full trading GUI. IB Gateway is a headless equivalent — same API, less memory, no charts. Either works; this guide uses TWS.

---

## Setting up TWS / IB Gateway

The API socket must be explicitly enabled before any Python client can connect. **This is the most common cause of `ConnectionRefusedError`.**

### 1. Log into TWS with your paper account

Switch the login tab to **Paper Trading** before signing in. The window title will read *"…(Simulated Trading)"* — that's how you know paper is active.

### 2. Enable the API

In TWS, open **Edit → Global Configuration → API → Settings** and apply:

| Setting | Value |
|---|---|
| Enable ActiveX and Socket Clients | ✅ checked |
| Socket port | **7497** (paper)  ·  **7496** (live) |
| Allow connections from localhost only | ✅ checked (recommended) |
| Read-Only API | ❌ unchecked (or check it if you only want to read data) |
| Master API client ID | leave blank |
| Download open orders on connection | ✅ checked |

Click **Apply** → **OK**. TWS may prompt you to restart — do so.

### 3. (Optional) Trusted IPs

Under **API → Trusted IPs**, add `127.0.0.1` if you plan to script from the same machine.

### 4. Verify the port is open

```powershell
Test-NetConnection -ComputerName 127.0.0.1 -Port 7497 -InformationLevel Quiet
# True  → API is reachable, you're done
# False → API socket isn't listening; recheck step 2
```

### IB Gateway equivalent

Same flow, simpler UI: **Configure → Settings → API**. Default ports differ:
- Paper: **4002**
- Live: **4001**

Pass `port=4002` to `IBKRConfig.paper_trading(port=4002)` if you use Gateway.

---

## Installation

```powershell
# Clone, then from project root:
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Dependencies (`requirements.txt`):
```
ib_async>=2.0.0
nest_asyncio>=1.6.0
```

---

## Smoke test

The repo ships with a self-contained end-to-end check that exercises every manager against your running TWS. Run it after the setup above to confirm everything works:

```powershell
.\.venv\Scripts\python.exe smoke_test.py
```

Add `--place-order` to also place a far-out-of-the-money AAPL limit order and immediately cancel it (safe — it cannot fill):

```powershell
.\.venv\Scripts\python.exe smoke_test.py --place-order
```

Expected output:

```
[PASS] Connect to TWS — connected to 127.0.0.1:7497, client_id=11
[PASS] Health check — latency=0.2ms
[PASS] Account summary — account=DU…, NetLiq=…, Cash=…
[PASS] Positions / portfolio — no open positions
[PASS] Market data (AAPL) — bid=… ask=… last=…
[PASS] Order placement + cancellation — order N final status: Cancelled
[PASS] Risk manager logic — approve/deny/halt all behave correctly
```

CLI flags:

| Flag | Default | Purpose |
|---|---|---|
| `--host` | `127.0.0.1` | TWS host |
| `--port` | `7497` | Paper port (use `7496` for live, `4002` for paper Gateway) |
| `--client-id` | `11` | Any 0–32, must be unique per simultaneous connection |
| `--place-order` | off | Also run the order place/cancel step |

---

## Quick start

### Connect and get account info

```python
import asyncio
from ibkr.config import IBKRConfig
from ibkr.connection import IBKRConnection

async def main():
    config = IBKRConfig.paper_trading()  # 127.0.0.1:7497, client_id=1
    async with IBKRConnection(config) as conn:
        summary = await conn.get_account_summary()
        print("Net Liquidation:", summary["NetLiquidation"]["value"])
        print("Cash:",            summary["TotalCashValue"]["value"])

asyncio.run(main())
```

### Stream real-time quotes

```python
from ibkr.market_data import MarketDataManager

async with IBKRConnection(config) as conn:
    md = MarketDataManager(conn.ib)
    await md.subscribe("AAPL")
    await md.subscribe("MSFT")

    md.subscribe_quotes(lambda q: print(q))   # push callback on every tick

    await asyncio.sleep(10)                   # let ticks flow for 10s
    md.unsubscribe_all()
```

### Place and cancel an order

```python
from ibkr.trading import OrderManager, OrderAction

async with IBKRConnection(config) as conn:
    om = OrderManager(conn.ib)

    # Far-OOM limit so we won't accidentally fill while testing
    order = await om.place_limit_order("AAPL", 1, OrderAction.BUY, limit_price=1.00)
    print("placed:", order.order_id)

    await asyncio.sleep(1)
    await om.cancel_order(order.order_id)
    print("status:", await om.get_order_status(order.order_id))
```

### Read positions

```python
from ibkr.positions import PositionManager

async with IBKRConnection(config) as conn:
    pm = PositionManager(conn.ib)
    for pos in await pm.get_positions():
        print(f"{pos.symbol}: {pos.quantity} @ ${pos.avg_cost:.2f}")

    # Optional: opt in to live market_price / unrealized_pnl
    await pm.subscribe_market_updates()
    summary = await pm.get_portfolio_summary()
    print(summary)
```

### Gate orders through the risk layer

```python
from ibkr.risk import RiskManager, RiskLimits

risk = RiskManager(RiskLimits(
    max_order_quantity   = 500,
    max_notional_per_symbol = 20_000,
    max_total_notional   = 100_000,
    max_daily_loss       = 1_000,
))

check = risk.check_order("AAPL", "BUY", 100, price=200.0)
if check.approved:
    order = await om.place_market_order("AAPL", 100, "BUY")
    risk.record_fill("AAPL", "BUY", 100, 200.0)
else:
    print("blocked:", check.code, check.reason)
```

### Fetch news

```python
from ibkr.news import NewsManager

async with IBKRConnection(config) as conn:
    news = NewsManager(conn.ib)
    headlines = await news.fetch_historical_headlines("AAPL", lookback_hours=24)
    for h in headlines[:5]:
        print(h.time, h.provider_code, h.headline)
```

---

## Session logging & error handling

Every runner writes two paired files per session into `logs/`:

```
logs/{runner}-YYYY-MM-DDTHH-MM-SS.log     # text log (human-readable)
logs/{runner}-YYYY-MM-DDTHH-MM-SS.jsonl   # structured events (machine-readable)
```

The JSONL stream is one JSON object per line, with a UTC millisecond timestamp and a `kind` discriminator. Events include `runner_start`, `warmup_complete` (per symbol), `bias_change`, `open`, `close`, `order_failed`, `session_summary`, and `session_end`. Trivial to grep/aggregate:

```powershell
# Show all closes with their PnL
Get-Content logs\multi-*.jsonl | %{ $_ | ConvertFrom-Json } | ?{ $_.kind -eq 'close' } | Select symbol,pnl,reason

# Or with jq if you have it
jq -c 'select(.kind=="close")' logs/multi-*.jsonl
```

Share these files when reporting an issue or asking for review — they capture every decision and outcome.

### Emergency flatten

If a previous run left positions open (or you just want a clean slate before a new run), use the flatten utility:

```powershell
# Preview what would be closed (no orders sent)
.\.venv\Scripts\python.exe flatten_positions.py --dry-run

# Close everything
.\.venv\Scripts\python.exe flatten_positions.py

# Close one symbol only
.\.venv\Scripts\python.exe flatten_positions.py --symbol TSLA
```

It cancels any working orders first (so previous-run leftovers don't fight you), then sends market closes and waits for fills. Refuses the live-trading port unless `--force-live`.

### IBKR message classification

The `ibkr.error_codes` module maps every IBKR message code to a `Severity` (INFO / WARNING / ERROR / FATAL) and `Category` (CONNECTION / DATA_FARM / ORDER / DATA / PACING / SYSTEM / UNKNOWN). The connection handler uses this to route messages correctly:

- Data-farm heartbeats (2104/2106/2108/2158/…) → DEBUG (silenced by default)
- Daily-reset connection lifecycle (1100/1101/1102) → WARNING + triggers `on_reconnect` callbacks for strategies to re-subscribe
- Real errors (200/201/202) → ERROR
- Fatals (326/502/504) → CRITICAL

Strategies register for reconnects via `conn.on_reconnect(callback)` — the agent calls them after a successful auto-reconnect so it can re-subscribe historical data subscriptions that the server reset dropped.

---

## Strategies

Reference agents are included. All are paper-only by default — the runners refuse the live-trading port — and all force-flatten any open position on Ctrl-C.

### 1. TQQQ / SQQQ mean-reversion pairs agent

A statistical pairs trade on the joint mispricing `s = ln(TQQQ) + ln(SQQQ)`, which is approximately stationary on short windows because TQQQ targets +3×QQQ and SQQQ targets −3×QQQ daily.

**This is not arbitrage.** It's a mean-reversion bet that has positive expected value in choppy markets and loses money in trends.

```powershell
.\.venv\Scripts\python.exe run_pairs_agent.py
.\.venv\Scripts\python.exe run_pairs_agent.py --dollars-per-leg 2000 --z-enter 2.5 --duration 600
```

| Flag | Default | Purpose |
|---|---|---|
| `--dollars-per-leg` | `5000` | $ exposure per leg (total notional ≈ 2×) |
| `--z-enter` | `2.0` | Open when \|z\| exceeds this |
| `--z-exit` | `0.5` | Close when \|z\| returns under this |
| `--z-stop` | `4.0` | Stop out on adverse move past this |
| `--lookback` | `300` | Rolling window samples |
| `--max-daily-loss` | `500` | RiskManager kill-switch |
| `--duration` | `0` | Auto-stop after N seconds |

Files: `strategies/pairs_signal.py` (pure signal), `strategies/tqqq_sqqq_agent.py` (executor), `run_pairs_agent.py`.

### 2. Regime-adaptive multi-timeframe technical agent (TSLA)

A multi-strategy agent that **switches its trading style based on detected market regime** instead of betting one style will fit every condition.

```
1-hour bars ──► BiasDetector ──► Bias (BULL / BEAR / NEUTRAL)
                                              │
5-min bars ───► RegimeDetector ──► Regime ◄───┘
                                       │
                                       ▼
                              SignalGenerator
                                       │
                                       ▼
                    OrderManager (sized by ATR, gated by RiskManager)
```

**Regimes** (detected on the entry timeframe via ADX + ATR/price):
- `TREND_UP` / `TREND_DOWN` — ADX ≥ 25, follow EMA direction
- `RANGE` — ADX < 20, mean-revert
- `HIGH_VOL` — ATR/price > 1.5%, stand aside
- `AMBIGUOUS` — ADX in [20, 25], no conviction, stand aside

**Entries**: trend → pullback to EMA20 + RSI confirmation;  range → Bollinger band touch + RSI extreme.

**Exits**: 1.5×ATR stop, 2.5×ATR target, time-stop after 24 bars, immediate exit on regime flip against the position.

**Sizing**: volatility-targeted. `shares = risk_$_per_trade / (1.5 × ATR)` — so a TSLA $400 move risks the same dollars as a TSLA $40 move.

```powershell
# Default: TSLA, HTF=1h, MTF=5m, 0.3% risk per trade
.\.venv\Scripts\python.exe run_regime_agent.py

# Different symbol / timeframe
.\.venv\Scripts\python.exe run_regime_agent.py --symbol NVDA --mtf "15 mins" --duration 1800

# More conservative
.\.venv\Scripts\python.exe run_regime_agent.py `
    --risk-pct 0.002 --max-risk-usd 100 --max-daily-loss 300 `
    --stop-atr 2.0 --target-atr 3.0
```

| Flag | Default | Purpose |
|---|---|---|
| `--symbol` | `TSLA` | Any tradeable US stock |
| `--htf` | `1 hour` | Bias timeframe |
| `--mtf` | `5 mins` | Entry timeframe |
| `--risk-pct` | `0.003` | Fraction of NetLiq risked per trade |
| `--max-risk-usd` | `200` | Absolute cap on per-trade $ risk |
| `--max-notional` | `30000` | Max $ exposure per position |
| `--stop-atr` | `1.5` | Stop = entry ± stop-atr × ATR |
| `--target-atr` | `2.5` | Target = entry ± target-atr × ATR |
| `--max-bars-held` | `24` | Time-stop in bars |
| `--max-daily-loss` | `500` | RiskManager kill switch |
| `--shorts` | `confident` | Short policy: `confident` / `symmetric` / `off` (see below) |
| `--duration` | `0` | Auto-stop after N seconds |

**Short policy** — equities have a structural long bias, so the agent treats shorts more conservatively than longs by default:

| Mode | Long requires | Short requires |
|---|---|---|
| `confident` (default) | HTF ≠ BEAR | **HTF == BEAR** (strict) |
| `symmetric` | HTF ≠ BEAR | HTF ≠ BULL (mirror of longs) |
| `off` | HTF ≠ BEAR | (never) |

Under the default `confident`, a short fires only when **all three** align: HTF bias is explicitly BEAR, MTF regime is TREND_DOWN (trend short) or RANGE (BB-upper mean-reversion short), and the entry trigger hits. Use `--shorts symmetric` if you want the agent to short on neutral HTF bias as well; use `--shorts off` for long-only.

**Caveats on real shorts** (paper doesn't capture these):
- IBKR refuses HTB (hard-to-borrow) names — paper happily shorts anything.
- Borrow fees are charged daily on real shorts (range: ~0.25%/yr for ETB to 30%+ for HTB).
- Earnings / news can gap shorts brutally — overnight shorts have no theoretical loss cap going up.

Files: `strategies/regime_adaptive/{indicators,bars,regime,signals,agent}.py`, `run_regime_agent.py`.

### 3. Multi-symbol regime-adaptive runner

Runs the regime-adaptive agent on multiple symbols **in parallel**, on a single IB connection. All agents share:

- one connection (single `client_id`)
- one `OrderManager` (no duplicate event handlers)
- one **shared `RiskManager`** so the daily-loss kill switch and total-notional cap are **global** across symbols, not per-name

Each agent maintains its own bar subscriptions, indicators, regime state, position, and stats — decisions are completely independent per symbol.

```powershell
# Default: MU, SNDK, TSLA, NVDA, AMD, QCOM
.\.venv\Scripts\python.exe run_multi_agent.py

# Custom basket and auto-stop
.\.venv\Scripts\python.exe run_multi_agent.py --symbols TSLA,NVDA,AMD --duration 1800

# Tighter risk
.\.venv\Scripts\python.exe run_multi_agent.py `
    --risk-pct 0.001 --max-risk-usd 100 --max-daily-loss 750
```

| Flag | Default | Purpose |
|---|---|---|
| `--symbols` | `MU,SNDK,TSLA,NVDA,AMD,QCOM` | Comma-separated list |
| `--htf` | `1 hour` | Bias timeframe (applied to all) |
| `--mtf` | `5 mins` | Entry timeframe (applied to all) |
| `--risk-pct` | `0.002` | Per-trade risk as fraction of NetLiq (lower than single-symbol — multiple may fire) |
| `--max-risk-usd` | `150` | Per-trade $ risk cap |
| `--max-notional-per-symbol` | `25000` | Per-symbol position cap |
| `--max-total-notional` | `150000` | **Global** notional cap (all symbols) |
| `--max-daily-loss` | `1500` | **Global** session loss kill switch |
| `--stop-atr` | `1.5` | Stop multiplier |
| `--target-atr` | `2.5` | Target multiplier |
| `--duration` | `0` | Auto-stop after N seconds |
| `--force-after-hours` | off | Allow outside RTH |

On shutdown the runner force-flattens every open position and prints a per-symbol PnL summary:

```
Multi-agent session summary
  MU     opened=2  target=1  stop=1  time=0  regime=0  PnL=$  +12.40
  SNDK   opened=1  target=0  stop=0  time=1  regime=0  PnL=$   -3.10
  TSLA   opened=0  target=0  stop=0  time=0  regime=0  PnL=$   +0.00
  ...
  TOTAL realized PnL: $+15.30
```

Files: `run_multi_agent.py` (reuses the same `RegimeAdaptiveAgent` internally).

### 4. News-driven agent

A reactive agent that subscribes to live IBKR news for a focused symbol list and trades on **actionable** headlines — strong sentiment from a credible provider.

```
NewsManager (per-symbol headline stream via genericTick 292)
   ↓
NewsClassifier  (keyword lexicon; phrase scoring -5..+5)
   ↓ if |score| ≥ threshold + provider weight ≥ ε + cooldown elapsed + flat
RiskManager (atomic check + reserve)
   ↓
OrderManager → market order with % stop / target / time-stop
```

**Sentiment scoring** uses a hand-curated lexicon of ~150 phrases — earnings beats, guidance cuts, FDA approvals, lawsuits, etc. Compound phrases ("fails to beat", "raises full-year guidance") are matched longest-first so negation is encoded in the phrase. Provider weight is multiplied in (DJNL = 1.0, BRFG = 0.8, BZ = 0.6, unknown = 0.5).

**Exits** — news effects decay fast, so the agent uses **percentage** stops + time-stop instead of ATR:
- Stop = entry × (1 ± `--stop-pct`)  (default 0.7%)
- Target = entry × (1 ± `--target-pct`) (default 1.4%, so 2:1 R:R)
- Time stop = `--time-stop-min` minutes (default 30)
- Cooldown per symbol = `--cooldown-min` (default 60) prevents follow-up headlines retriggering

```powershell
# Default — 10 high-liquidity names, $5k per trade, paper port
.\.venv\Scripts\python.exe run_news_agent.py

# Narrower focus + tighter risk
.\.venv\Scripts\python.exe run_news_agent.py `
    --symbols TSLA,NVDA,COIN --threshold 3.5 --dollars-per-trade 2000 `
    --duration 7200

# Long-only
.\.venv\Scripts\python.exe run_news_agent.py --shorts off
```

| Flag | Default | Purpose |
|---|---|---|
| `--symbols` | 10 large-cap names | Comma-separated universe |
| `--threshold` | `2.5` | Min \|weighted score\| to fire |
| `--shorts` | `confident` | `confident` requires strong negative + premium provider |
| `--short-strong-threshold` | `3.5` | Used in `confident` mode |
| `--dollars-per-trade` | `5000` | Gross $ exposure per trade |
| `--stop-pct` | `0.007` | 0.7% stop loss |
| `--target-pct` | `0.014` | 1.4% take-profit |
| `--time-stop-min` | `30` | Time-based exit |
| `--cooldown-min` | `60` | Per-symbol re-entry block |
| `--max-daily-loss` | `5000` | RiskManager kill switch |
| `--duration` | `0` | Auto-stop after N seconds |

**Honest caveats specific to news trading:**

- **IBKR's retail news is delayed** relative to Bloomberg/Reuters direct feeds. By the time a headline arrives on your terminal, the institutional move is often already over.
- **Symbol tagging is imperfect** — some headlines arrive via topic streams (DJ-RTG, DJ-N) without a symbol tag, so they can't trigger per-symbol trades.
- **Free providers (BRFG) are slow and noisy** vs paid (DJNL/DJ-RT). Premium subscriptions help materially.
- **The lexicon is the bottleneck** — if your strategy isn't firing, it's probably because real headlines don't match the templates. Read your JSONL `headline` and `score` events to find new phrases worth adding.

Files: `strategies/news_driven/{classifier,agent}.py`, `run_news_agent.py`.

---

### Honest expectations across all agents

- **Neither will print money.** Both have negative skew: many small wins, occasional larger losses.
- **Win rate ~45–55% is normal.** The edge, if any, is in the R:R management.
- **Overnight gaps bypass intraday stops** — a single Musk tweet can wipe weeks of gains on TSLA.
- **Paper fills are optimistic** vs live. Backtest fills are optimistic vs paper.
- **Treat the framework as scaffolding** — backtest on real historical data, walk-forward optimize parameters, monitor in paper for weeks before considering live capital.

---

## Package layout

```
ibkr/
├── config.py         # IBKRConfig — host/port/client_id + paper/live/readonly factories
├── connection.py     # IBKRConnection — async context manager, auto-reconnect, health
├── trading.py        # OrderManager + OrderInfo — market/limit/stop, cancel, status
├── positions.py      # PositionManager — positions, portfolio summary, live PnL opt-in
├── market_data.py    # MarketDataManager — L1 streaming quotes, push callbacks
├── news.py           # NewsManager — historical + live headlines, article bodies
├── risk.py           # RiskManager — pre-trade gating (rate, exposure, loss, kill switch)
├── utils.py          # contract factories (Stock/Option/Future/Forex/Index), helpers
├── exceptions.py     # IBKRException hierarchy
└── logging_config.py # setup_logging() — multi-file rotation by component
```

Pure-Python modules (`config`, `exceptions`, `risk`, `utils`) have **no `ib_async` dependency** — you can unit-test or import them without a broker connection.

---

## Common pitfalls

| Symptom | Likely cause | Fix |
|---|---|---|
| `ConnectionRefusedError`, `errno 1225` | API not enabled in TWS | See [Setting up TWS](#setting-up-tws--ib-gateway) step 2 |
| `Connection timeout` | TWS frozen, sleeping laptop, or wrong port | Restart TWS; check `Test-NetConnection -Port 7497` |
| `Already in use: client id 1` | Another script (or PyCharm scratch) connected with same id | Pick a distinct `client_id` (0–32) |
| `This event loop is already running` | Called a sync `ib.*` method (e.g. `ib.reqAccountUpdates`) from inside async code | Use the `*Async` variant (`reqAccountUpdatesAsync`, etc.) |
| `Error 10197/162: market data not subscribed` | No live data subscription for this symbol on your account | Use IBKR's free *Delayed-Frozen* mode, or subscribe to the data feed |
| `Error 10349: Order TIF was set to DAY based on order preset` | Informational — TWS applied a preset | Ignore; the order still goes through |
| `Disconnected from TWS/Gateway` shortly after connect | Daily server reset (≈ midnight ET) | Auto-reconnect is enabled by default; or reconnect manually |

### Paper vs live

| | Paper | Live |
|---|---|---|
| TWS port | `7497` | `7496` |
| Gateway port | `4002` | `4001` |
| Factory | `IBKRConfig.paper_trading()` | `IBKRConfig.live_trading()` |

Always finish a strategy on paper first. Switching to live is a one-line change — that's exactly the kind of footgun you want to have walked through twice.

### Logs

Call `ibkr.logging_config.setup_logging()` once at startup to write per-component log files into `./ibkr_logs/` (connection, trading, positions, all). Useful for post-mortems.

---

## Safety reminders

- Set `IBKRConfig(readonly=True)` (or use `IBKRConfig.readonly()`) while developing — the broker will refuse all order placements.
- Wire `RiskManager` in front of every `OrderManager.place_*` call. The risk layer is not automatic.
- The `--place-order` smoke test uses a $1 AAPL limit on purpose — it cannot fill at that price during normal market conditions.
- Live trading risks real capital. The contributors of this package are not responsible for trading losses.
