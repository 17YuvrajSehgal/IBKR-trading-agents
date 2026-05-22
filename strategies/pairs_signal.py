"""
TQQQ / SQQQ pairs-trade signal generator.

The math
--------
TQQQ targets +3x QQQ daily return; SQQQ targets -3x QQQ daily return.
Over a single rebalancing period these returns should approximately cancel,
so the joint quantity

    s_t = ln(TQQQ_t) + ln(SQQQ_t)

drifts only slowly (via the well-known leveraged-ETF decay). On any short
window, `s_t` is approximately stationary, which makes its deviation from a
rolling mean a tractable mean-reversion signal.

Signal interpretation
---------------------
Let z = (s_t - rolling_mean) / rolling_std.

    z >> 0  →  TQQQ and/or SQQQ are jointly rich relative to recent history
               (one leg over-rallied without the other catching its decline).
               Trade: SHORT TQQQ + SHORT SQQQ (short-spread).

    z << 0  →  jointly cheap relative to recent history.
               Trade: LONG TQQQ + LONG SQQQ (long-spread).

Exit when |z| returns under `z_exit` (mean reversion completed) or hits the
opposite-direction stop (`z_stop`, structural break).

This module is pure Python with no broker dependency so it can be unit-tested
and reasoned about in isolation.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Deque, Optional


class Signal(str, Enum):
    """Action recommended by the signal."""

    NONE = "NONE"               # no actionable signal
    ENTER_LONG_SPREAD = "ENTER_LONG_SPREAD"     # buy TQQQ + buy SQQQ
    ENTER_SHORT_SPREAD = "ENTER_SHORT_SPREAD"   # sell TQQQ + sell SQQQ
    EXIT = "EXIT"               # close any open position
    STOP = "STOP"               # adverse move past stop threshold


@dataclass
class SignalConfig:
    """Tunable thresholds for the pairs signal."""

    lookback: int = 300         # number of samples in rolling window
    z_enter: float = 2.0        # |z| at which to open a position
    z_exit: float = 0.5         # |z| at which to close
    z_stop: float = 4.0         # |z| at which to stop out (structural break)
    min_samples: int = 60       # need at least this many samples before any signal
    min_std: float = 1e-5       # ignore signals when the spread is essentially flat


@dataclass
class SignalState:
    """Snapshot of the signal at one point in time, for logging / debugging."""

    spread: float
    mean: float
    std: float
    z: float
    samples: int
    action: Signal


class PairsSignal:
    """
    Rolling-window z-score signal on log(TQQQ) + log(SQQQ).

    Usage
    -----
    >>> sig = PairsSignal(SignalConfig())
    >>> for tqqq, sqqq in ticks:
    ...     state = sig.update(tqqq, sqqq, in_position=current_position is not None)
    ...     if state.action == Signal.ENTER_LONG_SPREAD: ...
    """

    def __init__(self, config: SignalConfig) -> None:
        if config.lookback < 2:
            raise ValueError("lookback must be >= 2")
        if config.min_samples < 2 or config.min_samples > config.lookback:
            raise ValueError("min_samples must be in [2, lookback]")
        if config.z_exit < 0 or config.z_enter <= config.z_exit:
            raise ValueError("z_enter must be > z_exit >= 0")
        if config.z_stop <= config.z_enter:
            raise ValueError("z_stop must be > z_enter")

        self._cfg = config
        self._window: Deque[float] = deque(maxlen=config.lookback)

        # Running-sum accumulators so update() is O(1) instead of O(N).
        self._sum: float = 0.0
        self._sum_sq: float = 0.0

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def update(
        self,
        tqqq_price: float,
        sqqq_price: float,
        in_position: bool,
        position_side: Optional[Signal] = None,
    ) -> SignalState:
        """
        Push a new observation and return the resulting signal state.

        Args:
            tqqq_price:    Latest TQQQ price (mid or last, > 0)
            sqqq_price:    Latest SQQQ price (mid or last, > 0)
            in_position:   True if the agent currently holds a pair position
            position_side: When in_position=True, which side (ENTER_LONG_SPREAD
                           or ENTER_SHORT_SPREAD). Determines what counts as
                           an EXIT vs a STOP.

        Returns:
            SignalState with the current spread, z-score, and recommended action.
        """
        if tqqq_price <= 0 or sqqq_price <= 0:
            raise ValueError(
                f"Prices must be positive (got TQQQ={tqqq_price}, SQQQ={sqqq_price})"
            )

        spread = math.log(tqqq_price) + math.log(sqqq_price)

        # Maintain incremental sums for O(1) mean/var.
        if len(self._window) == self._window.maxlen:
            evicted = self._window[0]
            self._sum -= evicted
            self._sum_sq -= evicted * evicted

        self._window.append(spread)
        self._sum += spread
        self._sum_sq += spread * spread

        n = len(self._window)
        mean = self._sum / n
        # Population variance (we're tracking a fixed-size window, not a sample).
        variance = max(0.0, self._sum_sq / n - mean * mean)
        std = math.sqrt(variance)

        if n < self._cfg.min_samples or std < self._cfg.min_std:
            return SignalState(spread, mean, std, z=0.0, samples=n, action=Signal.NONE)

        z = (spread - mean) / std
        action = self._decide(z, in_position, position_side)

        return SignalState(spread, mean, std, z, samples=n, action=action)

    def reset(self) -> None:
        """Clear the rolling window (e.g. after a market halt or reconnect)."""
        self._window.clear()
        self._sum = 0.0
        self._sum_sq = 0.0

    @property
    def is_warm(self) -> bool:
        """True when the window has enough samples to emit a signal."""
        return len(self._window) >= self._cfg.min_samples

    @property
    def samples(self) -> int:
        return len(self._window)

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _decide(
        self,
        z: float,
        in_position: bool,
        position_side: Optional[Signal],
    ) -> Signal:
        c = self._cfg

        if not in_position:
            if z >= c.z_enter:
                return Signal.ENTER_SHORT_SPREAD
            if z <= -c.z_enter:
                return Signal.ENTER_LONG_SPREAD
            return Signal.NONE

        # We hold a position. Decide whether to close it.
        if position_side == Signal.ENTER_LONG_SPREAD:
            # We bet spread would rise. Exit when it has, stop if it falls further.
            if z >= -c.z_exit:
                return Signal.EXIT
            if z <= -c.z_stop:
                return Signal.STOP
        elif position_side == Signal.ENTER_SHORT_SPREAD:
            # We bet spread would fall.
            if z <= c.z_exit:
                return Signal.EXIT
            if z >= c.z_stop:
                return Signal.STOP

        return Signal.NONE
