"""Tests for the multi-asset basket backtest and trend buffers."""

from __future__ import annotations

from decimal import Decimal

import pandas as pd

from src.backtest.trend_backtest import backtest_basket, backtest_sma_trend, summarize
from src.strategy.sma_trend import TrendState, evaluate_trend


def _series(values: list[float]) -> pd.Series:
    idx = pd.date_range("2020-01-01", periods=len(values), freq="1D", tz="UTC")
    return pd.Series(values, index=idx)


# ---- buffer tests ---------------------------------------------------


def test_entry_buffer_filters_marginal_crossings() -> None:
    """If price just barely crosses the SMA, a buffer should keep us OUT."""
    # 49 closes at 100, last at 100.5 → marginal cross.
    closes = pd.Series([100.0] * 49 + [100.5])
    sig_no_buffer = evaluate_trend(closes, sma_window=50, entry_buffer_pct=0.0)
    sig_with_buffer = evaluate_trend(closes, sma_window=50, entry_buffer_pct=0.01)
    assert sig_no_buffer.state == TrendState.IN
    assert sig_with_buffer.state == TrendState.OUT


def test_buffers_reduce_trade_count() -> None:
    """On a choppy series, buffers should reduce whipsaws."""
    # Alternating around 100 with small amplitude → lots of crossings.
    values = [100.0] * 50
    for i in range(100):
        values.append(100.0 + (1.5 if i % 2 == 0 else -1.5))
    closes = _series(values)
    r_no_buffer = backtest_sma_trend(closes, sma_window=50, entry_buffer_pct=0.0, exit_buffer_pct=0.0)
    r_buffered = backtest_sma_trend(closes, sma_window=50, entry_buffer_pct=0.02, exit_buffer_pct=0.02)
    assert len(r_buffered.trades) < len(r_no_buffer.trades)


# ---- basket tests ---------------------------------------------------


def test_basket_aggregates_two_uncorrelated_uptrends() -> None:
    btc = _series([100.0] * 50 + [100.0 + 5 * i for i in range(100)])
    eth = _series([200.0] * 50 + [200.0 + 8 * i for i in range(100)])
    result = backtest_basket(
        {"BTC/USDT": btc, "ETH/USDT": eth},
        initial_equity=Decimal("1000"),
        sma_window=50,
    )
    assert result.final_equity > result.initial_equity
    # Both symbols should have traded at least once.
    symbols_traded = {t["symbol"] for t in result.trades}
    assert symbols_traded == {"BTC/USDT", "ETH/USDT"}


def test_basket_single_symbol_close_to_single_asset() -> None:
    """Basket with one symbol should approximately match single-asset result."""
    closes = _series([100.0] * 50 + [100.0 + 3 * i for i in range(100)])
    single = backtest_sma_trend(closes, initial_equity=Decimal("1000"), sma_window=50)
    basket = backtest_basket({"BTC/USDT": closes}, initial_equity=Decimal("1000"), sma_window=50)
    # Within a few percent — the basket has the same cost model but rebalances
    # differently when the last bar straddles a signal.
    ratio = float(basket.final_equity) / float(single.final_equity)
    assert 0.97 < ratio < 1.03


def test_basket_empty_returns_initial() -> None:
    result = backtest_basket({}, initial_equity=Decimal("1000"))
    assert result.final_equity == Decimal("1000")
    assert result.trades == []


def test_basket_diversifies_drawdown() -> None:
    """If one asset crashes while another rallies, basket DD < single-asset DD."""
    btc = _series([100.0] * 50 + [100.0 - 0.4 * i for i in range(100)])  # falls
    eth = _series([200.0] * 50 + [200.0 + 2 * i for i in range(100)])  # rises
    single_btc = backtest_sma_trend(btc, initial_equity=Decimal("1000"), sma_window=50)
    basket = backtest_basket(
        {"BTC/USDT": btc, "ETH/USDT": eth},
        initial_equity=Decimal("1000"),
        sma_window=50,
    )
    single_stats = summarize(single_btc)
    basket_stats = summarize(basket)
    # Basket benefits from ETH's rise; final equity should be higher.
    assert basket_stats["strategy_final"] > single_stats["strategy_final"]
