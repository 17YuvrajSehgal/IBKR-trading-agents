"""
Real-time market data subscription manager for IBKR.

This module provides a clean, async interface for subscribing to L1 market
data (bid/ask/last/volume) via ib_async's pendingTickersEvent. All quote
data is maintained in an in-memory dictionary and can be consumed either
by polling (get_quote) or via push callbacks (subscribe_quotes).

Design principles:
  - Single subscription per symbol (deduplication)
  - Thread-safe internal state via asyncio event loop
  - Graceful cleanup on unsubscribe / shutdown
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from ib_async import IB, Contract, Ticker, Stock, Index
from ibkr.exceptions import IBKRConnectionError, IBKRDataError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Quote:
    """
    Best bid/ask/last snapshot for a single symbol.

    Attributes:
        symbol:     Ticker symbol (e.g. "AAPL")
        bid:        Best bid price  (NaN if unavailable)
        ask:        Best ask price  (NaN if unavailable)
        last:       Last traded price
        bid_size:   Shares available at bid
        ask_size:   Shares available at ask
        volume:     Session volume
        mid:        Midpoint of bid/ask spread (computed property)
        spread:     Absolute bid-ask spread (computed property)
        timestamp:  Time of last update
    """
    symbol:    str
    bid:       float = float("nan")
    ask:       float = float("nan")
    last:      float = float("nan")
    bid_size:  float = 0.0
    ask_size:  float = 0.0
    volume:    float = 0.0
    timestamp: datetime = field(default_factory=datetime.now)

    # ------------------------------------------------------------------
    # Computed properties
    # ------------------------------------------------------------------

    @property
    def mid(self) -> float:
        """Midpoint between best bid and ask."""
        if self._is_valid(self.bid) and self._is_valid(self.ask):
            return (self.bid + self.ask) / 2.0
        if self._is_valid(self.last):
            return self.last
        return float("nan")

    @property
    def spread(self) -> float:
        """Absolute bid-ask spread."""
        if self._is_valid(self.bid) and self._is_valid(self.ask):
            return self.ask - self.bid
        return float("nan")

    @property
    def spread_bps(self) -> float:
        """Spread in basis points relative to mid."""
        m = self.mid
        s = self.spread
        if self._is_valid(m) and self._is_valid(s) and m > 0:
            return (s / m) * 10_000
        return float("nan")

    @property
    def is_tradeable(self) -> bool:
        """True if both bid and ask are valid numbers."""
        return self._is_valid(self.bid) and self._is_valid(self.ask)

    @staticmethod
    def _is_valid(v: float) -> bool:
        import math
        return not (math.isnan(v) or math.isinf(v)) and v > 0

    def __repr__(self) -> str:
        return (
            f"Quote({self.symbol} "
            f"bid={self.bid:.4f} ask={self.ask:.4f} "
            f"mid={self.mid:.4f} spread_bps={self.spread_bps:.2f})"
        )


# Callback type alias: receives a fresh Quote on every tick
QuoteCallback = Callable[[Quote], None]


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class MarketDataManager:
    """
    Manages real-time L1 market data subscriptions via ib_async.

    Subscribe to symbols, receive live Quotes through callbacks (push)
    or poll them directly (get_quote). The manager deduplicates subscriptions
    so subscribing to the same symbol twice is safe.

    Usage:
        >>> mdm = MarketDataManager(conn.ib)
        >>> mdm.subscribe("AAPL")
        >>> mdm.subscribe("MSFT")
        >>> mdm.subscribe_quotes(lambda q: print(q))
        >>> # ... quote callbacks fire on every tick ...
        >>> quote = mdm.get_quote("AAPL")
        >>> print(quote.mid)
        >>> mdm.unsubscribe_all()
    """

    def __init__(self, ib: IB) -> None:
        """
        Initialise market data manager.

        Args:
            ib: A connected IB instance (from IBKRConnection.ib)

        Raises:
            IBKRConnectionError: If the IB instance is not connected
        """
        if not ib.isConnected():
            raise IBKRConnectionError("IB instance is not connected")

        self._ib = ib

        # symbol → Quote (most-recent snapshot)
        self._quotes: dict[str, Quote] = {}

        # symbol → ib_async Ticker (used for cancel)
        self._tickers: dict[str, Ticker] = {}

        # symbol → Contract
        self._contracts: dict[str, Contract] = {}

        # Push subscribers
        self._callbacks: list[QuoteCallback] = []

        # Wire up the single ib_async event we need
        self._ib.pendingTickersEvent += self._on_pending_tickers

        logger.info("MarketDataManager initialised")

    # ------------------------------------------------------------------
    # Public – subscription management
    # ------------------------------------------------------------------

    async def subscribe(
        self,
        symbol: str,
        exchange: str = "SMART",
        currency: str = "USD",
    ) -> None:
        """
        Subscribe to real-time L1 data for a stock symbol.

        Idempotent — subscribing to an already-subscribed symbol is a no-op.

        Args:
            symbol:   Ticker, e.g. "AAPL"
            exchange: IB exchange (default: SMART for best routing)
            currency: Currency code (default: USD)

        Raises:
            IBKRDataError: If the subscription request fails
        """
        if symbol in self._tickers:
            logger.debug(f"Already subscribed to {symbol}, ignoring duplicate")
            return

        try:
            if symbol in ("SPX", "NDX", "RUT", "DJI", "VIX"):
                contract = Index(symbol=symbol, exchange="CBOE" if symbol in ("SPX", "VIX") else exchange, currency=currency)
            else:
                contract = Stock(symbol=symbol, exchange=exchange, currency=currency)

            await self._ib.qualifyContractsAsync(contract)
            
            # Tick types: 233=RT Vol, 375=RT Trade Vol — empty string = default
            ticker = self._ib.reqMktData(contract, genericTickList="", snapshot=False, regulatorySnapshot=False)

            self._tickers[symbol] = ticker
            self._contracts[symbol] = contract
            # Initialise empty quote so callers don't have to handle KeyError
            self._quotes[symbol] = Quote(symbol=symbol)

            logger.info(f"Subscribed to market data: {symbol}")

        except Exception as exc:
            raise IBKRDataError(f"Failed to subscribe to {symbol}: {exc}") from exc

    def unsubscribe(self, symbol: str) -> None:
        """
        Cancel the real-time data subscription for a symbol.

        Args:
            symbol: Ticker to unsubscribe
        """
        if symbol not in self._tickers:
            logger.warning(f"Not subscribed to {symbol}, cannot unsubscribe")
            return

        try:
            self._ib.cancelMktData(self._contracts[symbol])
            del self._tickers[symbol]
            del self._contracts[symbol]
            logger.info(f"Unsubscribed from market data: {symbol}")
        except Exception as exc:
            logger.error(f"Error unsubscribing from {symbol}: {exc}")

    def unsubscribe_all(self) -> None:
        """Cancel all active market data subscriptions."""
        for symbol in list(self._tickers.keys()):
            self.unsubscribe(symbol)
        logger.info("All market data subscriptions cancelled")

    # ------------------------------------------------------------------
    # Public – data access
    # ------------------------------------------------------------------

    def get_quote(self, symbol: str) -> Optional[Quote]:
        """
        Return the latest Quote snapshot for a symbol (poll-based).

        Returns None if the symbol is not subscribed or no data received yet.

        Args:
            symbol: Ticker symbol

        Returns:
            Most recent Quote, or None
        """
        return self._quotes.get(symbol)

    def get_all_quotes(self) -> dict[str, Quote]:
        """Return a shallow copy of all current Quote snapshots."""
        return dict(self._quotes)

    @property
    def subscribed_symbols(self) -> list[str]:
        """List of all currently subscribed symbols."""
        return list(self._tickers.keys())

    # ------------------------------------------------------------------
    # Public – push callbacks
    # ------------------------------------------------------------------

    def subscribe_quotes(self, callback: QuoteCallback) -> None:
        """
        Register a callback that fires on every incoming tick.

        The callback receives a Quote object (the updated symbol's latest
        snapshot). All callbacks share the same ib_async event loop so
        keep them non-blocking.

        Args:
            callback: Callable[[Quote], None]
        """
        self._callbacks.append(callback)
        logger.debug(f"Quote callback registered ({len(self._callbacks)} total)")

    def unsubscribe_quotes(self, callback: QuoteCallback) -> None:
        """Remove a previously registered quote callback."""
        if callback in self._callbacks:
            self._callbacks.remove(callback)

    # ------------------------------------------------------------------
    # Private – ib_async event handler
    # ------------------------------------------------------------------

    def _on_pending_tickers(self, tickers: set[Ticker]) -> None:
        """
        Called by ib_async whenever fresh tick data arrives.

        Iterates over the changed tickers, updates the in-memory Quote
        store, and fans out to registered callbacks.
        """
        for ticker in tickers:
            symbol = ticker.contract.symbol if ticker.contract else None
            if not symbol or symbol not in self._tickers:
                continue

            quote = self._build_quote(symbol, ticker)
            self._quotes[symbol] = quote

            # Fan out to all registered callbacks
            for cb in self._callbacks:
                try:
                    cb(quote)
                except Exception as exc:
                    logger.error(f"Quote callback error for {symbol}: {exc}")

    @staticmethod
    def _build_quote(symbol: str, ticker: Ticker) -> Quote:
        """
        Construct a Quote from an ib_async Ticker object.

        ib_async uses sentinel values (often nan or 0) for missing fields;
        we pass them through as-is and let Quote.is_tradeable guard callers.
        """
        def _safe(val) -> float:
            """Return val if float-like and positive, else nan."""
            try:
                f = float(val)
                return f if f > 0 else float("nan")
            except (TypeError, ValueError):
                return float("nan")

        return Quote(
            symbol=symbol,
            bid=_safe(ticker.bid),
            ask=_safe(ticker.ask),
            last=_safe(ticker.last),
            bid_size=_safe(ticker.bidSize),
            ask_size=_safe(ticker.askSize),
            volume=_safe(ticker.volume),
            timestamp=datetime.now(),
        )

    # ------------------------------------------------------------------
    # Stats / repr
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return f"MarketDataManager(subscriptions={len(self._tickers)})"
