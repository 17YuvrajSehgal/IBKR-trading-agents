"""
Logging configuration for IBKR package.

This module sets up comprehensive logging to both console and file,
with separate log files for different components.
"""

import logging
from datetime import datetime
from pathlib import Path


def setup_logging(
        log_dir: str = "ibkr_logs",
        log_level: int = logging.INFO,
        console_level: int = logging.INFO,
) -> None:
    """
    Setup comprehensive logging for IBKR package.
    
    Creates log directory and configures logging to both console and files.
    Separate log files are created for:
    - All logs (ibkr_all.log)
    - Connection logs (ibkr_connection.log)
    - Trading logs (ibkr_trading.log)
    - Position logs (ibkr_positions.log)
    
    Args:
        log_dir: Directory to store log files
        log_level: File logging level
        console_level: Console logging level
    
    Example:
        >>> from ibkr.logging_config import setup_logging
        >>> setup_logging(log_level=logging.DEBUG)
    """
    # Create log directory
    log_path = Path(log_dir)
    log_path.mkdir(exist_ok=True)

    # Create timestamp for log files
    timestamp = datetime.now().strftime("%Y%m%d")

    # Define log files
    all_log = log_path / f"ibkr_all_{timestamp}.log"
    connection_log = log_path / f"ibkr_connection_{timestamp}.log"
    trading_log = log_path / f"ibkr_trading_{timestamp}.log"
    positions_log = log_path / f"ibkr_positions_{timestamp}.log"

    # Create formatters
    detailed_formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    console_formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%H:%M:%S'
    )

    # Setup root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)  # Capture all levels

    # Remove existing handlers
    root_logger.handlers.clear()

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(console_level)
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)

    # All logs file handler
    all_handler = logging.FileHandler(all_log, mode='a')
    all_handler.setLevel(log_level)
    all_handler.setFormatter(detailed_formatter)
    root_logger.addHandler(all_handler)

    # Connection logs handler
    connection_handler = logging.FileHandler(connection_log, mode='a')
    connection_handler.setLevel(log_level)
    connection_handler.setFormatter(detailed_formatter)
    connection_handler.addFilter(lambda record: 'connection' in record.name.lower())
    root_logger.addHandler(connection_handler)

    # Trading logs handler
    trading_handler = logging.FileHandler(trading_log, mode='a')
    trading_handler.setLevel(log_level)
    trading_handler.setFormatter(detailed_formatter)
    trading_handler.addFilter(lambda record: 'trading' in record.name.lower())
    root_logger.addHandler(trading_handler)

    # Positions logs handler
    positions_handler = logging.FileHandler(positions_log, mode='a')
    positions_handler.setLevel(log_level)
    positions_handler.setFormatter(detailed_formatter)
    positions_handler.addFilter(lambda record: 'positions' in record.name.lower())
    root_logger.addHandler(positions_handler)

    # Log setup completion
    logger = logging.getLogger(__name__)
    logger.info(f"Logging initialized - logs saved to {log_path.absolute()}")
    logger.info(f"Log files: all, connection, trading, positions")


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger instance.
    
    Args:
        name: Logger name (typically __name__)
    
    Returns:
        Logger instance
    """
    return logging.getLogger(name)
