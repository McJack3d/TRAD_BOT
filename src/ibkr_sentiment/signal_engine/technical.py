"""Technical confirmation indicators.

Pure functions over arrays of bar closes. Used by the signal engine to
require that price action *agrees* with the sentiment signal before any
order is placed.

Two indicators are enough for the strategy as specified:

  * SMA   — long signals need close > SMA, short signals need close < SMA.
  * RSI   — block long entries when RSI is too low (sign of a collapse
            in progress) and short entries when RSI is too high (squeeze
            risk).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal


def _to_float_list(values: Sequence) -> list[float]:
    return [float(v) for v in values]


def simple_moving_average(closes: Sequence, window: int) -> float | None:
    """Return SMA(window) of the most recent `window` closes, or None
    if not enough data."""
    arr = _to_float_list(closes)
    if len(arr) < window or window <= 0:
        return None
    return sum(arr[-window:]) / window


def relative_strength_index(closes: Sequence, window: int = 14) -> float | None:
    """Wilder's RSI on the most recent `window`+1 closes.

    Returns a value in [0, 100], or None if there isn't enough data.
    """
    arr = _to_float_list(closes)
    if len(arr) < window + 1 or window <= 0:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for prev, cur in zip(arr[-(window + 1) : -1], arr[-window:], strict=True):
        diff = cur - prev
        if diff >= 0:
            gains.append(diff)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(-diff)
    avg_gain = sum(gains) / window
    avg_loss = sum(losses) / window
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


@dataclass(slots=True)
class TechnicalSnapshot:
    """Computed values for a single symbol at evaluation time."""

    symbol: str
    last_close: Decimal
    sma: Decimal | None
    rsi: float | None


def evaluate_technicals(
    symbol: str,
    closes: Sequence,
    sma_window: int,
    rsi_window: int,
) -> TechnicalSnapshot:
    arr = _to_float_list(closes)
    last = Decimal(str(arr[-1])) if arr else Decimal("0")
    sma_val = simple_moving_average(arr, sma_window)
    rsi_val = relative_strength_index(arr, rsi_window)
    return TechnicalSnapshot(
        symbol=symbol,
        last_close=last,
        sma=Decimal(str(sma_val)) if sma_val is not None else None,
        rsi=rsi_val,
    )


@dataclass(slots=True)
class TechnicalCheck:
    ok: bool
    reason: str


def long_technical_ok(
    snap: TechnicalSnapshot,
    *,
    sma_confirm_pct: float,
    rsi_long_min: float,
    required: bool,
) -> TechnicalCheck:
    """Pass criteria for taking a long position."""
    if not required:
        return TechnicalCheck(True, "technical confirmation disabled")
    if snap.sma is None:
        return TechnicalCheck(False, "not enough bars for SMA")
    threshold = snap.sma * (Decimal("1") + Decimal(str(sma_confirm_pct)))
    if snap.last_close <= threshold:
        return TechnicalCheck(
            False,
            f"close {snap.last_close} <= SMA*(1+{sma_confirm_pct}) {threshold}",
        )
    if snap.rsi is not None and snap.rsi < rsi_long_min:
        return TechnicalCheck(
            False, f"RSI {snap.rsi:.1f} < long floor {rsi_long_min:.1f}"
        )
    return TechnicalCheck(True, "long technicals confirmed")


def short_technical_ok(
    snap: TechnicalSnapshot,
    *,
    sma_confirm_pct: float,
    rsi_short_max: float,
    required: bool,
) -> TechnicalCheck:
    """Pass criteria for taking a short position."""
    if not required:
        return TechnicalCheck(True, "technical confirmation disabled")
    if snap.sma is None:
        return TechnicalCheck(False, "not enough bars for SMA")
    threshold = snap.sma * (Decimal("1") - Decimal(str(sma_confirm_pct)))
    if snap.last_close >= threshold:
        return TechnicalCheck(
            False,
            f"close {snap.last_close} >= SMA*(1-{sma_confirm_pct}) {threshold}",
        )
    if snap.rsi is not None and snap.rsi > rsi_short_max:
        return TechnicalCheck(
            False, f"RSI {snap.rsi:.1f} > short ceiling {rsi_short_max:.1f}"
        )
    return TechnicalCheck(True, "short technicals confirmed")
