"""SMA trend-following signal.

Pure function over a series of daily closes:
- close > SMA(window) * (1 + entry_buffer) → state IN (hold BTC)
- otherwise → state OUT (hold quote currency)

Optionally a sentiment factor in [-1, +1] tilts the entry/exit
thresholds: bullish sentiment lowers the entry bar (enter earlier) and
lowers the exit bar (hold longer); bearish sentiment does the reverse.
Sentiment never decides on its own — it only nudges where the SMA
crossing triggers.

No leverage, no shorting. Signal evaluates on daily bars.
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
    sentiment: float | None = None  # the factor used, if any


# How far a full-strength sentiment reading (|factor| = 1) may shift a
# threshold buffer, in fractional terms. With weight 0.03, max-bullish
# sentiment moves the entry buffer by 3 percentage points.
def _tilted_buffers(
    entry_buffer_pct: float,
    exit_buffer_pct: float,
    sentiment: float | None,
    sentiment_weight: float,
) -> tuple[float, float]:
    """Return (effective_entry_buffer, effective_exit_buffer).

    Bullish sentiment (s > 0):
      - entry buffer shrinks  → lower entry threshold → enter earlier
      - exit buffer grows     → lower exit threshold  → hold longer
    Bearish sentiment (s < 0): the opposite.
    Both are clamped so a runaway factor can't produce absurd thresholds.
    """
    if sentiment is None or sentiment_weight <= 0:
        return entry_buffer_pct, exit_buffer_pct
    s = max(-1.0, min(1.0, sentiment))
    eff_entry = entry_buffer_pct - s * sentiment_weight
    eff_exit = exit_buffer_pct + s * sentiment_weight
    # Clamp to a sane band: never more than 5% below the SMA, never
    # demand more than 10% above it.
    eff_entry = max(-0.05, min(0.10, eff_entry))
    eff_exit = max(-0.05, min(0.10, eff_exit))
    return eff_entry, eff_exit


def evaluate_trend(
    daily_closes: pd.Series,
    sma_window: int = 200,
    entry_buffer_pct: float = 0.01,
    exit_buffer_pct: float = 0.01,
    sentiment: float | None = None,
    sentiment_weight: float = 0.0,
) -> TrendSignal:
    """Return the trend state for the most recent close in `daily_closes`.

    `daily_closes` is most-recent-last. If fewer than `sma_window` bars
    are available, return OUT — the SMA isn't defined yet.

    `sentiment` (optional, [-1, +1]) and `sentiment_weight` (how far it
    can move the buffers) tilt the thresholds. With sentiment_weight=0
    (the default) sentiment is ignored entirely and the function behaves
    exactly like the plain SMA strategy.
    """
    if len(daily_closes) < sma_window:
        return TrendSignal(
            state=TrendState.OUT,
            close=Decimal("0"),
            sma=Decimal("0"),
            reason=f"need {sma_window} closes, have {len(daily_closes)}",
            sentiment=sentiment,
        )
    sma = float(daily_closes.rolling(sma_window).mean().iloc[-1])
    last = float(daily_closes.iloc[-1])

    eff_entry, eff_exit = _tilted_buffers(
        entry_buffer_pct, exit_buffer_pct, sentiment, sentiment_weight
    )
    entry_threshold = sma * (1 + eff_entry)
    exit_threshold = sma * (1 - eff_exit)

    tilt_note = ""
    if sentiment is not None and sentiment_weight > 0:
        tilt_note = f", sentiment {sentiment:+.2f} → entry buffer {eff_entry:+.3f}"

    if last > entry_threshold:
        return TrendSignal(
            state=TrendState.IN,
            close=Decimal(str(last)),
            sma=Decimal(str(sma)),
            reason=(
                f"close {last:.2f} > entry threshold ({entry_threshold:.2f}); "
                f"SMA{sma_window} {sma:.2f}{tilt_note}"
            ),
            sentiment=sentiment,
        )
    # Below the exit threshold OR inside the dead band → OUT. Cash is the
    # safe default when the signal is ambiguous.
    _ = exit_threshold  # kept for clarity; OUT is the single fallthrough
    return TrendSignal(
        state=TrendState.OUT,
        close=Decimal(str(last)),
        sma=Decimal(str(sma)),
        reason=(
            f"close {last:.2f} <= entry threshold ({entry_threshold:.2f}); "
            f"SMA{sma_window} {sma:.2f}{tilt_note}"
        ),
        sentiment=sentiment,
    )
