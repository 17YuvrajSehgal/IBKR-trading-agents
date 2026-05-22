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
