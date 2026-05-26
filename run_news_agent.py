"""
Runner for the news-driven trading agent.

Usage:
    .venv\\Scripts\\python.exe run_news_agent.py
    .venv\\Scripts\\python.exe run_news_agent.py --symbols TSLA,NVDA,AMD --duration 1800
    .venv\\Scripts\\python.exe run_news_agent.py --threshold 3.0 --shorts off

Safety:
    * Refuses to start on live-trading ports.
    * Refuses outside RTH unless --force-after-hours is passed.
    * Force-flattens any open position on Ctrl-C / duration timeout.
    * Honors RiskManager (rate, notional, daily loss).

Practical caveats:
    * IBKR's retail news feed is delayed vs Bloomberg/Reuters direct feeds.
      By the time a headline arrives, the move has often already started.
    * If your account has no premium news subscriptions you'll mostly see
      Briefing.com (BRFG) headlines, which are slower and noisier.
    * Run during US market hours (RTH) — news is concentrated 9:30-16:00 ET.
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
from strategies.news_driven.agent import (
    NewsAgentConfig, NewsDrivenAgent, ShortMode,
)
from strategies.news_driven.classifier import NewsClassifier


LIVE_PORTS = (7496, 4001)

DEFAULT_SYMBOLS = (
    "TSLA", "NVDA", "AMD", "MU", "COIN",
    "META", "AAPL", "MSFT", "GOOG", "AMZN",
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Connection
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=7497)
    p.add_argument("--client-id", type=int, default=22)

    # Universe
    p.add_argument(
        "--symbols",
        default=",".join(DEFAULT_SYMBOLS),
        help="Comma-separated symbols (default: %(default)s)",
    )

    # Signal
    p.add_argument("--threshold", type=float, default=2.5,
                   help="Minimum |weighted score| to fire a trade")
    p.add_argument(
        "--shorts",
        choices=["confident", "symmetric", "off"],
        default="confident",
        help="Short-side policy: confident=strong-neg + premium provider; "
             "symmetric=mirror longs; off=longs only",
    )
    p.add_argument("--short-strong-threshold", type=float, default=3.5,
                   help="With --shorts confident, |score| required to short")

    # Sizing & risk
    p.add_argument("--dollars-per-trade", type=float, default=5_000.0,
                   help="Gross $ exposure per trade")
    p.add_argument("--max-dollars-per-trade", type=float, default=25_000.0)
    p.add_argument("--stop-pct", type=float, default=0.007,
                   help="Stop loss as fraction of entry (default 0.7%%)")
    p.add_argument("--target-pct", type=float, default=0.014,
                   help="Take-profit as fraction of entry (default 1.4%%)")
    p.add_argument("--time-stop-min", type=float, default=30.0,
                   help="Auto-close after N minutes if neither stop nor target hit")
    p.add_argument("--cooldown-min", type=float, default=60.0,
                   help="Per-symbol cooldown after any trade")

    # Global risk
    p.add_argument("--max-daily-loss", type=float, default=5_000.0)
    p.add_argument("--max-total-notional", type=float, default=200_000.0)
    p.add_argument("--max-notional-per-symbol", type=float, default=50_000.0)

    # Operator
    p.add_argument("--duration", type=float, default=0.0,
                   help="Auto-stop after N seconds (0 = until Ctrl-C)")
    p.add_argument("--force-after-hours", action="store_true")
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--log-dir", default="logs")
    p.add_argument("--session-id", default=None)
    p.add_argument(
        "--observe", action="store_true",
        help="Observe-only: classify and record every headline + 'would-have' "
             "trade decisions to the JSONL, but DO NOT place any orders. "
             "Use to tune the lexicon, run alongside another trading agent, "
             "or just watch the news flow without risk.",
    )

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
    session_id = args.session_id or make_session_id("news")
    recorder, log_path = init_session_logging(
        log_dir=args.log_dir,
        session_id=session_id,
        console_level=logging.DEBUG if args.verbose else logging.INFO,
    )
    log = logging.getLogger("news-runner")
    recorder.event(
        "runner_start",
        runner="news",
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

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        log.error("No symbols provided.")
        return 2

    log.info(f"Symbols ({len(symbols)}): {symbols}")

    config = IBKRConfig.paper_trading(
        host=args.host, port=args.port, client_id=args.client_id,
        log_level="WARNING",
    )

    risk = RiskManager(RiskLimits(
        max_orders_per_second=5.0,
        max_position_per_symbol=int(args.max_notional_per_symbol // 20),
        max_notional_per_symbol=args.max_notional_per_symbol,
        max_total_notional=args.max_total_notional,
        max_daily_loss=args.max_daily_loss,
        min_order_quantity=1,
        max_order_quantity=10_000,
        readonly=False,
    ))

    classifier = NewsClassifier(action_threshold=args.threshold)

    agent_cfg = NewsAgentConfig(
        symbols=symbols,
        action_threshold=args.threshold,
        short_mode=ShortMode(args.shorts.upper()),
        short_strong_threshold=args.short_strong_threshold,
        dollars_per_trade=args.dollars_per_trade,
        max_dollars_per_trade=args.max_dollars_per_trade,
        stop_pct=args.stop_pct,
        target_pct=args.target_pct,
        time_stop_seconds=args.time_stop_min * 60.0,
        cooldown_seconds=args.cooldown_min * 60.0,
        observe=args.observe,
    )

    stop_event = asyncio.Event()
    install_signal_handlers(stop_event)

    conn = IBKRConnection(config)
    rc = 0
    agent: NewsDrivenAgent | None = None

    try:
        await conn.connect()

        agent = NewsDrivenAgent(
            ib=conn.ib,
            risk=risk,
            agent_config=agent_cfg,
            classifier=classifier,
            recorder=recorder,
        )
        await agent.start()

        log.info(
            f"News agent running. threshold={args.threshold} "
            f"size=${args.dollars_per_trade:,.0f} stop={args.stop_pct*100:.1f}% "
            f"target={args.target_pct*100:.1f}% time-stop={args.time_stop_min:.0f}min. "
            "Press Ctrl-C to flatten and stop."
        )

        try:
            if args.duration > 0:
                await asyncio.wait_for(stop_event.wait(), timeout=args.duration)
            else:
                await stop_event.wait()
        except asyncio.TimeoutError:
            log.info(f"Duration {args.duration}s elapsed, stopping cleanly")

        await agent.stop(flatten=True)

        s = agent.stats
        log.info("=" * 70)
        log.info("News agent session summary")
        log.info("=" * 70)
        log.info(f"  headlines seen          : {s.headlines_seen}")
        log.info(f"  headlines actionable    : {s.headlines_actionable}")
        log.info(f"  dropped (cooldown)      : {s.headlines_dropped_cooldown}")
        log.info(f"  dropped (no quote)      : {s.headlines_dropped_no_quote}")
        log.info(f"  dropped (short mode)    : {s.headlines_dropped_short_mode}")
        log.info(f"  trades attempted        : {s.trades_attempted}")
        log.info(f"  rejected by risk        : {s.trades_rejected_by_risk}")
        log.info(f"  positions opened        : {s.positions_opened}")
        log.info(f"  closed @ target         : {s.positions_closed_target}")
        log.info(f"  closed @ stop           : {s.positions_closed_stop}")
        log.info(f"  closed @ time           : {s.positions_closed_time}")
        log.info(f"  total realized PnL      : ${s.realized_pnl:+,.2f}")
        log.info(f"  risk summary            : {risk.summary()}")

        recorder.event(
            "session_summary",
            stats=s.__dict__,
            risk=risk.summary(),
        )

    except KeyboardInterrupt:
        log.warning("KeyboardInterrupt — attempting clean flatten")
        if agent is not None:
            await agent.stop(flatten=True)
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
