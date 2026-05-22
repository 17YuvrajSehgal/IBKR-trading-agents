"""
End-to-end smoke test for the ibkr package against a live paper TWS.

Run with:
    .venv\\Scripts\\python.exe smoke_test.py
    .venv\\Scripts\\python.exe smoke_test.py --place-order   (places + cancels a far-OOM limit order)

Exits with code 0 on success, 1 on failure.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import traceback
from typing import Any

from ibkr.config import IBKRConfig
from ibkr.connection import IBKRConnection
from ibkr.market_data import MarketDataManager
from ibkr.positions import PositionManager
from ibkr.trading import OrderManager, OrderAction
from ibkr.risk import RiskManager, RiskLimits


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("smoke")

PASS = "[PASS]"
FAIL = "[FAIL]"
SKIP = "[SKIP]"


class StepResult:
    def __init__(self, name: str):
        self.name = name
        self.status = "?"
        self.detail = ""

    def ok(self, detail: str = "") -> None:
        self.status = PASS
        self.detail = detail

    def fail(self, detail: str) -> None:
        self.status = FAIL
        self.detail = detail

    def skip(self, detail: str) -> None:
        self.status = SKIP
        self.detail = detail

    def __str__(self) -> str:
        return f"{self.status} {self.name}" + (f" — {self.detail}" if self.detail else "")


async def step_connect(conn: IBKRConnection) -> StepResult:
    r = StepResult("Connect to TWS")
    try:
        await conn.connect()
        r.ok(f"connected to {conn.config.host}:{conn.config.port}, client_id={conn.config.client_id}")
    except Exception as e:
        r.fail(str(e))
    return r


async def step_health(conn: IBKRConnection) -> StepResult:
    r = StepResult("Health check")
    try:
        h = await conn.check_health()
        r.ok(f"latency={h.get('latency_ms')}ms")
    except Exception as e:
        r.fail(str(e))
    return r


async def step_account(conn: IBKRConnection) -> StepResult:
    r = StepResult("Account summary")
    try:
        summary = await conn.get_account_summary()
        nlv = summary.get("NetLiquidation", {}).get("value", "?")
        cash = summary.get("TotalCashValue", {}).get("value", "?")
        account_id = next(iter({v["account"] for v in summary.values()}), "?")
        r.ok(f"account={account_id}, NetLiq={nlv}, Cash={cash}")
    except Exception as e:
        r.fail(str(e))
    return r


async def step_positions(ib) -> StepResult:
    r = StepResult("Positions / portfolio")
    try:
        pm = PositionManager(ib)
        positions = await pm.get_positions()
        if not positions:
            r.ok("no open positions")
        else:
            summary = await pm.get_portfolio_summary()
            r.ok(
                f"{summary.total_positions} positions, "
                f"value=${summary.total_market_value:,.2f}, "
                f"uPnL=${summary.total_unrealized_pnl:,.2f}"
            )
    except Exception as e:
        log.exception("positions step raised")
        r.fail(f"{type(e).__name__}: {e}")
    return r


async def step_market_data(ib) -> StepResult:
    r = StepResult("Market data (AAPL)")
    try:
        mdm = MarketDataManager(ib)
        await mdm.subscribe("AAPL")

        # Wait up to 5s for first tick
        for _ in range(50):
            q = mdm.get_quote("AAPL")
            if q and (q.is_tradeable or q._is_valid(q.last)):
                break
            await asyncio.sleep(0.1)

        q = mdm.get_quote("AAPL")
        mdm.unsubscribe_all()

        if q is None:
            r.fail("no quote object returned")
            return r

        bid = q.bid if q._is_valid(q.bid) else "?"
        ask = q.ask if q._is_valid(q.ask) else "?"
        last = q.last if q._is_valid(q.last) else "?"
        if bid == "?" and ask == "?" and last == "?":
            r.skip("no tick data (market closed or no data subscription)")
        else:
            r.ok(f"bid={bid} ask={ask} last={last}")
    except Exception as e:
        r.fail(str(e))
    return r


async def step_risk() -> StepResult:
    r = StepResult("Risk manager logic")
    try:
        rm = RiskManager(RiskLimits(max_order_quantity=100))
        approved = rm.check_order("AAPL", "BUY", 50, 200.0).approved
        denied = rm.check_order("AAPL", "BUY", 5_000, 200.0).approved
        rm.halt("test halt")
        halted = not rm.check_order("AAPL", "BUY", 1, 1.0).approved
        if approved and not denied and halted:
            r.ok("approve/deny/halt all behave correctly")
        else:
            r.fail(f"approved={approved}, denied_ok={not denied}, halted_ok={halted}")
    except Exception as e:
        r.fail(str(e))
    return r


async def step_order_lifecycle(ib, place_order: bool) -> StepResult:
    r = StepResult("Order placement + cancellation")
    if not place_order:
        r.skip("not requested (pass --place-order to enable)")
        return r
    try:
        om = OrderManager(ib)
        # Place a deeply OOM BUY limit so it cannot fill, then cancel it.
        order = await om.place_limit_order("AAPL", 1, OrderAction.BUY, limit_price=1.00)
        await asyncio.sleep(1.0)
        await om.cancel_order(order.order_id)
        await asyncio.sleep(1.0)
        status = await om.get_order_status(order.order_id)
        r.ok(f"order {order.order_id} final status: {status}")
    except Exception as e:
        r.fail(str(e))
    return r


async def main(args) -> int:
    config = IBKRConfig.paper_trading(
        host=args.host,
        port=args.port,
        client_id=args.client_id,
        log_level="WARNING",  # quiet ib_async, we have our own
    )

    results: list[StepResult] = []
    conn = IBKRConnection(config)

    connect_res = await step_connect(conn)
    results.append(connect_res)
    if connect_res.status == PASS:
        try:
            results.append(await step_health(conn))
            results.append(await step_account(conn))
            results.append(await step_positions(conn.ib))
            results.append(await step_market_data(conn.ib))
            results.append(await step_order_lifecycle(conn.ib, args.place_order))
        finally:
            await conn.disconnect()

    # Risk logic is pure-Python, runs regardless of broker connectivity
    results.append(await step_risk())

    print("\n" + "=" * 60)
    print("Smoke test results")
    print("=" * 60)
    for res in results:
        print(res)
    print("=" * 60)

    failed = [r for r in results if r.status == FAIL]
    if failed:
        print(f"\n{len(failed)} step(s) failed")
        return 1
    print("\nAll steps passed (skips treated as success).")
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ibkr package smoke test")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=7497)
    p.add_argument("--client-id", type=int, default=11)
    p.add_argument("--place-order", action="store_true",
                   help="Place a far-OOM AAPL limit order then cancel it")
    return p.parse_args()


if __name__ == "__main__":
    try:
        rc = asyncio.run(main(parse_args()))
    except KeyboardInterrupt:
        rc = 130
    except Exception:
        traceback.print_exc()
        rc = 1
    sys.exit(rc)
