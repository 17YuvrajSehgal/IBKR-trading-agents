"""
Position management and tracking for Interactive Brokers.

This module provides functionality for tracking positions, calculating PnL,
and monitoring portfolio changes in real-time.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Callable, Optional

from ib_async import IB, Position as IBPosition, PortfolioItem

from ibkr.exceptions import IBKRConnectionError


# Configure module logger
logger = logging.getLogger(__name__)


@dataclass
class Position:
    """
    Position information data structure.
    
    Represents a trading position with all relevant details including
    quantity, cost basis, market value, and PnL.
    
    Attributes:
        symbol: Stock ticker symbol
        quantity: Number of shares (positive=long, negative=short)
        avg_cost: Average cost per share
        market_price: Current market price
        market_value: Current market value (quantity * market_price)
        unrealized_pnl: Unrealized profit/loss
        realized_pnl: Realized profit/loss
        account: Account holding the position
    """
    
    symbol: str
    quantity: int
    avg_cost: float
    market_price: float
    market_value: float
    unrealized_pnl: float
    realized_pnl: float
    account: str
    
    def is_long(self) -> bool:
        """Check if position is long."""
        return self.quantity > 0
    
    def is_short(self) -> bool:
        """Check if position is short."""
        return self.quantity < 0
    
    def is_flat(self) -> bool:
        """Check if position is flat (no position)."""
        return self.quantity == 0
    
    def __repr__(self) -> str:
        """Return string representation."""
        direction = "LONG" if self.is_long() else "SHORT" if self.is_short() else "FLAT"
        return (
            f"Position("
            f"{self.symbol} {direction} {abs(self.quantity)}, "
            f"PnL=${self.unrealized_pnl:.2f})"
        )


@dataclass
class PortfolioSummary:
    """
    Portfolio summary data structure.
    
    Attributes:
        total_positions: Number of open positions
        total_market_value: Total market value of all positions
        total_unrealized_pnl: Total unrealized PnL
        total_realized_pnl: Total realized PnL
        long_positions: Number of long positions
        short_positions: Number of short positions
    """
    
    total_positions: int
    total_market_value: float
    total_unrealized_pnl: float
    total_realized_pnl: float
    long_positions: int
    short_positions: int
    
    def __repr__(self) -> str:
        """Return string representation."""
        return (
            f"PortfolioSummary("
            f"positions={self.total_positions}, "
            f"value=${self.total_market_value:.2f}, "
            f"PnL=${self.total_unrealized_pnl:.2f})"
        )


class PositionManager:
    """
    Position tracking and management for IBKR trading.
    
    This class provides methods for retrieving and monitoring positions,
    calculating PnL, and subscribing to real-time position updates.
    
    Attributes:
        ib: IB connection instance
        positions: Dictionary of symbol -> Position
        position_callbacks: List of callbacks for position updates
    
    Example:
        >>> from ibkr import IBKRConnection, IBKRConfig
        >>> from ibkr.positions import PositionManager
        >>> 
        >>> config = IBKRConfig.paper_trading()
        >>> async with IBKRConnection(config) as conn:
        ...     pos_mgr = PositionManager(conn.ib)
        ...     
        ...     # Get all positions
        ...     positions = await pos_mgr.get_positions()
        ...     for pos in positions:
        ...         print(f"{pos.symbol}: {pos.quantity} @ ${pos.avg_cost:.2f}")
        ...     
        ...     # Get specific position
        ...     aapl_pos = await pos_mgr.get_position("AAPL")
        ...     if aapl_pos:
        ...         print(f"AAPL PnL: ${aapl_pos.unrealized_pnl:.2f}")
    """
    
    def __init__(self, ib: IB) -> None:
        """
        Initialize position manager.
        
        Args:
            ib: Connected IB instance
        
        Raises:
            IBKRConnectionError: If IB is not connected
        """
        if not ib.isConnected():
            raise IBKRConnectionError("IB instance is not connected")
        
        self.ib = ib
        self.positions: dict[str, Position] = {}
        self.position_callbacks: list[Callable[[Position], None]] = []
        
        # Setup event handlers
        self._setup_event_handlers()
        
        logger.info("PositionManager initialized")
    
    def _setup_event_handlers(self) -> None:
        """Setup event handlers for position updates."""
        self.ib.positionEvent += self._on_position_update
    
    def _on_position_update(self, position: IBPosition) -> None:
        """
        Handle position updates.
        
        Args:
            position: IB Position object
        """
        symbol = position.contract.symbol
        
        # Create Position object
        pos = Position(
            symbol=symbol,
            quantity=int(position.position),
            avg_cost=position.avgCost,
            market_price=position.avgCost,  # Position object doesn't have marketPrice
            market_value=position.position * position.avgCost,
            unrealized_pnl=0.0,  # Will be updated by portfolio updates
            realized_pnl=0.0,
            account=position.account,
        )
        
        # Update positions dictionary
        if pos.is_flat():
            # Remove flat positions
            if symbol in self.positions:
                del self.positions[symbol]
                logger.info(f"Position closed: {symbol}")
        else:
            self.positions[symbol] = pos
            logger.info(
                f"Position update: {symbol} {pos.quantity} @ ${pos.avg_cost:.2f}, "
                f"PnL=${pos.unrealized_pnl:.2f}"
            )
        
        # Notify callbacks
        for callback in self.position_callbacks:
            try:
                callback(pos)
            except Exception as e:
                logger.error(f"Error in position callback: {e}")
    
    async def get_positions(self) -> list[Position]:
        """
        Get all current positions.

        Uses reqPositionsAsync() — a clean one-shot request that returns
        symbol/quantity/avgCost across all accounts.

        Live market_price and unrealized_pnl require a streaming
        accountUpdates subscription. If you need those, also call
        subscribe_market_updates() (which uses portfolio() under the hood).

        Returns:
            List of Position objects (market_price/unrealized_pnl reflect
            the most recent portfolio() snapshot if available, else fall
            back to avg_cost / 0.0).

        Example:
            >>> positions = await pos_mgr.get_positions()
            >>> for pos in positions:
            ...     print(f"{pos.symbol}: {pos.quantity} shares")
        """
        ib_positions = await self.ib.reqPositionsAsync()

        # Build a side index of any portfolio() items we may have from a
        # prior subscribe_market_updates() call, keyed by (account, conId).
        portfolio_index: dict[tuple[str, int], PortfolioItem] = {}
        for item in self.ib.portfolio():
            portfolio_index[(item.account, item.contract.conId)] = item

        positions: list[Position] = []
        for ib_pos in ib_positions:
            if ib_pos.position == 0:
                continue

            symbol = ib_pos.contract.symbol
            key = (ib_pos.account, ib_pos.contract.conId)
            pitem = portfolio_index.get(key)

            if pitem is not None:
                market_price = pitem.marketPrice
                market_value = pitem.marketValue
                unrealized_pnl = pitem.unrealizedPNL
                realized_pnl = pitem.realizedPNL
            else:
                market_price = ib_pos.avgCost
                market_value = ib_pos.position * ib_pos.avgCost
                unrealized_pnl = 0.0
                realized_pnl = 0.0

            pos = Position(
                symbol=symbol,
                quantity=int(ib_pos.position),
                avg_cost=ib_pos.avgCost,
                market_price=market_price,
                market_value=market_value,
                unrealized_pnl=unrealized_pnl,
                realized_pnl=realized_pnl,
                account=ib_pos.account,
            )
            positions.append(pos)
            self.positions[symbol] = pos

        logger.info(f"Retrieved {len(positions)} positions")
        return positions

    async def subscribe_market_updates(self, account: str = "") -> None:
        """
        Enable streaming portfolio updates so subsequent get_positions()
        calls populate market_price, market_value, unrealized_pnl, and
        realized_pnl with live values.

        This is a streaming subscription — call once after connection.

        Args:
            account: Account code (default: first managed account)
        """
        if getattr(self, "_account_updates_subscribed", False):
            return
        # accountSummaryAsync is awaitable and populates portfolio() cache
        # via the same managedAccounts + portfolioValue path.
        await self.ib.reqAccountSummaryAsync()
        self._account_updates_subscribed = True
        logger.info("Account summary subscription active for live portfolio data")
    
    async def get_position(self, symbol: str) -> Optional[Position]:
        """
        Get position for a specific symbol.
        
        Args:
            symbol: Stock ticker symbol
        
        Returns:
            Position object if exists, None otherwise
        
        Example:
            >>> pos = await pos_mgr.get_position("AAPL")
            >>> if pos:
            ...     print(f"AAPL: {pos.quantity} shares")
            ... else:
            ...     print("No AAPL position")
        """
        # Refresh positions
        await self.get_positions()
        
        return self.positions.get(symbol)
    
    async def get_portfolio_summary(self) -> PortfolioSummary:
        """
        Get portfolio summary with aggregated metrics.
        
        Returns:
            PortfolioSummary with aggregated data
        
        Example:
            >>> summary = await pos_mgr.get_portfolio_summary()
            >>> print(f"Total positions: {summary.total_positions}")
            >>> print(f"Total PnL: ${summary.total_unrealized_pnl:.2f}")
        """
        # Get all positions
        positions = await self.get_positions()
        
        # Calculate aggregates
        total_market_value = sum(pos.market_value for pos in positions)
        total_unrealized_pnl = sum(pos.unrealized_pnl for pos in positions)
        total_realized_pnl = sum(pos.realized_pnl for pos in positions)
        long_positions = sum(1 for pos in positions if pos.is_long())
        short_positions = sum(1 for pos in positions if pos.is_short())
        
        summary = PortfolioSummary(
            total_positions=len(positions),
            total_market_value=total_market_value,
            total_unrealized_pnl=total_unrealized_pnl,
            total_realized_pnl=total_realized_pnl,
            long_positions=long_positions,
            short_positions=short_positions,
        )
        
        logger.info(f"Portfolio summary: {summary}")
        return summary
    
    def subscribe_position_updates(
        self,
        callback: Callable[[Position], None]
    ) -> None:
        """
        Subscribe to real-time position updates.
        
        Args:
            callback: Function to call on position updates
        
        Example:
            >>> def on_position_update(pos: Position):
            ...     print(f"{pos.symbol}: PnL=${pos.unrealized_pnl:.2f}")
            >>> 
            >>> pos_mgr.subscribe_position_updates(on_position_update)
        """
        self.position_callbacks.append(callback)
        logger.info("Position update callback registered")
    
    def __repr__(self) -> str:
        """Return string representation."""
        return (
            f"PositionManager("
            f"positions={len(self.positions)})"
        )
