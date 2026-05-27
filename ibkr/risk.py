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
    SYMBOL_BLACKLISTED   = "SYMBOL_BLACKLISTED"


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

    # Per-symbol circuit breakers (set either / both to 0 to disable).
    # Designed to stop the "keep re-entering DBX and losing $500 each time"
    # pattern that lost -$3,640 on 2026-05-27.
    max_consecutive_losses_per_symbol: int   = 2     # consecutive losses before blacklist
    max_loss_per_symbol_per_session:   float = 500.0 # cumulative $ loss before blacklist


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

        # Per-symbol session PnL and consecutive-loss counters — used by
        # the circuit breaker that blacklists ticker that keep losing.
        self._symbol_pnl: dict[str, float] = {}
        self._symbol_consecutive_losses: dict[str, int] = {}
        self._symbol_blacklist: dict[str, str] = {}    # symbol -> reason

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
        reserve:  bool = True,
    ) -> RiskCheckResult:
        """
        Run all risk checks for a proposed order, atomically reserving the
        exposure on approval.

        Reservation prevents a race where N parallel callers all read the
        same pre-fill state and all pass — but together would exceed the cap.
        With ``reserve=True`` (the default), the n-th caller sees the
        in-flight reservations from callers 1..n-1.

        If the caller subsequently fails to place the order, it must call
        :meth:`release_reservation` with the same parameters to undo.

        Args:
            symbol:   Ticker symbol
            action:   "BUY" or "SELL"
            quantity: Number of shares
            price:    Proposed order price (limit price)
            reserve:  When True (default) the approved order's notional and
                      share count are added to the internal trackers
                      immediately, so subsequent concurrent checks see them.

        Returns:
            RiskCheckResult with approved=True if all checks pass.
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

        # 2b. Per-symbol blacklist (circuit breaker fired earlier)
        if symbol in self._symbol_blacklist:
            return RiskCheckResult.deny(
                f"{symbol} blacklisted for session: {self._symbol_blacklist[symbol]}",
                RejectionReason.SYMBOL_BLACKLISTED,
                symbol_pnl=self._symbol_pnl.get(symbol, 0.0),
                consecutive_losses=self._symbol_consecutive_losses.get(symbol, 0),
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

        # All checks passed — atomically reserve so concurrent checks see this.
        if reserve:
            sign = 1 if action.upper() == "BUY" else -1
            self._positions[symbol] = current_position + sign * quantity
            self._notional[symbol] = current_notional + order_notional

        self._session_orders_sent += 1
        return RiskCheckResult.ok()

    def release_reservation(
        self,
        symbol:   str,
        action:   str,
        quantity: int,
        price:    float,
    ) -> None:
        """
        Undo a reservation made by ``check_order(..., reserve=True)``.

        Call this when the order fails to place at the broker (e.g. exception
        from placeOrder, or TWS rejection before any partial fill). The
        reserved exposure is removed so it doesn't permanently inflate the
        running notional.

        Safe to call repeatedly — clamps at zero.
        """
        sign = 1 if action.upper() == "BUY" else -1
        order_notional = quantity * price

        self._positions[symbol] = self._positions.get(symbol, 0) - sign * quantity
        new_notional = self._notional.get(symbol, 0.0) - order_notional
        self._notional[symbol] = max(0.0, new_notional)

        logger.debug(
            f"Released reservation: {action} {quantity} {symbol} @ ${price:.4f} "
            f"-> position={self._positions[symbol]} notional=${self._notional[symbol]:,.2f}"
        )

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
        DEPRECATED. No-op.

        Exposure is now reserved atomically inside :meth:`check_order` on
        approval. Use :meth:`release_reservation` to undo a reservation when
        order placement fails, and :meth:`record_pnl` to track realized P&L.

        Kept as a no-op for backward compat so existing callers don't break.
        """
        return

    def record_pnl(self, realized_pnl: float) -> None:
        """
        Update session realized P&L.

        Called by the executor when a commission report arrives with realized PnL.

        Args:
            realized_pnl: The P&L amount (may be negative for losses)
        """
        self._session_realized_pnl += realized_pnl
        logger.debug(f"PnL update: {realized_pnl:+.2f} | session_total={self._session_realized_pnl:+.2f}")

    def record_close(self, symbol: str, pnl: float) -> None:
        """
        Record a closed trade against the per-symbol circuit-breaker counters
        and update session P&L.

        Trips the symbol blacklist when either:
          * cumulative session loss on the symbol crosses
            ``max_loss_per_symbol_per_session``, or
          * consecutive-loss count reaches
            ``max_consecutive_losses_per_symbol``.

        Once blacklisted, subsequent ``check_order`` calls for that symbol
        return ``SYMBOL_BLACKLISTED`` for the remainder of the session.

        Args:
            symbol: Ticker that just closed.
            pnl:    Realized P&L on this close (negative for losses).
        """
        self.record_pnl(pnl)

        self._symbol_pnl[symbol] = self._symbol_pnl.get(symbol, 0.0) + pnl
        if pnl < 0:
            self._symbol_consecutive_losses[symbol] = (
                self._symbol_consecutive_losses.get(symbol, 0) + 1
            )
        elif pnl > 0:
            # Profit resets the consecutive-loss counter
            self._symbol_consecutive_losses[symbol] = 0

        if symbol in self._symbol_blacklist:
            return  # already tripped

        limits = self._limits
        sym_pnl = self._symbol_pnl[symbol]
        sym_losses = self._symbol_consecutive_losses.get(symbol, 0)

        if (
            limits.max_loss_per_symbol_per_session > 0
            and sym_pnl <= -abs(limits.max_loss_per_symbol_per_session)
        ):
            reason = (
                f"session loss ${sym_pnl:,.2f} <= "
                f"-${limits.max_loss_per_symbol_per_session:,.0f} cap"
            )
            self._symbol_blacklist[symbol] = reason
            logger.warning(f"🛑 {symbol} BLACKLISTED — {reason}")
        elif (
            limits.max_consecutive_losses_per_symbol > 0
            and sym_losses >= limits.max_consecutive_losses_per_symbol
        ):
            reason = (
                f"{sym_losses} consecutive losses "
                f">= {limits.max_consecutive_losses_per_symbol} cap"
            )
            self._symbol_blacklist[symbol] = reason
            logger.warning(f"🛑 {symbol} BLACKLISTED — {reason}")

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
