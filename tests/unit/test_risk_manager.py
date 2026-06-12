"""Adversarial risk-manager test suite.

Spec §15 acceptance: at least 30 adversarial cases must pass. Each test
focuses on a single failure mode of pre-trade or continuous-monitoring
logic, with deliberately gnarly inputs (NaN, negative margin, duplicate
fills, edge-of-tolerance values, overflow-adjacent numbers, etc.).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.config import RiskConfig
from src.risk.checks import (
    PreTradeContext,
    check_clock_drift,
    check_cumulative_loss,
    check_daily_loss,
    check_inputs_sane,
    check_liq_distance,
    check_order_rate,
    check_per_symbol_exposure,
    check_reconciliation,
    check_system_active,
    check_total_exposure,
    run_pre_trade_checks,
)
from src.state.models import SystemStatusEnum


def _ctx(**overrides) -> PreTradeContext:
    base = dict(
        equity=Decimal("1000"),
        starting_equity=Decimal("1000"),
        total_exposure=Decimal("0"),
        per_symbol_exposure={},
        proposed_symbol="BTC/USDT",
        proposed_notional=Decimal("100"),
        proposed_short_liq_distance_pct=Decimal("0.50"),
        orders_in_last_minute=0,
        daily_realized_pnl=Decimal("0"),
        daily_unrealized_pnl=Decimal("0"),
        cumulative_realized_pnl=Decimal("0"),
        reconciliation_ok=True,
        system_status=SystemStatusEnum.ACTIVE,
        clock_drift_ms=0,
    )
    base.update(overrides)
    return PreTradeContext(**base)


# ---------------------------------------------------------------------
# 1. Happy path baseline
# ---------------------------------------------------------------------
def test_01_happy_path_passes(risk_cfg: RiskConfig) -> None:
    assert run_pre_trade_checks(_ctx(), risk_cfg).ok


# ---------------------------------------------------------------------
# Input-sanity adversarial cases
# ---------------------------------------------------------------------
def test_02_nan_equity_rejected(risk_cfg: RiskConfig) -> None:
    res = check_inputs_sane(_ctx(equity=Decimal("NaN")))
    assert not res.ok and "non-finite" in res.reason


def test_03_infinite_total_exposure_rejected(risk_cfg: RiskConfig) -> None:
    res = check_inputs_sane(_ctx(total_exposure=Decimal("Infinity")))
    assert not res.ok


def test_04_zero_equity_rejected(risk_cfg: RiskConfig) -> None:
    res = check_inputs_sane(_ctx(equity=Decimal("0")))
    assert not res.ok and "non-positive equity" in res.reason


def test_05_negative_equity_rejected(risk_cfg: RiskConfig) -> None:
    res = check_inputs_sane(_ctx(equity=Decimal("-1")))
    assert not res.ok


def test_06_zero_proposed_notional_rejected(risk_cfg: RiskConfig) -> None:
    res = check_inputs_sane(_ctx(proposed_notional=Decimal("0")))
    assert not res.ok


def test_07_negative_proposed_notional_rejected(risk_cfg: RiskConfig) -> None:
    res = check_inputs_sane(_ctx(proposed_notional=Decimal("-50")))
    assert not res.ok


def test_08_nan_per_symbol_exposure_rejected(risk_cfg: RiskConfig) -> None:
    res = check_inputs_sane(_ctx(per_symbol_exposure={"BTC/USDT": Decimal("NaN")}))
    assert not res.ok


def test_09_negative_order_count_rejected(risk_cfg: RiskConfig) -> None:
    res = check_inputs_sane(_ctx(orders_in_last_minute=-1))
    assert not res.ok


def test_10_zero_starting_equity_rejected(risk_cfg: RiskConfig) -> None:
    res = check_inputs_sane(_ctx(starting_equity=Decimal("0")))
    assert not res.ok


# ---------------------------------------------------------------------
# System status
# ---------------------------------------------------------------------
def test_11_paused_system_rejected(risk_cfg: RiskConfig) -> None:
    res = check_system_active(_ctx(system_status=SystemStatusEnum.PAUSED))
    assert not res.ok


def test_12_halted_system_rejected(risk_cfg: RiskConfig) -> None:
    res = check_system_active(_ctx(system_status=SystemStatusEnum.HALTED))
    assert not res.ok


# ---------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------
def test_13_reconciliation_failed_rejected(risk_cfg: RiskConfig) -> None:
    res = check_reconciliation(_ctx(reconciliation_ok=False))
    assert not res.ok


# ---------------------------------------------------------------------
# Per-symbol exposure
# ---------------------------------------------------------------------
def test_14_per_symbol_exposure_at_cap_passes(risk_cfg: RiskConfig) -> None:
    cap = Decimal("1000") * risk_cfg.max_gross_notional_pct
    res = check_per_symbol_exposure(
        _ctx(
            per_symbol_exposure={"BTC/USDT": cap - Decimal("100")},
            proposed_notional=Decimal("100"),
        ),
        risk_cfg,
    )
    assert res.ok


def test_15_per_symbol_exposure_one_cent_over_rejected(risk_cfg: RiskConfig) -> None:
    cap = Decimal("1000") * risk_cfg.max_gross_notional_pct
    res = check_per_symbol_exposure(
        _ctx(
            per_symbol_exposure={"BTC/USDT": cap - Decimal("99.99")},
            proposed_notional=Decimal("100"),
        ),
        risk_cfg,
    )
    assert not res.ok and "per-symbol exposure breach" in res.reason


def test_16_per_symbol_only_target_symbol_counts(risk_cfg: RiskConfig) -> None:
    """An entry for BTC shouldn't fail because ETH has a position."""
    res = check_per_symbol_exposure(
        _ctx(
            proposed_symbol="BTC/USDT",
            per_symbol_exposure={"ETH/USDT": Decimal("499")},
            proposed_notional=Decimal("100"),
        ),
        risk_cfg,
    )
    assert res.ok


