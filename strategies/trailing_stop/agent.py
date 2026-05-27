"""
Trailing-stop agent.

Watches every position in the connected account and ratchets a stop-loss
into the profit zone as price moves favorably. On every quote update:

  * LONG: peak = max(peak, last); stop = max(stop, peak * (1 - trail_pct))
  * SHORT: trough = min(trough, last); stop = min(stop, trough * (1 + trail_pct))

The stop NEVER moves the wrong direction. Once `breakeven_trigger_pct` of
favorable move has been observed, the stop is forced to at least entry
price (locked-in no-loss). On stop trigger, fires an immediate market
close and removes the position from tracking.

State machine per position
--------------------------

    INITIAL    (stop at entry - initial_stop_pct)
       │
       │   move >= breakeven_trigger_pct
       ▼
    BREAKEVEN  (stop forced to entry)
       │
       │   peak keeps ratcheting
       ▼
    TRAILING   (stop > entry — locked profit)

The agent picks up positions opened by ANY agent via ``ib.positionEvent``.
When a position is closed by another agent (or by itself), it removes the
tracking entry immediately so it doesn't double-close.

Design notes
------------
* It only CLOSES positions, never opens.
* It only uses market orders for the exit — slippage exists; size trail
  width to absorb that.
* If the agent crashes, no stop protection until it's restarted. A v2
  improvement is to also place an IBKR-native TRAIL order as a safety
  net, but for now the in-process monitor is the only protection.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

from ib_async import IB, Position as IBPosition

from ibkr.market_data import MarketDataManager, Quote
from ibkr.recorder import NULL_RECORDER, SessionRecorder
from ibkr.risk import RiskManager
from ibkr.trading import OrderAction, OrderManager


logger = logging.getLogger(__name__)


class Side(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class TrailPhase(str, Enum):
    INITIAL = "INITIAL"        # stop below entry (long) or above (short)
    BREAKEVEN = "BREAKEVEN"    # stop forced to entry
    TRAILING = "TRAILING"      # stop is in the profit zone


@dataclass
class TrailConfig:
    """Tunables for the trailing-stop logic."""

    # Max loss as % of entry before forcing a stop-out
    initial_stop_pct: float = 0.007

    # Favorable move required to shift stop to breakeven
    breakeven_trigger_pct: float = 0.005

    # Distance from peak/trough to keep the trailing stop
    trail_pct: float = 0.005

    # If True, only manage positions opened DURING the agent's lifetime.
    # If False, also manage positions that were already open at start
    # (best-effort — uses current price as initial peak/trough).
    manage_only_new: bool = False

    # Per-position re-evaluation timeout (safety: re-check every N seconds
    # even without a quote update, in case market data goes stale).
    safety_tick_seconds: float = 5.0

    # When True, the trail does NOT arm the INITIAL stop — only kicks in
    # once a position has moved at least `breakeven_trigger_pct` favorable.
    # Use this when running alongside a strategy agent that has its own
    # ATR-based stop: lets the strategy's stop manage initial risk and the
    # trail only protect profits. Discovered after 2026-05-27: the trail's
    # tight 0.7% INITIAL stop cut 5 regime-agent trades before the regime's
    # wider ATR stop could fire, including the W cascade (4 losses).
    passive_until_profit: bool = False


@dataclass
class TrackedPosition:
    symbol: str
    side: Side
    shares: int                # absolute share count
    entry_price: float         # broker-side avg cost
    peak: float                # most-favorable price seen (max for LONG, min for SHORT)
    stop_price: float
    phase: TrailPhase
    started_tracking_at: datetime = field(default_factory=datetime.now)

    # Indicates whether we've already fired the close order — guards
    # against duplicate closes if a quote ticks again before the order acks
    closing: bool = False


@dataclass
class TrailStats:
    positions_tracked: int = 0
    positions_closed_breakeven: int = 0     # closed at entry (BREAKEVEN phase)
    positions_closed_profit: int = 0        # closed in TRAILING phase (locked profit)
    positions_closed_loss: int = 0          # closed in INITIAL phase (small loss)
    positions_closed_externally: int = 0    # other agent or manual close
    realized_pnl: float = 0.0
    started_at: datetime = field(default_factory=datetime.now)


class TrailingStopAgent:
    """
    Watches every position in the account and ratchets a stop into profit.

    Lifecycle:
        agent = TrailingStopAgent(ib, risk)
        await agent.start()       # subscribe, snapshot positions
        ...                       # event-driven trailing
        await agent.stop()        # unsubscribe (does NOT flatten anything)
    """

    def __init__(
        self,
        ib: IB,
        risk: RiskManager,
        config: Optional[TrailConfig] = None,
        orders: Optional[OrderManager] = None,
        recorder: Optional[SessionRecorder] = None,
    ) -> None:
        self.ib = ib
        self.risk = risk
        self.cfg = config or TrailConfig()
        self.orders = orders if orders is not None else OrderManager(ib)
        self.recorder = recorder if recorder is not None else NULL_RECORDER

        self.market_data = MarketDataManager(ib)
        self.tracked: dict[str, TrackedPosition] = {}
        self.stats = TrailStats()

        # Symbols ignored on startup because of manage_only_new=True
        self._ignored_at_startup: set[str] = set()

        # Symbols that have a `_begin_tracking` coroutine in flight.
        # Added SYNCHRONOUSLY by `_on_position_event` so duplicate position
        # events for the same symbol (e.g. multiple partial-fill updates
        # arriving within milliseconds) can't all schedule independent
        # `_begin_tracking` tasks. Without this, a single market order
        # whose partial fills produce 8 positionEvent fires would create
        # 8 phantom tracking entries — exactly what happened on DBX in
        # the 2026-05-27 session for a -$3,640 cascade.
        self._tracking_in_flight: set[str] = set()

        self._executing = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        logger.info(
            f"Starting trailing-stop agent "
            f"(initial={self.cfg.initial_stop_pct*100:.2f}%, "
            f"breakeven_at={self.cfg.breakeven_trigger_pct*100:.2f}%, "
            f"trail={self.cfg.trail_pct*100:.2f}%, "
            f"manage_only_new={self.cfg.manage_only_new})"
        )
        self.recorder.event(
            "agent_start",
            runner="trailing_stop",
            initial_stop_pct=self.cfg.initial_stop_pct,
            breakeven_trigger_pct=self.cfg.breakeven_trigger_pct,
            trail_pct=self.cfg.trail_pct,
            manage_only_new=self.cfg.manage_only_new,
        )

        # Snapshot existing positions
        existing = [p for p in self.ib.positions() if p.position != 0]
        logger.info(f"Found {len(existing)} existing open position(s) at startup")
        for ib_pos in existing:
            symbol = ib_pos.contract.symbol
            if self.cfg.manage_only_new:
                self._ignored_at_startup.add(symbol)
                logger.info(
                    f"  ignoring pre-existing {symbol} "
                    f"({'LONG' if ib_pos.position > 0 else 'SHORT'} {abs(int(ib_pos.position))}) "
                    f"due to manage_only_new=True"
                )
                continue
            # Reserve before await so concurrent positionEvent fires won't
            # try to track the same symbol independently.
            if symbol in self._tracking_in_flight or symbol in self.tracked:
                continue
            self._tracking_in_flight.add(symbol)
            await self._begin_tracking(ib_pos, source="startup")

        # Live updates: new opens, external closes
        self.ib.positionEvent += self._on_position_event

        # Live quote pump: drives the ratchet
        self.market_data.subscribe_quotes(self._on_quote)

        logger.info(f"Trailing-stop agent live, tracking {len(self.tracked)} position(s)")

    async def stop(self) -> None:
        logger.info("Stopping trailing-stop agent")
        try:
            self.market_data.unsubscribe_all()
        except Exception as e:
            logger.warning(f"market-data cleanup error: {e}")
        logger.info(f"Final stats: {self.stats}")

    # ------------------------------------------------------------------
    # Position event handler
    # ------------------------------------------------------------------

    def _on_position_event(self, ib_pos: IBPosition) -> None:
        """Fires on every IBKR position change — new opens, partial fills, closes."""
        symbol = ib_pos.contract.symbol
        position_size = int(ib_pos.position)

        # External close (position went to zero, we were tracking it)
        if position_size == 0 and symbol in self.tracked:
            tp = self.tracked.pop(symbol)
            # Also drop any in-flight tracking placeholder so a future
            # re-open can be picked up cleanly.
            self._tracking_in_flight.discard(symbol)
            if tp.closing:
                # We initiated this close — already accounted for
                return
            logger.info(
                f"[TRAIL] {symbol} closed externally (was tracking "
                f"{tp.side.value} {tp.shares} @ ${tp.entry_price:.2f})"
            )
            self.stats.positions_closed_externally += 1
            self.recorder.event(
                "external_close",
                symbol=symbol,
                side=tp.side,
                shares=tp.shares,
                entry=tp.entry_price,
                last_stop=tp.stop_price,
                phase=tp.phase,
            )
            return

        # New open we should start tracking
        if position_size != 0 and symbol not in self.tracked:
            # CRITICAL: guard against the partial-fill cascade — many
            # positionEvent fires arrive in the same millisecond. We must
            # add to _tracking_in_flight SYNCHRONOUSLY (in this sync
            # handler, before any await) so later events for the same
            # symbol see it and skip.
            if symbol in self._tracking_in_flight:
                return
            if self.cfg.manage_only_new and symbol in self._ignored_at_startup:
                # Pre-existing position; still ignored
                return
            self._tracking_in_flight.add(symbol)
            asyncio.create_task(self._begin_tracking(ib_pos, source="position_event"))

    # ------------------------------------------------------------------
    # Tracking
    # ------------------------------------------------------------------

    async def _begin_tracking(self, ib_pos: IBPosition, source: str) -> None:
        symbol = ib_pos.contract.symbol
        # The sync side already added us to _tracking_in_flight; clear it
        # on every exit path so future re-opens can be picked up.
        try:
            await self._begin_tracking_inner(ib_pos, source)
        finally:
            self._tracking_in_flight.discard(symbol)

    async def _begin_tracking_inner(self, ib_pos: IBPosition, source: str) -> None:
        symbol = ib_pos.contract.symbol
        if symbol in self.tracked:
            return

        shares = abs(int(ib_pos.position))
        side = Side.LONG if ib_pos.position > 0 else Side.SHORT
        entry = float(ib_pos.avgCost)

        # Subscribe to market data so we get live ticks for the ratchet
        try:
            await self.market_data.subscribe(symbol)
        except Exception as e:
            logger.error(f"  [TRAIL] failed to subscribe market data for {symbol}: {e}")
            return

        # Wait briefly for a first quote so we can seed peak with current price
        peak = entry
        for _ in range(20):  # up to 2s
            q = self.market_data.get_quote(symbol)
            if q is not None and (q.is_tradeable or q.last > 0):
                px = q.mid if q.mid > 0 else q.last
                if px > 0:
                    peak = px
                    break
            await asyncio.sleep(0.1)

        # Initial stop. In passive-until-profit mode we don't arm a stop
        # at all in the INITIAL phase — the position's owning strategy
        # (e.g. the regime agent's ATR stop) handles initial risk, and we
        # only kick in once the trade reaches BREAKEVEN.
        if self.cfg.passive_until_profit:
            # Sentinel: far away from any reachable price, never triggers.
            stop_price = 0.0 if side == Side.LONG else float("inf")
        elif side == Side.LONG:
            stop_price = entry * (1 - self.cfg.initial_stop_pct)
        else:
            stop_price = entry * (1 + self.cfg.initial_stop_pct)

        tp = TrackedPosition(
            symbol=symbol,
            side=side,
            shares=shares,
            entry_price=entry,
            peak=peak,
            stop_price=stop_price,
            phase=TrailPhase.INITIAL,
        )
        self.tracked[symbol] = tp
        self.stats.positions_tracked += 1

        logger.info(
            f"[TRAIL] tracking {side.value} {shares} {symbol} "
            f"entry=${entry:.2f} peak=${peak:.2f} stop=${stop_price:.2f} "
            f"(source={source})"
        )
        self.recorder.event(
            "track_start",
            symbol=symbol,
            side=side,
            shares=shares,
            entry=entry,
            peak=peak,
            stop=stop_price,
            source=source,
        )

        # Re-evaluate now in case the entry quote already breached the stop
        self._evaluate(tp, peak)

    # ------------------------------------------------------------------
    # Quote handler
    # ------------------------------------------------------------------

    def _on_quote(self, quote: Quote) -> None:
        symbol = quote.symbol
        if symbol not in self.tracked:
            return
        tp = self.tracked[symbol]
        if tp.closing:
            return

        price = quote.mid if quote.mid > 0 else quote.last
        if not price or price <= 0:
            return

        self._evaluate(tp, price)

    def _evaluate(self, tp: TrackedPosition, price: float) -> None:
        """Update peak/stop and check for trigger. Called per-quote."""
        old_stop = tp.stop_price
        old_phase = tp.phase

        c = self.cfg

        if tp.side == Side.LONG:
            # Track highest price
            if price > tp.peak:
                tp.peak = price

            # Phase transitions
            favorable_move = (tp.peak - tp.entry_price) / tp.entry_price
            if tp.phase == TrailPhase.INITIAL and favorable_move >= c.breakeven_trigger_pct:
                tp.phase = TrailPhase.BREAKEVEN
                # Move stop to entry (locked in: no loss)
                tp.stop_price = max(tp.stop_price, tp.entry_price)

            if tp.phase in (TrailPhase.BREAKEVEN, TrailPhase.TRAILING):
                candidate = tp.peak * (1 - c.trail_pct)
                if candidate > tp.stop_price:
                    tp.stop_price = candidate
                # Once stop > entry, we're in profit-trailing zone
                if tp.stop_price > tp.entry_price:
                    tp.phase = TrailPhase.TRAILING

            # Trigger?
            if price <= tp.stop_price and not tp.closing:
                tp.closing = True
                asyncio.create_task(self._close(tp, price, "trail stop hit"))
        else:
            # SHORT — track lowest price (peak holds the trough for symmetry)
            if price < tp.peak:
                tp.peak = price

            favorable_move = (tp.entry_price - tp.peak) / tp.entry_price
            if tp.phase == TrailPhase.INITIAL and favorable_move >= c.breakeven_trigger_pct:
                tp.phase = TrailPhase.BREAKEVEN
                tp.stop_price = min(tp.stop_price, tp.entry_price)

            if tp.phase in (TrailPhase.BREAKEVEN, TrailPhase.TRAILING):
                candidate = tp.peak * (1 + c.trail_pct)
                if candidate < tp.stop_price:
                    tp.stop_price = candidate
                if tp.stop_price < tp.entry_price:
                    tp.phase = TrailPhase.TRAILING

            if price >= tp.stop_price and not tp.closing:
                tp.closing = True
                asyncio.create_task(self._close(tp, price, "trail stop hit"))

        # Log stop movement
        if tp.stop_price != old_stop or tp.phase != old_phase:
            logger.info(
                f"[TRAIL] {tp.symbol} {tp.side.value} phase={tp.phase.value:<9} "
                f"peak=${tp.peak:.2f} stop ${old_stop:.2f} → ${tp.stop_price:.2f}  "
                f"(price=${price:.2f}, entry=${tp.entry_price:.2f})"
            )
            self.recorder.event(
                "stop_update",
                symbol=tp.symbol,
                side=tp.side,
                phase=tp.phase,
                old_stop=old_stop,
                new_stop=tp.stop_price,
                peak=tp.peak,
                price=price,
                entry=tp.entry_price,
            )

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------

    async def _close(self, tp: TrackedPosition, trigger_price: float, reason: str) -> None:
        action = OrderAction.SELL if tp.side == Side.LONG else OrderAction.BUY

        # Cross-client coordination: skip if another agent (e.g. the regime
        # agent on its own bar-close exit logic, or a manual flatten) has
        # already submitted a close. tp.closing was set by the quote handler
        # before scheduling us, so duplicate ticks within this agent are
        # already filtered.
        if await self.orders.has_working_order(tp.symbol, action):
            logger.warning(
                f"[TRAIL] {tp.symbol}: another client has a working {action.value} "
                f"order — releasing our close so the other side handles it"
            )
            self.recorder.event(
                "close_skipped_race",
                symbol=tp.symbol,
                side=tp.side,
                shares=tp.shares,
                phase=tp.phase,
            )
            # Release the closing flag — if the other client's close fills,
            # positionEvent will remove us from `tracked`. If it doesn't,
            # the next tick will re-trigger us.
            tp.closing = False
            return

        # Categorize the exit BEFORE placing the order, based on the
        # phase the trail was in when we triggered.
        if tp.phase == TrailPhase.TRAILING:
            category = "profit"
            self.stats.positions_closed_profit += 1
        elif tp.phase == TrailPhase.BREAKEVEN:
            category = "breakeven"
            self.stats.positions_closed_breakeven += 1
        else:
            category = "loss"
            self.stats.positions_closed_loss += 1

        logger.info(
            f"[TRAIL] CLOSE {tp.side.value} {tp.shares} {tp.symbol} @~${trigger_price:.2f}  "
            f"phase={tp.phase.value} category={category}  reason={reason}"
        )
        self.recorder.event(
            "close_trigger",
            symbol=tp.symbol,
            side=tp.side,
            shares=tp.shares,
            entry=tp.entry_price,
            peak=tp.peak,
            stop=tp.stop_price,
            trigger_price=trigger_price,
            phase=tp.phase,
            category=category,
            reason=reason,
        )

        try:
            close_info = await self.orders.place_market_order(tp.symbol, tp.shares, action)
        except Exception as e:
            logger.error(f"[TRAIL] close order failed for {tp.symbol}: {e}")
            self.recorder.event(
                "close_order_failed",
                symbol=tp.symbol,
                shares=tp.shares,
                error=str(e),
            )
            # Re-allow future triggers — the position may still be open
            tp.closing = False
            return

        done = await self.orders.wait_for_done(close_info.order_id, timeout=10.0)
        if not done:
            logger.warning(
                f"[TRAIL] {tp.symbol} close order {close_info.order_id} did not "
                f"reach terminal state in 10s — position may still be open"
            )

        # Estimate realized PnL using the trigger price
        sign = 1 if tp.side == Side.LONG else -1
        trade_pnl = sign * (trigger_price - tp.entry_price) * tp.shares
        self.stats.realized_pnl += trade_pnl
        # record_close updates session PnL AND the per-symbol circuit breaker
        self.risk.record_close(tp.symbol, trade_pnl)

        logger.info(
            f"[TRAIL] {tp.symbol} trade PnL ≈ ${trade_pnl:+.2f}  "
            f"(session: ${self.stats.realized_pnl:+.2f})"
        )
        self.recorder.event(
            "close_done",
            symbol=tp.symbol,
            side=tp.side,
            shares=tp.shares,
            entry=tp.entry_price,
            exit=trigger_price,
            pnl=trade_pnl,
            category=category,
            session_pnl=self.stats.realized_pnl,
        )

        # The positionEvent will fire on the actual close and remove from `tracked`;
        # remove now too in case the event lags.
        self.tracked.pop(tp.symbol, None)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def snapshot(self) -> dict:
        return {
            "tracked": {
                sym: {
                    "side": tp.side.value,
                    "shares": tp.shares,
                    "entry": tp.entry_price,
                    "peak": tp.peak,
                    "stop": tp.stop_price,
                    "phase": tp.phase.value,
                }
                for sym, tp in self.tracked.items()
            },
            "stats": self.stats.__dict__,
        }
