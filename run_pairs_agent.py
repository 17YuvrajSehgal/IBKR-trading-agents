"""
Runner for the TQQQ/SQQQ mean-reversion pairs agent.

Usage:
    .venv\\Scripts\\python.exe run_pairs_agent.py
    .venv\\Scripts\\python.exe run_pairs_agent.py --dollars-per-leg 2000 --z-enter 2.5
    .venv\\Scripts\\python.exe run_pairs_agent.py --duration 600   # auto-stop after 10 minutes

Safety:
    * Refuses to start on the live-trading port (7496 / 4001).
    * Refuses outside US market hours by default — use --force-after-hours to override.
    * Force-flattens any open position on Ctrl-C or duration timeout.
    * Honors the RiskManager: daily loss limit, max notional, rate limit.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from ibkr.config import IBKRConfig
from ibkr.connection import IBKRConnection
from ibkr.risk import RiskManager, RiskLimits

from strategies.pairs_signal import SignalConfig
from strategies.tqqq_sqqq_agent import TqqqSqqqPairsAgent, AgentConfig


LIVE_PORTS = (7496, 4001)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)

    # Connection
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=7497, help="7497=TWS paper, 4002=Gateway paper")
    p.add_argument("--client-id", type=int, default=21)

    # Strategy knobs
    p.add_argument("--dollars-per-leg", type=float, default=5_000.0)
    p.add_argument("--z-enter", type=float, default=2.0)
    p.add_argument("--z-exit", type=float, default=0.5)
    p.add_argument("--z-stop", type=float, default=4.0)
    p.add_argument("--lookback", type=int, default=300)
    p.add_argument("--cooldown", type=float, default=30.0)

    # Risk limits
    p.add_argument("--max-daily-loss", type=float, default=500.0)
    p.add_argument("--max-total-notional", type=float, default=50_000.0)

    # Operator controls
    p.add_argument("--duration", type=float, default=0.0,
                   help="Auto-stop after N seconds (0 = run until Ctrl-C)")
    p.add_argument("--force-after-hours", action="store_true",
                   help="Allow running outside US market hours (paper testing only)")
    p.add_argument("--verbose", "-v", action="store_true")

    return p.parse_args()


def is_us_market_open(now: datetime | None = None) -> bool:
    now = now or datetime.now(ZoneInfo("America/New_York"))
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    return dtime(9, 30) <= now.time() <= dtime(16, 0)


def install_signal_handlers(stop_event: asyncio.Event) -> None:
    """Wire Ctrl-C / SIGTERM to a clean shutdown event."""

    def _handler(*_args):
        if not stop_event.is_set():
            logging.warning("Shutdown signal received — flattening positions...")
            stop_event.set()

    # On Windows asyncio.add_signal_handler isn't supported; fall back to signal.signal.
    try:
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _handler)
    except (NotImplementedError, AttributeError):
        signal.signal(signal.SIGINT, _handler)
        try:
            signal.signal(signal.SIGTERM, _handler)
        except (AttributeError, ValueError):
            pass


async def main(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("pairs-runner")

    # ---- safety gates ------------------------------------------------
    if args.port in LIVE_PORTS:
        log.error(
            f"Refusing to run on live port {args.port}. "
            "This agent is paper-trading only — use --port 7497 (TWS) or 4002 (Gateway)."
        )
        return 2

    if not is_us_market_open() and not args.force_after_hours:
        log.error(
            "US market is closed. Paper fills are unreliable outside RTH. "
            "Pass --force-after-hours to run anyway."
        )
        return 2

    # ---- build the stack --------------------------------------------
    config = IBKRConfig.paper_trading(
        host=args.host,
        port=args.port,
        client_id=args.client_id,
        log_level="WARNING",
    )

    risk = RiskManager(RiskLimits(
        max_orders_per_second=5.0,
        max_position_per_symbol=10_000,
        max_notional_per_symbol=args.dollars_per_leg * 4,  # leg can rebalance up to 2x intraday
        max_total_notional=args.max_total_notional,
        max_daily_loss=args.max_daily_loss,
        min_order_quantity=1,
        max_order_quantity=10_000,
        readonly=False,
    ))

    sig_cfg = SignalConfig(
        lookback=args.lookback,
        z_enter=args.z_enter,
        z_exit=args.z_exit,
        z_stop=args.z_stop,
        min_samples=min(60, max(2, args.lookback // 2)),
    )
    agent_cfg = AgentConfig(
        dollars_per_leg=args.dollars_per_leg,
        cooldown_seconds=args.cooldown,
    )

    stop_event = asyncio.Event()
    install_signal_handlers(stop_event)

    conn = IBKRConnection(config)
    rc = 0
    try:
        await conn.connect()
        agent = TqqqSqqqPairsAgent(conn.ib, risk, sig_cfg, agent_cfg)
        await agent.start()

        log.info(
            f"Agent running — z_enter={args.z_enter} z_exit={args.z_exit} "
            f"z_stop={args.z_stop} leg=${args.dollars_per_leg:,.0f}. "
            f"Press Ctrl-C to flatten and stop."
        )

        # Run until stop signal or duration timeout
        try:
            if args.duration > 0:
                await asyncio.wait_for(stop_event.wait(), timeout=args.duration)
            else:
                await stop_event.wait()
        except asyncio.TimeoutError:
            log.info(f"Duration {args.duration}s elapsed, stopping cleanly")

        await agent.stop(flatten=True)

    except KeyboardInterrupt:
        log.warning("KeyboardInterrupt — attempting clean flatten")
        rc = 130
    except Exception as e:
        log.exception(f"Fatal error: {e}")
        rc = 1
    finally:
        try:
            await conn.disconnect()
        except Exception:
            pass

    return rc


if __name__ == "__main__":
    sys.exit(asyncio.run(main(parse_args())))
