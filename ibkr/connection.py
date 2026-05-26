"""
Connection management for Interactive Brokers TWS/Gateway.

This module provides the main connection class for interacting with
Interactive Brokers TWS or IB Gateway using the ib_async library.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Optional

from ib_async import IB, util

from ibkr.config import IBKRConfig, TradingMode
from ibkr.error_codes import Category, Severity, classify
from ibkr.exceptions import (
    IBKRConnectionError,
    IBKRTimeoutError,
    IBKRAuthenticationError,
)


# Configure module logger
logger = logging.getLogger(__name__)


class IBKRConnection:
    """
    Production-ready connection manager for Interactive Brokers TWS/Gateway.
    
    This class provides a robust, async interface to IBKR with:
    - Automatic connection management
    - Health monitoring and auto-reconnect
    - Comprehensive error handling
    - Context manager support
    - Detailed logging
    
    The connection uses the ib_async library which provides a clean async/await
    interface to the Interactive Brokers API.
    
    Attributes:
        config: IBKR configuration
        ib: Underlying ib_async IB instance
        is_connected: Connection status
        connection_time: Timestamp of last successful connection
        reconnect_count: Number of reconnection attempts
    
    Example:
        >>> # Basic usage with context manager
        >>> config = IBKRConfig.paper_trading()
        >>> async with IBKRConnection(config) as conn:
        ...     account = await conn.get_account_summary()
        ...     print(f"Connected to account: {account}")
        
        >>> # Manual connection management
        >>> conn = IBKRConnection(config)
        >>> await conn.connect()
        >>> # ... do work ...
        >>> await conn.disconnect()
    """
    
    def __init__(self, config: IBKRConfig) -> None:
        """
        Initialize IBKR connection manager.

        Args:
            config: IBKR configuration

        Raises:
            ValueError: If configuration is invalid
        """
        self.config = config
        self.ib = IB()
        self.is_connected = False
        self.connection_time: Optional[datetime] = None
        self.reconnect_count = 0

        # Callbacks fired AFTER auto-reconnect succeeds. Callers (e.g. strategy
        # agents) register here to re-subscribe market/historical data that
        # was dropped by the server reset.
        self._on_reconnect_callbacks: list = []

        # Configure logging
        self._setup_logging()

        # Setup event handlers
        self._setup_event_handlers()
        
        logger.info(
            f"Initialized IBKR connection manager "
            f"(mode={config.trading_mode}, port={config.port}, "
            f"client_id={config.client_id})"
        )
    
    def _setup_logging(self) -> None:
        """Configure logging based on config settings."""
        log_level = getattr(logging, self.config.log_level)
        logger.setLevel(log_level)
        
        # Configure ib_async logging
        util.logToConsole(log_level)
        
        if self.config.log_to_file:
            handler = logging.FileHandler(self.config.log_file_path)
            handler.setLevel(log_level)
            formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            )
            handler.setFormatter(formatter)
            logger.addHandler(handler)
            logger.info(f"Logging to file: {self.config.log_file_path}")
    
    def _setup_event_handlers(self) -> None:
        """Setup event handlers for connection events."""
        # Connection events
        self.ib.connectedEvent += self._on_connected
        self.ib.disconnectedEvent += self._on_disconnected
        
        # Error events
        self.ib.errorEvent += self._on_error
        
        # Timeout events
        self.ib.timeoutEvent += self._on_timeout
    
    def _on_connected(self) -> None:
        """Handle connection established event."""
        self.is_connected = True
        self.connection_time = datetime.now()
        logger.info(
            f"Connected to TWS/Gateway at {self.config.host}:{self.config.port} "
            f"(client_id={self.config.client_id})"
        )
    
    def _on_disconnected(self) -> None:
        """Handle disconnection event."""
        was_connected = self.is_connected
        self.is_connected = False
        
        if was_connected:
            logger.warning("Disconnected from TWS/Gateway")
            
            # Attempt auto-reconnect if enabled
            if self.config.auto_reconnect:
                asyncio.create_task(self._auto_reconnect())
    
    def _on_error(self, reqId: int, errorCode: int, errorString: str, contract: Any) -> None:
        """
        Handle error/info events from TWS/Gateway.

        Routes each message through `ibkr.error_codes.classify` so:
          * Data-farm heartbeats (2104/2106/2158/...) log at DEBUG — not WARNING.
          * Connection lifecycle (1100/1101/1102) logs at WARNING and triggers
            on_reconnect callbacks when connectivity is restored with data
            loss (1101) — callers must re-subscribe.
          * Real errors and fatals log loudly.
          * Unknown codes log at WARNING so they're visible without crashing.
        """
        info = classify(errorCode)

        level_map = {
            Severity.INFO: logging.INFO,
            Severity.WARNING: logging.WARNING,
            Severity.ERROR: logging.ERROR,
            Severity.FATAL: logging.CRITICAL,
        }
        level = level_map[info.severity]

        # Data farm pings come in a constant stream during the daily reset.
        # Demote those to DEBUG so the operator log isn't drowned by them.
        if info.category is Category.DATA_FARM and info.auto_recovers:
            level = logging.DEBUG

        logger.log(
            level,
            f"[{info.severity.value}][{info.category.value}] "
            f"{errorCode}: {errorString} (reqId={reqId})"
        )

        # Connectivity restored WITH data loss → ask subscribers to resubscribe
        if errorCode == 1101:
            self._fire_on_reconnect("connection-restored-data-lost")
        elif errorCode == 1102:
            # Restored without data loss — nothing to do, but record it
            logger.info("Connectivity restored without data loss")

    # ------------------------------------------------------------------
    # Reconnect-callback API (for strategy agents)
    # ------------------------------------------------------------------

    def on_reconnect(self, callback) -> None:
        """
        Register a callback to be invoked after auto-reconnect succeeds, so
        the strategy can re-subscribe market or historical data that was
        dropped during the disconnection.

        Callback signature:  callback(reason: str) -> None | Awaitable[None]

        Sync callbacks run inline; async callbacks are scheduled on the loop.
        """
        if callback not in self._on_reconnect_callbacks:
            self._on_reconnect_callbacks.append(callback)

    def _fire_on_reconnect(self, reason: str) -> None:
        """Invoke all registered on_reconnect callbacks, never raising."""
        for cb in list(self._on_reconnect_callbacks):
            try:
                result = cb(reason)
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)
            except Exception as e:
                logger.error(f"on_reconnect callback {cb!r} raised: {e}")
    
    def _on_timeout(self, idlePeriod: float) -> None:
        """
        Handle timeout events.
        
        Args:
            idlePeriod: Idle period in seconds
        """
        logger.warning(f"Connection timeout after {idlePeriod:.1f}s idle")
    
    async def connect(self) -> None:
        """
        Establish connection to TWS/Gateway.
        
        This method connects to the Interactive Brokers TWS or IB Gateway
        using the configured settings. It includes timeout handling and
        comprehensive error reporting.
        
        Raises:
            IBKRConnectionError: If connection fails
            IBKRTimeoutError: If connection times out
            IBKRAuthenticationError: If authentication fails
        
        Example:
            >>> conn = IBKRConnection(config)
            >>> await conn.connect()
        """
        if self.is_connected:
            logger.warning("Already connected to TWS/Gateway")
            return
        
        logger.info(
            f"Connecting to TWS/Gateway at {self.config.host}:{self.config.port} "
            f"(client_id={self.config.client_id}, timeout={self.config.timeout}s)"
        )
        
        try:
            # connectAsync handles its own timeout internally
            await self.ib.connectAsync(
                host=self.config.host,
                port=self.config.port,
                clientId=self.config.client_id,
                timeout=self.config.timeout,
                readonly=self.config.readonly,
                account=self.config.account or '',
            )
            
            # Verify connection
            if not self.ib.isConnected():
                raise IBKRConnectionError(
                    "Connection established but verification failed"
                )
            
            # Log connection details
            logger.info("Successfully connected to TWS/Gateway")
            
            # Log trading mode warning
            if self.config.trading_mode == TradingMode.LIVE:
                logger.warning(
                    "⚠️  LIVE TRADING MODE - Real money at risk! ⚠️"
                )
            else:
                logger.info("Paper trading mode - Safe for testing")
            
            if self.config.readonly:
                logger.info("Read-only mode - Order placement disabled")
            
        except asyncio.TimeoutError as e:
            error_msg = (
                f"Connection timeout after {self.config.timeout}s. "
                f"Please verify TWS/Gateway is running on port {self.config.port}"
            )
            logger.error(error_msg)
            raise IBKRTimeoutError(error_msg) from e
        
        except ConnectionRefusedError as e:
            error_msg = (
                f"Connection refused to {self.config.host}:{self.config.port}. "
                f"Please verify TWS/Gateway is running and API connections are enabled."
            )
            logger.error(error_msg)
            raise IBKRConnectionError(error_msg) from e
        
        except Exception as e:
            error_msg = f"Failed to connect to TWS/Gateway: {e}"
            logger.error(error_msg)
            raise IBKRConnectionError(error_msg) from e
    
    async def disconnect(self) -> None:
        """
        Disconnect from TWS/Gateway.
        
        This method gracefully closes the connection to TWS/Gateway,
        ensuring all pending operations are completed.
        
        Example:
            >>> await conn.disconnect()
        """
        if not self.is_connected:
            logger.warning("Not connected to TWS/Gateway")
            return
        
        logger.info("Disconnecting from TWS/Gateway...")
        
        try:
            self.ib.disconnect()
            self.is_connected = False
            logger.info("Successfully disconnected from TWS/Gateway")
        
        except Exception as e:
            logger.error(f"Error during disconnect: {e}")
            raise IBKRConnectionError(f"Disconnect failed: {e}") from e
    
    async def _auto_reconnect(self) -> None:
        """
        Automatically attempt to reconnect after disconnection.
        
        This method implements exponential backoff for reconnection attempts.
        """
        if not self.config.auto_reconnect:
            return
        
        if self.reconnect_count >= self.config.max_reconnect_attempts:
            logger.error(
                f"Max reconnection attempts ({self.config.max_reconnect_attempts}) "
                f"reached. Giving up."
            )
            return
        
        self.reconnect_count += 1
        delay = self.config.reconnect_delay * (
            self.config.reconnect_backoff ** (self.reconnect_count - 1)
        )
        
        logger.info(
            f"Attempting reconnection {self.reconnect_count}/"
            f"{self.config.max_reconnect_attempts} in {delay:.1f}s..."
        )
        
        await asyncio.sleep(delay)
        
        try:
            await self.connect()
            self.reconnect_count = 0  # Reset on successful reconnection
            logger.info("Reconnection successful — notifying subscribers")
            # Tell strategy callers to re-subscribe their data streams
            self._fire_on_reconnect("auto-reconnect")

        except Exception as e:
            logger.error(f"Reconnection attempt {self.reconnect_count} failed: {e}")
            # Will retry on next disconnect event if attempts remain
    
    async def check_health(self) -> dict[str, Any]:
        """
        Check connection health and return status information.
        
        Returns:
            Dictionary with health status information
        
        Example:
            >>> health = await conn.check_health()
            >>> print(f"Connected: {health['is_connected']}")
            >>> print(f"Latency: {health['latency_ms']}ms")
        """
        health = {
            "is_connected": self.is_connected,
            "connection_time": self.connection_time,
            "reconnect_count": self.reconnect_count,
            "config": {
                "host": self.config.host,
                "port": self.config.port,
                "client_id": self.config.client_id,
                "trading_mode": self.config.trading_mode.value,
                "readonly": self.config.readonly,
            },
        }
        
        if self.is_connected:
            try:
                # Measure latency with a simple request
                start = datetime.now()
                await self.ib.reqCurrentTimeAsync()
                latency = (datetime.now() - start).total_seconds() * 1000
                
                health["latency_ms"] = round(latency, 2)
            
            except Exception as e:
                health["error"] = str(e)
                logger.warning(f"Health check failed: {e}")
        
        return health
    
    async def get_account_summary(self) -> dict[str, Any]:
        """
        Get account summary information.
        
        Returns:
            Dictionary with account information
        
        Raises:
            IBKRConnectionError: If not connected
        
        Example:
            >>> account = await conn.get_account_summary()
            >>> print(f"Net Liquidation: ${account['NetLiquidation']}")
        """
        if not self.is_connected:
            raise IBKRConnectionError("Not connected to TWS/Gateway")
        
        try:
            # Request account summary
            account_values = await self.ib.accountSummaryAsync()
            
            # Convert to dictionary
            summary = {}
            for av in account_values:
                summary[av.tag] = {
                    "value": av.value,
                    "currency": av.currency,
                    "account": av.account,
                }
            
            return summary
        
        except Exception as e:
            logger.error(f"Failed to get account summary: {e}")
            raise IBKRConnectionError(f"Account summary failed: {e}") from e
    
    async def __aenter__(self) -> IBKRConnection:
        """
        Async context manager entry.
        
        Example:
            >>> async with IBKRConnection(config) as conn:
            ...     # Use connection
            ...     pass
        """
        await self.connect()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        await self.disconnect()
    
    def __repr__(self) -> str:
        """Return string representation."""
        status = "connected" if self.is_connected else "disconnected"
        return (
            f"IBKRConnection("
            f"host={self.config.host}, "
            f"port={self.config.port}, "
            f"client_id={self.config.client_id}, "
            f"status={status})"
        )