# ---------------------------------------------------------------------
# Total exposure
# ---------------------------------------------------------------------
def test_17_total_exposure_at_cap_passes(risk_cfg: RiskConfig) -> None:
    cap = Decimal("1000") * risk_cfg.max_total_exposure_pct
    res = check_total_exposure(
        _ctx(total_exposure=cap - Decimal("100"), proposed_notional=Decimal("100")),
        risk_cfg,
    )
    assert res.ok


def test_18_total_exposure_one_unit_over_rejected(risk_cfg: RiskConfig) -> None:
    cap = Decimal("1000") * risk_cfg.max_total_exposure_pct
    res = check_total_exposure(
        _ctx(total_exposure=cap, proposed_notional=Decimal("1")),
        risk_cfg,
    )
    assert not res.ok


# ---------------------------------------------------------------------
# Liquidation distance
# ---------------------------------------------------------------------
def test_19_liq_distance_exactly_at_min_passes(risk_cfg: RiskConfig) -> None:
    res = check_liq_distance(
        _ctx(proposed_short_liq_distance_pct=risk_cfg.pre_trade_min_liq_distance_pct),
        risk_cfg,
    )
    assert res.ok


def test_20_liq_distance_just_below_min_rejected(risk_cfg: RiskConfig) -> None:
    res = check_liq_distance(
        _ctx(proposed_short_liq_distance_pct=risk_cfg.pre_trade_min_liq_distance_pct - Decimal("0.0001")),
        risk_cfg,
    )
    assert not res.ok


def test_21_liq_distance_zero_rejected(risk_cfg: RiskConfig) -> None:
    res = check_liq_distance(_ctx(proposed_short_liq_distance_pct=Decimal("0")), risk_cfg)
    assert not res.ok


# ---------------------------------------------------------------------
# Order rate limit
# ---------------------------------------------------------------------
def test_22_order_rate_just_under_limit_passes(risk_cfg: RiskConfig) -> None:
    res = check_order_rate(
        _ctx(orders_in_last_minute=risk_cfg.max_orders_per_minute - 1), risk_cfg
    )
    assert res.ok


def test_23_order_rate_at_limit_rejected(risk_cfg: RiskConfig) -> None:
    res = check_order_rate(
        _ctx(orders_in_last_minute=risk_cfg.max_orders_per_minute), risk_cfg
    )
    assert not res.ok


def test_24_order_rate_runaway_rejected(risk_cfg: RiskConfig) -> None:
    res = check_order_rate(_ctx(orders_in_last_minute=10_000), risk_cfg)
    assert not res.ok


# ---------------------------------------------------------------------
# Daily loss stop
# ---------------------------------------------------------------------
def test_25_daily_loss_just_under_stop_passes(risk_cfg: RiskConfig) -> None:
    stop = Decimal("1000") * risk_cfg.daily_loss_stop_pct
    res = check_daily_loss(
        _ctx(daily_realized_pnl=-(stop - Decimal("0.01"))), risk_cfg
    )
    assert res.ok


def test_26_daily_loss_at_stop_rejected(risk_cfg: RiskConfig) -> None:
    stop = Decimal("1000") * risk_cfg.daily_loss_stop_pct
    res = check_daily_loss(_ctx(daily_realized_pnl=-stop), risk_cfg)
    assert not res.ok


def test_27_daily_loss_realized_plus_unrealized_summed(risk_cfg: RiskConfig) -> None:
    stop = Decimal("1000") * risk_cfg.daily_loss_stop_pct
    res = check_daily_loss(
        _ctx(
            daily_realized_pnl=-stop / 2 - Decimal("0.01"),
            daily_unrealized_pnl=-stop / 2,
        ),
        risk_cfg,
    )
    assert not res.ok


