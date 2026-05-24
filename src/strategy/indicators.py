"""Technical indicators: Bollinger Bands, RSI, MACD.

Pure pandas — no TA-Lib (avoids the C-extension build pain). Each
function takes a Series of closes most-recent-last and returns
same-indexed Series. Functions are stateless: the same input always
produces the same output, which makes them trivial to backtest.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(slots=True)
class BollingerBands:
    lower: pd.Series
    middle: pd.Series  # the SMA itself
    upper: pd.Series
    width: pd.Series  # (upper - lower) / middle


def bollinger_bands(
    closes: pd.Series, window: int = 20, num_std: float = 2.0
) -> BollingerBands:
    """Classic Bollinger Bands: SMA ± num_std * rolling stdev.

    Width is normalized by the middle band so it's comparable across
    price levels (a $200 spread at $20k is wildly different from $200
    at $200).
    """
    middle = closes.rolling(window).mean()
    # ddof=0 matches the population stdev convention most TA tools use.
    std = closes.rolling(window).std(ddof=0)
    upper = middle + num_std * std
    lower = middle - num_std * std
    width = (upper - lower) / middle
    return BollingerBands(lower=lower, middle=middle, upper=upper, width=width)


def rsi(closes: pd.Series, window: int = 14) -> pd.Series:
    """Wilder's RSI.

    First N values use a simple average of gains/losses to seed; after
    that the average updates as `prev * (window-1)/window + new/window`
    — the recursive Wilder smoothing, equivalent to EMA with
    alpha = 1/window.
    """
    delta = closes.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    # Wilder smoothing is equivalent to ewm with alpha=1/window.
    avg_gain = gain.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()
    avg_loss = loss.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    # When avg_loss is 0 the result is undefined; conventionally 100
    # (no losses → max momentum). When both are 0 (flatline) NaN is fine.
    out = out.where(~(avg_loss == 0) | (avg_gain == 0), 100.0)
    return out


@dataclass(slots=True)
class MACD:
    line: pd.Series  # EMA(fast) - EMA(slow)
    signal: pd.Series  # EMA(line, signal_window)
    histogram: pd.Series  # line - signal


def macd(
    closes: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal_window: int = 9,
) -> MACD:
    """Standard MACD with the 12/26/9 defaults Gerald Appel popularized."""
    ema_fast = closes.ewm(span=fast, adjust=False).mean()
    ema_slow = closes.ewm(span=slow, adjust=False).mean()
    line = ema_fast - ema_slow
    sig = line.ewm(span=signal_window, adjust=False).mean()
    hist = line - sig
    return MACD(line=line, signal=sig, histogram=hist)
