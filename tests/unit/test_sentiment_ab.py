"""Tests for the sentiment A/B harness.

Synthetic price + sentiment, no network. Verifies the comparator's
contract: weight 0 ignores sentiment, the metrics are well-formed, and
the verdict is a sane plain-English string.
"""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import pandas as pd

from src.backtest.sentiment_ab import (
    compare_sentiment_weights,
    verdict,
)


def _daily_closes(n: int = 600, seed: int = 0) -> pd.Series:
    rng = np.random.default_rng(seed)
    # A trending-with-noise series so the SMA actually takes positions.
    close = 100 + np.cumsum(rng.normal(0.15, 1.0, n))
    close = np.maximum(close, 1.0)
    idx = pd.date_range("2022-01-01", periods=n, freq="1D", tz="UTC")
    return pd.Series(close, index=idx)


def _sentiment(n: int = 600, seed: int = 1) -> pd.Series:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2022-01-01", periods=n, freq="1D", tz="UTC")
    return pd.Series(np.clip(rng.normal(0, 0.5, n), -1, 1), index=idx)


def test_compare_returns_one_row_per_weight():
    rows = compare_sentiment_weights(
        _daily_closes(), _sentiment(), weights=(0.0, 0.02, 0.05), sma_window=50
    )
    assert len(rows) == 3
    assert rows[0].weight == 0.0
    assert "baseline" in rows[0].label.lower()


def test_metrics_are_well_formed():
    rows = compare_sentiment_weights(
        _daily_closes(), _sentiment(), weights=(0.0, 0.03), sma_window=50
    )
    for r in rows:
        assert isinstance(r.apr, float)
        assert isinstance(r.sharpe, float)
        assert -1.0 <= r.max_drawdown <= 0.0
        assert r.n_trades >= 0
        assert r.final_equity > 0


def test_weight_zero_is_independent_of_sentiment_series():
    """The baseline (weight 0) must produce identical results regardless
    of what sentiment series is passed — proves no leakage."""
    closes = _daily_closes()
    r_a = compare_sentiment_weights(closes, _sentiment(seed=1), weights=(0.0,), sma_window=50)
    r_b = compare_sentiment_weights(closes, _sentiment(seed=99), weights=(0.0,), sma_window=50)
    assert r_a[0].final_equity == r_b[0].final_equity
    assert r_a[0].n_trades == r_b[0].n_trades


def test_nonzero_weight_can_change_results():
    """A large weight with a real sentiment series should change *something*
    versus the baseline (otherwise the tilt is a no-op)."""
    closes = _daily_closes()
    sent = _sentiment()
    rows = compare_sentiment_weights(closes, sent, weights=(0.0, 0.05), sma_window=50)
    base, weighted = rows[0], rows[1]
    # Either equity or trade count should differ; if identical, the
    # sentiment tilt did nothing at all.
    assert (base.final_equity != weighted.final_equity) or (
        base.n_trades != weighted.n_trades
    )


def test_verdict_is_a_string():
    rows = compare_sentiment_weights(
        _daily_closes(), _sentiment(), weights=(0.0, 0.03), sma_window=50
    )
    v = verdict(rows)
    assert isinstance(v, str) and v


def test_verdict_handles_missing_baseline():
    rows = compare_sentiment_weights(
        _daily_closes(), _sentiment(), weights=(0.03,), sma_window=50
    )
    assert "baseline" in verdict(rows).lower()


def test_initial_equity_respected():
    rows = compare_sentiment_weights(
        _daily_closes(), _sentiment(), weights=(0.0,), sma_window=50,
        initial_equity=Decimal("5000"),
    )
    # Final equity should be in a plausible range relative to 5000 start,
    # not the 1000 default.
    assert rows[0].final_equity > 1000
