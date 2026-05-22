"""
Configuration management for IBKR connections.

This module provides dataclasses for configuring Interactive Brokers
connections with validation and factory methods for common scenarios.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


class TradingMode(str, Enum):
    """Trading mode enumeration."""
    
    PAPER = "paper"
    LIVE = "live"
    
    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class IBKRConfig:
    """
    Configuration for IBKR TWS/Gateway connection.
    
    This configuration class provides all settings needed to connect to
    Interactive Brokers TWS or IB Gateway, with sensible defaults and
    validation.
    
    Attributes:
        host: TWS/Gateway host address
        port: TWS/Gateway port number
        client_id: Unique client identifier (0-32)
        trading_mode: Paper or live trading mode
        timeout: Connection timeout in seconds
        readonly: If True, prevent order placement (safety feature)
        auto_reconnect: Enable automatic reconnection on disconnect
        max_reconnect_attempts: Maximum reconnection attempts
        reconnect_delay: Initial delay between reconnection attempts (seconds)
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR)
    
    Example:
        >>> # Paper trading configuration
        >>> config = IBKRConfig.paper_trading()
        >>> 
        >>> # Live trading configuration
        >>> config = IBKRConfig.live_trading()
        >>> 
        >>> # Custom configuration
        >>> config = IBKRConfig(
        ...     host="127.0.0.1",
        ...     port=7497,
        ...     client_id=1,
        ...     trading_mode=TradingMode.PAPER
        ... )
    """
    
    # Connection settings
    host: str = "127.0.0.1"
    port: int = 7497
    client_id: int = 1
    trading_mode: TradingMode = TradingMode.PAPER
    
    # Timeout settings
    timeout: int = 30
    request_timeout: int = 10
    
    # Safety settings
    readonly: bool = False
    
    # Reconnection settings
    auto_reconnect: bool = True
    max_reconnect_attempts: int = 5
    reconnect_delay: float = 2.0
    reconnect_backoff: float = 2.0  # Exponential backoff multiplier
    
    # Logging settings
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_to_file: bool = False
    log_file_path: str = "ibkr.log"
    
    # Advanced settings
    account: str | None = None  # Specific account (for multi-account setups)
    download_short_account: bool = False  # Download short account data
    
    def __post_init__(self) -> None:
        """Validate configuration parameters."""
        # Validate port
        if not 1024 <= self.port <= 65535:
            raise ValueError(
                f"Port must be between 1024 and 65535, got {self.port}"
            )
        
        # Validate client_id
        if not 0 <= self.client_id <= 32:
            raise ValueError(
                f"Client ID must be between 0 and 32, got {self.client_id}"
            )
        
        # Validate timeout
        if self.timeout <= 0:
            raise ValueError(
                f"Timeout must be positive, got {self.timeout}"
            )
        
        if self.request_timeout <= 0:
            raise ValueError(
                f"Request timeout must be positive, got {self.request_timeout}"
            )
        
        # Validate reconnection settings
        if self.max_reconnect_attempts < 0:
            raise ValueError(
                f"Max reconnect attempts must be non-negative, got {self.max_reconnect_attempts}"
            )
        
        if self.reconnect_delay <= 0:
            raise ValueError(
                f"Reconnect delay must be positive, got {self.reconnect_delay}"
            )
        
        if self.reconnect_backoff < 1.0:
            raise ValueError(
                f"Reconnect backoff must be >= 1.0, got {self.reconnect_backoff}"
            )
    
    @classmethod
    def paper_trading(
        cls,
        host: str = "127.0.0.1",
        port: int = 7497,
        client_id: int = 1,
        **kwargs,
    ) -> IBKRConfig:
        """
        Create configuration for paper trading.
        
        Paper trading uses TWS port 7497 by default and is safe for testing
        strategies without risking real capital.
        
        Args:
            host: TWS host address
            port: TWS port (default: 7497 for paper trading)
            client_id: Client identifier
            **kwargs: Additional configuration parameters
        
        Returns:
            IBKRConfig configured for paper trading
        
        Example:
            >>> config = IBKRConfig.paper_trading()
            >>> print(config.trading_mode)
            TradingMode.PAPER
        """
        return cls(
            host=host,
            port=port,
            client_id=client_id,
            trading_mode=TradingMode.PAPER,
            **kwargs,
        )
    
    @classmethod
    def live_trading(
        cls,
        host: str = "127.0.0.1",
        port: int = 7496,
        client_id: int = 1,
        **kwargs,
    ) -> IBKRConfig:
        """
        Create configuration for live trading.
        
        Live trading uses TWS port 7496 by default. This configuration
        should be used with caution as it involves real capital.
        
        Args:
            host: TWS host address
            port: TWS port (default: 7496 for live trading)
            client_id: Client identifier
            **kwargs: Additional configuration parameters
        
        Returns:
            IBKRConfig configured for live trading
        
        Warning:
            Live trading involves real money. Always test thoroughly in
            paper trading mode before switching to live.
        
        Example:
            >>> config = IBKRConfig.live_trading()
            >>> print(config.trading_mode)
            TradingMode.LIVE
        """
        return cls(
            host=host,
            port=port,
            client_id=client_id,
            trading_mode=TradingMode.LIVE,
            **kwargs,
        )
    
    @classmethod
    def readonly(
        cls,
        trading_mode: TradingMode = TradingMode.PAPER,
        **kwargs,
    ) -> IBKRConfig:
        """
        Create read-only configuration.
        
        Read-only mode prevents any order placement, making it safe for
        monitoring and data collection without risk of accidental trades.
        
        Args:
            trading_mode: Paper or live trading mode
            **kwargs: Additional configuration parameters
        
        Returns:
            IBKRConfig with readonly=True
        
        Example:
            >>> config = IBKRConfig.readonly()
            >>> print(config.readonly)
            True
        """
        port = 7497 if trading_mode == TradingMode.PAPER else 7496
        return cls(
            port=port,
            trading_mode=trading_mode,
            readonly=True,
            **kwargs,
        )
    
    def to_dict(self) -> dict:
        """
        Convert configuration to dictionary.
        
        Returns:
            Dictionary representation of configuration
        """
        return {
            "host": self.host,
            "port": self.port,
            "client_id": self.client_id,
            "trading_mode": self.trading_mode.value,
            "timeout": self.timeout,
            "request_timeout": self.request_timeout,
            "readonly": self.readonly,
            "auto_reconnect": self.auto_reconnect,
            "max_reconnect_attempts": self.max_reconnect_attempts,
            "reconnect_delay": self.reconnect_delay,
            "reconnect_backoff": self.reconnect_backoff,
            "log_level": self.log_level,
            "log_to_file": self.log_to_file,
            "log_file_path": self.log_file_path,
            "account": self.account,
        }
    
    def __repr__(self) -> str:
        """Return string representation."""
        return (
            f"IBKRConfig("
            f"host={self.host!r}, "
            f"port={self.port}, "
            f"client_id={self.client_id}, "
            f"trading_mode={self.trading_mode}, "
            f"readonly={self.readonly})"
        )
