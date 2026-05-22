"""
Custom exceptions for IBKR operations.

This module defines a hierarchy of exceptions for different error scenarios
when interacting with Interactive Brokers TWS/Gateway.
"""

from __future__ import annotations


class IBKRException(Exception):
    """
    Base exception for all IBKR-related errors.
    
    All custom exceptions in this package inherit from this base class,
    making it easy to catch all IBKR-specific errors.
    
    Attributes:
        message: Human-readable error message
        error_code: Optional IBKR error code
        details: Optional additional error details
    """
    
    def __init__(
        self,
        message: str,
        error_code: int | None = None,
        details: dict | None = None,
    ) -> None:
        """
        Initialize IBKR exception.
        
        Args:
            message: Error message
            error_code: Optional IBKR error code
            details: Optional additional error details
        """
        self.message = message
        self.error_code = error_code
        self.details = details or {}
        
        # Format error message
        if error_code is not None:
            full_message = f"[Error {error_code}] {message}"
        else:
            full_message = message
            
        super().__init__(full_message)
    
    def __repr__(self) -> str:
        """Return detailed string representation."""
        return (
            f"{self.__class__.__name__}("
            f"message={self.message!r}, "
            f"error_code={self.error_code}, "
            f"details={self.details})"
        )


class IBKRConnectionError(IBKRException):
    """
    Exception raised for connection-related errors.
    
    Examples:
        - Failed to connect to TWS/Gateway
        - Connection lost during operation
        - Connection timeout
    """
    pass


class IBKRAuthenticationError(IBKRException):
    """
    Exception raised for authentication/authorization errors.
    
    Examples:
        - Invalid client ID
        - Account not authorized for operation
        - Insufficient permissions
    """
    pass


class IBKRTimeoutError(IBKRException):
    """
    Exception raised when operations timeout.
    
    Examples:
        - Connection timeout
        - Request timeout
        - Order acknowledgment timeout
    """
    pass


class IBKROrderError(IBKRException):
    """
    Exception raised for order-related errors.
    
    Examples:
        - Invalid order parameters
        - Order rejected by exchange
        - Insufficient buying power
        - Position limit exceeded
    """
    pass


class IBKRDataError(IBKRException):
    """
    Exception raised for data-related errors.
    
    Examples:
        - Invalid contract specification
        - Market data subscription failed
        - Historical data request failed
        - Data parsing error
    """
    pass


class IBKRRateLimitError(IBKRException):
    """
    Exception raised when API rate limits are exceeded.
    
    Examples:
        - Too many requests per second
        - Message pacing violation
        - Historical data request limit exceeded
    """
    pass


class IBKRMarketClosedError(IBKRException):
    """
    Exception raised when attempting operations during market closure.
    
    Examples:
        - Placing market order when market is closed
        - Requesting real-time data outside trading hours
    """
    pass


class IBKRInsufficientFundsError(IBKRException):
    """
    Exception raised when account has insufficient funds for operation.
    
    Examples:
        - Placing order exceeding buying power
        - Margin call situation
    """
    pass


class IBKRInvalidOrderError(IBKRException):
    """
    Exception raised for invalid order parameters.
    
    Examples:
        - Negative quantity
        - Invalid price
        - Unsupported order type
    """
    pass
