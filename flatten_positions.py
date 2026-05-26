"""
Emergency flatten — close every open position in the connected account.

Useful when:
  * A previous agent run left positions open (shutdown didn't await fills)
  * You want a clean slate before starting a fresh agent
  * Something looks wrong and you need to bail out NOW

Usage:
    .venv\\Scripts\\python.exe flatten_positions.py
    .venv\\Scripts\\python.exe flatten_positions.py --dry-run    (preview only)
    .venv\\Scripts\\python.exe flatten_positions.py --symbol TSLA  (one symbol)

Safety:
    * Refuses to run on live-trading ports unless --force-live is passed.
    * Dry-run shows exactly what it would do without sending orders.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Optional

from ib_async import Stock

from ibkr.config import IBKRConfig
from ibkr.connection import IBKRConnection
from ibkr.trading import OrderManager, OrderAction


LIVE_PORTS = (7496, 4001)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=7497)
    p.add_argument("--client-id", type=int, default=15)
    p.add_argument("--symbol", default=None,
                   help="Only flatten this one symbol (default: all open positions)")
    p.add_argument("--dry-run", action="store_true",
                   help="Show what would be closed without sending orders")
    p.add_argument("--force-live", action="store_true",
                   help="Permit running against the live trading port (DANGEROUS)")
    p.add_argument("--timeout", type=float, default=10.0,
                   help="Seconds to wait for each close order to fill")
    return p.parse_args()


async def main(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("flatten")

    if args.port in LIVE_PORTS and not args.force_live:
        log.error(
            f"Refusing live port {args.port}. Pass --force-live to override "
            "(this will trade your real account!)"
        )
        return 2

    if args.port in LIVE_PORTS:
        log.critical("*** LIVE TRADING MODE — closing real positions ***")

    config = IBKRConfig.paper_trading(
        host=args.host, port=args.port, client_id=args.client_id,
        log_level="WARNING",
    )
    conn = IBKRConnection(config)
    rc = 0
    try:
        await conn.connect()

        # Cancel any still-working orders first. A previous run that timed
        # out can leave orders working in TWS even after the client
        # disconnected; those will interfere with our flatten if we don't
        # cancel them. The cancellations are tagged with the current client
        # so only API orders are affected — manual TWS orders are untouched.
        try:
            open_trades = conn.ib.openTrades()
        except Exception:
            open_trades = []
        if open_trades:
            log.info(f"Cancelling {len(open_trades)} working order(s) before flatten:")
            for t in open_trades:
                try:
                    sym = t.contract.symbol if t.contract else "?"
                    side = t.order.action
                    qty = int(t.order.totalQuantity)
                    log.info(f"  cancel  {sym:<8} {side:<4} {qty}")
                    conn.ib.cancelOrder(t.order)
                except Exception as e:
                    log.warning(f"  cancel failed: {e}")
            # Give TWS a moment to process the cancellations
            await asyncio.sleep(1.5)

        # Refresh positions from TWS
        positions = conn.ib.positions()
        non_flat = [p for p in positions if p.position != 0]

        if args.symbol:
            sym = args.symbol.upper()
            non_flat = [p for p in non_flat if p.contract.symbol.upper() == sym]
            if not non_flat:
                log.info(f"No open position for {sym}")
                return 0

        if not non_flat:
            log.info("No open positions to flatten.")
            return 0

        log.info(f"Found {len(non_flat)} open position(s):")
        for p in non_flat:
            side = "LONG" if p.position > 0 else "SHORT"
            log.info(
                f"  {p.contract.symbol:<8} {side:<5} "
                f"{abs(int(p.position)):>6} shares  avg cost ${p.avgCost:.2f}  "
                f"account={p.account}"
            )

        if args.dry_run:
            log.info("--dry-run set — no orders sent.")
            return 0

        order_mgr = OrderManager(conn.ib)

        # Close each position with a market order; wait for terminal state.
        results = []
        for p in non_flat:
            symbol = p.contract.symbol
            shares = abs(int(p.position))
            action = OrderAction.SELL if p.position > 0 else OrderAction.BUY
            try:
                info = await order_mgr.place_market_order(symbol, shares, action)
                done = await order_mgr.wait_for_done(info.order_id, timeout=args.timeout)
                status = order_mgr.orders[info.order_id].status.value
                if not done:
                    log.warning(f"  {symbol}: order {info.order_id} status={status} (timeout)")
                    results.append((symbol, "TIMEOUT", status))
                else:
                    log.info(f"  {symbol}: closed (order {info.order_id} status={status})")
                    results.append((symbol, "OK", status))
            except Exception as e:
                log.error(f"  {symbol}: close failed — {e}")
                results.append((symbol, "FAILED", str(e)))

        # Final verification: re-query positions
        await asyncio.sleep(1.0)
        remaining = [p for p in conn.ib.positions() if p.position != 0]
        if remaining:
            log.warning(f"{len(remaining)} position(s) still open after flatten:")
            for p in remaining:
                log.warning(
                    f"  {p.contract.symbol}  {int(p.position):>+6}  "
                    f"avg cost ${p.avgCost:.2f}"
                )
            rc = 1
        else:
            log.info("All positions closed successfully.")

    except KeyboardInterrupt:
        log.warning("Interrupted")
        rc = 130
    except Exception as e:
        log.exception(f"Fatal: {e}")
        rc = 1
    finally:
        try:
            await conn.disconnect()
        except Exception:
            pass
    return rc


if __name__ == "__main__":
    sys.exit(asyncio.run(main(parse_args())))
