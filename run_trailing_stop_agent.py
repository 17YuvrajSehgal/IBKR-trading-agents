"""
Runner for the trailing-stop agent.

Runs as a stand-alone process that watches every open position in the
account and ratchets a stop-loss into the profit zone as price moves
favorably. When a stop triggers, it fires an immediate market close.

Use it ALONGSIDE another trading agent — e.g.:

    Terminal 1:  run_multi_agent.py  --watchlist ...   (opens positions)
    Terminal 2:  run_trailing_stop_agent.py            (protects profit)

The two communicate via IBKR's position events. When trailing-stop closes
a position, the trading agent sees the broker go flat and clears its
internal state, so it can re-enter on the next signal.

Safety:
    * Refuses live trading port unless explicitly overridden.
    * NEVER opens positions — only closes them.
    * Force-close on Ctrl-C only if --flatten-on-stop is passed (default
      OFF — the agent leaves positions alone on shutdown so a restart can
      pick up where it left off).
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
from ibkr.trading import OrderManager

from session_logging import init_session_logging, make_session_id
from strategies.trailing_stop.agent import TrailConfig, TrailingStopAgent


LIVE_PORTS = (7496, 4001)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Connection
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=7497)
    p.add_argument("--client-id", type=int, default=8,
                   help="Distinct from trading-agent client IDs (default: 8)")

    # Trailing logic
    p.add_argument("--initial-stop-pct", type=float, default=0.007,
                   help="Initial stop distance below entry (default 0.7%%)")
    p.add_argument("--breakeven-trigger-pct", type=float, default=0.005,
                   help="Favorable move needed before shifting stop to entry (default 0.5%%)")
    p.add_argument("--trail-pct", type=float, default=0.005,
                   help="Distance from peak to maintain trailing stop (default 0.5%%)")
    p.add_argument("--manage-only-new", action="store_true",
                   help="Skip positions that were already open at startup. "
                        "By default, pre-existing positions are picked up too.")

    # Risk-manager passthrough (mostly used for the daily-loss kill switch)
    p.add_argument("--max-daily-loss", type=float, default=20_000.0,
                   help="Session loss cap (the trailing agent only closes, so this is generous)")
    p.add_argument("--max-total-notional", type=float, default=1_000_000.0)
    # Per-symbol circuit breaker (defaults disabled here — trailing-stop only
    # closes, so blacklisting on losses is purely informational unless the
    # trail is opening positions, which it doesn't)
    p.add_argument("--max-loss-per-symbol", type=float, default=0.0,
                   help="0=disabled. Trailing-stop only closes, so blacklist tripping is informational.")
    p.add_argument("--max-consecutive-losses", type=int, default=0)

    # Operator
    p.add_argument("--duration", type=float, default=0.0,
                   help="Auto-stop after N seconds (0 = until Ctrl-C)")
    p.add_argument("--flatten-on-stop", action="store_true",
                   help="On shutdown, force-close every tracked position "
                        "(default: leave them — restart can pick up)")
    p.add_argument("--force-after-hours", action="store_true")
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--log-dir", default="logs")
    p.add_argument("--session-id", default=None)

    return p.parse_args()


def is_us_market_open(now: datetime | None = None) -> bool:
    now = now or datetime.now(ZoneInfo("America/New_York"))
    if now.weekday() >= 5:
        return False
    return dtime(9, 30) <= now.time() <= dtime(16, 0)


def install_signal_handlers(stop_event: asyncio.Event) -> None:
    def _handler(*_args):
        if not stop_event.is_set():
            logging.warning("Shutdown signal received...")
            stop_event.set()

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
    session_id = args.session_id or make_session_id("trail")
    recorder, log_path = init_session_logging(
        log_dir=args.log_dir,
        session_id=session_id,
        console_level=logging.DEBUG if args.verbose else logging.INFO,
    )
    log = logging.getLogger("trail-runner")
    recorder.event(
        "runner_start",
        runner="trailing_stop",
        argv={k: v for k, v in vars(args).items() if not k.startswith("_")},
    )

    if args.port in LIVE_PORTS:
        log.error(
            f"Refusing live port {args.port}. Paper only — use 7497 (TWS) or 4002 (Gateway)."
        )
        return 2

    if not is_us_market_open() and not args.force_after_hours:
        log.error("US market is closed. Pass --force-after-hours to run anyway.")
        return 2

    config = IBKRConfig.paper_trading(
        host=args.host, port=args.port, client_id=args.client_id,
        log_level="WARNING",
    )

    risk = RiskManager(RiskLimits(
        max_orders_per_second=10.0,
        max_position_per_symbol=1_000_000,   # the trailing agent only closes
        max_notional_per_symbol=1_000_000.0,
        max_total_notional=args.max_total_notional,
        max_daily_loss=args.max_daily_loss,
        min_order_quantity=1,
        max_order_quantity=1_000_000,
        readonly=False,
        max_consecutive_losses_per_symbol=args.max_consecutive_losses,
        max_loss_per_symbol_per_session=args.max_loss_per_symbol,
    ))

    cfg = TrailConfig(
        initial_stop_pct=args.initial_stop_pct,
        breakeven_trigger_pct=args.breakeven_trigger_pct,
        trail_pct=args.trail_pct,
        manage_only_new=args.manage_only_new,
    )

    stop_event = asyncio.Event()
    install_signal_handlers(stop_event)

    conn = IBKRConnection(config)
    rc = 0
    agent: TrailingStopAgent | None = None

    try:
        await conn.connect()

        order_mgr = OrderManager(conn.ib)
        agent = TrailingStopAgent(
            ib=conn.ib,
            risk=risk,
            config=cfg,
            orders=order_mgr,
            recorder=recorder,
        )
        await agent.start()

        log.info(
            f"Trailing-stop agent running. "
            f"initial={args.initial_stop_pct*100:.2f}%  "
            f"breakeven_at={args.breakeven_trigger_pct*100:.2f}%  "
            f"trail={args.trail_pct*100:.2f}%. "
            "Press Ctrl-C to stop."
        )

        try:
            if args.duration > 0:
                await asyncio.wait_for(stop_event.wait(), timeout=args.duration)
            else:
                await stop_event.wait()
        except asyncio.TimeoutError:
            log.info(f"Duration {args.duration}s elapsed, stopping cleanly")

        # Optionally force-close tracked positions on shutdown
        if args.flatten_on_stop and agent.tracked:
            log.warning(
                f"Flattening {len(agent.tracked)} tracked position(s) on shutdown..."
            )
            for tp in list(agent.tracked.values()):
                tp.closing = True
                await agent._close(tp, tp.entry_price, "shutdown flatten")

        await agent.stop()

        s = agent.stats
        log.info("=" * 70)
        log.info("Trailing-stop session summary")
        log.info("=" * 70)
        log.info(f"  positions tracked        : {s.positions_tracked}")
        log.info(f"  closed @ profit          : {s.positions_closed_profit}")
        log.info(f"  closed @ breakeven       : {s.positions_closed_breakeven}")
        log.info(f"  closed @ initial loss    : {s.positions_closed_loss}")
        log.info(f"  closed externally        : {s.positions_closed_externally}")
        log.info(f"  total realized PnL       : ${s.realized_pnl:+,.2f}")
        log.info(f"  still tracking on exit   : {len(agent.tracked)}")

        recorder.event(
            "session_summary",
            stats=s.__dict__,
            tracked_at_exit=list(agent.tracked.keys()),
        )

    except KeyboardInterrupt:
        log.warning("KeyboardInterrupt — exiting (positions left as-is)")
        rc = 130
    except Exception as e:
        log.exception(f"Fatal error: {e}")
        rc = 1
    finally:
        try:
            await conn.disconnect()
        except Exception:
            pass
        recorder.close()
        log.info(f"Session artifacts: {log_path}  +  {recorder.path}")

    return rc


if __name__ == "__main__":
    sys.exit(asyncio.run(main(parse_args())))
