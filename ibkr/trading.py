"""
Order execution and trading operations for Interactive Brokers.

This module provides comprehensive order management functionality including
order placement, status tracking, and cancellation for various order types.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Callable, Optional

from ib_async import IB, Order as IBOrder, Trade, Contract, Stock, LimitOrder, MarketOrder, StopOrder

from ibkr.exceptions import IBKROrderError, IBKRConnectionError
from ibkr.utils import create_stock_contract


# Configure module logger
logger = logging.getLogger(__name__)


class OrderAction(str, Enum):
    """Order action enumeration."""
    BUY = "BUY"
    SELL = "SELL"
    
    def __str__(self) -> str:
        return self.value


class OrderType(str, Enum):
    """Order type enumeration."""
    MARKET = "MKT"
    LIMIT = "LMT"
    STOP = "STP"
    STOP_LIMIT = "STP LMT"
    
    def __str__(self) -> str:
        return self.value


class OrderStatus(str, Enum):
    """Order status enumeration."""
    PENDING_SUBMIT = "PendingSubmit"
    PENDING_CANCEL = "PendingCancel"
    PRE_SUBMITTED = "PreSubmitted"
    SUBMITTED = "Submitted"
    FILLED = "Filled"
    CANCELLED = "Cancelled"
    INACTIVE = "Inactive"
    UNKNOWN = "Unknown"
    
    def __str__(self) -> str:
        return self.value


@dataclass
class OrderInfo:
    """
    Order information data structure.
    
    Represents a trading order with all relevant details including
    status, fill information, and pricing.
    
    Attributes:
        order_id: Unique order identifier
        symbol: Stock ticker symbol
        action: BUY or SELL
        quantity: Number of shares
        order_type: Order type (MARKET, LIMIT, STOP, etc.)
        status: Current order status
        filled_quantity: Number of shares filled
        remaining_quantity: Number of shares remaining
        avg_fill_price: Average fill price
        limit_price: Limit price (for limit orders)
        stop_price: Stop price (for stop orders)
        timestamp: Order creation timestamp
        commission: Commission paid
        pnl: Realized PnL for this trade
    """
    
    order_id: int
    symbol: str
    action: OrderAction
    quantity: int
    order_type: OrderType
    status: OrderStatus
    filled_quantity: int = 0
    remaining_quantity: int = 0
    avg_fill_price: float = 0.0
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    timestamp: datetime = field(default_factory=datetime.now)
    commission: float = 0.0
    pnl: float = 0.0
    
    def is_filled(self) -> bool:
        """Check if order is completely filled."""
        return self.status == OrderStatus.FILLED
    
    def is_active(self) -> bool:
        """Check if order is active (not filled or cancelled)."""
        return self.status in [
            OrderStatus.PENDING_SUBMIT,
            OrderStatus.PRE_SUBMITTED,
            OrderStatus.SUBMITTED,
        ]
    
    def is_cancelled(self) -> bool:
        """Check if order is cancelled."""
        return self.status == OrderStatus.CANCELLED
    
    def __repr__(self) -> str:
        """Return string representation."""
        return (
            f"OrderInfo("
            f"id={self.order_id}, "
            f"symbol={self.symbol}, "
            f"{self.action} {self.quantity}, "
            f"type={self.order_type}, "
            f"status={self.status})"
        )


class OrderManager:
    """
    Order execution and management for IBKR trading.
    
    This class provides methods for placing, tracking, and cancelling orders
    through Interactive Brokers. It supports various order types and provides
    real-time order status updates.
    
    Attributes:
        ib: IB connection instance
        orders: Dictionary of order_id -> OrderInfo
        trades: Dictionary of order_id -> Trade
        order_callbacks: List of callbacks for order updates
    
    Example:
        >>> from ibkr import IBKRConnection, IBKRConfig
        >>> from ibkr.trading import OrderManager
        >>> 
        >>> config = IBKRConfig.paper_trading()
        >>> async with IBKRConnection(config) as conn:
        ...     order_mgr = OrderManager(conn.ib)
        ...     
        ...     # Place market order
        ...     order = await order_mgr.place_market_order("AAPL", 100, "BUY")
        ...     print(f"Order placed: {order.order_id}")
        ...     
        ...     # Check status
        ...     status = await order_mgr.get_order_status(order.order_id)
        ...     print(f"Status: {status}")
    """
    
    def __init__(self, ib: IB) -> None:
        """
        Initialize order manager.
        
        Args:
            ib: Connected IB instance
        
        Raises:
            IBKRConnectionError: If IB is not connected
        """
        if not ib.isConnected():
            raise IBKRConnectionError("IB instance is not connected")
        
        self.ib = ib
        self.orders: dict[int, OrderInfo] = {}
        self.trades: dict[int, Trade] = {}
        self.order_callbacks: list[Callable[[OrderInfo], None]] = []
        
        # Setup event handlers
        self._setup_event_handlers()
        
        logger.info("OrderManager initialized")
    
    def _setup_event_handlers(self) -> None:
        """Setup event handlers for order updates."""
        self.ib.orderStatusEvent += self._on_order_status
        self.ib.execDetailsEvent += self._on_exec_details
        self.ib.commissionReportEvent += self._on_commission_report
    
    def _on_order_status(self, trade: Trade) -> None:
        """
        Handle order status updates.
        
        Args:
            trade: Trade object with order status
        """
        order_id = trade.order.orderId
        
        # Update or create order info
        if order_id in self.orders:
            order_info = self.orders[order_id]
            order_info.status = OrderStatus(trade.orderStatus.status)
            order_info.filled_quantity = int(trade.orderStatus.filled)
            order_info.remaining_quantity = int(trade.orderStatus.remaining)
            order_info.avg_fill_price = trade.orderStatus.avgFillPrice
        else:
            # Create new order info from trade
            order_info = self._create_order_info_from_trade(trade)
            self.orders[order_id] = order_info
        
        # Store trade
        self.trades[order_id] = trade
        
        logger.info(
            f"Order {order_id} status update: {order_info.status} "
            f"(filled={order_info.filled_quantity}/{order_info.quantity})"
        )
        
        # Notify callbacks
        for callback in self.order_callbacks:
            try:
                callback(order_info)
            except Exception as e:
                logger.error(f"Error in order callback: {e}")
    
    def _on_exec_details(self, trade: Trade, fill) -> None:
        """
        Handle execution details.
        
        Args:
            trade: Trade object
            fill: Fill details
        """
        order_id = trade.order.orderId
        logger.info(
            f"Order {order_id} execution: "
            f"{fill.execution.shares} @ {fill.execution.price}"
        )
    
    def _on_commission_report(self, trade: Trade, fill, report) -> None:
        """
        Handle commission reports.
        
        Args:
            trade: Trade object
            fill: Fill details
            report: Commission report
        """
        order_id = trade.order.orderId
        if order_id in self.orders:
            self.orders[order_id].commission = report.commission
            self.orders[order_id].pnl = report.realizedPNL
            
            logger.info(
                f"Order {order_id} commission: ${report.commission:.2f}, "
                f"PnL: ${report.realizedPNL:.2f}"
            )
    
    def _create_order_info_from_trade(self, trade: Trade) -> OrderInfo:
        """
        Create OrderInfo from Trade object.
        
        Args:
            trade: Trade object
        
        Returns:
            OrderInfo instance
        """
        order = trade.order
        status = trade.orderStatus
        
        # Extract symbol from contract
        symbol = trade.contract.symbol if hasattr(trade.contract, 'symbol') else "UNKNOWN"
        
        # Determine order type
        order_type = OrderType.MARKET
        limit_price = None
        stop_price = None
        
        if hasattr(order, 'lmtPrice') and order.lmtPrice:
            order_type = OrderType.LIMIT
            limit_price = order.lmtPrice
        
        if hasattr(order, 'auxPrice') and order.auxPrice:
            if order_type == OrderType.LIMIT:
                order_type = OrderType.STOP_LIMIT
            else:
                order_type = OrderType.STOP
            stop_price = order.auxPrice
        
        return OrderInfo(
            order_id=order.orderId,
            symbol=symbol,
            action=OrderAction(order.action),
            quantity=int(order.totalQuantity),
            order_type=order_type,
            status=OrderStatus(status.status),
            filled_quantity=int(status.filled),
            remaining_quantity=int(status.remaining),
            avg_fill_price=status.avgFillPrice,
            limit_price=limit_price,
            stop_price=stop_price,
        )
    
    async def place_market_order(
        self,
        symbol: str,
        quantity: int,
        action: str | OrderAction,
        outside_rth: bool = False,
    ) -> OrderInfo:
        """
        Place a market order.
        
        Market orders execute immediately at the best available price.
        
        Args:
            symbol: Stock ticker symbol
            quantity: Number of shares (positive integer)
            action: "BUY" or "SELL"
            outside_rth: Allow trading outside regular trading hours (after-hours)
        
        Returns:
            OrderInfo with order details
        
        Raises:
            IBKROrderError: If order placement fails
        
        Example:
            >>> order = await order_mgr.place_market_order("AAPL", 100, "BUY")
            >>> print(f"Order ID: {order.order_id}")
            
            >>> # After-hours trading
            >>> order = await order_mgr.place_market_order("AAPL", 100, "BUY", outside_rth=True)
        """
        # Validate inputs
        if quantity <= 0:
            raise IBKROrderError(f"Quantity must be positive, got {quantity}")
        
        action = OrderAction(action.upper() if isinstance(action, str) else action)
        
        # Create contract
        contract = create_stock_contract(symbol)
        
        # Create market order
        order = MarketOrder(action.value, quantity)
        order.outsideRth = outside_rth  # Enable after-hours trading if requested
        
        logger.info(f"Placing market order: {action} {quantity} {symbol} (outsideRth={outside_rth})")
        
        try:
            # Place order
            trade = self.ib.placeOrder(contract, order)
            
            # Wait for order to be acknowledged
            await asyncio.sleep(0.1)  # Small delay for order acknowledgment
            
            # Create order info
            order_info = OrderInfo(
                order_id=order.orderId,
                symbol=symbol,
                action=action,
                quantity=quantity,
                order_type=OrderType.MARKET,
                status=OrderStatus.PENDING_SUBMIT,
            )
            
            self.orders[order.orderId] = order_info
            self.trades[order.orderId] = trade
            
            logger.info(f"Market order placed successfully: ID={order.orderId}")
            
            return order_info
        
        except Exception as e:
            error_msg = f"Failed to place market order: {e}"
            logger.error(error_msg)
            raise IBKROrderError(error_msg) from e
    
    async def place_limit_order(
        self,
        symbol: str,
        quantity: int,
        action: str | OrderAction,
        limit_price: float,
        outside_rth: bool = False,
    ) -> OrderInfo:
        """
        Place a limit order.
        
        Limit orders execute only at the specified price or better.
        
        Args:
            symbol: Stock ticker symbol
            quantity: Number of shares (positive integer)
            action: "BUY" or "SELL"
            limit_price: Maximum price for BUY, minimum price for SELL
            outside_rth: Allow trading outside regular trading hours
        
        Returns:
            OrderInfo with order details
        
        Raises:
            IBKROrderError: If order placement fails
        
        Example:
            >>> order = await order_mgr.place_limit_order("AAPL", 100, "BUY", 150.00)
            >>> print(f"Limit order placed at ${order.limit_price}")
        """
        # Validate inputs
        if quantity <= 0:
            raise IBKROrderError(f"Quantity must be positive, got {quantity}")
        
        if limit_price <= 0:
            raise IBKROrderError(f"Limit price must be positive, got {limit_price}")
        
        action = OrderAction(action.upper() if isinstance(action, str) else action)
        
        # Create contract
        contract = create_stock_contract(symbol)
        
        # Create limit order
        order = LimitOrder(action.value, quantity, limit_price)
        order.outsideRth = outside_rth
        
        logger.info(
            f"Placing limit order: {action} {quantity} {symbol} @ ${limit_price:.2f}"
        )
        
        try:
            # Place order
            trade = self.ib.placeOrder(contract, order)
            
            # Wait for order to be acknowledged
            await asyncio.sleep(0.1)
            
            # Create order info
            order_info = OrderInfo(
                order_id=order.orderId,
                symbol=symbol,
                action=action,
                quantity=quantity,
                order_type=OrderType.LIMIT,
                status=OrderStatus.PENDING_SUBMIT,
                limit_price=limit_price,
            )
            
            self.orders[order.orderId] = order_info
            self.trades[order.orderId] = trade
            
            logger.info(f"Limit order placed successfully: ID={order.orderId}")
            
            return order_info
        
        except Exception as e:
            error_msg = f"Failed to place limit order: {e}"
            logger.error(error_msg)
            raise IBKROrderError(error_msg) from e
    
    async def place_stop_order(
        self,
        symbol: str,
        quantity: int,
        action: str | OrderAction,
        stop_price: float,
        outside_rth: bool = False,
    ) -> OrderInfo:
        """
        Place a stop order.
        
        Stop orders become market orders when the stop price is reached.
        
        Args:
            symbol: Stock ticker symbol
            quantity: Number of shares (positive integer)
            action: "BUY" or "SELL"
            stop_price: Trigger price for the order
            outside_rth: Allow trading outside regular trading hours
        
        Returns:
            OrderInfo with order details
        
        Raises:
            IBKROrderError: If order placement fails
        
        Example:
            >>> # Stop-loss at $145
            >>> order = await order_mgr.place_stop_order("AAPL", 100, "SELL", 145.00)
        """
        # Validate inputs
        if quantity <= 0:
            raise IBKROrderError(f"Quantity must be positive, got {quantity}")
        
        if stop_price <= 0:
            raise IBKROrderError(f"Stop price must be positive, got {stop_price}")
        
        action = OrderAction(action.upper() if isinstance(action, str) else action)
        
        # Create contract
        contract = create_stock_contract(symbol)
        
        # Create stop order
        order = StopOrder(action.value, quantity, stop_price)
        order.outsideRth = outside_rth
        
        logger.info(
            f"Placing stop order: {action} {quantity} {symbol} @ ${stop_price:.2f}"
        )
        
        try:
            # Place order
            trade = self.ib.placeOrder(contract, order)
            
            # Wait for order to be acknowledged
            await asyncio.sleep(0.1)
            
            # Create order info
            order_info = OrderInfo(
                order_id=order.orderId,
                symbol=symbol,
                action=action,
                quantity=quantity,
                order_type=OrderType.STOP,
                status=OrderStatus.PENDING_SUBMIT,
                stop_price=stop_price,
            )
            
            self.orders[order.orderId] = order_info
            self.trades[order.orderId] = trade
            
            logger.info(f"Stop order placed successfully: ID={order.orderId}")
            
            return order_info
        
        except Exception as e:
            error_msg = f"Failed to place stop order: {e}"
            logger.error(error_msg)
            raise IBKROrderError(error_msg) from e
    
    async def open_long_position(
        self,
        symbol: str,
        quantity: int,
    ) -> OrderInfo:
        """
        Open a long position (buy shares).
        
        Convenience method for opening a long position with a market order.
        
        Args:
            symbol: Stock ticker symbol
            quantity: Number of shares to buy
        
        Returns:
            OrderInfo with order details
        
        Example:
            >>> order = await order_mgr.open_long_position("AAPL", 100)
            >>> print(f"Opened long position: {order.order_id}")
        """
        return await self.place_market_order(symbol, quantity, OrderAction.BUY)
    
    async def close_long_position(
        self,
        symbol: str,
        quantity: int,
    ) -> OrderInfo:
        """
        Close a long position (sell shares).
        
        Convenience method for closing a long position with a market order.
        
        Args:
            symbol: Stock ticker symbol
            quantity: Number of shares to sell
        
        Returns:
            OrderInfo with order details
        
        Example:
            >>> order = await order_mgr.close_long_position("AAPL", 100)
            >>> print(f"Closed long position: {order.order_id}")
        """
        return await self.place_market_order(symbol, quantity, OrderAction.SELL)
    
    async def open_short_position(
        self,
        symbol: str,
        quantity: int,
    ) -> OrderInfo:
        """
        Open a short position (sell shares you don't own).
        
        Convenience method for opening a short position with a market order.
        This sells shares you don't currently own, betting the price will decrease.
        
        Args:
            symbol: Stock ticker symbol
            quantity: Number of shares to short
        
        Returns:
            OrderInfo with order details
        
        Example:
            >>> order = await order_mgr.open_short_position("AAPL", 100)
            >>> print(f"Opened short position: {order.order_id}")
        
        Note:
            Short selling requires margin account and may have restrictions.
            Check with your broker for short selling requirements.
        """
        return await self.place_market_order(symbol, quantity, OrderAction.SELL)
    
    async def close_short_position(
        self,
        symbol: str,
        quantity: int,
    ) -> OrderInfo:
        """
        Close a short position (buy back shares).
        
        Convenience method for closing a short position with a market order.
        This buys back shares to close your short position.
        
        Args:
            symbol: Stock ticker symbol
            quantity: Number of shares to buy back
        
        Returns:
            OrderInfo with order details
        
        Example:
            >>> order = await order_mgr.close_short_position("AAPL", 100)
            >>> print(f"Closed short position: {order.order_id}")
        """
        return await self.place_market_order(symbol, quantity, OrderAction.BUY)

    
    async def cancel_order(self, order_id: int) -> bool:
        """
        Cancel an order.
        
        Args:
            order_id: Order ID to cancel
        
        Returns:
            True if cancellation request was successful
        
        Raises:
            IBKROrderError: If order not found or cancellation fails
        
        Example:
            >>> success = await order_mgr.cancel_order(123)
            >>> if success:
            ...     print("Order cancelled")
        """
        if order_id not in self.trades:
            raise IBKROrderError(f"Order {order_id} not found")
        
        trade = self.trades[order_id]
        
        logger.info(f"Cancelling order {order_id}")
        
        try:
            self.ib.cancelOrder(trade.order)
            
            # Update status
            if order_id in self.orders:
                self.orders[order_id].status = OrderStatus.PENDING_CANCEL
            
            logger.info(f"Order {order_id} cancellation requested")
            return True
        
        except Exception as e:
            error_msg = f"Failed to cancel order {order_id}: {e}"
            logger.error(error_msg)
            raise IBKROrderError(error_msg) from e
    
    async def get_order_status(self, order_id: int) -> OrderStatus:
        """
        Get current status of an order.
        
        Args:
            order_id: Order ID
        
        Returns:
            Current order status
        
        Raises:
            IBKROrderError: If order not found
        
        Example:
            >>> status = await order_mgr.get_order_status(123)
            >>> if status == OrderStatus.FILLED:
            ...     print("Order filled!")
        """
        if order_id not in self.orders:
            raise IBKROrderError(f"Order {order_id} not found")
        
        return self.orders[order_id].status
    
    async def get_order(self, order_id: int) -> OrderInfo:
        """
        Get order information.
        
        Args:
            order_id: Order ID
        
        Returns:
            OrderInfo with complete order details
        
        Raises:
            IBKROrderError: If order not found
        """
        if order_id not in self.orders:
            raise IBKROrderError(f"Order {order_id} not found")
        
        return self.orders[order_id]
    
    async def get_open_orders(self) -> list[OrderInfo]:
        """
        Get all open (active) orders.
        
        Returns:
            List of active orders
        
        Example:
            >>> open_orders = await order_mgr.get_open_orders()
            >>> for order in open_orders:
            ...     print(f"{order.symbol}: {order.status}")
        """
        return [
            order for order in self.orders.values()
            if order.is_active()
        ]
    
    async def get_all_orders(self) -> list[OrderInfo]:
        """
        Get all orders (open and closed).
        
        Returns:
            List of all orders
        
        Example:
            >>> all_orders = await order_mgr.get_all_orders()
            >>> print(f"Total orders: {len(all_orders)}")
        """
        return list(self.orders.values())
    
    def subscribe_order_updates(self, callback: Callable[[OrderInfo], None]) -> None:
        """
        Subscribe to order status updates.
        
        Args:
            callback: Function to call on order updates
        
        Example:
            >>> def on_order_update(order: OrderInfo):
            ...     print(f"Order {order.order_id}: {order.status}")
            >>> 
            >>> order_mgr.subscribe_order_updates(on_order_update)
        """
        self.order_callbacks.append(callback)
        logger.info("Order update callback registered")
    
    def __repr__(self) -> str:
        """Return string representation."""
        return (
            f"OrderManager("
            f"orders={len(self.orders)}, "
            f"open={len([o for o in self.orders.values() if o.is_active()])})"
        )
