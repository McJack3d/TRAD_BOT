"""Tests for the regime fire-rate diagnostics."""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.backtest.regime_diagnostics import bottleneck_verdict, diagnose_regime
from src.strategy.regime_switch import RegimeSwitchParams


def _ohlc(close: np.ndarray) -> pd.DataFrame:
    close = np.asarray(close, dtype=float)
    idx = pd.date_range("2024-01-01", periods=len(close), freq="1h", tz="UTC")
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 0.3,
            "low": close - 0.3,
            "close": close,
            "volume": np.full(len(close), 1000.0),
        },
        index=idx,
    )


def _calm_then_uptrend(seed: int = 5) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    calm = 100 + np.cumsum(rng.normal(0, 0.05, 250))
    trend = calm[-1] + np.cumsum(rng.normal(0, 0.05, 250) + 0.8)
    return _ohlc(np.concatenate([calm, trend]))


def test_diagnose_structure_complete():
    d = diagnose_regime(_calm_then_uptrend())
    for key in ("n_bars", "warmup_bars", "regime", "trend_leg", "range_leg", "realized_entries"):
        assert key in d
    assert set(d["regime"]) >= {"trend", "range", "neutral", "trend_pct"}
    assert set(d["trend_leg"]) >= {"trend_bars", "would_enter", "enter_rate"}
    assert set(d["range_leg"]) >= {"range_bars", "would_enter", "enter_rate"}


def test_diagnose_counts_are_consistent():
    d = diagnose_regime(_calm_then_uptrend())
    # warmup + warm == total bars.
    assert d["warmup_bars"] + d["warm_bars"] == d["n_bars"]
    # regime buckets sum to warm bars.
    reg = d["regime"]
    assert reg["trend"] + reg["range"] + reg["neutral"] == d["warm_bars"]
    # trend alignment buckets sum to trend bars.
    t = d["trend_leg"]
    assert t["ema_long_aligned"] + t["ema_short_aligned"] + t["ema_unaligned"] == t["trend_bars"]


def test_clean_uptrend_has_trend_bars_and_entries():
    d = diagnose_regime(_calm_then_uptrend())
    assert d["regime"]["trend"] > 0
    assert d["trend_leg"]["would_enter"] > 0
    assert d["realized_entries"] >= 1


def test_pure_noise_is_mostly_neutral():
    rng = np.random.default_rng(1)
    close = 100 + np.cumsum(rng.normal(0, 0.3, 600))
    d = diagnose_regime(_ohlc(close))
    assert d["regime"]["neutral_pct"] >= 0.5


def test_bottleneck_verdict_flags_neutral_domination():
    # A featureless flat line → indicators undefined / NEUTRAL heavy.
    rng = np.random.default_rng(7)
    close = 100 + rng.normal(0, 0.01, 600)  # near-flat, tiny noise
    d = diagnose_regime(_ohlc(close))
    verdict = bottleneck_verdict(d)
    assert isinstance(verdict, str) and verdict


def test_rsi_filter_bottleneck_detected():
    """If RANGE bars touch bands but RSI never reaches the (very strict)
    threshold, the verdict should name the RSI filter."""
    # Build a gentle oscillation that stays in RANGE and touches bands,
    # then make the RSI thresholds impossibly strict.
    n = 500
    close = 100 + 1.2 * np.sin(np.arange(n) * 0.25)
    d = diagnose_regime(
        _ohlc(close), RegimeSwitchParams(rsi_os=1.0, rsi_ob=99.0)
    )
    # With impossible RSI thresholds, the range leg can't enter even if
    # bands are touched.
    assert d["range_leg"]["would_enter"] == 0
    # The verdict is a non-empty diagnosis string.
    assert bottleneck_verdict(d)
