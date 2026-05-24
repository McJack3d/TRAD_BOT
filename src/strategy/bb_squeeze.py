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

`precompute_squeeze` is the fast path: vectorize the indicator
calculations ONCE over the full series so a backtest can walk the
state machine in O(1) per bar instead of O(n).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

import numpy as np
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


@dataclass(slots=True)
class SqueezePrecomputed:
    """All bar-wise indicator values, computed once for fast bar-by-bar lookup."""
    bb_lower: np.ndarray
    bb_middle: np.ndarray
    bb_width: np.ndarray
    rsi: np.ndarray
    macd_hist: np.ndarray
    bbw_filter_pass: np.ndarray  # bool array, True where BBW filter would allow


def precompute_squeeze(closes: pd.Series, params: SqueezeParams) -> SqueezePrecomputed:
    """Vectorize all indicator computations over the full series.

    This is the single O(n) pass that lets a backtest walk the state
    machine in O(1) per bar (total O(n)) instead of O(n²) for slicing-
    and-recomputing on every bar.
    """
    p = params
    bb = bollinger_bands(closes, window=p.bb_window, num_std=p.bb_num_std)
    rsi_series = rsi(closes, window=p.rsi_window)
    mac = macd(closes, fast=p.macd_fast, slow=p.macd_slow, signal_window=p.macd_signal)

    if p.min_bbw_percentile > 0:
        # Per-bar threshold = rolling quantile of recent BBW. Until we have at
        # least 10 valid BBW values in the window, threshold is NaN and we
        # treat it as "filter not yet meaningful → allow".
        thr = bb.width.rolling(p.bbw_lookback, min_periods=10).quantile(
            p.min_bbw_percentile / 100.0
        )
        passes = ((bb.width >= thr) | thr.isna()).to_numpy()
    else:
        passes = np.ones(len(closes), dtype=bool)

    return SqueezePrecomputed(
        bb_lower=bb.lower.to_numpy(),
        bb_middle=bb.middle.to_numpy(),
        bb_width=bb.width.to_numpy(),
        rsi=rsi_series.to_numpy(),
        macd_hist=mac.histogram.to_numpy(),
        bbw_filter_pass=passes,
    )


def _eval_state_machine(
    close_now: float,
    lower: float,
    middle: float,
    width: float,
    rsi_now: float,
    hist_now: float,
    hist_prev: float,
    bbw_pass: bool,
    current_index: int,
    state: SqueezeState,
    armed_at_index: int | None,
    params: SqueezeParams,
    trend_up: bool,
) -> SqueezeSignal:
    """The pure state-machine logic — same for live and backtest paths."""
    p = params
    last_close = Decimal(str(close_now))
    base = dict(
        close=last_close,
        bb_lower=Decimal(str(lower)),
        bb_middle=Decimal(str(middle)),
        bb_width=Decimal(str(width)),
        rsi=Decimal(str(rsi_now)),
        macd_hist=Decimal(str(hist_now)),
    )

    if state == SqueezeState.LONG:
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
            if not trend_up:
                return SqueezeSignal(
                    action=SqueezeAction.HOLD,
                    state_after=SqueezeState.ARMED,
                    reason=(
                        f"trigger blocked (trend down): close {close_now:.2f} "
                        f"> lower BB {lower:.2f}, RSI {rsi_now:.1f}"
                    ),
                    **base,
                )
            return SqueezeSignal(
                action=SqueezeAction.BUY,
                state_after=SqueezeState.LONG,
                reason=(
                    f"trigger: close {close_now:.2f} > lower BB {lower:.2f} "
                    f"(setup {bars_since_arm} bars ago, RSI {rsi_now:.1f})"
                ),
                **base,
            )
        if rsi_now < p.rsi_entry_max:
            return SqueezeSignal(
                action=SqueezeAction.ARM,
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
    if not bbw_pass:
        return SqueezeSignal(
            action=SqueezeAction.HOLD,
            state_after=SqueezeState.FLAT,
            reason=f"setup seen but BBW filter rejected (width {width:.4f})",
            **base,
        )
    if not trend_up:
        return SqueezeSignal(
            action=SqueezeAction.HOLD,
            state_after=SqueezeState.FLAT,
            reason=f"setup seen but trend filter rejected (trend down)",
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


def evaluate_at(
    closes: pd.Series,
    pre: SqueezePrecomputed,
    i: int,
    state: SqueezeState,
    armed_at_index: int | None,
    params: SqueezeParams,
    trend_up: bool = True,
) -> SqueezeSignal:
    """Fast path used by the backtest: evaluate bar `i` against precomputed
    indicators. O(1) per call after the one-time `precompute_squeeze`."""
    p = params
    close_now = float(closes.iloc[i])
    lower = float(pre.bb_lower[i])
    middle = float(pre.bb_middle[i])
    width = float(pre.bb_width[i])
    rsi_now = float(pre.rsi[i])
    hist_now = float(pre.macd_hist[i])
    hist_prev = float(pre.macd_hist[i - 1]) if i > 0 else hist_now

    if (math.isnan(lower) or math.isnan(rsi_now) or math.isnan(hist_now)):
        return SqueezeSignal(
            action=SqueezeAction.HOLD,
            state_after=state,
            close=Decimal(str(close_now)),
            bb_lower=Decimal("0"),
            bb_middle=Decimal("0"),
            bb_width=Decimal("0"),
            rsi=Decimal("0"),
            macd_hist=Decimal("0"),
            reason=f"indicators not warmed up at bar {i}",
        )

    return _eval_state_machine(
        close_now=close_now,
        lower=lower,
        middle=middle,
        width=width,
        rsi_now=rsi_now,
        hist_now=hist_now,
        hist_prev=hist_prev,
        bbw_pass=bool(pre.bbw_filter_pass[i]),
        current_index=i,
        state=state,
        armed_at_index=armed_at_index,
        params=p,
        trend_up=trend_up,
    )


def evaluate_bb_squeeze(
    closes: pd.Series,
    state: SqueezeState,
    armed_at_index: int | None,
    entry_bar_index: int | None,
    params: SqueezeParams | None = None,
    trend_up: bool = True,
) -> SqueezeSignal:
    """Live-path entry point: compute indicators on the spot and evaluate the
    last bar. Use `precompute_squeeze` + `evaluate_at` for backtests.

    `state` / `armed_at_index` / `entry_bar_index` are the bot's prior state
    going into this bar; the caller persists them between calls.

    `trend_up` is an external regime filter — typically from a slower
    timeframe (e.g. daily SMA-200). False blocks new entries; exits are
    never blocked.
    """
    p = params or SqueezeParams()
    needed = max(p.macd_slow + p.macd_signal, p.bb_window, p.rsi_window) + 5
    if len(closes) < needed:
        return SqueezeSignal(
            action=SqueezeAction.HOLD,
            state_after=state,
            close=Decimal(str(closes.iloc[-1])) if len(closes) else Decimal("0"),
            bb_lower=Decimal("0"),
            bb_middle=Decimal("0"),
            bb_width=Decimal("0"),
            rsi=Decimal("0"),
            macd_hist=Decimal("0"),
            reason=f"need {needed} bars, have {len(closes)}",
        )
    pre = precompute_squeeze(closes, p)
    return evaluate_at(closes, pre, len(closes) - 1, state, armed_at_index, p, trend_up)
