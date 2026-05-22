"""
TQQQ / SQQQ mean-reversion pairs agent.

Wires the pure-Python PairsSignal to the broker stack:
    MarketDataManager → PairsSignal → RiskManager → OrderManager

State machine
-------------
    FLAT  --(z >= +z_enter)--> SHORT_SPREAD   (short TQQQ + short SQQQ)
    FLAT  --(z <= -z_enter)--> LONG_SPREAD    (long  TQQQ + long  SQQQ)
    *     --(|z| <= z_exit)-->  FLAT          (mean reverted, take profit)
    *     --(stop hit)------>  FLAT           (adverse, take loss)

Important
---------
* Designed and tested for paper trading. The runner refuses non-paper ports.
* All sizing flows through RiskManager — no order is sent if a risk check fails.
* On shutdown the agent forcibly flattens any open position.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

from ib_async import IB

from ibkr.market_data import MarketDataManager, Quote
from ibkr.trading import OrderManager, OrderAction, OrderInfo
from ibkr.risk import RiskManager

from strategies.pairs_signal import PairsSignal, Signal, SignalConfig, SignalState


logger = logging.getLogger(__name__)


class PositionSide(str, Enum):
    FLAT = "FLAT"
    LONG_SPREAD = "LONG_SPREAD"     # long TQQQ + long SQQQ
    SHORT_SPREAD = "SHORT_SPREAD"   # short TQQQ + short SQQQ


@dataclass
class AgentConfig:
    """Runtime knobs for the pairs agent."""

    # Dollar value of each leg. Position is dollar-neutral, so total notional
    # is roughly 2 * dollars_per_leg.
    dollars_per_leg: float = 5_000.0

    # Symbols (held as constants so tests can swap).
    symbol_a: str = "TQQQ"
    symbol_b: str = "SQQQ"

    # Cool-down between closing one trade and opening the next.
    cooldown_seconds: float = 30.0

    # Log a heartbeat with current z-score every N quotes (informational).
    heartbeat_every: int = 50


@dataclass
class AgentStats:
    """Counters for observability."""

    quotes_seen: int = 0
    signals_emitted: int = 0
    trades_attempted: int = 0
    trades_rejected_by_risk: int = 0
    positions_opened: int = 0
    positions_closed_profit: int = 0
    positions_closed_stop: int = 0
    started_at: datetime = None  # type: ignore

    def __post_init__(self) -> None:
        if self.started_at is None:
            self.started_at = datetime.now()


class TqqqSqqqPairsAgent:
    """
    Event-driven mean-reversion agent for TQQQ / SQQQ.

    Lifecycle:
        1. ``await agent.start()`` — subscribe to quotes, install callback
        2. agent runs in the ib_async event loop
        3. ``await agent.stop()`` — flatten any position, unsubscribe
    """

    def __init__(
        self,
        ib: IB,
        risk: RiskManager,
        signal_config: Optional[SignalConfig] = None,
        agent_config: Optional[AgentConfig] = None,
    ) -> None:
        self.ib = ib
        self.risk = risk
        self.market_data = MarketDataManager(ib)
        self.orders = OrderManager(ib)

        self.signal = PairsSignal(signal_config or SignalConfig())
        self.cfg = agent_config or AgentConfig()
        self.stats = AgentStats()

        # Current position state
        self.side: PositionSide = PositionSide.FLAT
        self._last_close_at: float = 0.0   # monotonic clock
        self._open_orders: list[OrderInfo] = []
        self._open_shares_a: int = 0
        self._open_shares_b: int = 0

        # Latest quotes (set on every callback)
        self._a: Optional[Quote] = None
        self._b: Optional[Quote] = None

        # Re-entrancy guard so we don't fire two orders for one quote burst
        self._executing = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        logger.info(
            f"Starting pairs agent on {self.cfg.symbol_a}/{self.cfg.symbol_b} "
            f"(${self.cfg.dollars_per_leg:,.0f} per leg)"
        )
        await self.market_data.subscribe(self.cfg.symbol_a)
        await self.market_data.subscribe(self.cfg.symbol_b)
        self.market_data.subscribe_quotes(self._on_quote)

    async def stop(self, flatten: bool = True) -> None:
        logger.info("Stopping pairs agent")
        try:
            if flatten and self.side != PositionSide.FLAT:
                logger.warning(f"Force-flattening {self.side} position on shutdown")
                await self._close_position(reason="shutdown")
        finally:
            self.market_data.unsubscribe_all()
            logger.info(f"Final stats: {self.stats}")

    # ------------------------------------------------------------------
    # Quote handler  (push from MarketDataManager)
    # ------------------------------------------------------------------

    def _on_quote(self, quote: Quote) -> None:
        """Push callback — fires on every TQQQ or SQQQ tick."""
        self.stats.quotes_seen += 1

        if quote.symbol == self.cfg.symbol_a:
            self._a = quote
        elif quote.symbol == self.cfg.symbol_b:
            self._b = quote
        else:
            return  # not our symbol

        # Need both quotes valid before we can act.
        if self._a is None or self._b is None:
            return
        if not self._a.is_tradeable or not self._b.is_tradeable:
            return

        # Schedule async evaluation. We can't await from this sync callback,
        # so we hand off to the event loop.
        asyncio.create_task(self._evaluate())

    async def _evaluate(self) -> None:
        """Compute the signal and act on it."""
        if self._executing:
            return  # an earlier task is mid-flight; skip this tick
        self._executing = True
        try:
            a_mid = self._a.mid
            b_mid = self._b.mid
            if not (a_mid > 0 and b_mid > 0):
                return

            position_side_signal = (
                Signal.ENTER_LONG_SPREAD if self.side == PositionSide.LONG_SPREAD
                else Signal.ENTER_SHORT_SPREAD if self.side == PositionSide.SHORT_SPREAD
                else None
            )

            state = self.signal.update(
                tqqq_price=a_mid,
                sqqq_price=b_mid,
                in_position=self.side != PositionSide.FLAT,
                position_side=position_side_signal,
            )
            self.stats.signals_emitted += 1

            self._heartbeat(state)

            if state.action == Signal.NONE:
                return

            await self._act(state)
        finally:
            self._executing = False

    # ------------------------------------------------------------------
    # Decision dispatch
    # ------------------------------------------------------------------

    async def _act(self, state: SignalState) -> None:
        action = state.action

        if action in (Signal.ENTER_LONG_SPREAD, Signal.ENTER_SHORT_SPREAD):
            if self.side != PositionSide.FLAT:
                return
            if not self._cooldown_elapsed():
                return
            await self._open_position(action, state)
            return

        if action in (Signal.EXIT, Signal.STOP):
            if self.side == PositionSide.FLAT:
                return
            await self._close_position(reason=action.value.lower())
            return

    def _cooldown_elapsed(self) -> bool:
        return (asyncio.get_event_loop().time() - self._last_close_at) >= self.cfg.cooldown_seconds

    # ------------------------------------------------------------------
    # Order operations
    # ------------------------------------------------------------------

    async def _open_position(self, side: Signal, state: SignalState) -> None:
        a_price = self._a.mid
        b_price = self._b.mid

        shares_a = max(1, int(self.cfg.dollars_per_leg // a_price))
        shares_b = max(1, int(self.cfg.dollars_per_leg // b_price))

        is_long = side == Signal.ENTER_LONG_SPREAD
        action_a = OrderAction.BUY if is_long else OrderAction.SELL
        action_b = OrderAction.BUY if is_long else OrderAction.SELL

        logger.info(
            f"OPEN {side.value}  z={state.z:+.2f}  "
            f"{action_a} {shares_a} {self.cfg.symbol_a} @~${a_price:.2f}  "
            f"{action_b} {shares_b} {self.cfg.symbol_b} @~${b_price:.2f}"
        )

        # Risk gate, both legs
        self.stats.trades_attempted += 1
        check_a = self.risk.check_order(self.cfg.symbol_a, action_a.value, shares_a, a_price)
        if not check_a.approved:
            self.stats.trades_rejected_by_risk += 1
            logger.warning(f"Risk rejected leg A: {check_a.reason}")
            return
        check_b = self.risk.check_order(self.cfg.symbol_b, action_b.value, shares_b, b_price)
        if not check_b.approved:
            self.stats.trades_rejected_by_risk += 1
            logger.warning(f"Risk rejected leg B: {check_b.reason}")
            return

        # Place both legs (market orders — TQQQ/SQQQ are highly liquid)
        try:
            order_a = await self.orders.place_market_order(self.cfg.symbol_a, shares_a, action_a)
            order_b = await self.orders.place_market_order(self.cfg.symbol_b, shares_b, action_b)
        except Exception as e:
            logger.error(f"Failed to open position: {e}")
            return

        self.side = PositionSide.LONG_SPREAD if is_long else PositionSide.SHORT_SPREAD
        self._open_orders = [order_a, order_b]
        self._open_shares_a = shares_a if is_long else -shares_a
        self._open_shares_b = shares_b if is_long else -shares_b
        self.stats.positions_opened += 1

        # Inform risk of the (assumed) fill so subsequent checks see the exposure.
        self.risk.record_fill(self.cfg.symbol_a, action_a.value, shares_a, a_price)
        self.risk.record_fill(self.cfg.symbol_b, action_b.value, shares_b, b_price)

    async def _close_position(self, reason: str) -> None:
        if self.side == PositionSide.FLAT:
            return

        a_price = self._a.mid if self._a else 0.0
        b_price = self._b.mid if self._b else 0.0

        # Reverse direction of each leg
        action_a = OrderAction.SELL if self._open_shares_a > 0 else OrderAction.BUY
        action_b = OrderAction.SELL if self._open_shares_b > 0 else OrderAction.BUY
        shares_a = abs(self._open_shares_a)
        shares_b = abs(self._open_shares_b)

        logger.info(
            f"CLOSE {self.side.value} reason={reason}  "
            f"{action_a} {shares_a} {self.cfg.symbol_a} @~${a_price:.2f}  "
            f"{action_b} {shares_b} {self.cfg.symbol_b} @~${b_price:.2f}"
        )

        try:
            await self.orders.place_market_order(self.cfg.symbol_a, shares_a, action_a)
            await self.orders.place_market_order(self.cfg.symbol_b, shares_b, action_b)
        except Exception as e:
            logger.error(f"Failed to close position cleanly: {e}")
            # Don't reset state — let the operator intervene.
            return

        # Update risk model
        if a_price > 0:
            self.risk.record_fill(self.cfg.symbol_a, action_a.value, shares_a, a_price)
        if b_price > 0:
            self.risk.record_fill(self.cfg.symbol_b, action_b.value, shares_b, b_price)

        if reason == "stop":
            self.stats.positions_closed_stop += 1
        else:
            self.stats.positions_closed_profit += 1

        self.side = PositionSide.FLAT
        self._open_orders = []
        self._open_shares_a = 0
        self._open_shares_b = 0
        self._last_close_at = asyncio.get_event_loop().time()

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    def _heartbeat(self, state: SignalState) -> None:
        if self.stats.quotes_seen % self.cfg.heartbeat_every != 0:
            return
        logger.info(
            f"hb  side={self.side.value:<13} "
            f"z={state.z:+.2f}  samples={state.samples}  "
            f"TQQQ={self._a.mid:.2f}  SQQQ={self._b.mid:.2f}"
        )

    def snapshot(self) -> dict:
        """Return a snapshot dict useful for monitoring."""
        return {
            "side": self.side.value,
            "samples": self.signal.samples,
            "stats": self.stats.__dict__,
        }
