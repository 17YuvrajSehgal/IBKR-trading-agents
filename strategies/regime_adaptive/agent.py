"""
Regime-adaptive multi-timeframe trading agent.

Plumbing:
    ib_async.reqHistoricalDataAsync(keepUpToDate=True)  → live bars
       ├── HTF (e.g. 1-hour)  → BiasDetector  → Bias
       └── MTF (e.g. 5-min)   → RegimeDetector → RegimeSnapshot
                              → SignalGenerator → Decision
                              → OrderManager (gated by RiskManager)

Lifecycle:
    agent = RegimeAdaptiveAgent(ib, risk, ...)
    await agent.start()       # subscribe & warm up
    ...                       # event-driven trading
    await agent.stop()        # force-flatten + unsubscribe
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from ib_async import IB, Stock

from ibkr.recorder import NULL_RECORDER, SessionRecorder
from ibkr.trading import OrderManager, OrderAction, OrderInfo
from ibkr.risk import RiskManager

from strategies.regime_adaptive.bars import Bar
from strategies.regime_adaptive.regime import (
    Bias, BiasDetector, Regime, RegimeDetector, RegimeSnapshot,
)
from strategies.regime_adaptive.signals import (
    Action, Decision, SignalConfig, SignalGenerator,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config & state
# ---------------------------------------------------------------------------

@dataclass
class AgentConfig:
    """Runtime knobs for the regime-adaptive agent."""

    symbol: str = "TSLA"

    # Timeframes — using ib_async barSizeSetting strings
    htf_bar: str = "1 hour"        # bias TF
    mtf_bar: str = "5 mins"        # regime + entry TF
    htf_lookback: str = "10 D"     # warmup window
    mtf_lookback: str = "5 D"

    # Risk per trade as fraction of account NetLiq.
    risk_per_trade_pct: float = 0.003   # 0.3%
    # Hard cap on per-trade $ risk, regardless of account size.
    max_risk_per_trade_usd: float = 200.0
    # Hard cap on position $ notional.
    max_position_notional: float = 30_000.0

    # Cool-down between exiting one trade and re-entering.
    cooldown_seconds: float = 120.0


@dataclass
class OpenPosition:
    side: Action                          # ENTER_LONG or ENTER_SHORT
    shares: int
    entry_price: float
    entry_time: datetime
    stop_price: float
    target_price: float
    entry_regime: Regime
    bars_held: int = 0


@dataclass
class AgentStats:
    htf_bars: int = 0
    mtf_bars: int = 0
    decisions: int = 0
    trades_attempted: int = 0
    trades_rejected_by_risk: int = 0
    positions_opened: int = 0
    positions_closed_target: int = 0
    positions_closed_stop: int = 0
    positions_closed_time: int = 0
    positions_closed_regime: int = 0
    realized_pnl: float = 0.0
    started_at: datetime = field(default_factory=datetime.now)


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class RegimeAdaptiveAgent:
    """
    Multi-timeframe regime-adaptive technical agent.

    Designed for paper trading. Use ATR-based sizing, hard stops, and
    a daily-loss kill switch via RiskManager.
    """

    def __init__(
        self,
        ib: IB,
        risk: RiskManager,
        account_nlv: float,
        agent_config: Optional[AgentConfig] = None,
        signal_config: Optional[SignalConfig] = None,
        orders: Optional[OrderManager] = None,
        recorder: Optional[SessionRecorder] = None,
    ) -> None:
        self.ib = ib
        self.risk = risk
        self.account_nlv = float(account_nlv)
        self.cfg = agent_config or AgentConfig()
        self.signal_cfg = signal_config or SignalConfig()
        # Recorder is optional — NULL_RECORDER is a no-op so callers never need
        # to guard with `if self.recorder is not None`.
        self.recorder = recorder if recorder is not None else NULL_RECORDER

        # Multi-agent setups pass a shared OrderManager so we don't register
        # duplicate event handlers on the same IB connection.
        self.orders = orders if orders is not None else OrderManager(ib)

        self.bias_detector = BiasDetector()
        self.regime_detector = RegimeDetector()
        self.signals = SignalGenerator(self.signal_cfg)

        # Live state
        self.position: Optional[OpenPosition] = None
        self._last_close_at: float = 0.0
        self.stats = AgentStats()
        self._executing = False

        # Latest snapshots (set during bar updates)
        self._latest_bias: Bias = Bias.NEUTRAL
        self._latest_regime: Optional[RegimeSnapshot] = None
        self._last_mtf_close: Optional[Bar] = None

        # ib_async BarDataList handles (held to keep subscriptions alive)
        self._htf_bars = None
        self._mtf_bars = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        logger.info(
            f"Starting regime-adaptive agent on {self.cfg.symbol} "
            f"(HTF={self.cfg.htf_bar}, MTF={self.cfg.mtf_bar}, "
            f"risk={self.cfg.risk_per_trade_pct*100:.2f}% per trade)"
        )

        contract = Stock(self.cfg.symbol, "SMART", "USD")
        qualified = await self.ib.qualifyContractsAsync(contract)
        if not qualified:
            raise RuntimeError(f"Failed to qualify contract for {self.cfg.symbol}")

        # Subscribe to HTF (bias) bars
        self._htf_bars = await self.ib.reqHistoricalDataAsync(
            contract,
            endDateTime="",
            durationStr=self.cfg.htf_lookback,
            barSizeSetting=self.cfg.htf_bar,
            whatToShow="TRADES",
            useRTH=False,
            formatDate=1,
            keepUpToDate=True,
        )
        # Warm up the bias detector with the historical portion
        for raw in self._htf_bars:
            self.bias_detector.update(self._to_bar(raw))
            self.stats.htf_bars += 1
        logger.info(
            f"[{self.cfg.symbol}] HTF warmup: {len(self._htf_bars)} bars, "
            f"initial bias={self.bias_detector.bias.value}"
        )
        self._htf_bars.updateEvent += self._on_htf_update

        # Subscribe to MTF (regime + entry) bars
        self._mtf_bars = await self.ib.reqHistoricalDataAsync(
            contract,
            endDateTime="",
            durationStr=self.cfg.mtf_lookback,
            barSizeSetting=self.cfg.mtf_bar,
            whatToShow="TRADES",
            useRTH=False,
            formatDate=1,
            keepUpToDate=True,
        )
        # Warm up the regime detector + signal indicators with historical bars
        for raw in self._mtf_bars:
            bar = self._to_bar(raw)
            self._latest_regime = self.regime_detector.update(bar)
            # Also prime RSI / Bollinger inside SignalGenerator
            self.signals.rsi.update(bar.close)
            self.signals.bb.update(bar.close)
            self.stats.mtf_bars += 1
            self._last_mtf_close = bar
        if self._latest_regime is not None:
            logger.info(
                f"[{self.cfg.symbol}] MTF warmup: {len(self._mtf_bars)} bars, "
                f"initial regime={self._latest_regime.regime.value} "
                f"ADX={self._latest_regime.adx:.1f} ATR%={self._latest_regime.atr_pct*100:.2f}"
            )
        self._mtf_bars.updateEvent += self._on_mtf_update

        # Latest bias from warmup
        self._latest_bias = self.bias_detector.bias
        logger.info(f"[{self.cfg.symbol}] Agent live — waiting for bar updates")

        # Record warmup state for offline analysis
        self.recorder.event(
            "warmup_complete",
            symbol=self.cfg.symbol,
            bias=self._latest_bias,
            regime=self._latest_regime.regime if self._latest_regime else None,
            adx=self._latest_regime.adx if self._latest_regime else None,
            atr_pct=self._latest_regime.atr_pct if self._latest_regime else None,
            htf_bars=self.stats.htf_bars,
            mtf_bars=self.stats.mtf_bars,
        )

    async def stop(self, flatten: bool = True) -> None:
        logger.info("Stopping regime-adaptive agent")
        try:
            if flatten and self.position is not None:
                logger.warning(f"Force-flattening {self.position.side.value} on shutdown")
                await self._close_position(reason="shutdown")
        finally:
            try:
                if self._htf_bars is not None:
                    self.ib.cancelHistoricalData(self._htf_bars)
                if self._mtf_bars is not None:
                    self.ib.cancelHistoricalData(self._mtf_bars)
            except Exception as e:
                logger.warning(f"Error cancelling bar subscriptions: {e}")
            logger.info(f"Final stats: {self.stats}")

    # ------------------------------------------------------------------
    # Bar update handlers  (sync; ib_async invokes from event loop)
    # ------------------------------------------------------------------

    def _on_htf_update(self, bars, has_new_bar: bool) -> None:
        """Higher-timeframe bar update from ib_async."""
        if not has_new_bar or len(bars) < 2:
            return  # only react to closed bars
        # The just-closed HTF bar is at index -2 (last one is forming)
        closed = self._to_bar(bars[-2])
        new_bias = self.bias_detector.update(closed)
        self.stats.htf_bars += 1

        if new_bias != self._latest_bias:
            logger.info(
                f"[{self.cfg.symbol}] HTF bias change: "
                f"{self._latest_bias.value} → {new_bias.value} @ ${closed.close:.2f}"
            )
            self.recorder.event(
                "bias_change",
                symbol=self.cfg.symbol,
                old=self._latest_bias,
                new=new_bias,
                price=closed.close,
            )
        self._latest_bias = new_bias

    def _on_mtf_update(self, bars, has_new_bar: bool) -> None:
        """Entry-timeframe bar update — the meat of the agent."""
        if not has_new_bar or len(bars) < 2:
            return

        closed = self._to_bar(bars[-2])
        self._latest_regime = self.regime_detector.update(closed)
        self._last_mtf_close = closed
        self.stats.mtf_bars += 1

        if self._latest_regime is None:
            return  # still warming up

        # Tick bars_held on any open position
        if self.position is not None:
            self.position.bars_held += 1

        # Schedule async evaluation; we can't await from the sync callback.
        asyncio.create_task(self._evaluate(closed))

    # ------------------------------------------------------------------
    # Decision + execution
    # ------------------------------------------------------------------

    async def _evaluate(self, bar: Bar) -> None:
        if self._executing:
            return
        self._executing = True
        try:
            regime = self._latest_regime
            if regime is None:
                return

            in_position = self.position is not None
            decision = self.signals.update(
                bar=bar,
                regime=regime,
                htf_bias=self._latest_bias,
                in_position=in_position,
                position_side=self.position.side if in_position else None,
                bars_held=self.position.bars_held if in_position else 0,
                stop_price=self.position.stop_price if in_position else None,
                target_price=self.position.target_price if in_position else None,
                entry_regime=self.position.entry_regime if in_position else None,
            )
            self.stats.decisions += 1

            self._log_heartbeat(bar, regime, decision)

            if decision.action == Action.NONE:
                return

            if decision.action in (Action.ENTER_LONG, Action.ENTER_SHORT):
                if not self._cooldown_elapsed():
                    logger.debug(f"Cooldown active, skipping {decision.action.value}")
                    return
                await self._open_position(bar, regime, decision)
            elif decision.action == Action.EXIT:
                await self._close_position(reason=decision.reason)
        finally:
            self._executing = False

    def _cooldown_elapsed(self) -> bool:
        return (asyncio.get_event_loop().time() - self._last_close_at) >= self.cfg.cooldown_seconds

    async def _open_position(
        self, bar: Bar, regime: RegimeSnapshot, decision: Decision,
    ) -> None:
        if self.position is not None:
            return
        if decision.stop_price is None or decision.target_price is None:
            logger.error(f"Cannot open without stop/target: {decision}")
            return

        entry = bar.close
        stop_distance = abs(entry - decision.stop_price)
        if stop_distance <= 0:
            logger.error("Zero stop distance — aborting trade")
            return

        # Volatility-targeted sizing
        risk_dollars = min(
            self.cfg.risk_per_trade_pct * self.account_nlv,
            self.cfg.max_risk_per_trade_usd,
        )
        shares = int(risk_dollars // stop_distance)

        # Notional cap
        max_shares_by_notional = int(self.cfg.max_position_notional // max(entry, 1.0))
        shares = min(shares, max_shares_by_notional)

        if shares < 1:
            logger.info(
                f"Sizing produced 0 shares (risk=${risk_dollars:.0f}, "
                f"stop_dist=${stop_distance:.2f}). Skipping."
            )
            return

        action = OrderAction.BUY if decision.action == Action.ENTER_LONG else OrderAction.SELL

        # Risk gate
        self.stats.trades_attempted += 1
        check = self.risk.check_order(self.cfg.symbol, action.value, shares, entry)
        if not check.approved:
            self.stats.trades_rejected_by_risk += 1
            logger.warning(f"Risk rejected: {check.reason}")
            return

        logger.info(
            f"OPEN {decision.action.value}  {shares} {self.cfg.symbol} @~${entry:.2f}  "
            f"stop=${decision.stop_price:.2f}  target=${decision.target_price:.2f}  "
            f"R:R={abs(decision.target_price-entry)/stop_distance:.2f}  reason: {decision.reason}"
        )

        try:
            order_info = await self.orders.place_market_order(self.cfg.symbol, shares, action)
        except Exception as e:
            # check_order already reserved exposure — undo it on failure.
            self.risk.release_reservation(self.cfg.symbol, action.value, shares, entry)
            logger.error(f"Order placement failed: {e}")
            self.recorder.event(
                "order_failed",
                symbol=self.cfg.symbol,
                side=action,
                shares=shares,
                error=str(e),
            )
            return

        self.position = OpenPosition(
            side=decision.action,
            shares=shares,
            entry_price=entry,
            entry_time=bar.time,
            stop_price=decision.stop_price,
            target_price=decision.target_price,
            entry_regime=regime.regime,
        )
        self._open_order_id = order_info.order_id
        self.stats.positions_opened += 1
        # NOTE: exposure is already counted via check_order's reservation;
        # no need to call record_fill (which is now a no-op).

        self.recorder.event(
            "open",
            symbol=self.cfg.symbol,
            side=decision.action,
            shares=shares,
            entry=entry,
            stop=decision.stop_price,
            target=decision.target_price,
            regime=regime.regime,
            bias=self._latest_bias,
            adx=regime.adx,
            atr=regime.atr,
            rsi=self.signals.last_rsi,
            reason=decision.reason,
        )

    async def _close_position(self, reason: str) -> None:
        if self.position is None:
            return

        pos = self.position
        # Reverse the side
        action = OrderAction.SELL if pos.side == Action.ENTER_LONG else OrderAction.BUY
        last_price = self._last_mtf_close.close if self._last_mtf_close else pos.entry_price

        logger.info(
            f"CLOSE {pos.side.value} {pos.shares} {self.cfg.symbol} @~${last_price:.2f}  "
            f"reason: {reason}"
        )

        # Closes are NOT risk-gated — we always want to exit a position. Place
        # the order directly and then wait for it to reach a terminal state
        # before returning, so a fast shutdown doesn't leak open positions.
        try:
            close_info = await self.orders.place_market_order(
                self.cfg.symbol, pos.shares, action
            )
        except Exception as e:
            logger.error(f"Close order failed: {e}")
            self.recorder.event(
                "close_order_failed",
                symbol=self.cfg.symbol,
                shares=pos.shares,
                error=str(e),
            )
            return

        done = await self.orders.wait_for_done(close_info.order_id, timeout=10.0)
        if not done:
            logger.warning(
                f"Close order {close_info.order_id} for {self.cfg.symbol} did not "
                f"reach terminal state within 10s — position may still be open"
            )

        # PnL estimate using last bar close (commissionReport will adjust real PnL)
        sign = 1 if pos.side == Action.ENTER_LONG else -1
        trade_pnl = sign * (last_price - pos.entry_price) * pos.shares
        self.stats.realized_pnl += trade_pnl

        # Release the original entry's reservation so exposure clears.
        entry_action = "BUY" if pos.side == Action.ENTER_LONG else "SELL"
        self.risk.release_reservation(
            self.cfg.symbol, entry_action, pos.shares, pos.entry_price,
        )
        self.risk.record_pnl(trade_pnl)

        # Categorize the exit
        if "stop hit" in reason:
            self.stats.positions_closed_stop += 1
        elif "target hit" in reason:
            self.stats.positions_closed_target += 1
        elif "time stop" in reason:
            self.stats.positions_closed_time += 1
        elif "regime" in reason or "flipped" in reason:
            self.stats.positions_closed_regime += 1

        logger.info(
            f"  → trade PnL ≈ ${trade_pnl:+.2f}  "
            f"(session realized: ${self.stats.realized_pnl:+.2f})"
        )

        self.recorder.event(
            "close",
            symbol=self.cfg.symbol,
            side=pos.side,
            shares=pos.shares,
            entry=pos.entry_price,
            exit=last_price,
            pnl=trade_pnl,
            bars_held=pos.bars_held,
            reason=reason,
            session_pnl=self.stats.realized_pnl,
        )

        self.position = None
        self._last_close_at = asyncio.get_event_loop().time()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _to_bar(raw) -> Bar:
        """Convert ib_async BarData → our Bar."""
        # `raw.date` is datetime for intraday, date for daily.
        t = raw.date if isinstance(raw.date, datetime) else datetime.combine(
            raw.date, datetime.min.time()
        )
        return Bar(
            time=t,
            open=float(raw.open),
            high=float(raw.high),
            low=float(raw.low),
            close=float(raw.close),
            volume=float(raw.volume) if raw.volume is not None else 0.0,
        )

    def _log_heartbeat(self, bar: Bar, regime: RegimeSnapshot, decision: Decision) -> None:
        if self.position is None:
            pos_str = "FLAT"
        else:
            pos_str = (
                f"{self.position.side.value}@{self.position.entry_price:.2f}"
                f"(stop={self.position.stop_price:.2f} tgt={self.position.target_price:.2f})"
            )
        rsi_str = f"{self.signals.last_rsi:.1f}" if self.signals.last_rsi is not None else "—"
        logger.info(
            f"[{self.cfg.symbol:<5}] close=${bar.close:.2f}  "
            f"regime={regime.regime.value:<10} HTF={self._latest_bias.value:<7} "
            f"ADX={regime.adx:.1f} ATR%={regime.atr_pct*100:.2f} "
            f"RSI={rsi_str}  pos={pos_str}  decision={decision.action.value}"
        )

    def snapshot(self) -> dict:
        return {
            "position": (
                None if self.position is None
                else {
                    "side": self.position.side.value,
                    "shares": self.position.shares,
                    "entry": self.position.entry_price,
                    "stop": self.position.stop_price,
                    "target": self.position.target_price,
                    "bars_held": self.position.bars_held,
                }
            ),
            "bias": self._latest_bias.value,
            "regime": self._latest_regime.regime.value if self._latest_regime else "WARMUP",
            "stats": self.stats.__dict__.copy(),
        }
