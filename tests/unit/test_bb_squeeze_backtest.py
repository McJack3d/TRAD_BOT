"""BB-squeeze backtest harness tests.

The harness is short but easy to break. These tests pin down:
  - The shape of the result (equity curve length, trades list, summary).
  - No-trade case (smooth uptrend never sets up).
  - At least one round trip on a synthetic dump-recover-spike pattern.
  - Fees actually reduce realized PnL.
"""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import pandas as pd

from src.backtest.bb_squeeze_backtest import backtest_bb_squeeze, summarize
from src.strategy.bb_squeeze import SqueezeParams


def _ts(n: int) -> pd.DatetimeIndex:
    return pd.date_range("2024-01-01", periods=n, freq="5min", tz="UTC")


def test_smooth_uptrend_produces_no_trades() -> None:
    closes = pd.Series(np.linspace(100, 200, 500), index=_ts(500))
    res = backtest_bb_squeeze(closes, initial_equity=Decimal("1000"))
    assert res.trades == []
    # Equity unchanged (we never bought anything).
    assert res.final_equity == Decimal("1000")


def test_dump_recover_pattern_creates_round_trip() -> None:
    """Construct a series with a clear dump → bounce → spike pattern that
    the strategy should fully execute (arm, buy, sell)."""
    rng = np.random.default_rng(123)
    # 60 bars of mild chop to seed the bands and BBW history.
    base = (100 + rng.normal(0, 1.0, 60)).tolist()
    # 8 bars dumping fast → arm + likely buy on the bounce.
    dump = np.linspace(100, 80, 8).tolist()
    # 10 bars bouncing back through the middle band → exit.
    bounce = np.linspace(82, 105, 10).tolist()
    closes = pd.Series(np.array(base + dump + bounce), index=_ts(78))
    p = SqueezeParams(min_bbw_percentile=0.0)  # disable filter to be deterministic
    res = backtest_bb_squeeze(closes, initial_equity=Decimal("1000"), params=p)
    assert len(res.trades) >= 1
    t = res.trades[0]
    assert t["entry_price"] > 0
    assert t["exit_price"] > t["entry_price"], (
        f"expected a profitable round trip on a recovery pattern; got entry "
        f"{t['entry_price']} exit {t['exit_price']}"
    )


def test_summary_has_expected_keys_when_trades_happen() -> None:
    rng = np.random.default_rng(7)
    base = (100 + rng.normal(0, 1.0, 60)).tolist()
    dump = np.linspace(100, 80, 8).tolist()
    bounce = np.linspace(82, 105, 10).tolist()
    closes = pd.Series(np.array(base + dump + bounce), index=_ts(78))
    res = backtest_bb_squeeze(closes, params=SqueezeParams(min_bbw_percentile=0.0))
    s = summarize(res)
    for k in (
        "n_trades", "win_rate", "avg_win_pct", "avg_loss_pct", "avg_bars_held",
        "strategy_apr", "buy_and_hold_apr", "strategy_max_dd", "buy_and_hold_max_dd",
    ):
        assert k in s, f"missing key {k}"


def test_fees_reduce_pnl() -> None:
    rng = np.random.default_rng(7)
    base = (100 + rng.normal(0, 1.0, 60)).tolist()
    dump = np.linspace(100, 80, 8).tolist()
    bounce = np.linspace(82, 105, 10).tolist()
    closes = pd.Series(np.array(base + dump + bounce), index=_ts(78))
    p = SqueezeParams(min_bbw_percentile=0.0)
    no_fee = backtest_bb_squeeze(closes, fee_bps=Decimal("0"), slippage_bps=Decimal("0"), params=p)
    with_fee = backtest_bb_squeeze(closes, fee_bps=Decimal("10"), slippage_bps=Decimal("5"), params=p)
    assert with_fee.final_equity < no_fee.final_equity


def test_trend_filter_blocks_trades_in_a_downtrend() -> None:
    """When daily SMA-200 says downtrend, the trend filter must zero out trades."""
    # Same dump-recover pattern as the round-trip test...
    rng = np.random.default_rng(123)
    base = (100 + rng.normal(0, 1.0, 60)).tolist()
    dump = np.linspace(100, 80, 8).tolist()
    bounce = np.linspace(82, 105, 10).tolist()
    closes = pd.Series(np.array(base + dump + bounce), index=_ts(78))

    # ...but the daily series is a sliding decline that stays below SMA-200
    # for the entire intraday window. We need at least 200+78 daily bars
    # so the SMA is defined across the test window.
    n_daily = 300
    daily_idx = pd.date_range("2023-01-01", periods=n_daily, freq="D", tz="UTC")
    daily_vals = np.linspace(200.0, 50.0, n_daily)  # straight-line decline
    daily = pd.Series(daily_vals, index=daily_idx)

    p = SqueezeParams(min_bbw_percentile=0.0)
    res = backtest_bb_squeeze(
        closes, params=p, daily_closes=daily,
        trend_sma_window=50,  # smaller SMA to fit the daily fixture
    )
    assert res.trades == [], (
        f"trend filter should have blocked all entries; got {len(res.trades)} trades"
    )


def test_trend_filter_off_means_trades_happen() -> None:
    """Sanity: with daily_closes=None (default), trades still happen on the
    dump-recover pattern. This is the same fixture as the trend-blocked test."""
    rng = np.random.default_rng(123)
    base = (100 + rng.normal(0, 1.0, 60)).tolist()
    dump = np.linspace(100, 80, 8).tolist()
    bounce = np.linspace(82, 105, 10).tolist()
    closes = pd.Series(np.array(base + dump + bounce), index=_ts(78))
    p = SqueezeParams(min_bbw_percentile=0.0)
    res = backtest_bb_squeeze(closes, params=p, daily_closes=None)
    assert len(res.trades) >= 1


def test_buy_and_hold_benchmark_matches_first_to_last_ratio() -> None:
    closes = pd.Series(np.linspace(100, 150, 300), index=_ts(300))
    res = backtest_bb_squeeze(closes, initial_equity=Decimal("1000"))
    # B&H final = 1000 * (150/100) = 1500.
    assert abs(float(res.final_buy_and_hold) - 1500.0) < 0.01
