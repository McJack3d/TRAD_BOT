"""Tests for the ablation knobs (disable individual legs) and the
CLI->params helper that lets us run targeted experiments without
re-coding the backtester."""

from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from scripts.backtest_regime_switch import _params_from_args
from src.backtest.regime_diagnostics import diagnose_regime
from src.backtest.regime_switch_backtest import backtest_regime_switch
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


def test_no_trend_leg_produces_zero_trend_entries():
    df = _calm_then_uptrend()
    p = RegimeSwitchParams(disable_trend_leg=True)
    res = backtest_regime_switch(df, params=p, initial_equity=1000.0)
    assert all(t["leg"] != "trend" for t in res.trades)


def test_no_range_leg_produces_zero_range_entries():
    df = _calm_then_uptrend()
    p = RegimeSwitchParams(disable_range_leg=True)
    res = backtest_regime_switch(df, params=p, initial_equity=1000.0)
    assert all(t["leg"] != "range" for t in res.trades)


def test_disable_both_legs_produces_no_trades():
    df = _calm_then_uptrend()
    p = RegimeSwitchParams(disable_trend_leg=True, disable_range_leg=True)
    res = backtest_regime_switch(df, params=p, initial_equity=1000.0)
    assert res.trades == []


def test_diagnose_still_counts_would_enter_when_leg_disabled():
    """Disabling a leg suppresses ENTRY decisions in the state machine,
    but the diagnostic measures the UPSTREAM signal arithmetic (regime
    + alignment) — those numbers must be unchanged so we can compare
    enabled vs disabled apples-to-apples."""
    df = _calm_then_uptrend()
    d_base = diagnose_regime(df, RegimeSwitchParams())
    d_off = diagnose_regime(df, RegimeSwitchParams(disable_range_leg=True))
    assert d_base["range_leg"]["would_enter"] == d_off["range_leg"]["would_enter"]
    assert d_base["trend_leg"]["would_enter"] == d_off["trend_leg"]["would_enter"]


def test_params_from_args_respects_overrides():
    args = argparse.Namespace(
        no_trend_leg=False, no_range_leg=True,
        adx_trend_min=20.0, adx_range_max=None,
        rv_high_pctile=0.5, rv_low_pctile=None,
        atr_mult=1.5, rsi_os=None, rsi_ob=None,
    )
    p = _params_from_args(args)
    assert p.adx_trend_min == 20.0
    assert p.adx_range_max == 20.0  # default preserved when not overridden
    assert p.rv_high_pctile == 0.5
    assert p.rv_low_pctile == 0.40  # default preserved
    assert p.atr_mult == 1.5
    assert p.disable_range_leg is True
    assert p.disable_trend_leg is False


def test_params_from_args_extra_overrides_take_precedence():
    """The sweep grid passes extra overrides on top of the CLI baseline."""
    args = argparse.Namespace(
        no_trend_leg=False, no_range_leg=False,
        adx_trend_min=25.0, atr_mult=2.0,
        adx_range_max=None, rv_high_pctile=None, rv_low_pctile=None,
        rsi_os=None, rsi_ob=None,
    )
    p = _params_from_args(args, adx_trend_min=30.0, atr_mult=1.5)
    assert p.adx_trend_min == 30.0  # grid wins over CLI
    assert p.atr_mult == 1.5


def test_params_from_args_handles_missing_attrs():
    """When called from older code paths that don't set every attr."""
    args = argparse.Namespace()  # no override attrs at all
    p = _params_from_args(args)
    # All defaults preserved.
    assert p.adx_trend_min == 25.0
    assert p.disable_trend_leg is False
    assert p.disable_range_leg is False
