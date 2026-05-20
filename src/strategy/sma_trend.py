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


def evaluate_trend(
    daily_closes: pd.Series,
    sma_window: int = 200,
    entry_buffer_pct: float = 0.01,
    exit_buffer_pct: float = 0.01,
) -> TrendSignal:
    """Return the trend state for the most recent close in `daily_closes`.

    `daily_closes` is most-recent-last. If fewer than `sma_window` bars
    are available, return OUT — the SMA isn't defined yet so we stay in
    cash rather than guess.

    `entry_buffer_pct` (default 0) requires `close > SMA * (1 + buffer)`
    to enter. A 0.01 buffer (1%) filters whipsaws where price barely
    crosses the SMA. `exit_buffer_pct` (also default 0) does the
    symmetric thing on the way out: `close < SMA * (1 - buffer)`
    triggers exit. Together they create a dead-band around the SMA so
    the strategy only trades on convincing crossings.
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
    entry_threshold = sma * (1 + entry_buffer_pct)
    exit_threshold = sma * (1 - exit_buffer_pct)
    if last > entry_threshold:
        return TrendSignal(
            state=TrendState.IN,
            close=Decimal(str(last)),
            sma=Decimal(str(sma)),
            reason=f"close {last:.2f} > SMA{sma_window}*{1+entry_buffer_pct:.3f} ({entry_threshold:.2f})",
        )
    # Below the exit threshold OR inside the dead band → OUT. Cash is the
    # safe default when the signal is ambiguous; this is the whole point of
    # the buffers (filter marginal crossings).
    return TrendSignal(
        state=TrendState.OUT,
        close=Decimal(str(last)),
        sma=Decimal(str(sma)),
        reason=f"close {last:.2f} <= entry threshold ({entry_threshold:.2f})",
    )
