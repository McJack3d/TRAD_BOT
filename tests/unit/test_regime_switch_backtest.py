"""Tests for the regime-switch backtester."""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.backtest.regime_switch_backtest import (
    backtest_regime_switch,
    summarize,
)
from src.strategy.regime_switch import RegimeSwitchParams


def _ohlc_from_close(close: np.ndarray, freq: str = "1h") -> pd.DataFrame:
    close = np.asarray(close, dtype=float)
    idx = pd.date_range("2024-01-01", periods=len(close), freq=freq, tz="UTC")
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
    return _ohlc_from_close(np.concatenate([calm, trend]))


def test_backtest_runs_and_curve_aligns():
    df = _calm_then_uptrend()
    res = backtest_regime_switch(df, initial_equity=1000.0)
    assert len(res.equity_curve) == len(df)
    assert {"ts", "close", "equity", "position", "regime"}.issubset(
        res.equity_curve.columns
    )


def test_trend_leg_profits_on_clean_uptrend():
    """The whole point: a clean high-ADX uptrend should make the trend
    leg money after costs."""
    df = _calm_then_uptrend()
    res = backtest_regime_switch(
        df, initial_equity=1000.0, fee_bps=4.0, slippage_bps=2.0
    )
    assert res.trades, "expected at least one trade in a clean trend"
    assert res.final_equity > res.initial_equity


def test_no_trades_in_pure_noise_stays_near_flat():
    """In structureless noise the regime should mostly be NEUTRAL and
    the strategy should not bleed catastrophically."""
    rng = np.random.default_rng(2)
    close = 100 + np.cumsum(rng.normal(0, 0.3, 600))
    df = _ohlc_from_close(close)
    res = backtest_regime_switch(df, initial_equity=1000.0)
    # Equity shouldn't blow up or vanish on noise (no edge, but bounded).
    assert res.final_equity > 0.5 * res.initial_equity


def test_summarize_reports_core_metrics():
    df = _calm_then_uptrend()
    res = backtest_regime_switch(df, initial_equity=1000.0)
    stats = summarize(res)
    for key in (
        "n_trades", "win_rate", "sharpe", "max_drawdown",
        "strategy_apr", "exposure_pct", "pnl_by_leg",
    ):
        assert key in stats


def test_funding_reduces_long_pnl():
    """Positive funding should cost a long position money."""
    df = _calm_then_uptrend()
    # Constant positive funding every 8h.
    fhours = pd.date_range(df.index[0], df.index[-1], freq="8h", tz="UTC")
    funding = pd.Series(0.0005, index=fhours)  # 5 bps / 8h
    no_f = backtest_regime_switch(df, initial_equity=1000.0)
    with_f = backtest_regime_switch(df, initial_equity=1000.0, funding=funding)
    assert with_f.funding_applied is True
    # With mostly-long trend exposure, funding drag lowers the result.
    assert with_f.final_equity <= no_f.final_equity


def test_leverage_cap_limits_position_size():
    df = _calm_then_uptrend()
    # Tiny ATR stop would imply huge size; the cap must bound notional.
    res = backtest_regime_switch(
        df, initial_equity=1000.0, max_leverage=2.0, risk_per_trade_pct=0.5
    )
    for t in res.trades:
        notional = t["qty"] * t["entry_price"]
        # Cap is relative to equity AT ENTRY (which grows with wins).
        assert notional <= t["equity_at_entry"] * 2.0 + 1e-6
