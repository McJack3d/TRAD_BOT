"""SMA trend-strategy backtest tests."""

from __future__ import annotations

from decimal import Decimal

import pandas as pd

from src.backtest.trend_backtest import backtest_sma_trend, summarize


def _series(values: list[float]) -> pd.Series:
    idx = pd.date_range("2020-01-01", periods=len(values), freq="1D", tz="UTC")
    return pd.Series(values, index=idx)


def test_steady_uptrend_beats_cash() -> None:
    # Linear uptrend after a flat warm-up.
    values = [100.0] * 50 + [100.0 + 5 * i for i in range(100)]
    result = backtest_sma_trend(_series(values), initial_equity=Decimal("1000"), sma_window=50)
    assert result.final_equity > result.initial_equity
    # Should have entered (at least one buy trade).
    assert any(t["side"] == "buy" for t in result.trades)


def test_steady_downtrend_protects_capital() -> None:
    # Decline → strategy should sell early and hold cash, beating buy-and-hold.
    values = [100.0] * 50 + [100.0 - 0.4 * i for i in range(100)]
    result = backtest_sma_trend(_series(values), initial_equity=Decimal("1000"), sma_window=50)
    # Strategy should preserve significantly more capital than buy-and-hold.
    assert result.final_equity > result.final_buy_and_hold


def test_flat_prices_few_trades() -> None:
    values = [100.0] * 150
    result = backtest_sma_trend(_series(values), initial_equity=Decimal("1000"), sma_window=50)
    # Flat → close never strictly above SMA → stays OUT → no trades.
    assert len(result.trades) == 0
    assert result.final_equity == result.initial_equity


def test_summary_fields() -> None:
    values = [100.0] * 50 + [100.0 + 2 * i for i in range(100)]
    result = backtest_sma_trend(_series(values), initial_equity=Decimal("1000"), sma_window=50)
    stats = summarize(result)
    for k in (
        "span_days",
        "n_trades",
        "strategy_apr",
        "buy_and_hold_apr",
        "strategy_final",
        "buy_and_hold_final",
        "strategy_max_dd",
        "buy_and_hold_max_dd",
    ):
        assert k in stats


def test_fees_reduce_strategy_equity() -> None:
    values = [100.0] * 50 + [100.0 + i for i in range(100)]
    no_fees = backtest_sma_trend(
        _series(values),
        initial_equity=Decimal("1000"),
        sma_window=50,
        fee_bps=Decimal("0"),
        slippage_bps=Decimal("0"),
    )
    with_fees = backtest_sma_trend(
        _series(values),
        initial_equity=Decimal("1000"),
        sma_window=50,
        fee_bps=Decimal("10"),
        slippage_bps=Decimal("5"),
    )
    assert no_fees.final_equity > with_fees.final_equity
