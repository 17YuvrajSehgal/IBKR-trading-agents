"""
Utility functions for IBKR operations.

This module provides helper functions for common IBKR operations
such as contract creation, order building, and data validation.
"""

from __future__ import annotations

from typing import Literal

from ib_async import Contract, Stock, Option, Future, Forex, Index


def create_stock_contract(
    symbol: str,
    exchange: str = "SMART",
    currency: str = "USD",
) -> Contract:
    """
    Create a stock contract.
    
    Args:
        symbol: Stock ticker symbol (e.g., "AAPL", "MSFT")
        exchange: Exchange (default: "SMART" for smart routing)
        currency: Currency (default: "USD")
    
    Returns:
        Stock contract
    
    Example:
        >>> contract = create_stock_contract("AAPL")
        >>> print(contract.symbol)
        AAPL
    """
    return Stock(symbol, exchange, currency)


def create_forex_contract(
    pair: str,
    exchange: str = "IDEALPRO",
) -> Contract:
    """
    Create a forex contract.
    
    Args:
        pair: Currency pair (e.g., "EURUSD", "GBPUSD")
        exchange: Exchange (default: "IDEALPRO")
    
    Returns:
        Forex contract
    
    Example:
        >>> contract = create_forex_contract("EURUSD")
        >>> print(contract.pair())
        EURUSD
    """
    return Forex(pair, exchange=exchange)


def create_option_contract(
    symbol: str,
    last_trade_date: str,
    strike: float,
    right: Literal["C", "P"],
    exchange: str = "SMART",
    currency: str = "USD",
) -> Contract:
    """
    Create an option contract.
    
    Args:
        symbol: Underlying symbol
        last_trade_date: Expiration date (YYYYMMDD format)
        strike: Strike price
        right: "C" for call, "P" for put
        exchange: Exchange (default: "SMART")
        currency: Currency (default: "USD")
    
    Returns:
        Option contract
    
    Example:
        >>> contract = create_option_contract(
        ...     symbol="AAPL",
        ...     last_trade_date="20260320",
        ...     strike=150.0,
        ...     right="C"
        ... )
    """
    return Option(
        symbol=symbol,
        lastTradeDateOrContractMonth=last_trade_date,
        strike=strike,
        right=right,
        exchange=exchange,
        currency=currency,
    )


def create_future_contract(
    symbol: str,
    last_trade_date: str,
    exchange: str,
    currency: str = "USD",
) -> Contract:
    """
    Create a futures contract.
    
    Args:
        symbol: Future symbol (e.g., "ES" for E-mini S&P 500)
        last_trade_date: Expiration date (YYYYMM format)
        exchange: Exchange (e.g., "GLOBEX")
        currency: Currency (default: "USD")
    
    Returns:
        Future contract
    
    Example:
        >>> contract = create_future_contract(
        ...     symbol="ES",
        ...     last_trade_date="202603",
        ...     exchange="GLOBEX"
        ... )
    """
    return Future(
        symbol=symbol,
        lastTradeDateOrContractMonth=last_trade_date,
        exchange=exchange,
        currency=currency,
    )


def create_index_contract(
    symbol: str,
    exchange: str,
    currency: str = "USD",
) -> Contract:
    """
    Create an index contract.
    
    Args:
        symbol: Index symbol (e.g., "SPX")
        exchange: Exchange (e.g., "CBOE")
        currency: Currency (default: "USD")
    
    Returns:
        Index contract
    
    Example:
        >>> contract = create_index_contract("SPX", "CBOE")
    """
    return Index(symbol, exchange, currency)


def validate_symbol(symbol: str) -> bool:
    """
    Validate stock symbol format.
    
    Args:
        symbol: Stock ticker symbol
    
    Returns:
        True if valid, False otherwise
    
    Example:
        >>> validate_symbol("AAPL")
        True
        >>> validate_symbol("123")
        False
    """
    if not symbol:
        return False

    # IBKR symbols may contain '.' (e.g. BRK.B) and run up to ~12 chars.
    if not all(c.isalnum() or c in "._-" for c in symbol):
        return False

    if not 1 <= len(symbol) <= 12:
        return False

    return True


def format_currency(value: float, currency: str = "USD") -> str:
    """
    Format currency value for display.
    
    Args:
        value: Numeric value
        currency: Currency code
    
    Returns:
        Formatted string
    
    Example:
        >>> format_currency(1234.56)
        '$1,234.56'
    """
    if currency == "USD":
        return f"${value:,.2f}"
    else:
        return f"{value:,.2f} {currency}"


def calculate_position_size(
    account_value: float,
    risk_percent: float,
    entry_price: float,
    stop_price: float,
) -> int:
    """
    Calculate position size based on risk management rules.
    
    Args:
        account_value: Total account value
        risk_percent: Risk as percentage of account (e.g., 0.01 for 1%)
        entry_price: Planned entry price
        stop_price: Stop loss price
    
    Returns:
        Number of shares to trade
    
    Example:
        >>> shares = calculate_position_size(
        ...     account_value=100000,
        ...     risk_percent=0.01,  # 1% risk
        ...     entry_price=150.0,
        ...     stop_price=145.0
        ... )
        >>> print(shares)
        200
    """
    if entry_price <= 0 or stop_price <= 0:
        raise ValueError("Prices must be positive")
    
    if entry_price == stop_price:
        raise ValueError("Entry and stop prices cannot be equal")
    
    # Calculate risk per share
    risk_per_share = abs(entry_price - stop_price)
    
    # Calculate total risk amount
    risk_amount = account_value * risk_percent
    
    # Calculate position size
    shares = int(risk_amount / risk_per_share)
    
    return max(shares, 0)
