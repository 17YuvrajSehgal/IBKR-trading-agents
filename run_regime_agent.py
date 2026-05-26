"""
Runner for the regime-adaptive multi-timeframe TSLA agent.

Usage:
    .venv\\Scripts\\python.exe run_regime_agent.py
    .venv\\Scripts\\python.exe run_regime_agent.py --duration 1800
    .venv\\Scripts\\python.exe run_regime_agent.py --symbol NVDA --mtf "5 mins" --htf "1 hour"

Safety:
    * Refuses to start on live-trading port.
    * Refuses outside RTH unless --force-after-hours is set.
    * Force-flattens any open position on Ctrl-C / duration timeout.
    * Honors RiskManager (rate, notional, daily loss).
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

from session_logging import init_session_logging, make_session_id
from strategies.regime_adaptive.agent import (
    AgentConfig, RegimeAdaptiveAgent,
)
from strategies.regime_adaptive.signals import SignalConfig, ShortMode


LIVE_PORTS = (7496, 4001)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Connection
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=7497)
    p.add_argument("--client-id", type=int, default=31)

    # Strategy
    p.add_argument("--symbol", default="TSLA")
    p.add_argument("--htf", default="1 hour", help="Higher timeframe (bias)")
    p.add_argument("--mtf", default="5 mins", help="Entry timeframe (regime + signals)")

    # Sizing (calibrated for a ~$1M paper account)
    p.add_argument("--risk-pct", type=float, default=0.003,
                   help="Risk per trade as fraction of NetLiq (default 0.3%%)")
    p.add_argument("--max-risk-usd", type=float, default=750.0)
    p.add_argument("--max-notional", type=float, default=100_000.0)

    # Risk limits
    p.add_argument("--max-daily-loss", type=float, default=5_000.0)
    p.add_argument("--max-total-notional", type=float, default=150_000.0)

    # Signal tuning
    p.add_argument("--stop-atr", type=float, default=1.5)
    p.add_argument("--target-atr", type=float, default=2.5)
    p.add_argument("--max-bars-held", type=int, default=24)
    p.add_argument(
        "--shorts",
        choices=["confident", "symmetric", "off"],
        default="confident",
        help="Short-side aggressiveness. confident=HTF must be BEAR; "
             "symmetric=mirror longs; off=longs only (default: confident)",
    )

    # Operator controls
    p.add_argument("--duration", type=float, default=0.0,
                   help="Auto-stop after N seconds (0 = until Ctrl-C)")
    p.add_argument("--force-after-hours", action="store_true")
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--log-dir", default="logs",
                   help="Directory for session text log + JSONL event stream")
    p.add_argument("--session-id", default=None,
                   help="Override session ID (default: auto-generated)")

    return p.parse_args()


def is_us_market_open(now: datetime | None = None) -> bool:
    now = now or datetime.now(ZoneInfo("America/New_York"))
    if now.weekday() >= 5:
        return False
    return dtime(9, 30) <= now.time() <= dtime(16, 0)


def install_signal_handlers(stop_event: asyncio.Event) -> None:
    def _handler(*_args):
        if not stop_event.is_set():
            logging.warning("Shutdown signal received — flattening positions...")
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
    session_id = args.session_id or make_session_id(f"regime-{args.symbol}")
    recorder, log_path = init_session_logging(
        log_dir=args.log_dir,
        session_id=session_id,
        console_level=logging.DEBUG if args.verbose else logging.INFO,
    )
    log = logging.getLogger("regime-runner")
    recorder.event(
        "runner_start",
        runner="regime",
        argv={k: v for k, v in vars(args).items() if not k.startswith("_")},
    )

    # ---- safety ------------------------------------------------------
    if args.port in LIVE_PORTS:
        log.error(
            f"Refusing live port {args.port}. This runner is paper-only. "
            "Use --port 7497 (TWS) or 4002 (Gateway)."
        )
        return 2

    if not is_us_market_open() and not args.force_after_hours:
        log.error(
            "US market is closed. Paper fills are unreliable outside RTH. "
            "Pass --force-after-hours to run anyway."
        )
        return 2

    # ---- build stack -------------------------------------------------
    config = IBKRConfig.paper_trading(
        host=args.host,
        port=args.port,
        client_id=args.client_id,
        log_level="WARNING",
    )

    risk = RiskManager(RiskLimits(
        max_orders_per_second=5.0,
        max_position_per_symbol=int(args.max_notional // 50),  # rough share cap
        max_notional_per_symbol=args.max_notional * 2,
        max_total_notional=args.max_total_notional,
        max_daily_loss=args.max_daily_loss,
        min_order_quantity=1,
        max_order_quantity=10_000,
        readonly=False,
    ))

    agent_cfg = AgentConfig(
        symbol=args.symbol,
        htf_bar=args.htf,
        mtf_bar=args.mtf,
        risk_per_trade_pct=args.risk_pct,
        max_risk_per_trade_usd=args.max_risk_usd,
        max_position_notional=args.max_notional,
    )
    signal_cfg = SignalConfig(
        stop_atr_mult=args.stop_atr,
        target_atr_mult=args.target_atr,
        max_bars_held=args.max_bars_held,
        short_mode=ShortMode(args.shorts.upper()),
    )

    stop_event = asyncio.Event()
    install_signal_handlers(stop_event)

    conn = IBKRConnection(config)
    rc = 0
    try:
        await conn.connect()

        # Pull NetLiq for sizing
        summary = await conn.get_account_summary()
        nlv_str = summary.get("NetLiquidation", {}).get("value", "0")
        try:
            account_nlv = float(nlv_str)
        except ValueError:
            account_nlv = 100_000.0
            log.warning(f"Could not parse NetLiquidation={nlv_str!r}, using fallback")
        log.info(f"Account NetLiquidation = ${account_nlv:,.2f}")

        agent = RegimeAdaptiveAgent(
            conn.ib, risk, account_nlv, agent_cfg, signal_cfg, recorder=recorder,
        )
        await agent.start()
        log.info("Agent running. Press Ctrl-C to flatten and stop.")

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
        recorder.close()
        log.info(f"Session artifacts: {log_path}  +  {recorder.path}")

    return rc


if __name__ == "__main__":
    sys.exit(asyncio.run(main(parse_args())))
