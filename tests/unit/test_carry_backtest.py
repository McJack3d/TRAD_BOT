"""Tests for the two-sided funding-carry backtester.

Synthetic funding + borrow series on the 8h settlement grid, no network.
These verify the economics the acceptance gates depend on: net-of-borrow
income, round-trip cost, per-leg attribution, the borrow-cap kill, and
that the gate checker reports the four spec §6 criteria.
"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd

from src.backtest.carry_backtest import (
    backtest_carry,
    carry_acceptance_gates,
    gates_summary,
)
from src.strategy.funding_carry import CarrySide


def _grid(n: int, start: str = "2022-01-01") -> pd.DatetimeIndex:
    return pd.date_range(start, periods=n, freq="8h", tz="UTC")


def _oscillating_negative(
    cycles: int, hi_len: int = 8, lo_len: int = 3, hi: float = -0.0008, lo: float = -0.00001
) -> pd.Series:
    """Repeating blocks of strong-negative funding (enter) followed by
    near-zero funding (decay → exit). Produces `cycles` distinct episodes."""
    vals: list[float] = []
    for _ in range(cycles):
        vals += [hi] * hi_len + [lo] * lo_len
    return pd.Series(vals, index=_grid(len(vals)))


def _const_borrow(funding: pd.Series, apr: float) -> pd.Series:
    """Daily borrow series spanning the funding window — exercises the
    daily→8h forward-fill in the backtester."""
    days = pd.date_range(
        funding.index[0].normalize(),
        funding.index[-1].normalize() + pd.Timedelta(days=1),
        freq="1D",
    )
    return pd.Series(apr, index=days)


# ---- negative leg ----------------------------------------------------


def test_negative_leg_trades_and_is_net_positive_with_cheap_borrow():
    funding = _oscillating_negative(cycles=22)
    borrow = _const_borrow(funding, 0.05)  # 5% APR, well under the 15% cap
    res = backtest_carry(funding, borrow, "BTC/USDT", initial_equity=Decimal("1000"))

    assert res.negative.n_episodes >= 20
    assert res.negative.total_return > 0  # carry beats fees + borrow
    assert res.negative.settlements_held > 0
    # The positive leg never traded in an all-negative series.
    assert res.positive.n_episodes == 0
    assert res.positive.total_return == 0.0
    assert res.final_equity > res.initial_equity


def test_borrow_above_cap_blocks_all_negative_entries():
    funding = _oscillating_negative(cycles=22)
    borrow = _const_borrow(funding, 0.20)  # 20% APR > 15% cap → kill switch
    res = backtest_carry(funding, borrow, "BTC/USDT")

    assert res.negative.n_episodes == 0
    assert res.negative.settlements_held == 0
    assert res.final_equity == res.initial_equity  # never traded


def test_negative_leg_refused_for_non_universe_symbol():
    funding = _oscillating_negative(cycles=22)
    borrow = _const_borrow(funding, 0.05)
    res = backtest_carry(funding, borrow, "SOL/USDT")  # outside BTC/ETH
    assert res.negative.n_episodes == 0


def test_fees_make_a_marginal_carry_unprofitable():
    """A negative funding that only just clears the entry threshold should
    be eaten by the round-trip cost — proving fees are actually charged."""
    # |f| − borrow_8h just over 0.0003: f = -0.00035, borrow 5% (8h≈0.0000457)
    # net ≈ 0.000304. A handful of settlements can't out-earn ~24bps round trip.
    funding = _oscillating_negative(cycles=10, hi_len=4, hi=-0.00035)
    borrow = _const_borrow(funding, 0.05)
    res = backtest_carry(funding, borrow, "BTC/USDT")
    assert res.negative.n_episodes >= 1  # it does enter
    assert res.negative.total_return < 0  # but loses to fees


# ---- positive leg ----------------------------------------------------


def test_positive_leg_trades_on_positive_funding():
    funding = pd.Series([0.0004] * 30, index=_grid(30))
    res = backtest_carry(funding, None, "BTC/USDT")
    assert res.positive.settlements_held > 0
    assert res.positive.total_return > 0
    assert res.negative.n_episodes == 0
    assert res.final_equity > res.initial_equity


def test_positive_leg_with_no_borrow_series_still_works():
    """The positive leg never needs borrow data; passing None must be fine."""
    funding = pd.Series([0.0004] * 20 + [0.0] * 5, index=_grid(25))
    res = backtest_carry(funding, None, "BTC/USDT")
    assert res.positive.total_return > 0


# ---- attribution & curves --------------------------------------------


def test_per_leg_attribution_is_mutually_exclusive():
    """Combined return = positive + negative contributions (only one leg
    is ever open). A mixed series should populate both legs."""
    pos = pd.Series([0.0004] * 20, index=_grid(20, "2022-01-01"))
    neg = _oscillating_negative(cycles=10)
    # Stitch: positive block, then negative blocks, on one continuous grid.
    funding = pd.concat([pos, neg]).reset_index(drop=True)
    funding.index = _grid(len(funding))
    borrow = _const_borrow(funding, 0.05)
    res = backtest_carry(funding, borrow, "BTC/USDT")
    assert res.positive.settlements_held > 0
    assert res.negative.settlements_held > 0
    assert not res.equity_curve.empty
    assert len(res.equity_curve) == len(funding)


def test_empty_funding_returns_flat_result():
    res = backtest_carry(pd.Series(dtype=float), None, "BTC/USDT")
    assert res.final_equity == res.initial_equity
    assert res.equity_curve.empty
    assert res.negative.n_episodes == 0
    assert res.combined_sharpe == 0.0


def test_idle_leg_has_zero_sharpe_not_nan():
    funding = pd.Series([0.0004] * 20, index=_grid(20))
    res = backtest_carry(funding, None, "BTC/USDT")
    # Negative leg never traded → Sharpe must be a clean 0.0, never NaN.
    assert res.negative.sharpe == 0.0
    assert res.negative.max_drawdown == 0.0


# ---- acceptance gates ------------------------------------------------


def test_acceptance_gates_report_four_criteria():
    funding = _oscillating_negative(cycles=22)
    borrow = _const_borrow(funding, 0.05)
    res = backtest_carry(funding, borrow, "BTC/USDT")
    gates = carry_acceptance_gates(res)
    assert len(gates) == 4
    names = " ".join(g.name for g in gates)
    assert "net-positive" in names
    assert "Sharpe" in names
    assert "episodes" in names
    assert "max DD" in names
    # net-positive and episode-count gates should pass on this good series.
    by_name = {g.name: g for g in gates}
    assert by_name["negative leg net-positive after borrow + fees"].passed
    assert by_name["≥ 20 distinct negative-funding episodes traded"].passed


def test_gates_summary_renders_pass_fail():
    funding = _oscillating_negative(cycles=22)
    borrow = _const_borrow(funding, 0.05)
    res = backtest_carry(funding, borrow, "BTC/USDT")
    text = gates_summary(carry_acceptance_gates(res))
    assert isinstance(text, str)
    assert "PASS" in text or "FAIL" in text


def test_gates_fail_when_negative_leg_never_trades():
    funding = _oscillating_negative(cycles=22)
    borrow = _const_borrow(funding, 0.20)  # blocked by cap
    res = backtest_carry(funding, borrow, "BTC/USDT")
    gates = carry_acceptance_gates(res)
    assert not all(g.passed for g in gates)
    assert "GATES FAILED" in gates_summary(gates)


def test_episodes_carry_side_tag():
    funding = _oscillating_negative(cycles=22)
    borrow = _const_borrow(funding, 0.05)
    res = backtest_carry(funding, borrow, "BTC/USDT")
    assert res.episodes
    assert all(e.side == CarrySide.NEGATIVE for e in res.episodes)
    assert all(e.n_settlements > 0 for e in res.episodes)
