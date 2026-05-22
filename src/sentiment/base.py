"""Sentiment source abstraction.

A sentiment source produces a normalized factor in [-1, +1]:
  +1  strongly bullish
   0  neutral
  -1  strongly bearish

The trend strategy uses the factor to *tilt* its SMA entry/exit
thresholds — bullish sentiment lets it enter a bit earlier and hold a
bit longer; bearish sentiment makes it demand more confirmation. The
factor never overrides the SMA on its own; it only nudges the bar.

Any source (Fear & Greed index, funding rates, an LLM scoring news
headlines) can implement this interface. Fear & Greed is the default
because it is free and has history back to 2018, so it can be
backtested. A source that can't be backtested (live news) should be
treated with much more suspicion.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(slots=True)
class SentimentReading:
    value: float  # normalized [-1, +1]; + bullish, - bearish
    label: str  # human-readable, e.g. "Extreme Fear"
    raw: float  # the source's native value (e.g. 0-100 for Fear & Greed)
    source: str  # identifier, e.g. "fear_greed"
    ts: datetime


class SentimentSource(Protocol):
    async def current(self) -> SentimentReading:
        """Return the latest sentiment reading."""
        ...


def clamp_factor(value: float) -> float:
    """Clamp a raw factor into the valid [-1, +1] range."""
    return max(-1.0, min(1.0, value))
