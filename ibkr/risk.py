"""
Risk management layer for HFT operations.

This module is the GATING layer that sits between signal detection and order
execution. Every potential order must pass through RiskManager.check_order()
before it is submitted to the broker. If any check fails the order is
silently dropped and the rejection is recorded for post-session analysis.

Risk checks (in evaluation order):
  1. Global trading halt   – manual kill-switch
  2. Daily loss limit      – halt all trading if session loss exceeds threshold
  3. Max orders per second – token-bucket rate limiter
  4. Per-symbol exposure   – max notional value per symbol
  5. Total portfolio exposure – max notional across all open positions
  6. Order size sanity     – minimum / maximum lot size

Design principles:
  - Pure Python (no I/O), easy to unit-test
  - All state is updated atomically within the Python GIL
  - Returns rich RiskCheckResult so callers can log the exact rejection reason
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

class RejectionReason(str, Enum):
    """Enumerated rejection reasons — makes downstream analysis easy."""
    TRADING_HALTED       = "TRADING_HALTED"
    DAILY_LOSS_LIMIT     = "DAILY_LOSS_LIMIT"
    RATE_LIMIT           = "RATE_LIMIT"
    SYMBOL_EXPOSURE      = "SYMBOL_EXPOSURE"
    PORTFOLIO_EXPOSURE   = "PORTFOLIO_EXPOSURE"
    INVALID_QUANTITY     = "INVALID_QUANTITY"
    INVALID_PRICE        = "INVALID_PRICE"
    READONLY_MODE        = "READONLY_MODE"


@dataclass
class RiskCheckResult:
    """
    Result of a risk check evaluation.

    Attributes:
        approved:  True → order may proceed; False → order must be dropped
        reason:    Human-readable explanation (set when approved=False)
        code:      Structured rejection code (set when approved=False)
        details:   Optional dict of diagnostic data
    """
    approved: bool
    reason:   str = ""
    code:     Optional[RejectionReason] = None
    details:  dict = field(default_factory=dict)

    @classmethod
    def ok(cls) -> "RiskCheckResult":
        return cls(approved=True)

    @classmethod
    def deny(
        cls,
        reason: str,
        code: RejectionReason,
        **details,
    ) -> "RiskCheckResult":
        return cls(approved=False, reason=reason, code=code, details=details)

    def __str__(self) -> str:
        if self.approved:
            return "RiskCheckResult(approved=True)"
        return f"RiskCheckResult(approved=False, code={self.code}, reason={self.reason!r})"


# ---------------------------------------------------------------------------
# Token-bucket rate limiter (orders per second)
# ---------------------------------------------------------------------------

class TokenBucket:
    """
    Thread-safe token-bucket rate limiter.

    Each call to `consume()` attempts to take one token from the bucket.
    Tokens refill at `rate` per second up to `capacity`.

    Args:
        rate:     Tokens added per second (= max sustained order rate)
        capacity: Maximum burst size (= bucket depth)
    """

    def __init__(self, rate: float, capacity: float) -> None:
        self._rate     = rate
        self._capacity = capacity
        self._tokens   = capacity
        self._last_ts  = time.monotonic()

    def consume(self) -> bool:
        """
        Try to consume one token.

        Returns:
            True if a token was available (order is within rate limit)
            False if the bucket is empty (order must be rejected)
        """
        now = time.monotonic()
        elapsed = now - self._last_ts
        self._last_ts = now

        # Refill
        self._tokens = min(
            self._capacity,
            self._tokens + elapsed * self._rate,
        )

        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False

    @property
    def available(self) -> float:
        """Current token count (informational)."""
        return self._tokens


# ---------------------------------------------------------------------------
# Risk limits configuration
# ---------------------------------------------------------------------------

@dataclass
class RiskLimits:
    """
    All configurable risk parameters in one place.

    These are independent of HFTConfig so the risk layer can be used
    standalone, tested in isolation, or adjusted at runtime.

    Attributes:
        max_orders_per_second:    Token-bucket rate (hard cap per IBKR rules)
        max_position_per_symbol:  Max shares held in any single symbol
        max_notional_per_symbol:  Max USD notional exposure per symbol
        max_total_notional:       Max USD notional across all open positions
        max_daily_loss:           Session loss threshold — halts all trading
        min_order_quantity:       Smallest allowed order size (shares)
        max_order_quantity:       Largest allowed single order size (shares)
        readonly:                 If True all orders are rejected immediately
    """
    max_orders_per_second:   float = 45.0           # IBKR hard limit is 50
    max_position_per_symbol: int   = 2_000           # shares
    max_notional_per_symbol: float = 20_000.0        # USD
    max_total_notional:      float = 100_000.0       # USD  (= account size)
    max_daily_loss:          float = 1_000.0         # USD  (1% of $100k)
    min_order_quantity:      int   = 1
    max_order_quantity:      int   = 1_000           # shares
    readonly:                bool  = False


# ---------------------------------------------------------------------------
# Risk manager
# ---------------------------------------------------------------------------

class RiskManager:
    """
    Pre-flight risk checker for every outbound order.

    Maintains running totals for positions, notional exposure, and session
    P&L. Must be kept in sync by the executor via record_fill() and
    record_pnl_update() after every confirmed fill.

    Usage:
        >>> limits = RiskLimits(max_daily_loss=500.0)
        >>> risk = RiskManager(limits)
        >>> result = risk.check_order("AAPL", "BUY", 100, 185.50)
        >>> if result.approved:
        ...     executor.submit(...)
        >>> risk.record_fill("AAPL", "BUY", 100, 185.50)
    """

    def __init__(self, limits: RiskLimits) -> None:
        self._limits = limits
        self._rate_limiter = TokenBucket(
            rate=limits.max_orders_per_second,
            capacity=limits.max_orders_per_second,  # burst = 1 second of orders
        )

        # Per-symbol position tracking (shares, sign-aware: + long, - short)
        self._positions: dict[str, int] = {}

        # Per-symbol notional exposure (abs value, USD)
        self._notional: dict[str, float] = {}

        # Session financials
        self._session_realized_pnl: float = 0.0
        self._session_orders_sent:  int   = 0
        self._session_orders_rejected: int = 0

        # Kill switch
        self._halted: bool = False
        self._halt_reason: str = ""

        logger.info(
            f"RiskManager initialised — "
            f"max_orders/s={limits.max_orders_per_second}, "
            f"max_total_notional={limits.max_total_notional:,.0f}, "
            f"max_daily_loss={limits.max_daily_loss:,.0f}"
        )

    # ------------------------------------------------------------------
    # Public – primary check
    # ------------------------------------------------------------------

    def check_order(
        self,
        symbol:   str,
        action:   str,         # "BUY" or "SELL"
        quantity: int,
        price:    float,
    ) -> RiskCheckResult:
        """
        Run all risk checks for a proposed order.

        Checks are evaluated in order of cheapness; the first failure
        short-circuits the rest (fail-fast).

        Args:
            symbol:   Ticker symbol
            action:   "BUY" or "SELL"
            quantity: Number of shares
            price:    Proposed order price (limit price)

        Returns:
            RiskCheckResult with approved=True if all checks pass
        """
        # 1. Readonly mode
        if self._limits.readonly:
            return RiskCheckResult.deny(
                "Risk manager is in read-only mode — order placement disabled",
                RejectionReason.READONLY_MODE,
            )

        # 2. Global halt
        if self._halted:
            return RiskCheckResult.deny(
                f"Trading halted: {self._halt_reason}",
                RejectionReason.TRADING_HALTED,
            )

        # 3. Daily loss limit
        if self._session_realized_pnl <= -abs(self._limits.max_daily_loss):
            self._halt(f"Daily loss limit of ${self._limits.max_daily_loss:,.2f} reached")
            return RiskCheckResult.deny(
                self._halt_reason,
                RejectionReason.DAILY_LOSS_LIMIT,
                session_pnl=self._session_realized_pnl,
            )

        # 4. Rate limit
        if not self._rate_limiter.consume():
            self._session_orders_rejected += 1
            return RiskCheckResult.deny(
                f"Rate limit exceeded ({self._limits.max_orders_per_second}/s)",
                RejectionReason.RATE_LIMIT,
                available_tokens=self._rate_limiter.available,
            )

        # 5. Quantity sanity
        if quantity < self._limits.min_order_quantity:
            return RiskCheckResult.deny(
                f"Quantity {quantity} below minimum {self._limits.min_order_quantity}",
                RejectionReason.INVALID_QUANTITY,
            )
        if quantity > self._limits.max_order_quantity:
            return RiskCheckResult.deny(
                f"Quantity {quantity} exceeds maximum {self._limits.max_order_quantity}",
                RejectionReason.INVALID_QUANTITY,
            )

        # 6. Price sanity
        if price <= 0:
            return RiskCheckResult.deny(
                f"Invalid price {price}",
                RejectionReason.INVALID_PRICE,
            )

        # 7. Per-symbol position limit (shares)
        current_position = self._positions.get(symbol, 0)
        projected_position = (
            current_position + quantity if action.upper() == "BUY"
            else current_position - quantity
        )
        if abs(projected_position) > self._limits.max_position_per_symbol:
            self._session_orders_rejected += 1
            return RiskCheckResult.deny(
                f"{symbol} projected position {projected_position} exceeds "
                f"limit of ±{self._limits.max_position_per_symbol} shares",
                RejectionReason.SYMBOL_EXPOSURE,
                current=current_position,
                projected=projected_position,
            )

        # 8. Per-symbol notional limit (USD)
        order_notional = quantity * price
        current_notional = self._notional.get(symbol, 0.0)
        projected_notional = current_notional + order_notional
        if projected_notional > self._limits.max_notional_per_symbol:
            self._session_orders_rejected += 1
            return RiskCheckResult.deny(
                f"{symbol} projected notional ${projected_notional:,.2f} exceeds "
                f"limit of ${self._limits.max_notional_per_symbol:,.2f}",
                RejectionReason.SYMBOL_EXPOSURE,
                current_notional=current_notional,
                order_notional=order_notional,
            )

        # 9. Total portfolio notional limit
        total_notional = sum(self._notional.values()) + order_notional
        if total_notional > self._limits.max_total_notional:
            self._session_orders_rejected += 1
            return RiskCheckResult.deny(
                f"Total portfolio notional ${total_notional:,.2f} would exceed "
                f"limit of ${self._limits.max_total_notional:,.2f}",
                RejectionReason.PORTFOLIO_EXPOSURE,
                current_total=sum(self._notional.values()),
                order_notional=order_notional,
            )

        # All checks passed
        self._session_orders_sent += 1
        return RiskCheckResult.ok()

    # ------------------------------------------------------------------
    # Public – state updates (called after fills)
    # ------------------------------------------------------------------

    def record_fill(
        self,
        symbol:    str,
        action:    str,
        quantity:  int,
        price:     float,
    ) -> None:
        """
        Update internal position and notional tracking after a confirmed fill.

        Must be called by the executor for every filled order so that
        subsequent risk checks reflect reality.

        Args:
            symbol:   Ticker
            action:   "BUY" or "SELL"
            quantity: Filled shares
            price:    Fill price
        """
        sign = 1 if action.upper() == "BUY" else -1
        self._positions[symbol] = self._positions.get(symbol, 0) + sign * quantity

        # Notional tracks absolute open exposure
        notional_change = quantity * price
        if sign == 1:
            self._notional[symbol] = self._notional.get(symbol, 0.0) + notional_change
        else:
            self._notional[symbol] = max(0.0, self._notional.get(symbol, 0.0) - notional_change)

        logger.debug(
            f"Fill recorded: {action} {quantity} {symbol} @ ${price:.4f} | "
            f"position={self._positions[symbol]} notional=${self._notional[symbol]:,.2f}"
        )

    def record_pnl(self, realized_pnl: float) -> None:
        """
        Update session realized P&L.

        Called by the executor when a commission report arrives with realized PnL.

        Args:
            realized_pnl: The P&L amount (may be negative for losses)
        """
        self._session_realized_pnl += realized_pnl
        logger.debug(f"PnL update: {realized_pnl:+.2f} | session_total={self._session_realized_pnl:+.2f}")

    # ------------------------------------------------------------------
    # Public – manual kill switch
    # ------------------------------------------------------------------

    def halt(self, reason: str = "Manual halt") -> None:
        """
        Immediately halt all trading.

        Args:
            reason: Human-readable description of why trading was halted
        """
        self._halt(reason)

    def resume(self) -> None:
        """
        Resume trading after a manual halt.

        Note: Automatic halts (e.g. daily loss limit) cannot be resumed
        programmatically — restart the engine.
        """
        if not self._halted:
            return
        self._halted = False
        self._halt_reason = ""
        logger.warning("Trading resumed — ensure you intend to continue")

    # ------------------------------------------------------------------
    # Public – introspection
    # ------------------------------------------------------------------

    def get_position(self, symbol: str) -> int:
        """Current tracked position for symbol (shares, ± signed)."""
        return self._positions.get(symbol, 0)

    def get_notional(self, symbol: str) -> float:
        """Current tracked notional exposure for symbol (USD)."""
        return self._notional.get(symbol, 0.0)

    @property
    def total_notional(self) -> float:
        """Total absolute notional exposure across all symbols (USD)."""
        return sum(self._notional.values())

    @property
    def session_pnl(self) -> float:
        """Cumulative session realized P&L (USD)."""
        return self._session_realized_pnl

    @property
    def is_halted(self) -> bool:
        """True if all trading has been manually or automatically stopped."""
        return self._halted

    def summary(self) -> dict:
        """Return a snapshot dict suitable for logging or dashboards."""
        return {
            "halted":             self._halted,
            "halt_reason":        self._halt_reason,
            "session_pnl":        self._session_realized_pnl,
            "orders_sent":        self._session_orders_sent,
            "orders_rejected":    self._session_orders_rejected,
            "total_notional":     self.total_notional,
            "positions":          dict(self._positions),
            "rate_tokens_avail":  round(self._rate_limiter.available, 2),
        }

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _halt(self, reason: str) -> None:
        self._halted = True
        self._halt_reason = reason
        logger.critical(f"🛑 TRADING HALTED — {reason}")

    def __repr__(self) -> str:
        return (
            f"RiskManager("
            f"halted={self._halted}, "
            f"pnl={self._session_realized_pnl:+.2f}, "
            f"notional={self.total_notional:,.2f})"
        )
