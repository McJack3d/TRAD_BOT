"""SMA trend-following signal.

Pure function over a series of daily closes:
- close > SMA(window) → state IN (hold BTC)
- close <= SMA(window) → state OUT (hold USDT)

No leverage, no shorting, no funding settlements. Signal evaluates on
daily bars so missing an evaluation by a few hours costs nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

import pandas as pd


class TrendState(str, Enum):
    IN = "in"
    OUT = "out"


@dataclass(slots=True)
class TrendSignal:
    state: TrendState
    close: Decimal
    sma: Decimal
    reason: str


def evaluate_trend(daily_closes: pd.Series, sma_window: int = 50) -> TrendSignal:
    """Return the trend state for the most recent close in `daily_closes`.

    `daily_closes` is most-recent-last. If fewer than `sma_window` bars
    are available, return OUT — the SMA isn't defined yet so we stay in
    cash rather than guess.
    """
    if len(daily_closes) < sma_window:
        return TrendSignal(
            state=TrendState.OUT,
            close=Decimal("0"),
            sma=Decimal("0"),
            reason=f"need {sma_window} closes, have {len(daily_closes)}",
        )
    sma = float(daily_closes.rolling(sma_window).mean().iloc[-1])
    last = float(daily_closes.iloc[-1])
    if last > sma:
        return TrendSignal(
            state=TrendState.IN,
            close=Decimal(str(last)),
            sma=Decimal(str(sma)),
            reason=f"close {last:.2f} > SMA{sma_window} {sma:.2f}",
        )
    return TrendSignal(
        state=TrendState.OUT,
        close=Decimal(str(last)),
        sma=Decimal(str(sma)),
        reason=f"close {last:.2f} <= SMA{sma_window} {sma:.2f}",
    )
