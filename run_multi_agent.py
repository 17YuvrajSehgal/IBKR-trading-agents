"""
Multi-symbol regime-adaptive runner.

Runs one RegimeAdaptiveAgent per symbol on a single IB connection. All agents
share:
  * one IBKRConnection (single client_id)
  * one OrderManager     (no duplicate event handlers)
  * one RiskManager      (global daily-loss + total notional kill switch)

Each agent maintains its own bar subscriptions, indicators, regime state,
position, and stats — independent decisions per symbol.

Usage:
    .venv\\Scripts\\python.exe run_multi_agent.py
    .venv\\Scripts\\python.exe run_multi_agent.py --symbols TSLA,NVDA,AMD --duration 1800
    .venv\\Scripts\\python.exe run_multi_agent.py --risk-pct 0.002 --max-daily-loss 1000

Defaults to: MU SNDK TSLA NVDA AMD QCOM
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

from ibkr.config import IBKRConfig
from ibkr.connection import IBKRConnection
from ibkr.risk import RiskManager, RiskLimits
from ibkr.trading import OrderManager

from session_logging import init_session_logging, make_session_id
from strategies.regime_adaptive.agent import (
    AgentConfig, RegimeAdaptiveAgent,
)
from strategies.regime_adaptive.signals import SignalConfig, ShortMode


LIVE_PORTS = (7496, 4001)
DEFAULT_SYMBOLS = ("MU", "SNDK", "TSLA", "NVDA", "AMD", "QCOM")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Connection
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=7497)
    p.add_argument("--client-id", type=int, default=4)

    # Universe — either --symbols or --watchlist (mutually exclusive)
    universe = p.add_mutually_exclusive_group()
    universe.add_argument(
        "--symbols",
        default=None,
        help="Comma-separated symbols (default: " + ",".join(DEFAULT_SYMBOLS) + ")",
    )
    universe.add_argument(
        "--watchlist",
        type=str,
        default=None,
        help="Path to a JSON watchlist file ({category: [symbols, ...]}).",
    )
    p.add_argument(
        "--category",
        type=str,
        default=None,
        help="With --watchlist: filter to specific categories (comma-separated, "
             "matched case-insensitive substring). Default: all categories.",
    )
    p.add_argument(
        "--list-categories",
        action="store_true",
        help="With --watchlist: print the available categories and exit.",
    )
    p.add_argument(
        "--max-symbols",
        type=int,
        default=0,
        help="Cap loaded symbols (0 = no cap). Useful with --watchlist.",
    )
    p.add_argument(
        "--warmup-delay",
        type=float,
        default=1.5,
        help="Seconds between agent warmups (raise on pacing errors).",
    )

    # Timeframes (applied to every agent)
    p.add_argument("--htf", default="1 hour")
    p.add_argument("--mtf", default="5 mins")

    # Per-trade sizing (calibrated for a ~$1M paper account)
    p.add_argument("--risk-pct", type=float, default=0.002,
                   help="Risk per trade as fraction of NetLiq (default 0.2%%)")
    p.add_argument("--max-risk-usd", type=float, default=500.0,
                   help="Hard cap on $ risk per trade")
    p.add_argument("--max-notional-per-symbol", type=float, default=75_000.0,
                   help="Max exposure on any single symbol")

    # Global risk limits (across ALL agents)
    p.add_argument("--max-daily-loss", type=float, default=10_000.0,
                   help="Global session loss cap (across all symbols)")
    p.add_argument("--max-total-notional", type=float, default=750_000.0,
                   help="Global notional cap (across all symbols)")

    # Per-symbol circuit breakers
    p.add_argument("--max-loss-per-symbol", type=float, default=500.0,
                   help="Blacklist a symbol after cumulative session loss exceeds this $ amount")
    p.add_argument("--max-consecutive-losses", type=int, default=2,
                   help="Blacklist a symbol after N consecutive losing trades")

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

    # Operator
    p.add_argument("--duration", type=float, default=0.0,
                   help="Auto-stop after N seconds (0 = until Ctrl-C)")
    p.add_argument("--force-after-hours", action="store_true")
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--log-dir", default="logs",
                   help="Directory for session text log + JSONL event stream "
                        "(default: logs/)")
    p.add_argument("--session-id", default=None,
                   help="Override session ID (default: auto-generated from timestamp)")

    return p.parse_args()


def resolve_symbols(args: argparse.Namespace, log: logging.Logger) -> list[str] | None:
    """
    Build the symbol list from --symbols or --watchlist + --category.

    Returns None on validation failure.
    """
    raw: list[str] = []

    if args.watchlist:
        path = Path(args.watchlist)
        if not path.is_file():
            log.error(f"Watchlist file not found: {path}")
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            log.error(f"Failed to parse watchlist JSON: {e}")
            return None
        if not isinstance(data, dict):
            log.error("Watchlist JSON must be an object of {category: [symbols, ...]}")
            return None

        if args.list_categories:
            print(f"Categories in {path}:")
            for cat, syms in data.items():
                print(f"  {cat:<45} ({len(syms)} symbols)")
            sys.exit(0)

        if args.category:
            wanted = [c.strip().lower() for c in args.category.split(",") if c.strip()]
            for cat, syms in data.items():
                if any(w in cat.lower() for w in wanted):
                    raw.extend(syms)
            if not raw:
                log.error(
                    f"No categories matched --category={args.category!r}. "
                    f"Available: {list(data.keys())}"
                )
                return None
        else:
            for syms in data.values():
                raw.extend(syms)
    elif args.symbols:
        raw = args.symbols.split(",")
    else:
        raw = list(DEFAULT_SYMBOLS)

    # Normalize + dedupe (preserve first-seen order)
    seen: set[str] = set()
    symbols: list[str] = []
    for s in raw:
        s = s.strip().upper()
        if s and s not in seen:
            seen.add(s)
            symbols.append(s)

    if args.max_symbols and len(symbols) > args.max_symbols:
        log.warning(
            f"Truncating {len(symbols)} symbols to first {args.max_symbols} "
            f"(--max-symbols). Drop the flag to include them all."
        )
        symbols = symbols[: args.max_symbols]

    if not symbols:
        log.error("No symbols resolved.")
        return None

    return symbols


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
    session_id = args.session_id or make_session_id("multi")
    recorder, log_path = init_session_logging(
        log_dir=args.log_dir,
        session_id=session_id,
        console_level=logging.DEBUG if args.verbose else logging.INFO,
    )
    log = logging.getLogger("multi-runner")
    recorder.event(
        "runner_start",
        runner="multi",
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

    symbols = resolve_symbols(args, log)
    if symbols is None:
        return 2

    if len(symbols) <= 25:
        log.info(f"Symbols ({len(symbols)}): {symbols}")
    else:
        log.info(
            f"Symbols ({len(symbols)}): {symbols[:10]} ... {symbols[-5:]}"
        )
        est_minutes = (len(symbols) * 2 * 10) / 60
        log.warning(
            f"{len(symbols)} symbols × 2 historical requests each = {len(symbols)*2} total. "
            f"IBKR pacing limits to ~60 req / 10 min, so warmup may take "
            f"~{est_minutes:.0f} min and may hit pacing errors. "
            f"Use --category or --max-symbols to narrow down."
        )

    # ---- shared infrastructure --------------------------------------
    config = IBKRConfig.paper_trading(
        host=args.host,
        port=args.port,
        client_id=args.client_id,
        log_level="WARNING",
    )

    risk = RiskManager(RiskLimits(
        max_orders_per_second=10.0,    # 6 agents could batch — give some headroom
        max_position_per_symbol=int(args.max_notional_per_symbol // 20),
        max_notional_per_symbol=args.max_notional_per_symbol * 2,
        max_total_notional=args.max_total_notional,
        max_daily_loss=args.max_daily_loss,
        min_order_quantity=1,
        max_order_quantity=10_000,
        readonly=False,
        max_consecutive_losses_per_symbol=args.max_consecutive_losses,
        max_loss_per_symbol_per_session=args.max_loss_per_symbol,
    ))

    stop_event = asyncio.Event()
    install_signal_handlers(stop_event)

    conn = IBKRConnection(config)
    rc = 0
    agents: list[RegimeAdaptiveAgent] = []

    try:
        await conn.connect()

        # NetLiq for sizing
        summary = await conn.get_account_summary()
        nlv_str = summary.get("NetLiquidation", {}).get("value", "0")
        try:
            account_nlv = float(nlv_str)
        except ValueError:
            account_nlv = 100_000.0
            log.warning(f"Could not parse NetLiquidation={nlv_str!r}, using fallback")
        log.info(f"Account NetLiquidation = ${account_nlv:,.2f}")

        # Single OrderManager shared across all agents
        order_mgr = OrderManager(conn.ib)

        signal_cfg = SignalConfig(
            stop_atr_mult=args.stop_atr,
            target_atr_mult=args.target_atr,
            max_bars_held=args.max_bars_held,
            short_mode=ShortMode(args.shorts.upper()),
        )

        # Spin up agents — start() does an awaitable warmup so do them sequentially
        # to avoid hammering IBKR's historical data pacing limits.
        for symbol in symbols:
            agent_cfg = AgentConfig(
                symbol=symbol,
                htf_bar=args.htf,
                mtf_bar=args.mtf,
                risk_per_trade_pct=args.risk_pct,
                max_risk_per_trade_usd=args.max_risk_usd,
                max_position_notional=args.max_notional_per_symbol,
            )
            agent = RegimeAdaptiveAgent(
                ib=conn.ib,
                risk=risk,
                account_nlv=account_nlv,
                agent_config=agent_cfg,
                signal_config=signal_cfg,
                orders=order_mgr,
                recorder=recorder,
            )
            agents.append(agent)
            try:
                await agent.start()
            except Exception as e:
                log.error(f"Failed to start agent for {symbol}: {e}")
                # Continue starting the others — partial fleet still useful.

            # Pacing-friendly delay between historical data requests.
            # IBKR limits ~60 req / 10 min; each agent fires 2 requests.
            await asyncio.sleep(args.warmup_delay)

        log.info(
            f"All {len(agents)} agents running. "
            f"Global risk: daily_loss=${args.max_daily_loss:,.0f}, "
            f"total_notional=${args.max_total_notional:,.0f}. "
            "Press Ctrl-C to flatten and stop."
        )

        try:
            if args.duration > 0:
                await asyncio.wait_for(stop_event.wait(), timeout=args.duration)
            else:
                await stop_event.wait()
        except asyncio.TimeoutError:
            log.info(f"Duration {args.duration}s elapsed, stopping cleanly")

        # Stop all agents in parallel (force-flatten each)
        log.info("Stopping all agents...")
        await asyncio.gather(*(a.stop(flatten=True) for a in agents),
                             return_exceptions=True)

        # Summary
        log.info("=" * 70)
        log.info("Multi-agent session summary")
        log.info("=" * 70)
        total_pnl = 0.0
        for a in agents:
            s = a.stats
            total_pnl += s.realized_pnl
            log.info(
                f"  {a.cfg.symbol:<5}  "
                f"opened={s.positions_opened:<3} "
                f"target={s.positions_closed_target:<2} "
                f"stop={s.positions_closed_stop:<2} "
                f"time={s.positions_closed_time:<2} "
                f"regime={s.positions_closed_regime:<2} "
                f"PnL=${s.realized_pnl:+8.2f}"
            )
        log.info(f"  TOTAL realized PnL: ${total_pnl:+,.2f}")
        log.info(f"  Risk summary: {risk.summary()}")

        # Persist the final summary into the JSONL event stream too
        recorder.event(
            "session_summary",
            symbols=[a.cfg.symbol for a in agents],
            total_pnl=total_pnl,
            per_symbol=[
                {"symbol": a.cfg.symbol, **a.stats.__dict__}
                for a in agents
            ],
            risk=risk.summary(),
        )

    except KeyboardInterrupt:
        log.warning("KeyboardInterrupt — attempting clean flatten")
        await asyncio.gather(*(a.stop(flatten=True) for a in agents),
                             return_exceptions=True)
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
