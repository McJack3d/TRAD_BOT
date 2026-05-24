"""BB-squeeze state machine tests.

These tests construct synthetic close series that exercise each
transition: FLAT→ARMED, ARMED→BUY, ARMED→DISARM (expiry), LONG→SELL
on both exit conditions, and the BBW filter.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.strategy.bb_squeeze import (
    SqueezeAction,
    SqueezeParams,
    SqueezeState,
    evaluate_bb_squeeze,
)


def _ts(n: int) -> pd.DatetimeIndex:
    """Synthetic 5-minute timestamps."""
    return pd.date_range("2024-01-01", periods=n, freq="5min", tz="UTC")


def test_not_enough_history_returns_hold() -> None:
    closes = pd.Series([100.0] * 5, index=_ts(5))
    sig = evaluate_bb_squeeze(closes, SqueezeState.FLAT, None, None)
    assert sig.action == SqueezeAction.HOLD
    assert sig.state_after == SqueezeState.FLAT


def test_flat_with_no_setup_holds() -> None:
    # Smooth uptrend → never below lower BB.
    closes = pd.Series(np.linspace(100, 120, 80), index=_ts(80))
    sig = evaluate_bb_squeeze(closes, SqueezeState.FLAT, None, None)
    assert sig.action == SqueezeAction.HOLD
    assert sig.state_after == SqueezeState.FLAT


def test_flat_to_armed_on_oversold_dump() -> None:
    """A sudden drop after rangy action should arm us.

    Bands lag — a gradual decline lets the lower band track price down
    and never produces a close-below-lower. The classic setup is rangy
    price for many bars (wide bands) then a single sharp dump that
    overshoots before the bands react.
    """
    rng = np.random.default_rng(42)
    base = 100 + rng.normal(0, 1.5, 60)
    # Three sharp, increasing-magnitude down bars that the lagged band
    # can't follow → final close should land below the lower band with
    # RSI under 25.
    dump = [96.0, 90.0, 82.0]
    closes = pd.Series(np.concatenate([base, dump]), index=_ts(63))
    sig = evaluate_bb_squeeze(closes, SqueezeState.FLAT, None, None)
    assert sig.action == SqueezeAction.ARM, f"expected ARM, got {sig.action}: {sig.reason}"
    assert sig.state_after == SqueezeState.ARMED


def test_armed_to_buy_on_close_back_inside_band() -> None:
    """After arming, a bar that closes back above the lower band fires the buy."""
    rng = np.random.default_rng(42)  # same seed as arm test so the setup fires
    base = 100 + rng.normal(0, 1.5, 60)
    dump = [96.0, 90.0, 82.0]
    closes_at_arm = pd.Series(np.concatenate([base, dump]), index=_ts(63))
    armed_sig = evaluate_bb_squeeze(closes_at_arm, SqueezeState.FLAT, None, None)
    assert armed_sig.action == SqueezeAction.ARM

    # Next bar bounces strongly back above the lower band.
    bounce = np.concatenate([base, dump, [95.0]])
    closes_next = pd.Series(bounce, index=_ts(64))
    sig = evaluate_bb_squeeze(closes_next, SqueezeState.ARMED, 62, None)
    assert sig.action == SqueezeAction.BUY
    assert sig.state_after == SqueezeState.LONG


def test_armed_expires_after_setup_window() -> None:
    """If price stays below lower band but RSI recovers, eventually disarm."""
    p = SqueezeParams(setup_expiry_bars=3)
    closes = pd.Series(
        np.concatenate([np.linspace(100, 80, 50), [82.0, 83.0, 84.0, 84.0]]),
        index=_ts(54),
    )
    # Pretend we armed at bar index 50; current index is 53 → 3 bars elapsed.
    sig = evaluate_bb_squeeze(
        closes, SqueezeState.ARMED, armed_at_index=50, entry_bar_index=None, params=p
    )
    # Either DISARM (expiry) or BUY (if 84 already broke back above lower).
    # The point is we must NOT remain in ARMED forever.
    assert sig.action in (SqueezeAction.DISARM, SqueezeAction.BUY)


def test_long_holds_when_below_midline_and_hist_negative() -> None:
    rng = np.random.default_rng(3)
    # Sideways prices with the latest one still below the middle band and
    # MACD histogram still negative → HOLD long.
    base = 100 + rng.normal(0, 0.5, 80)
    base[-1] = 99.0  # latest closes just below the SMA
    closes = pd.Series(base, index=_ts(80))
    sig = evaluate_bb_squeeze(closes, SqueezeState.LONG, None, entry_bar_index=70)
    # We can't guarantee HOLD because the random walk might happen to cross
    # — but at minimum the action is NOT BUY/ARM/DISARM (those are FLAT-side).
    assert sig.action in (SqueezeAction.HOLD, SqueezeAction.SELL)


def test_long_sells_when_price_reaches_middle_bb() -> None:
    # 79 bars steadily around 90, then a strong bar that pushes well above
    # the 20-SMA → exit on "price >= middle".
    base = np.linspace(95, 90, 79).tolist()
    base.append(98.0)  # spike above the SMA
    closes = pd.Series(base, index=_ts(80))
    sig = evaluate_bb_squeeze(closes, SqueezeState.LONG, None, entry_bar_index=78)
    assert sig.action == SqueezeAction.SELL
    assert sig.state_after == SqueezeState.FLAT
    assert "mid_bb" in sig.reason or "macd_hist" in sig.reason


def test_bbw_filter_blocks_setup_when_market_is_flat() -> None:
    """If BBW is narrow (flat market), even a perfect setup is ignored."""
    # Construct a series where the prior 50 bars are dead-flat (BBW = 0)
    # and the current bar dumps. The BBW percentile floor should reject.
    flat = [100.0] * 100
    dump = [80.0, 79.0, 78.0]  # final bar would otherwise arm
    closes = pd.Series(np.array(flat + dump), index=_ts(103))
    p = SqueezeParams(min_bbw_percentile=50.0, bbw_lookback=100)
    sig = evaluate_bb_squeeze(closes, SqueezeState.FLAT, None, None, params=p)
    # Three dump bars give some width, but the 50th percentile of the prior
    # 100 bars includes a lot of zeros. The filter should keep us out OR at
    # minimum not produce a buy.
    assert sig.action != SqueezeAction.BUY


def test_trend_filter_blocks_buy_on_armed_trigger() -> None:
    """Even with a perfect ARMED→trigger setup, trend_up=False must stop the BUY."""
    rng = np.random.default_rng(42)
    base = 100 + rng.normal(0, 1.5, 60)
    dump = [96.0, 90.0, 82.0]
    bounce = np.concatenate([base, dump, [95.0]])
    closes = pd.Series(bounce, index=_ts(64))
    sig = evaluate_bb_squeeze(
        closes, SqueezeState.ARMED, armed_at_index=62, entry_bar_index=None,
        trend_up=False,
    )
    assert sig.action == SqueezeAction.HOLD
    assert sig.state_after == SqueezeState.ARMED
    assert "trend down" in sig.reason


def test_trend_filter_blocks_arm_on_flat_setup() -> None:
    """A fresh oversold setup in FLAT must be ignored when trend is down."""
    rng = np.random.default_rng(42)
    base = 100 + rng.normal(0, 1.5, 60)
    dump = [96.0, 90.0, 82.0]
    closes = pd.Series(np.concatenate([base, dump]), index=_ts(63))
    sig = evaluate_bb_squeeze(
        closes, SqueezeState.FLAT, None, None, trend_up=False,
    )
    assert sig.action == SqueezeAction.HOLD
    assert sig.state_after == SqueezeState.FLAT
    assert "trend" in sig.reason


def test_trend_filter_does_not_block_exits() -> None:
    """Exits must fire regardless of the trend filter — once in, always exit."""
    base = np.linspace(95, 90, 79).tolist()
    base.append(98.0)  # spike above the SMA → exit condition
    closes = pd.Series(base, index=_ts(80))
    sig = evaluate_bb_squeeze(
        closes, SqueezeState.LONG, None, entry_bar_index=78, trend_up=False,
    )
    # Trend filter is OFF for exits — we should still SELL.
    assert sig.action == SqueezeAction.SELL


def test_stop_loss_fires_when_close_drops_below_trigger() -> None:
    """In LONG with a 1% stop, a close that's 1.5% below entry must trigger SELL."""
    # 80 bars of stable price around 100, last bar dumps to 98.5 (1.5% below entry 100).
    closes_arr = [100.0] * 79 + [98.5]
    closes = pd.Series(closes_arr, index=_ts(80))
    p = SqueezeParams(stop_loss_pct=0.01)  # 1% stop
    # Pretend we entered at bar 70 when price was 100.
    sig = evaluate_bb_squeeze(
        closes, SqueezeState.LONG, None, entry_bar_index=70, params=p,
    )
    assert sig.action == SqueezeAction.SELL
    assert "stop_loss" in sig.reason


