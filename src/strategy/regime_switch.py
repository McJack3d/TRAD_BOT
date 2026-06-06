"""Regime-switching long/short meta-strategy.

Per bar, the regime classifier (`src.strategy.regime`) says TREND,
RANGE, or NEUTRAL. This module runs the matching leg:

  * TREND  → trend-follow with an EMA(fast/slow) cross, long or short.
  * RANGE  → mean-revert at the Bollinger bands with an RSI filter,
             long or short, taking profit at the mid-band.
  * NEUTRAL → no new entries (existing positions are still managed).

Every position carries a hard ATR stop fixed at entry. The exit rule
depends on which leg opened the position (trend flip / regime exit for
trend trades; mid-band target / regime exit for range trades).

`precompute` vectorizes every indicator once; `evaluate_at` then walks
the bar-by-bar state machine in O(1) per bar. `evaluate_live` is the
single-bar path for the live bot. The logic is identical on both paths
— the backtest is therefore a faithful simulation of live behaviour.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

import numpy as np
import pandas as pd

from src.strategy.indicators import atr as atr_indicator
from src.strategy.indicators import bollinger_bands
from src.strategy.indicators import rsi as rsi_indicator
from src.strategy.regime import Regime, RegimeParams, classify_series


class EntryLeg(str, Enum):
    TREND = "trend"
    RANGE = "range"


class Action(str, Enum):
    HOLD = "hold"
    ENTER_LONG = "enter_long"
    ENTER_SHORT = "enter_short"
    EXIT = "exit"


@dataclass(slots=True)
class RegimeSwitchParams:
    # Regime detector (delegated to RegimeParams).
    adx_window: int = 14
    adx_trend_min: float = 25.0
    adx_range_max: float = 20.0
    rv_window: int = 20
    rv_lookback: int = 200
    rv_high_pctile: float = 0.60
    rv_low_pctile: float = 0.40
    # Trend leg.
    ema_fast: int = 21
    ema_slow: int = 55
    # Range leg.
    bb_window: int = 20
    bb_std: float = 2.0
    rsi_window: int = 14
    rsi_os: float = 30.0
    rsi_ob: float = 70.0
    # Sizing / stop.
    atr_window: int = 14
    atr_mult: float = 2.0

    def regime_params(self) -> RegimeParams:
        return RegimeParams(
            adx_window=self.adx_window,
            adx_trend_min=self.adx_trend_min,
            adx_range_max=self.adx_range_max,
            rv_window=self.rv_window,
            rv_lookback=self.rv_lookback,
            rv_high_pctile=self.rv_high_pctile,
            rv_low_pctile=self.rv_low_pctile,
        )


@dataclass(slots=True)
class SwitchPosition:
    """Current position the state machine is managing."""

    side: int = 0  # +1 long, -1 short, 0 flat
    entry_price: float = 0.0
    entry_leg: EntryLeg | None = None
    atr_at_entry: float = 0.0
    entry_index: int = -1
    stop_price: float = 0.0
    qty: float = 0.0  # base-asset units; set by the executor/backtester
    entry_equity: float = 0.0  # account equity at entry; set by executor/backtester

    @classmethod
    def flat(cls) -> SwitchPosition:
        return cls()


@dataclass(slots=True)
class SwitchSignal:
    action: Action
    leg: EntryLeg | None
    reason: str
    stop_price: float = 0.0  # set on ENTER_*; the stop to arm
    exit_at_stop: bool = False  # on EXIT, fill at stop_price not close


@dataclass(slots=True)
class SwitchPrecomputed:
    close: np.ndarray
    high: np.ndarray
    low: np.ndarray
    ema_fast: np.ndarray
    ema_slow: np.ndarray
    bb_lower: np.ndarray
    bb_mid: np.ndarray
    bb_upper: np.ndarray
    rsi: np.ndarray
    atr: np.ndarray
    regime: np.ndarray  # object array of Regime
    adx: np.ndarray
    rv_pct: np.ndarray


def precompute(
    df: pd.DataFrame, params: RegimeSwitchParams | None = None
) -> SwitchPrecomputed:
    """Vectorize all indicators once. `df` needs columns
    high/low/close (open/volume optional)."""
    p = params or RegimeSwitchParams()
    high, low, close = df["high"], df["low"], df["close"]
    ema_fast = close.ewm(span=p.ema_fast, adjust=False).mean()
    ema_slow = close.ewm(span=p.ema_slow, adjust=False).mean()
    bb = bollinger_bands(close, window=p.bb_window, num_std=p.bb_std)
    rsi_s = rsi_indicator(close, window=p.rsi_window)
    atr_s = atr_indicator(high, low, close, window=p.atr_window)
    reg = classify_series(high, low, close, p.regime_params())
    # copy=True so the returned arrays are writable and don't alias the
    # pandas frames' memory.
    return SwitchPrecomputed(
        close=close.to_numpy(dtype=float, copy=True),
        high=high.to_numpy(dtype=float, copy=True),
        low=low.to_numpy(dtype=float, copy=True),
        ema_fast=ema_fast.to_numpy(dtype=float, copy=True),
        ema_slow=ema_slow.to_numpy(dtype=float, copy=True),
        bb_lower=bb.lower.to_numpy(dtype=float, copy=True),
        bb_mid=bb.middle.to_numpy(dtype=float, copy=True),
        bb_upper=bb.upper.to_numpy(dtype=float, copy=True),
        rsi=rsi_s.to_numpy(dtype=float, copy=True),
        atr=atr_s.to_numpy(dtype=float, copy=True),
        regime=reg.regime.to_numpy(copy=True),
        adx=reg.adx.to_numpy(dtype=float, copy=True),
        rv_pct=reg.rv_pct.to_numpy(dtype=float, copy=True),
    )


def _warm(pre: SwitchPrecomputed, i: int) -> bool:
    """Are all indicators defined at bar i?"""
    return not (
        math.isnan(pre.ema_slow[i])
        or math.isnan(pre.bb_lower[i])
        or math.isnan(pre.rsi[i])
        or math.isnan(pre.atr[i])
    )


def evaluate_at(
    pre: SwitchPrecomputed, i: int, pos: SwitchPosition, params: RegimeSwitchParams
) -> SwitchSignal:
    """The pure state machine for bar `i` given the current position."""
    p = params
    if not _warm(pre, i):
        return SwitchSignal(Action.HOLD, None, "indicators warming up")

    close = pre.close[i]
    regime = pre.regime[i]
    atr_now = pre.atr[i]

    # ---- managing an open position -----------------------------------
    if pos.side != 0:
        # Hard ATR stop checked intrabar (low for longs, high for shorts).
        if pos.side == 1 and pre.low[i] <= pos.stop_price:
            return SwitchSignal(
                Action.EXIT, pos.entry_leg,
                f"stop hit: low {pre.low[i]:.2f} <= stop {pos.stop_price:.2f}",
                exit_at_stop=True,
            )
        if pos.side == -1 and pre.high[i] >= pos.stop_price:
            return SwitchSignal(
                Action.EXIT, pos.entry_leg,
                f"stop hit: high {pre.high[i]:.2f} >= stop {pos.stop_price:.2f}",
                exit_at_stop=True,
            )

        if pos.entry_leg == EntryLeg.TREND:
            # Exit on EMA flip against us OR regime leaving TREND.
            if pos.side == 1 and (regime != Regime.TREND or pre.ema_fast[i] < pre.ema_slow[i]):
                return SwitchSignal(Action.EXIT, EntryLeg.TREND, "trend long exit (flip/regime)")
            if pos.side == -1 and (regime != Regime.TREND or pre.ema_fast[i] > pre.ema_slow[i]):
                return SwitchSignal(Action.EXIT, EntryLeg.TREND, "trend short exit (flip/regime)")
        else:  # RANGE leg
            if pos.side == 1 and close >= pre.bb_mid[i]:
                return SwitchSignal(Action.EXIT, EntryLeg.RANGE, "range long target (mid-band)")
            if pos.side == -1 and close <= pre.bb_mid[i]:
                return SwitchSignal(Action.EXIT, EntryLeg.RANGE, "range short target (mid-band)")
            if regime != Regime.RANGE:
                return SwitchSignal(Action.EXIT, EntryLeg.RANGE, "range exit (regime left RANGE)")
        return SwitchSignal(Action.HOLD, pos.entry_leg, "hold")

    # ---- flat: look for an entry -------------------------------------
    if regime == Regime.TREND:
        up = pre.ema_fast[i] > pre.ema_slow[i] and close > pre.ema_slow[i]
        down = pre.ema_fast[i] < pre.ema_slow[i] and close < pre.ema_slow[i]
        if up:
            return SwitchSignal(
                Action.ENTER_LONG, EntryLeg.TREND,
                f"trend long: EMA{p.ema_fast}>{p.ema_slow}, close>{pre.ema_slow[i]:.2f}",
                stop_price=close - p.atr_mult * atr_now,
            )
        if down:
            return SwitchSignal(
                Action.ENTER_SHORT, EntryLeg.TREND,
                f"trend short: EMA{p.ema_fast}<{p.ema_slow}, close<{pre.ema_slow[i]:.2f}",
                stop_price=close + p.atr_mult * atr_now,
            )
        return SwitchSignal(Action.HOLD, None, "trend regime but EMAs not aligned")

    if regime == Regime.RANGE:
        if close < pre.bb_lower[i] and pre.rsi[i] < p.rsi_os:
            return SwitchSignal(
                Action.ENTER_LONG, EntryLeg.RANGE,
                f"range long: close<lowerBB {pre.bb_lower[i]:.2f}, RSI {pre.rsi[i]:.1f}",
                stop_price=close - p.atr_mult * atr_now,
            )
        if close > pre.bb_upper[i] and pre.rsi[i] > p.rsi_ob:
            return SwitchSignal(
                Action.ENTER_SHORT, EntryLeg.RANGE,
                f"range short: close>upperBB {pre.bb_upper[i]:.2f}, RSI {pre.rsi[i]:.1f}",
                stop_price=close + p.atr_mult * atr_now,
            )
        return SwitchSignal(Action.HOLD, None, "range regime but no band touch")

    return SwitchSignal(Action.HOLD, None, "neutral regime — stand aside")


def open_from_signal(
    sig: SwitchSignal, pre: SwitchPrecomputed, i: int, fill_price: float
) -> SwitchPosition:
    """Build the SwitchPosition that results from acting on an ENTER_*."""
    side = 1 if sig.action == Action.ENTER_LONG else -1
    return SwitchPosition(
        side=side,
        entry_price=fill_price,
        entry_leg=sig.leg,
        atr_at_entry=pre.atr[i],
        entry_index=i,
        stop_price=sig.stop_price,
    )


def evaluate_live(
    df: pd.DataFrame,
    pos: SwitchPosition,
    params: RegimeSwitchParams | None = None,
) -> SwitchSignal:
    """Single-bar path for the live bot: compute indicators on the spot
    and evaluate the most recent bar. Use `precompute` + `evaluate_at`
    for backtests."""
    p = params or RegimeSwitchParams()
    needed = max(p.ema_slow, p.bb_window, p.rsi_window, p.atr_window, p.rv_lookback) + 5
    if len(df) < needed:
        return SwitchSignal(Action.HOLD, None, f"need {needed} bars, have {len(df)}")
    pre = precompute(df, p)
    return evaluate_at(pre, len(df) - 1, pos, p)
