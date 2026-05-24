"""BB-squeeze + RSI mean reversion strategy for intraday bars.

The setup (per the user's spec):

1. Trend filter — Bollinger Band Width. If the bands are too narrow
   the market is dead; skip entries. We require BBW above a rolling
   percentile of recent BBW values, so the filter adapts to whichever
   asset/regime we're trading.

2. Entry — A 5-minute candle closes BELOW the lower Bollinger Band
   while RSI is below 25 (oversold). That bar arms the setup; we
   enter on the next bar that closes back ABOVE the lower band
   (i.e. price has crossed back inside). The setup expires after
   `setup_expiry_bars` bars if no trigger comes.

3. Exit — close the position when MACD histogram crosses back above
   zero (downside momentum exhausted) OR price reaches the middle BB
   (mean-reversion target hit), whichever comes first.

No shorting. Long-only mean reversion in an active-volatility regime.

The function is stateless — it takes a full bar history plus the
current bot position and returns the action for the most recent bar.
The caller is responsible for tracking which bar was the "arm" bar
and feeding it back in via `armed_at_index`.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

import pandas as pd

from src.strategy.indicators import bollinger_bands, macd, rsi


class SqueezeState(str, Enum):
    FLAT = "flat"  # no position, not armed
    ARMED = "armed"  # saw a setup bar; waiting for trigger
    LONG = "long"  # in position


class SqueezeAction(str, Enum):
    HOLD = "hold"
    ARM = "arm"  # transition FLAT → ARMED
    DISARM = "disarm"  # transition ARMED → FLAT (expired)
    BUY = "buy"  # transition ARMED → LONG (and pin entry_bar_index)
    SELL = "sell"  # transition LONG → FLAT


@dataclass(slots=True)
class SqueezeSignal:
    action: SqueezeAction
    state_after: SqueezeState
    close: Decimal
    bb_lower: Decimal
    bb_middle: Decimal
    bb_width: Decimal
    rsi: Decimal
    macd_hist: Decimal
    reason: str


@dataclass(slots=True)
class SqueezeParams:
    bb_window: int = 20
    bb_num_std: float = 2.0
    rsi_window: int = 14
    rsi_entry_max: float = 25.0
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    # BBW must be at or above the `min_bbw_percentile`-th percentile of
    # the last `bbw_lookback` bars' BBW values. 0 = no filter.
    bbw_lookback: int = 100
    min_bbw_percentile: float = 30.0
    # After arming, the setup expires if no trigger fires within this many bars.
    setup_expiry_bars: int = 6


def _enough_history(closes: pd.Series, p: SqueezeParams) -> bool:
    # We need enough bars for the slowest indicator (MACD slow EMA + signal)
    # plus a few for the BBW percentile to be meaningful.
    needed = max(p.macd_slow + p.macd_signal, p.bb_window, p.rsi_window) + 5
    return len(closes) >= needed


def _bbw_filter_pass(width: pd.Series, p: SqueezeParams) -> bool:
    """True if current BBW is at/above the recent percentile floor."""
    if p.min_bbw_percentile <= 0:
        return True
    recent = width.iloc[-p.bbw_lookback :].dropna()
    if len(recent) < 10:
        return True  # not enough history for a meaningful percentile
    threshold = float(recent.quantile(p.min_bbw_percentile / 100.0))
    current = float(width.iloc[-1])
    return current >= threshold


def evaluate_bb_squeeze(
    closes: pd.Series,
    state: SqueezeState,
    armed_at_index: int | None,
    entry_bar_index: int | None,
    params: SqueezeParams | None = None,
) -> SqueezeSignal:
    """Decide the action for the bar at `closes.iloc[-1]`.

    `state` is the bot's current state going into this bar.
    `armed_at_index` is the integer index of the bar that armed us
    (only meaningful when state == ARMED). `entry_bar_index` is the
    integer index of the bar we entered on (only meaningful when
    state == LONG). The CALLER persists these between bars.

    Returns a `SqueezeSignal` describing the action and the state we
    end up in after acting on this bar.
    """
    p = params or SqueezeParams()
    last_close = Decimal(str(closes.iloc[-1]))

    if not _enough_history(closes, p):
        return SqueezeSignal(
            action=SqueezeAction.HOLD,
            state_after=state,
            close=last_close,
            bb_lower=Decimal("0"),
            bb_middle=Decimal("0"),
            bb_width=Decimal("0"),
            rsi=Decimal("0"),
            macd_hist=Decimal("0"),
            reason=f"need {p.macd_slow + p.macd_signal + 5} bars, have {len(closes)}",
        )

    bb = bollinger_bands(closes, window=p.bb_window, num_std=p.bb_num_std)
    rsi_series = rsi(closes, window=p.rsi_window)
    mac = macd(closes, fast=p.macd_fast, slow=p.macd_slow, signal_window=p.macd_signal)

    lower = float(bb.lower.iloc[-1])
    middle = float(bb.middle.iloc[-1])
    width = float(bb.width.iloc[-1])
    rsi_now = float(rsi_series.iloc[-1])
    hist_now = float(mac.histogram.iloc[-1])
    hist_prev = float(mac.histogram.iloc[-2]) if len(mac.histogram) >= 2 else hist_now
    close_now = float(closes.iloc[-1])
    current_index = len(closes) - 1

    base = dict(
        close=last_close,
        bb_lower=Decimal(str(lower)),
        bb_middle=Decimal(str(middle)),
        bb_width=Decimal(str(width)),
        rsi=Decimal(str(rsi_now)),
        macd_hist=Decimal(str(hist_now)),
    )

    if state == SqueezeState.LONG:
        # Exit conditions: MACD hist crosses ↑0, or price reaches middle band.
        crossed_up = hist_prev <= 0.0 < hist_now
        hit_middle = close_now >= middle
        if crossed_up or hit_middle:
            why = "macd_hist↑0" if crossed_up else "price>=mid_bb"
            return SqueezeSignal(
                action=SqueezeAction.SELL,
                state_after=SqueezeState.FLAT,
                reason=f"exit ({why}); close {close_now:.2f}, mid {middle:.2f}, hist {hist_now:+.4f}",
                **base,
            )
        return SqueezeSignal(
            action=SqueezeAction.HOLD,
            state_after=SqueezeState.LONG,
            reason=f"hold long; close {close_now:.2f}, mid {middle:.2f}, hist {hist_now:+.4f}",
            **base,
        )

    if state == SqueezeState.ARMED:
        bars_since_arm = current_index - (armed_at_index or current_index)
        if bars_since_arm >= p.setup_expiry_bars:
            return SqueezeSignal(
                action=SqueezeAction.DISARM,
                state_after=SqueezeState.FLAT,
                reason=f"setup expired after {bars_since_arm} bars",
                **base,
            )
        if close_now > lower:
            return SqueezeSignal(
                action=SqueezeAction.BUY,
                state_after=SqueezeState.LONG,
                reason=(
                    f"trigger: close {close_now:.2f} > lower BB {lower:.2f} "
                    f"(setup {bars_since_arm} bars ago, RSI {rsi_now:.1f})"
                ),
                **base,
            )
        # Still below the lower band — check if we should re-arm (refresh
        # the setup bar so the expiry window restarts on the latest dip).
        if rsi_now < p.rsi_entry_max:
            return SqueezeSignal(
                action=SqueezeAction.ARM,  # re-arm refreshes armed_at_index
                state_after=SqueezeState.ARMED,
                reason=f"re-arm: still below lower BB, RSI {rsi_now:.1f}",
                **base,
            )
        return SqueezeSignal(
            action=SqueezeAction.HOLD,
            state_after=SqueezeState.ARMED,
            reason=f"armed, waiting for trigger; close {close_now:.2f}, lower {lower:.2f}",
            **base,
        )

    # state == FLAT — look for a new setup.
    setup_seen = close_now < lower and rsi_now < p.rsi_entry_max
    if not setup_seen:
        return SqueezeSignal(
            action=SqueezeAction.HOLD,
            state_after=SqueezeState.FLAT,
            reason=f"no setup; close {close_now:.2f}, lower {lower:.2f}, RSI {rsi_now:.1f}",
            **base,
        )
    if not _bbw_filter_pass(bb.width, p):
        return SqueezeSignal(
            action=SqueezeAction.HOLD,
            state_after=SqueezeState.FLAT,
            reason=f"setup seen but BBW filter rejected (width {width:.4f})",
            **base,
        )
    return SqueezeSignal(
        action=SqueezeAction.ARM,
        state_after=SqueezeState.ARMED,
        reason=(
            f"arm: close {close_now:.2f} < lower BB {lower:.2f}, "
            f"RSI {rsi_now:.1f}, BBW {width:.4f}"
        ),
        **base,
    )