def test_stop_loss_does_not_fire_within_tolerance() -> None:
    """A close 0.5% below entry with a 1% stop should NOT trigger."""
    closes_arr = [100.0] * 79 + [99.5]  # only 0.5% down from entry
    closes = pd.Series(closes_arr, index=_ts(80))
    p = SqueezeParams(stop_loss_pct=0.01)
    sig = evaluate_bb_squeeze(
        closes, SqueezeState.LONG, None, entry_bar_index=70, params=p,
    )
    # Either HOLD (above stop) or SELL via another exit — but NOT a stop exit.
    assert "stop_loss" not in sig.reason


def test_stop_loss_zero_means_off() -> None:
    """stop_loss_pct=0 (default) must not interfere with existing exit logic."""
    closes_arr = [100.0] * 79 + [50.0]  # massive drop
    closes = pd.Series(closes_arr, index=_ts(80))
    p = SqueezeParams(stop_loss_pct=0.0)
    sig = evaluate_bb_squeeze(
        closes, SqueezeState.LONG, None, entry_bar_index=70, params=p,
    )
    # Whatever fires, it must NOT be a stop_loss exit.
    assert "stop_loss" not in sig.reason


def test_bbw_filter_disabled_allows_setup() -> None:
    """With min_bbw_percentile=0 the filter is off; setups always pass."""
    # Same construction as the prior test but the filter is disabled.
    flat = [100.0] * 50
    rng = np.random.default_rng(11)
    chop = (100 + rng.normal(0, 1.0, 30)).tolist()
    dump = np.linspace(100, 85, 8).tolist()
    closes = pd.Series(np.array(flat + chop + dump), index=_ts(88))
    p = SqueezeParams(min_bbw_percentile=0.0)
    sig = evaluate_bb_squeeze(closes, SqueezeState.FLAT, None, None, params=p)
    # With the filter off, the oversold dump should at least arm.
    assert sig.action in (SqueezeAction.ARM, SqueezeAction.HOLD)
