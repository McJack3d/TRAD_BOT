"""Tests for trailing stop, metrics, walk-forward, OOS split."""

from __future__ import annotations

from decimal import Decimal

import pandas as pd

from src.backtest.trend_backtest import backtest_sma_trend
from src.backtest.trend_metrics import compute_full_metrics
from src.backtest.trend_walk_forward import oos_split, walk_forward_trend


def _series(values: list[float]) -> pd.Series:
    idx = pd.date_range("2020-01-01", periods=len(values), freq="1D", tz="UTC")
    return pd.Series(values, index=idx)


# ---- trailing stop --------------------------------------------------


def test_trailing_stop_fires_on_deep_pullback() -> None:
    # Rise then sharp drop. The trailing stop should fire on the drop day.
    values = [100.0] * 50 + [100.0 + 2 * i for i in range(50)] + [140.0]
    r = backtest_sma_trend(_series(values), sma_window=20, trailing_stop_pct=0.20)
    stop_trades = [t for t in r.trades if t.get("reason") == "trailing_stop"]
    assert len(stop_trades) >= 1


def test_trailing_stop_beats_no_stop_when_signal_lags() -> None:
    """When the SMA is slow to react, the trailing stop should exit earlier
    and preserve more capital."""
    # 50 days warm-up at 100, 150 days rising to 250, then a drop to 200
    # (-20% from peak, but SMA50 is still well below current price so the
    # signal would say IN). The 15% trailing stop fires, the no-stop run
    # rides the drop all the way down via the signal.
    values = [100.0] * 50 + [100.0 + i for i in range(150)] + [200.0]
    no_stop = backtest_sma_trend(_series(values), sma_window=50, trailing_stop_pct=0.0)
    with_stop = backtest_sma_trend(_series(values), sma_window=50, trailing_stop_pct=0.15)
    assert with_stop.final_equity >= no_stop.final_equity


def test_trailing_stop_zero_means_off() -> None:
    """trailing_stop_pct=0 → no stop, same as before."""
    values = [100.0] * 50 + [100.0 + i for i in range(100)]
    a = backtest_sma_trend(_series(values), sma_window=50, trailing_stop_pct=0.0)
    b = backtest_sma_trend(_series(values), sma_window=50, trailing_stop_pct=0.0)
    assert a.final_equity == b.final_equity
    assert all(t.get("reason") != "trailing_stop" for t in a.trades)


def test_trailing_stop_does_not_re_enter_immediately() -> None:
    """After a stop, must wait for a fresh OUT->IN signal to re-enter."""
    # Drop then immediate recovery — without the cooldown the bot would
    # bounce back in. With cooldown, it stays OUT until SMA signal clears.
    values = [100.0] * 50 + [100.0 + 3 * i for i in range(30)] + [120.0] + [200.0]
    r = backtest_sma_trend(_series(values), sma_window=20, trailing_stop_pct=0.10)
    # Count consecutive buys (should never be > 1 without an intervening sell).
    sides = [t["side"] for t in r.trades]
    for i in range(1, len(sides)):
        if sides[i] == sides[i - 1]:
            raise AssertionError(f"two consecutive {sides[i]}s — cooldown broken")


# ---- metrics --------------------------------------------------------


def test_metrics_compute_all_fields() -> None:
    values = [100.0] * 50 + [100.0 + 2 * i for i in range(100)]
    r = backtest_sma_trend(_series(values), sma_window=50)
    m = compute_full_metrics(r)
    assert m.sharpe != 0  # rising → positive returns → positive sharpe
    assert m.sortino >= m.sharpe  # downside vol ≤ total vol
    assert m.calmar > 0
    assert m.ulcer_index >= 0
    assert m.span_days > 0
    assert m.n_trades > 0


def test_metrics_flat_series_zero_sharpe() -> None:
    values = [100.0] * 150
    r = backtest_sma_trend(_series(values), sma_window=50)
    m = compute_full_metrics(r)
    assert m.sharpe == 0  # no variance
    assert m.max_drawdown == 0


# ---- OOS split ------------------------------------------------------


def test_oos_split_proportions() -> None:
    s = _series(list(range(100)))
    in_sample, oos = oos_split(s, oos_fraction=0.30)
    assert len(in_sample) == 70
    assert len(oos) == 30
    # No overlap.
    assert in_sample.index[-1] < oos.index[0]


def test_oos_split_default_fraction() -> None:
    s = _series(list(range(1000)))
    in_sample, oos = oos_split(s)
    assert len(in_sample) == 700
    assert len(oos) == 300


# ---- walk-forward ---------------------------------------------------


def test_walk_forward_yields_windows_on_long_series() -> None:
    # 3y of synthetic data with a clear up-then-down-then-up cycle so the
    # SMA strategy round-trips at least once per train window (needed for
    # the >=2 trade filter to pass).
    import math

    n = 3 * 365
    values = [100.0 + 50 * math.sin(i / 60) + 0.05 * i for i in range(n)]
    s = _series(values)
    windows = walk_forward_trend(
        s, train_days=365, test_days=90, initial_equity=Decimal("1000")
    )
    assert len(windows) >= 3
    for w in windows:
        assert w.test_metrics.span_days > 0
        assert w.best_sma in (100, 150, 200, 250)
        assert w.best_buffer in (0.0, 0.005, 0.01, 0.02)


def test_walk_forward_short_series_returns_empty() -> None:
    # Only 6 months — not enough for default 2y train.
    values = [100.0 + i for i in range(180)]
    windows = walk_forward_trend(_series(values), train_days=730, test_days=180)
    assert windows == []
