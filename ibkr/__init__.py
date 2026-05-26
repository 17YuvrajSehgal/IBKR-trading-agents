"""
Interactive Brokers TWS API Integration Package.

This package provides a production-ready interface to Interactive Brokers
Trader Workstation (TWS) and IB Gateway for algorithmic trading.

Features:
- Async connection management with auto-reconnect
- Order execution and tracking
- Position and account monitoring
- Real-time market data
- Comprehensive error handling and logging

Import notes:
    Broker-dependent modules (connection, trading, positions, market_data) require
    ib_async to be installed.  Pure-logic modules (config, exceptions, risk, utils)
    do NOT require ib_async and can be imported for unit testing without a broker.

    The __init__.py does NOT eagerly import broker modules so that:
      - ibkr.config, ibkr.exceptions, ibkr.risk can be used standalone
      - Unit tests work without ib_async installed

    To use the broker modules, import them directly:
        from ibkr.connection import IBKRConnection
        from ibkr.trading import OrderManager
        from ibkr.market_data import MarketDataManager
        from ibkr.news import NewsManager

Example:
    >>> from ibkr.config import IBKRConfig
    >>> from ibkr.connection import IBKRConnection
    >>>
    >>> config = IBKRConfig.paper_trading()
    >>> async with IBKRConnection(config) as conn:
    ...     account = await conn.get_account_summary()
    ...     print(f"Account: {account}")
"""

# ── Always-safe imports (no ib_async dependency) ──────────────────────────
from ibkr.config import IBKRConfig, TradingMode
from ibkr.error_codes import (
    Category as IBKRMessageCategory,
    MessageInfo,
    Severity as IBKRMessageSeverity,
    classify as classify_ibkr_message,
)
from ibkr.exceptions import (
    IBKRException,
    IBKRConnectionError,
    IBKRAuthenticationError,
    IBKRTimeoutError,
    IBKROrderError,
    IBKRDataError,
    IBKRInsufficientFundsError,
    IBKRInvalidOrderError,
)
from ibkr.recorder import NULL_RECORDER, SessionRecorder

__version__ = "0.2.0"

__all__ = [
    # Configuration (no ib_async dependency)
    "IBKRConfig",
    "TradingMode",
    # Exceptions (no ib_async dependency)
    "IBKRException",
    "IBKRConnectionError",
    "IBKRAuthenticationError",
    "IBKRTimeoutError",
    "IBKROrderError",
    "IBKRDataError",
    "IBKRInsufficientFundsError",
    "IBKRInvalidOrderError",
    # Error classification (no ib_async dependency)
    "IBKRMessageSeverity",
    "IBKRMessageCategory",
    "MessageInfo",
    "classify_ibkr_message",
    # Session recording (no ib_async dependency)
    "SessionRecorder",
    "NULL_RECORDER",
    # Broker modules — import directly when needed:
    #   from ibkr.connection import IBKRConnection
    #   from ibkr.trading import OrderManager, OrderInfo, OrderAction, OrderType, OrderStatus
    #   from ibkr.positions import PositionManager, Position, PortfolioSummary
    #   from ibkr.market_data import MarketDataManager, Quote
    #   from ibkr.news import NewsManager, NewsHeadline, NewsArticle
    #   from ibkr.risk import RiskManager, RiskLimits
]

