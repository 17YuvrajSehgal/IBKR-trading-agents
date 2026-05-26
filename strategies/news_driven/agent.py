"""
News-driven trading agent.

Lifecycle:
    NewsManager.subscribe_symbol_headlines(symbol)
       ↓ live headline arrives
    NewsClassifier.score(headline, provider_code)
       ↓ if is_actionable + not in cooldown + flat
    MarketDataManager.get_quote(symbol) — current mid price
       ↓
    RiskManager.check_order(...)  — gated, atomic reserve
       ↓ approved
    OrderManager.place_market_order(...)
       ↓
    Position held with:
       * % stop loss (default 0.7%)
       * % take-profit (default 1.4%)
       * time stop (default 30 min)
       * regime exit if market data goes stale

This is a SHORT-HORIZON strategy. News-driven moves typically play out in
minutes, not hours. Defaults are tuned for that.

Designed for paper trading. The runner refuses non-paper ports.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

from ib_async import IB

from ibkr.market_data import MarketDataManager, Quote
from ibkr.news import NewsManager, NewsHeadline
from ibkr.recorder import NULL_RECORDER, SessionRecorder
from ibkr.risk import RiskManager
from ibkr.trading import OrderAction, OrderInfo, OrderManager

from strategies.news_driven.classifier import NewsClassifier, NewsScore


logger = logging.getLogger(__name__)


class PositionSide(str, Enum):
    FLAT = "FLAT"
    LONG = "LONG"
    SHORT = "SHORT"


class ShortMode(str, Enum):
    """Same semantic as the regime agent — controls when we'll short."""
    CONFIDENT = "CONFIDENT"   # only on strongly-negative + premium provider
    SYMMETRIC = "SYMMETRIC"   # mirror longs
    OFF = "OFF"               # no shorts


@dataclass
class NewsAgentConfig:
    # Universe & subscription
    symbols: list[str] = field(default_factory=lambda: [
        "TSLA", "NVDA", "AMD", "MU", "COIN", "META", "AAPL", "MSFT", "GOOG", "AMZN",
    ])

    # Sentiment thresholds
    action_threshold: float = 2.5
    short_mode: ShortMode = ShortMode.CONFIDENT
    short_strong_threshold: float = 3.5  # CONFIDENT shorts need stronger signal
    # Limit shorts to premium providers only (DJNL/DJ-RT/RSF-Z)
    short_premium_providers: tuple[str, ...] = ("DJNL", "DJ-RT", "DJ-N", "RSF-Z")

    # Sizing
    dollars_per_trade: float = 5_000.0   # gross $ exposure per trade
    max_dollars_per_trade: float = 25_000.0

    # Exits — news effects decay fast, so use % stops + time stop
    stop_pct: float = 0.007      # 0.7% adverse move
    target_pct: float = 0.014    # 1.4% favorable move (2:1 R:R)
    time_stop_seconds: float = 1800.0   # 30 min

    # Cooldown per symbol (avoid firing repeatedly on follow-up headlines)
    cooldown_seconds: float = 3600.0    # 1 hour

    # How long to wait for a quote after a headline before giving up
    quote_wait_seconds: float = 5.0


@dataclass
class OpenPosition:
    symbol: str
    side: PositionSide
    shares: int
    entry_price: float
    entry_time: datetime
    stop_price: float
    target_price: float
    triggering_headline: str
    triggering_score: float


@dataclass
class NewsAgentStats:
    headlines_seen: int = 0
    headlines_actionable: int = 0
    headlines_dropped_cooldown: int = 0
    headlines_dropped_no_quote: int = 0
    headlines_dropped_short_mode: int = 0
    trades_attempted: int = 0
    trades_rejected_by_risk: int = 0
    positions_opened: int = 0
    positions_closed_target: int = 0
    positions_closed_stop: int = 0
    positions_closed_time: int = 0
    realized_pnl: float = 0.0
    started_at: datetime = field(default_factory=datetime.now)