def test_28_daily_unrealized_alone_can_trip(risk_cfg: RiskConfig) -> None:
    stop = Decimal("1000") * risk_cfg.daily_loss_stop_pct
    res = check_daily_loss(_ctx(daily_unrealized_pnl=-stop), risk_cfg)
    assert not res.ok


# ---------------------------------------------------------------------
# Cumulative loss stop
# ---------------------------------------------------------------------
def test_29_cumulative_loss_at_stop_rejected(risk_cfg: RiskConfig) -> None:
    stop = Decimal("1000") * risk_cfg.cumulative_loss_stop_pct
    res = check_cumulative_loss(_ctx(cumulative_realized_pnl=-stop), risk_cfg)
    assert not res.ok


def test_30_cumulative_loss_just_under_passes(risk_cfg: RiskConfig) -> None:
    stop = Decimal("1000") * risk_cfg.cumulative_loss_stop_pct
    res = check_cumulative_loss(
        _ctx(cumulative_realized_pnl=-(stop - Decimal("0.01"))), risk_cfg
    )
    assert res.ok


# ---------------------------------------------------------------------
# Clock drift
# ---------------------------------------------------------------------
def test_31_clock_drift_at_limit_passes(risk_cfg: RiskConfig) -> None:
    res = check_clock_drift(_ctx(clock_drift_ms=risk_cfg.max_clock_drift_ms), risk_cfg)
    assert res.ok


def test_32_clock_drift_one_ms_over_rejected(risk_cfg: RiskConfig) -> None:
    res = check_clock_drift(
        _ctx(clock_drift_ms=risk_cfg.max_clock_drift_ms + 1), risk_cfg
    )
    assert not res.ok


def test_33_negative_clock_drift_compared_absolutely(risk_cfg: RiskConfig) -> None:
    res = check_clock_drift(
        _ctx(clock_drift_ms=-(risk_cfg.max_clock_drift_ms + 1)), risk_cfg
    )
    assert not res.ok


# ---------------------------------------------------------------------
# Composite / first-failure-wins behaviour
# ---------------------------------------------------------------------
def test_34_first_failure_short_circuits(risk_cfg: RiskConfig) -> None:
    """Multiple failures present → returns the first one encountered."""
    res = run_pre_trade_checks(
        _ctx(
            equity=Decimal("NaN"),
            system_status=SystemStatusEnum.HALTED,
        ),
        risk_cfg,
    )
    assert not res.ok and "non-finite" in res.reason


def test_35_all_clean_passes_run(risk_cfg: RiskConfig) -> None:
    res = run_pre_trade_checks(_ctx(), risk_cfg)
    assert res.ok


def test_36_tiny_proposed_notional_passes(risk_cfg: RiskConfig) -> None:
    res = run_pre_trade_checks(_ctx(proposed_notional=Decimal("0.01")), risk_cfg)
    assert res.ok


def test_37_huge_proposed_notional_rejected_by_exposure(risk_cfg: RiskConfig) -> None:
    res = run_pre_trade_checks(_ctx(proposed_notional=Decimal("1e6")), risk_cfg)
    assert not res.ok


def test_38_decimal_precision_extremes(risk_cfg: RiskConfig) -> None:
    """Very small but finite Decimal values must not be coerced to 0/nan."""
    res = run_pre_trade_checks(
        _ctx(proposed_notional=Decimal("0.0000000001")), risk_cfg
    )
    assert res.ok


def test_39_equity_drop_during_open_trade_protects_against_new_entry(
    risk_cfg: RiskConfig,
) -> None:
    """If equity has collapsed but existing exposure is still on the book,
    a new entry must be sized against current (smaller) equity, not starting."""
    res = run_pre_trade_checks(
        _ctx(
            equity=Decimal("100"),
            per_symbol_exposure={"BTC/USDT": Decimal("60")},
            proposed_notional=Decimal("10"),  # cap is 50 → 60+10=70 over
        ),
        risk_cfg,
    )
    assert not res.ok


def test_40_paused_status_blocks_even_with_pristine_ctx(risk_cfg: RiskConfig) -> None:
    res = run_pre_trade_checks(_ctx(system_status=SystemStatusEnum.PAUSED), risk_cfg)
    assert not res.ok and "system not ACTIVE" in res.reason


# ---------------------------------------------------------------------
# Sanity-check: count of adversarial cases meets §15 acceptance gate
# ---------------------------------------------------------------------
@pytest.mark.parametrize("dummy", [0])
def test_99_adversarial_count_meets_spec(dummy: int) -> None:
    import inspect

    from tests.unit import test_risk_manager  # noqa: PLW0406 — introspects its own module

    test_funcs = [
        name
        for name, fn in inspect.getmembers(test_risk_manager, inspect.isfunction)
        if name.startswith("test_") and name != "test_99_adversarial_count_meets_spec"
    ]
    assert len(test_funcs) >= 30, f"only {len(test_funcs)} cases; spec requires >= 30"
