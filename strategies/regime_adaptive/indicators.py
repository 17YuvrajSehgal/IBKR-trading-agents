"""
Pure-Python streaming technical indicators.

All indicators expose the same shape:
    ind = Indicator(period=...)
    value = ind.update(...)   # returns None until warm, then a float

State is incremental — no full recompute on every bar. Safe to unit-test
without a broker.

Implemented:
    EMA       — exponential moving average
    SMA       — simple moving average
    ATR       — Wilder smoothed Average True Range
    RSI       — Wilder smoothed Relative Strength Index
    ADX       — Average Directional Index (+ DI+ / DI- as side outputs)
    Bollinger — middle / upper / lower band tuple
"""

from __future__ import annotations

from collections import deque
from typing import Deque, Optional


class EMA:
    """Exponential moving average. Warm after `period` samples."""

    def __init__(self, period: int) -> None:
        if period < 1:
            raise ValueError("period must be >= 1")
        self.period = period
        self.alpha = 2.0 / (period + 1)
        self.value: Optional[float] = None
        self._count = 0

    def update(self, x: float) -> Optional[float]:
        if self.value is None:
            self.value = x
        else:
            self.value = self.alpha * x + (1.0 - self.alpha) * self.value
        self._count += 1
        return self.value if self._count >= self.period else None


class SMA:
    """Simple moving average over a fixed window."""

    def __init__(self, period: int) -> None:
        if period < 1:
            raise ValueError("period must be >= 1")
        self.period = period
        self._window: Deque[float] = deque(maxlen=period)
        self._sum = 0.0

    def update(self, x: float) -> Optional[float]:
        if len(self._window) == self.period:
            self._sum -= self._window[0]
        self._window.append(x)
        self._sum += x
        if len(self._window) < self.period:
            return None
        return self._sum / self.period


class _TrueRange:
    """Internal helper: True Range bar by bar."""

    def __init__(self) -> None:
        self._prev_close: Optional[float] = None

    def update(self, high: float, low: float, close: float) -> float:
        if self._prev_close is None:
            tr = high - low
        else:
            tr = max(
                high - low,
                abs(high - self._prev_close),
                abs(low - self._prev_close),
            )
        self._prev_close = close
        return tr


class ATR:
    """Wilder smoothed Average True Range. Warm after `period` bars."""

    def __init__(self, period: int = 14) -> None:
        if period < 1:
            raise ValueError("period must be >= 1")
        self.period = period
        self._tr = _TrueRange()
        self.value: Optional[float] = None
        self._count = 0

    def update(self, high: float, low: float, close: float) -> Optional[float]:
        tr = self._tr.update(high, low, close)
        self._count += 1

        if self.value is None:
            self.value = tr
        else:
            # Wilder smoothing: ATR = (prev * (n-1) + TR) / n
            self.value = (self.value * (self.period - 1) + tr) / self.period

        return self.value if self._count >= self.period else None


class RSI:
    """Wilder smoothed RSI in [0, 100]. Warm after `period + 1` closes."""

    def __init__(self, period: int = 14) -> None:
        if period < 1:
            raise ValueError("period must be >= 1")
        self.period = period
        self._prev_close: Optional[float] = None
        self._avg_gain: Optional[float] = None
        self._avg_loss: Optional[float] = None
        self._count = 0

    def update(self, close: float) -> Optional[float]:
        if self._prev_close is None:
            self._prev_close = close
            return None

        change = close - self._prev_close
        gain = change if change > 0 else 0.0
        loss = -change if change < 0 else 0.0
        self._prev_close = close
        self._count += 1

        if self._avg_gain is None:
            self._avg_gain = gain
            self._avg_loss = loss
        else:
            self._avg_gain = (self._avg_gain * (self.period - 1) + gain) / self.period
            self._avg_loss = (self._avg_loss * (self.period - 1) + loss) / self.period

        if self._count < self.period:
            return None

        if self._avg_loss == 0:
            return 100.0
        rs = self._avg_gain / self._avg_loss
        return 100.0 - 100.0 / (1.0 + rs)


class ADX:
    """
    Average Directional Index — measures trend strength regardless of direction.

    ADX < 20 → no trend (ranging)
    ADX > 25 → trending; check sign of (DI+ minus DI-) for direction
    """

    def __init__(self, period: int = 14) -> None:
        if period < 2:
            raise ValueError("period must be >= 2")
        self.period = period
        self._tr = _TrueRange()
        self._prev_high: Optional[float] = None
        self._prev_low: Optional[float] = None
        self._atr: Optional[float] = None
        self._plus_dm_sm: float = 0.0
        self._minus_dm_sm: float = 0.0
        self._adx: Optional[float] = None
        self.di_plus: Optional[float] = None
        self.di_minus: Optional[float] = None
        self._count = 0

    def update(self, high: float, low: float, close: float) -> Optional[float]:
        tr = self._tr.update(high, low, close)

        if self._prev_high is None:
            self._prev_high, self._prev_low = high, low
            return None

        up_move = high - self._prev_high
        down_move = self._prev_low - low
        plus_dm = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm = down_move if (down_move > up_move and down_move > 0) else 0.0

        self._prev_high, self._prev_low = high, low
        self._count += 1

        if self._atr is None:
            self._atr = tr
            self._plus_dm_sm = plus_dm
            self._minus_dm_sm = minus_dm
        else:
            self._atr = (self._atr * (self.period - 1) + tr) / self.period
            self._plus_dm_sm = (self._plus_dm_sm * (self.period - 1) + plus_dm) / self.period
            self._minus_dm_sm = (self._minus_dm_sm * (self.period - 1) + minus_dm) / self.period

        if self._atr == 0 or self._count < self.period:
            return None

        self.di_plus = 100.0 * self._plus_dm_sm / self._atr
        self.di_minus = 100.0 * self._minus_dm_sm / self._atr

        di_sum = self.di_plus + self.di_minus
        dx = 0.0 if di_sum == 0 else 100.0 * abs(self.di_plus - self.di_minus) / di_sum

        if self._adx is None:
            self._adx = dx
        else:
            self._adx = (self._adx * (self.period - 1) + dx) / self.period

        # Require 2*period bars for a stable ADX value
        return self._adx if self._count >= self.period * 2 else None


class Bollinger:
    """Bollinger Bands. Returns (middle, upper, lower) once warm."""

    def __init__(self, period: int = 20, num_std: float = 2.0) -> None:
        if period < 2:
            raise ValueError("period must be >= 2")
        self.period = period
        self.num_std = num_std
        self._window: Deque[float] = deque(maxlen=period)

    def update(self, x: float) -> Optional[tuple[float, float, float]]:
        self._window.append(x)
        if len(self._window) < self.period:
            return None
        mean = sum(self._window) / self.period
        var = sum((v - mean) ** 2 for v in self._window) / self.period
        std = var ** 0.5
        return mean, mean + self.num_std * std, mean - self.num_std * std
