"""
Per-regime entry/exit signal generator.

Entry rules
-----------
TREND_UP   (HTF bias != BEAR):  pullback to EMA_fast + RSI in [30, 55]  → LONG
TREND_DOWN (HTF bias != BULL):  rally to EMA_fast + RSI in [45, 70]     → SHORT
RANGE:                          BB-lower touch + RSI < 30                → LONG
                                BB-upper touch + RSI > 70                → SHORT
HIGH_VOL / AMBIGUOUS / WARMUP:  no entries

Exit rules
----------
* Stop loss   = entry ± stop_atr_mult × ATR
* Take profit = entry ± target_atr_mult × ATR  (asymmetric R:R)
* Time stop   = close after max_bars_held bars  (avoid lingering)
* Regime flip = close immediately if regime flips to HIGH_VOL or opposite trend

These are deliberate, defensible rules — not parameter-overfit to TSLA history.
Tune `stop_atr_mult`, `target_atr_mult`, and `max_bars_held` on out-of-sample data.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from strategies.regime_adaptive.bars import Bar
from strategies.regime_adaptive.indicators import RSI, Bollinger
from strategies.regime_adaptive.regime import Bias, Regime, RegimeSnapshot


class Action(str, Enum):
    NONE = "NONE"
    ENTER_LONG = "ENTER_LONG"
    ENTER_SHORT = "ENTER_SHORT"
    EXIT = "EXIT"


class ShortMode(str, Enum):
    """How aggressively to take short entries.

    CONFIDENT  — Default. Shorts require HTF bias == BEAR (strict).
                 Longs require HTF != BEAR (current behavior).
                 Reflects the structural long bias of equities.
    SYMMETRIC  — Shorts and longs use mirrored rules (HTF only needs to not
                 disagree). More short entries, lower per-entry conviction.
    OFF        — No short entries at all. Longs only.
    """
    CONFIDENT = "CONFIDENT"
    SYMMETRIC = "SYMMETRIC"
    OFF = "OFF"


@dataclass
class Decision:
    """Single decision emitted on a closed bar."""

    action: Action
    reason: str = ""
    stop_price: Optional[float] = None
    target_price: Optional[float] = None


@dataclass
class SignalConfig:
    rsi_period: int = 14
    bb_period: int = 20
    bb_std: float = 2.0

    # Trend pullback entries
    trend_rsi_long_lo: float = 30.0
    trend_rsi_long_hi: float = 55.0
    trend_rsi_short_lo: float = 45.0
    trend_rsi_short_hi: float = 70.0
    trend_pullback_pct: float = 0.005    # within 0.5% of EMA_fast

    # Mean-reversion entries
    range_rsi_oversold: float = 30.0
    range_rsi_overbought: float = 70.0

    # Risk management
    stop_atr_mult: float = 1.5
    target_atr_mult: float = 2.5
    max_bars_held: int = 24              # 2 hours on 5-min bars

    # Short-side aggressiveness — see ShortMode docstring.
    short_mode: "ShortMode" = None       # set in __post_init__ → CONFIDENT

    def __post_init__(self) -> None:
        if self.short_mode is None:
            self.short_mode = ShortMode.CONFIDENT


class SignalGenerator:
    """
    Produces buy / sell / exit Decisions from closed bars + regime context.

    Holds RSI / Bollinger state internally; you only need to feed it bars and
    the latest RegimeSnapshot per timeframe.
    """

    def __init__(self, config: Optional[SignalConfig] = None) -> None:
        self.cfg = config or SignalConfig()
        self.rsi = RSI(self.cfg.rsi_period)
        self.bb = Bollinger(self.cfg.bb_period, self.cfg.bb_std)
        self.last_rsi: Optional[float] = None
        self.last_bb: Optional[tuple[float, float, float]] = None

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def update(
        self,
        bar: Bar,
        regime: RegimeSnapshot,
        htf_bias: Bias,
        in_position: bool,
        position_side: Optional[Action],
        bars_held: int,
        stop_price: Optional[float],
        target_price: Optional[float],
        entry_regime: Optional[Regime],
    ) -> Decision:
        self.last_rsi = self.rsi.update(bar.close)
        self.last_bb = self.bb.update(bar.close)

        # Need full indicator warmup before any decision
        if self.last_rsi is None or self.last_bb is None:
            return Decision(Action.NONE, reason="warmup")

        if in_position:
            return self._exit_decision(
                bar, regime, position_side, bars_held,
                stop_price, target_price, entry_regime,
            )
        return self._entry_decision(bar, regime, htf_bias)

    # ------------------------------------------------------------------
    # Short-side gating
    # ------------------------------------------------------------------

    def _short_allowed(self, htf_bias: Bias) -> bool:
        """
        Whether a short entry passes the HTF conviction check for the
        configured short_mode.
        """
        mode = self.cfg.short_mode
        if mode == ShortMode.OFF:
            return False
        if mode == ShortMode.CONFIDENT:
            return htf_bias == Bias.BEAR        # strict: must be explicitly bearish
        # SYMMETRIC — the legacy behavior: just don't fight a clear uptrend
        return htf_bias != Bias.BULL

    # ------------------------------------------------------------------
    # Entry
    # ------------------------------------------------------------------

    def _entry_decision(
        self,
        bar: Bar,
        regime: RegimeSnapshot,
        htf_bias: Bias,
    ) -> Decision:
        c = self.cfg
        rsi = self.last_rsi
        _, bb_up, bb_lo = self.last_bb

        # No new entries in adverse regimes
        if regime.regime in (Regime.HIGH_VOL, Regime.AMBIGUOUS, Regime.WARMUP):
            return Decision(Action.NONE, reason=f"no-entry regime={regime.regime.value}")

        # Trend regimes — require HTF agreement
        if regime.regime == Regime.TREND_UP:
            if htf_bias == Bias.BEAR:
                return Decision(Action.NONE, reason="HTF bearish against TREND_UP")

            near_ema = abs(bar.close - regime.ema_fast) / regime.ema_fast <= c.trend_pullback_pct
            if near_ema and c.trend_rsi_long_lo <= rsi <= c.trend_rsi_long_hi:
                stop = bar.close - c.stop_atr_mult * regime.atr
                target = bar.close + c.target_atr_mult * regime.atr
                return Decision(
                    Action.ENTER_LONG,
                    reason=f"pullback to EMA in uptrend (rsi={rsi:.1f})",
                    stop_price=stop,
                    target_price=target,
                )

        if regime.regime == Regime.TREND_DOWN:
            if not self._short_allowed(htf_bias):
                return Decision(
                    Action.NONE,
                    reason=f"short gated by mode={c.short_mode.value} bias={htf_bias.value}",
                )

            near_ema = abs(bar.close - regime.ema_fast) / regime.ema_fast <= c.trend_pullback_pct
            if near_ema and c.trend_rsi_short_lo <= rsi <= c.trend_rsi_short_hi:
                stop = bar.close + c.stop_atr_mult * regime.atr
                target = bar.close - c.target_atr_mult * regime.atr
                return Decision(
                    Action.ENTER_SHORT,
                    reason=f"rally to EMA in downtrend (rsi={rsi:.1f})",
                    stop_price=stop,
                    target_price=target,
                )

        # Range — mean reversion
        if regime.regime == Regime.RANGE:
            if bar.close <= bb_lo and rsi < c.range_rsi_oversold:
                stop = bar.close - c.stop_atr_mult * regime.atr
                target = bar.close + c.target_atr_mult * regime.atr
                return Decision(
                    Action.ENTER_LONG,
                    reason=f"BB lower touch in range (rsi={rsi:.1f})",
                    stop_price=stop,
                    target_price=target,
                )
            if bar.close >= bb_up and rsi > c.range_rsi_overbought:
                if not self._short_allowed(htf_bias):
                    return Decision(
                        Action.NONE,
                        reason=f"short gated by mode={c.short_mode.value} bias={htf_bias.value}",
                    )
                stop = bar.close + c.stop_atr_mult * regime.atr
                target = bar.close - c.target_atr_mult * regime.atr
                return Decision(
                    Action.ENTER_SHORT,
                    reason=f"BB upper touch in range (rsi={rsi:.1f})",
                    stop_price=stop,
                    target_price=target,
                )

        return Decision(Action.NONE, reason=f"no-trigger regime={regime.regime.value}")

    # ------------------------------------------------------------------
    # Exit
    # ------------------------------------------------------------------

    def _exit_decision(
        self,
        bar: Bar,
        regime: RegimeSnapshot,
        position_side: Optional[Action],
        bars_held: int,
        stop_price: Optional[float],
        target_price: Optional[float],
        entry_regime: Optional[Regime],
    ) -> Decision:
        c = self.cfg

        # Hard stop / target — use bar high/low so wicks count
        if position_side == Action.ENTER_LONG:
            if stop_price is not None and bar.low <= stop_price:
                return Decision(Action.EXIT, reason=f"stop hit @ {stop_price:.2f}")
            if target_price is not None and bar.high >= target_price:
                return Decision(Action.EXIT, reason=f"target hit @ {target_price:.2f}")
        elif position_side == Action.ENTER_SHORT:
            if stop_price is not None and bar.high >= stop_price:
                return Decision(Action.EXIT, reason=f"stop hit @ {stop_price:.2f}")
            if target_price is not None and bar.low <= target_price:
                return Decision(Action.EXIT, reason=f"target hit @ {target_price:.2f}")

        # Regime flip — exit defensively
        if regime.regime == Regime.HIGH_VOL:
            return Decision(Action.EXIT, reason="regime flipped to HIGH_VOL")

        if position_side == Action.ENTER_LONG and regime.regime == Regime.TREND_DOWN:
            return Decision(Action.EXIT, reason="regime flipped to TREND_DOWN against long")
        if position_side == Action.ENTER_SHORT and regime.regime == Regime.TREND_UP:
            return Decision(Action.EXIT, reason="regime flipped to TREND_UP against short")

        # Time stop
        if bars_held >= c.max_bars_held:
            return Decision(Action.EXIT, reason=f"time stop ({bars_held} bars)")

        return Decision(Action.NONE, reason="hold")
