"""
Bar primitives and multi-timeframe aggregation.

We expose a typed Bar dataclass and a BarAggregator that rolls finer-resolution
bars up into coarser ones (e.g., 1-min → 5-min). Used when only one realtime
subscription is available but multiple timeframes are needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class Bar:
    """OHLCV bar. `time` is the bar OPEN time."""

    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def body(self) -> float:
        return abs(self.close - self.open)


class BarAggregator:
    """
    Aggregate finer bars into coarser ones.

    Example: feed 1-min bars in, emit a 5-min bar every 5 closes.
    Uses the bar's `time` and a `factor` rather than wall-clock so it works on
    backtests and live equally.
    """

    def __init__(self, factor: int) -> None:
        if factor < 1:
            raise ValueError("factor must be >= 1")
        self.factor = factor
        self._pending: list[Bar] = []

    def update(self, bar: Bar) -> Optional[Bar]:
        """
        Push a finer bar; return an aggregated coarser bar when one completes,
        else None.
        """
        self._pending.append(bar)
        if len(self._pending) < self.factor:
            return None

        first = self._pending[0]
        last = self._pending[-1]
        aggregated = Bar(
            time=first.time,
            open=first.open,
            high=max(b.high for b in self._pending),
            low=min(b.low for b in self._pending),
            close=last.close,
            volume=sum(b.volume for b in self._pending),
        )
        self._pending.clear()
        return aggregated