class NewsDrivenAgent:
    """
    Reactive news trader. One agent watches N symbols and trades the first
    actionable headline per symbol (within cooldown).
    """

    def __init__(
        self,
        ib: IB,
        risk: RiskManager,
        agent_config: Optional[NewsAgentConfig] = None,
        classifier: Optional[NewsClassifier] = None,
        orders: Optional[OrderManager] = None,
        recorder: Optional[SessionRecorder] = None,
    ) -> None:
        self.ib = ib
        self.risk = risk
        self.cfg = agent_config or NewsAgentConfig()
        self.classifier = classifier or NewsClassifier(action_threshold=self.cfg.action_threshold)
        self.orders = orders if orders is not None else OrderManager(ib)
        self.recorder = recorder if recorder is not None else NULL_RECORDER

        self.news = NewsManager(ib)
        self.market_data = MarketDataManager(ib)

        # Per-symbol state
        self.positions: dict[str, OpenPosition] = {}
        self._last_action_at: dict[str, float] = {}    # symbol -> monotonic time
        self._exit_monitors: dict[str, asyncio.Task] = {}

        self.stats = NewsAgentStats()
        self._executing = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        logger.info(
            f"Starting news-driven agent on {self.cfg.symbols} "
            f"(threshold={self.cfg.action_threshold}, "
            f"size=${self.cfg.dollars_per_trade:,.0f}, shorts={self.cfg.short_mode.value})"
        )

        # Show available providers so the operator knows what feed they're using
        try:
            providers = await self.news.get_news_providers()
            codes = sorted(p.code for p in providers)
            logger.info(f"Available news providers: {codes}")
            self.recorder.event("news_providers", providers=codes)
        except Exception as e:
            logger.warning(f"Could not list news providers: {e}")

        # Subscribe to live headlines AND market data for each symbol.
        # Market data is needed for entry price + exit checks.
        for symbol in self.cfg.symbols:
            try:
                await self.market_data.subscribe(symbol)
            except Exception as e:
                logger.error(f"Market-data subscribe failed for {symbol}: {e}")
                continue
            try:
                await self.news.subscribe_symbol_headlines(symbol)
            except Exception as e:
                logger.error(f"News subscribe failed for {symbol}: {e}")
            await asyncio.sleep(0.25)   # pacing-friendly

        self.news.subscribe_headline_updates(self._on_headline)
        logger.info(f"News-driven agent live on {len(self.cfg.symbols)} symbols")

    async def stop(self, flatten: bool = True) -> None:
        logger.info("Stopping news-driven agent")

        # Cancel any pending exit monitors
        for task in list(self._exit_monitors.values()):
            task.cancel()

        try:
            if flatten:
                for symbol in list(self.positions.keys()):
                    logger.warning(f"Force-flattening {symbol} on shutdown")
                    await self._close_position(symbol, reason="shutdown")
        finally:
            try:
                self.news.unsubscribe_all()
                self.market_data.unsubscribe_all()
            except Exception as e:
                logger.warning(f"Error during subscription cleanup: {e}")
            logger.info(f"Final stats: {self.stats}")

    # ------------------------------------------------------------------
    # Headline handling
    # ------------------------------------------------------------------

    def _on_headline(self, headline: NewsHeadline) -> None:
        """ib_async-style sync callback. Hand off to async loop."""
        self.stats.headlines_seen += 1
        # Log every headline at INFO so the operator sees the feed working
        sym_str = headline.symbol or "?"
        logger.info(
            f"[NEWS] {headline.time:%H:%M:%S} [{headline.provider_code}/{sym_str}] "
            f"{headline.headline[:120]}"
        )
        self.recorder.event(
            "headline",
            symbol=headline.symbol,
            provider=headline.provider_code,
            article_id=headline.article_id,
            text=headline.headline,
            time=headline.time,
        )
        asyncio.create_task(self._maybe_act(headline))

    async def _maybe_act(self, headline: NewsHeadline) -> None:
        if self._executing:
            return
        self._executing = True
        try:
            symbol = headline.symbol
            if not symbol:
                return

            score = self.classifier.score(headline.headline, headline.provider_code)

            self.recorder.event(
                "score",
                symbol=symbol,
                provider=headline.provider_code,
                score=score.score,
                raw=score.raw_score,
                matched=[m[0] for m in score.matched],
                actionable=score.is_actionable,
            )

            if not score.is_actionable:
                return
            self.stats.headlines_actionable += 1

            # Short-side gating
            if score.is_negative:
                if not self._short_allowed(headline.provider_code, score.score):
                    self.stats.headlines_dropped_short_mode += 1
                    logger.info(f"  short blocked by mode={self.cfg.short_mode.value}")
                    return

            # Cooldown per symbol
            if not self._cooldown_elapsed(symbol):
                self.stats.headlines_dropped_cooldown += 1
                logger.info(f"  {symbol} in cooldown — skipping")
                return

            # Need a quote to size & set stops
            quote = await self._wait_for_quote(symbol)
            if quote is None:
                self.stats.headlines_dropped_no_quote += 1
                logger.warning(f"  no tradeable quote for {symbol} — skipping")
                return

            if symbol in self.positions:
                logger.info(f"  {symbol} already has open position — skipping")
                return

            await self._open_position(symbol, score, quote)
        except Exception as e:
            logger.exception(f"Error in headline handler: {e}")
        finally:
            self._executing = False

    # ------------------------------------------------------------------
    # Order operations
    # ------------------------------------------------------------------

    async def _open_position(
        self, symbol: str, score: NewsScore, quote: Quote,
    ) -> None:
        side = OrderAction.BUY if score.is_positive else OrderAction.SELL
        entry = quote.mid if quote.mid > 0 else (quote.last if quote.last > 0 else 0.0)
        if entry <= 0:
            logger.warning(f"  {symbol} quote unusable (mid={quote.mid}, last={quote.last})")
            return

        shares = max(1, int(self.cfg.dollars_per_trade // entry))

        check = self.risk.check_order(symbol, side.value, shares, entry)
        self.stats.trades_attempted += 1
        if not check.approved:
            self.stats.trades_rejected_by_risk += 1
            logger.warning(f"  risk rejected {symbol}: {check.reason}")
            self.recorder.event(
                "risk_reject", symbol=symbol, side=side, shares=shares, reason=check.reason,
            )
            return

        if score.is_positive:
            stop_price = entry * (1 - self.cfg.stop_pct)
            target_price = entry * (1 + self.cfg.target_pct)
        else:
            stop_price = entry * (1 + self.cfg.stop_pct)
            target_price = entry * (1 - self.cfg.target_pct)

        logger.info(
            f"  OPEN {('LONG' if score.is_positive else 'SHORT')} {shares} {symbol} "
            f"@~${entry:.2f}  stop=${stop_price:.2f}  target=${target_price:.2f}  "
            f"score={score.score:+.1f}"
        )

        try:
            order_info = await self.orders.place_market_order(symbol, shares, side)
        except Exception as e:
            self.risk.release_reservation(symbol, side.value, shares, entry)
            logger.error(f"  open order failed for {symbol}: {e}")
            self.recorder.event(
                "order_failed", symbol=symbol, side=side, shares=shares, error=str(e),
            )
            return

        position = OpenPosition(
            symbol=symbol,
            side=PositionSide.LONG if score.is_positive else PositionSide.SHORT,
            shares=shares,
            entry_price=entry,
            entry_time=datetime.now(),
            stop_price=stop_price,
            target_price=target_price,
            triggering_headline=score.headline,
            triggering_score=score.score,
        )
        self.positions[symbol] = position
        self._last_action_at[symbol] = asyncio.get_event_loop().time()
        self.stats.positions_opened += 1

        self.recorder.event(
            "open",
            symbol=symbol,
            side=position.side,
            shares=shares,
            entry=entry,
            stop=stop_price,
            target=target_price,
            score=score.score,
            provider=score.provider_code,
            headline=score.headline,
        )

        # Spin up the exit monitor for this position
        self._exit_monitors[symbol] = asyncio.create_task(self._monitor_exit(symbol))

    async def _monitor_exit(self, symbol: str) -> None:
        """Watch price + clock for this symbol; close on stop/target/time."""
        try:
            position = self.positions.get(symbol)
            if position is None:
                return
            start = asyncio.get_event_loop().time()
            while symbol in self.positions:
                quote = self.market_data.get_quote(symbol)
                if quote and quote.mid > 0:
                    last = quote.mid
                    if position.side == PositionSide.LONG:
                        if last <= position.stop_price:
                            await self._close_position(symbol, reason="stop", exit_price=last)
                            self.stats.positions_closed_stop += 1
                            return
                        if last >= position.target_price:
                            await self._close_position(symbol, reason="target", exit_price=last)
                            self.stats.positions_closed_target += 1
                            return
                    else:  # SHORT
                        if last >= position.stop_price:
                            await self._close_position(symbol, reason="stop", exit_price=last)
                            self.stats.positions_closed_stop += 1
                            return
                        if last <= position.target_price:
                            await self._close_position(symbol, reason="target", exit_price=last)
                            self.stats.positions_closed_target += 1
                            return

                if (asyncio.get_event_loop().time() - start) >= self.cfg.time_stop_seconds:
                    last = quote.mid if quote else position.entry_price
                    await self._close_position(symbol, reason="time_stop", exit_price=last)
                    self.stats.positions_closed_time += 1
                    return

                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.exception(f"  exit monitor failed for {symbol}: {e}")

    async def _close_position(
        self,
        symbol: str,
        reason: str,
        exit_price: Optional[float] = None,
    ) -> None:
        position = self.positions.get(symbol)
        if position is None:
            return

        action = OrderAction.SELL if position.side == PositionSide.LONG else OrderAction.BUY
        last_quote = self.market_data.get_quote(symbol)
        last_price = exit_price if exit_price is not None else (
            last_quote.mid if last_quote and last_quote.mid > 0 else position.entry_price
        )

        logger.info(
            f"  CLOSE {position.side.value} {position.shares} {symbol} @~${last_price:.2f}  "
            f"reason: {reason}"
        )

        try:
            close_info = await self.orders.place_market_order(symbol, position.shares, action)
        except Exception as e:
            logger.error(f"  close order failed for {symbol}: {e}")
            self.recorder.event(
                "close_order_failed", symbol=symbol, shares=position.shares, error=str(e),
            )
            # Don't release reservation — position state is now uncertain.
            return

        # Wait for terminal state before considering the position closed
        done = await self.orders.wait_for_done(close_info.order_id, timeout=10.0)
        if not done:
            logger.warning(
                f"  close order {close_info.order_id} for {symbol} did not reach "
                f"terminal state in 10s — position may still be open"
            )

        sign = 1 if position.side == PositionSide.LONG else -1
        trade_pnl = sign * (last_price - position.entry_price) * position.shares
        self.stats.realized_pnl += trade_pnl

        entry_action = "BUY" if position.side == PositionSide.LONG else "SELL"
        self.risk.release_reservation(
            symbol, entry_action, position.shares, position.entry_price,
        )
        self.risk.record_pnl(trade_pnl)

        logger.info(f"     → trade PnL ≈ ${trade_pnl:+.2f}")

        self.recorder.event(
            "close",
            symbol=symbol,
            side=position.side,
            shares=position.shares,
            entry=position.entry_price,
            exit=last_price,
            pnl=trade_pnl,
            reason=reason,
            session_pnl=self.stats.realized_pnl,
            triggering_headline=position.triggering_headline,
            triggering_score=position.triggering_score,
        )

        self.positions.pop(symbol, None)
        self._exit_monitors.pop(symbol, None)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _cooldown_elapsed(self, symbol: str) -> bool:
        last = self._last_action_at.get(symbol)
        if last is None:
            return True
        return (asyncio.get_event_loop().time() - last) >= self.cfg.cooldown_seconds

    def _short_allowed(self, provider_code: str, score: float) -> bool:
        if self.cfg.short_mode == ShortMode.OFF:
            return False
        if self.cfg.short_mode == ShortMode.SYMMETRIC:
            return True
        # CONFIDENT: stronger sentiment + premium provider
        return (
            abs(score) >= self.cfg.short_strong_threshold
            and provider_code in self.cfg.short_premium_providers
        )

    async def _wait_for_quote(self, symbol: str) -> Optional[Quote]:
        end = asyncio.get_event_loop().time() + self.cfg.quote_wait_seconds
        while asyncio.get_event_loop().time() < end:
            q = self.market_data.get_quote(symbol)
            if q and (q.is_tradeable or (q.last > 0)):
                return q
            await asyncio.sleep(0.2)
        return None
