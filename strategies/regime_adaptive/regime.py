"""
Regime detection and bias inference.

Two layers:
  * Bias       — direction filter from a higher timeframe (don't fight the trend)
  * Regime     — market state on the entry timeframe (trend / range / hi-vol)

A trade is only taken when bias and regime agree.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from strategies.regime_adaptive.bars import Bar
from strategies.regime_adaptive.indicators import ADX, ATR, EMA


class Bias(str, Enum):
    BULL = "BULL"
    BEAR = "BEAR"
    NEUTRAL = "NEUTRAL"


class Regime(str, Enum):
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    RANGE = "RANGE"
    HIGH_VOL = "HIGH_VOL"
    AMBIGUOUS = "AMBIGUOUS"          # ADX in the dead zone between trend & range
    WARMUP = "WARMUP"                # not enough data yet


@dataclass
class RegimeSnapshot:
    """Everything callers need to log a decision."""

    regime: Regime
    bias: Bias
    adx: float
    atr: float
    atr_pct: float
    ema_fast: float
    ema_slow: float


# ---------------------------------------------------------------------------
# Bias detector (higher timeframe)
# ---------------------------------------------------------------------------

class BiasDetector:
    """
    Directional bias from a higher timeframe via EMA-fast / EMA-slow.

    BULL when fast > slow by `min_gap` (relative). BEAR when below. Else NEUTRAL.
    """

    def __init__(
        self,
        ema_fast: int = 20,
        ema_slow: int = 50,
        min_gap_pct: float = 0.001,    # 0.1% — filters out noise crossovers
    ) -> None:
        self.ema_fast = EMA(ema_fast)
        self.ema_slow = EMA(ema_slow)
        self.min_gap_pct = min_gap_pct
        self.bias: Bias = Bias.NEUTRAL

    def update(self, bar: Bar) -> Bias:
        fast = self.ema_fast.update(bar.close)
        slow = self.ema_slow.update(bar.close)
        if fast is None or slow is None or slow == 0:
            self.bias = Bias.NEUTRAL
            return self.bias

        gap = (fast - slow) / abs(slow)
        if gap > self.min_gap_pct:
            self.bias = Bias.BULL
        elif gap < -self.min_gap_pct:
            self.bias = Bias.BEAR
        else:
            self.bias = Bias.NEUTRAL
        return self.bias


# ---------------------------------------------------------------------------
# Regime detector (entry timeframe)
# ---------------------------------------------------------------------------

class RegimeDetector:
    """
    Classifies the entry-timeframe regime using ADX + ATR/price.

    HIGH_VOL    — ATR/price > `high_vol_pct` (e.g. > 1.5% per 5-min bar on TSLA)
    TREND_UP    — ADX >= `trend_adx` and EMA_fast > EMA_slow
    TREND_DOWN  — ADX >= `trend_adx` and EMA_fast < EMA_slow
    RANGE       — ADX < `range_adx`
    AMBIGUOUS   — ADX in [range_adx, trend_adx)

    Default thresholds are conservative; tune per instrument.
    """

    def __init__(
        self,
        adx_period: int = 14,
        ema_fast: int = 20,
        ema_slow: int = 50,
        atr_period: int = 14,
        trend_adx: float = 25.0,
        range_adx: float = 20.0,
        high_vol_pct: float = 0.015,   # ATR/price threshold (per-bar volatility)
    ) -> None:
        self.adx = ADX(adx_period)
        self.ema_fast = EMA(ema_fast)
        self.ema_slow = EMA(ema_slow)
        self.atr = ATR(atr_period)
        self.trend_adx = trend_adx
        self.range_adx = range_adx
        self.high_vol_pct = high_vol_pct

    def update(self, bar: Bar) -> Optional[RegimeSnapshot]:
        adx_val = self.adx.update(bar.high, bar.low, bar.close)
        fast = self.ema_fast.update(bar.close)
        slow = self.ema_slow.update(bar.close)
        atr_val = self.atr.update(bar.high, bar.low, bar.close)

        if adx_val is None or fast is None or slow is None or atr_val is None or bar.close <= 0:
            return None

        atr_pct = atr_val / bar.close

        if atr_pct > self.high_vol_pct:
            regime = Regime.HIGH_VOL
        elif adx_val >= self.trend_adx:
            regime = Regime.TREND_UP if fast > slow else Regime.TREND_DOWN
        elif adx_val < self.range_adx:
            regime = Regime.RANGE
        else:
            regime = Regime.AMBIGUOUS

        local_bias = Bias.BULL if fast > slow else Bias.BEAR if fast < slow else Bias.NEUTRAL

        return RegimeSnapshot(
            regime=regime,
            bias=local_bias,
            adx=adx_val,
            atr=atr_val,
            atr_pct=atr_pct,
            ema_fast=fast,
            ema_slow=slow,
        )
