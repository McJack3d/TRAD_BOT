"""Pure pre-trade checks.

Each check is a pure function over a `PreTradeContext` snapshot and
returns a `CheckResult`. The order is rejected if any check fails.

These are exhaustively tested with adversarial inputs in
`tests/unit/test_risk_manager.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from src.config import RiskConfig
from src.state.models import SystemStatusEnum


@dataclass(slots=True)
class CheckResult:
    ok: bool
    reason: str = ""

    @classmethod
    def pass_(cls) -> "CheckResult":
        return cls(ok=True)

    @classmethod
    def fail(cls, reason: str) -> "CheckResult":
        return cls(ok=False, reason=reason)


@dataclass(slots=True)
class PreTradeContext:
    """Snapshot of the world at the moment a pre-trade check runs."""

    equity: Decimal
    starting_equity: Decimal
    total_exposure: Decimal  # current gross notional across all symbols
    per_symbol_exposure: dict[str, Decimal]
    proposed_symbol: str
    proposed_notional: Decimal
    proposed_short_liq_distance_pct: Decimal  # post-order, fraction of margin
    orders_in_last_minute: int
    daily_realized_pnl: Decimal  # negative = loss
    daily_unrealized_pnl: Decimal
    cumulative_realized_pnl: Decimal
    reconciliation_ok: bool
    system_status: SystemStatusEnum
    clock_drift_ms: int = 0


def _is_finite(d: Decimal) -> bool:
    return d.is_finite()


def check_inputs_sane(ctx: PreTradeContext) -> CheckResult:
    """Reject any NaN / non-finite numeric input from upstream feeds."""
    fields = [
        ctx.equity,
        ctx.starting_equity,
        ctx.total_exposure,
        ctx.proposed_notional,
        ctx.proposed_short_liq_distance_pct,
        ctx.daily_realized_pnl,
        ctx.daily_unrealized_pnl,
        ctx.cumulative_realized_pnl,
    ]
    if not all(_is_finite(f) for f in fields):
        return CheckResult.fail("non-finite numeric input")
    for v in ctx.per_symbol_exposure.values():
        if not _is_finite(v):
            return CheckResult.fail("non-finite per-symbol exposure")
    if ctx.equity <= 0:
        return CheckResult.fail(f"non-positive equity: {ctx.equity}")
    if ctx.starting_equity <= 0:
        return CheckResult.fail(f"non-positive starting equity: {ctx.starting_equity}")
    if ctx.proposed_notional <= 0:
        return CheckResult.fail(f"non-positive proposed notional: {ctx.proposed_notional}")
    if ctx.orders_in_last_minute < 0:
        return CheckResult.fail("negative order count")
    return CheckResult.pass_()


def check_system_active(ctx: PreTradeContext) -> CheckResult:
    if ctx.system_status != SystemStatusEnum.ACTIVE:
        return CheckResult.fail(f"system not ACTIVE: {ctx.system_status.value}")
    return CheckResult.pass_()


def check_reconciliation(ctx: PreTradeContext) -> CheckResult:
    if not ctx.reconciliation_ok:
        return CheckResult.fail("reconciliation not OK")
    return CheckResult.pass_()


def check_per_symbol_exposure(ctx: PreTradeContext, cfg: RiskConfig) -> CheckResult:
    cap = ctx.equity * cfg.max_gross_notional_pct
    current = ctx.per_symbol_exposure.get(ctx.proposed_symbol, Decimal("0"))
    post = current + ctx.proposed_notional
    if post > cap:
        return CheckResult.fail(
            f"per-symbol exposure breach: {post} > cap {cap} ({ctx.proposed_symbol})"
        )
    return CheckResult.pass_()


def check_total_exposure(ctx: PreTradeContext, cfg: RiskConfig) -> CheckResult:
    cap = ctx.equity * cfg.max_total_exposure_pct
    post = ctx.total_exposure + ctx.proposed_notional
    if post > cap:
        return CheckResult.fail(f"total exposure breach: {post} > cap {cap}")
    return CheckResult.pass_()


def check_liq_distance(ctx: PreTradeContext, cfg: RiskConfig) -> CheckResult:
    if ctx.proposed_short_liq_distance_pct < cfg.pre_trade_min_liq_distance_pct:
        return CheckResult.fail(
            f"post-order liq distance {ctx.proposed_short_liq_distance_pct} < "
            f"min {cfg.pre_trade_min_liq_distance_pct}"
        )
    return CheckResult.pass_()


def check_order_rate(ctx: PreTradeContext, cfg: RiskConfig) -> CheckResult:
    if ctx.orders_in_last_minute >= cfg.max_orders_per_minute:
        return CheckResult.fail(
            f"order rate limit: {ctx.orders_in_last_minute} in last 60s "
            f">= {cfg.max_orders_per_minute}"
        )
    return CheckResult.pass_()


def check_daily_loss(ctx: PreTradeContext, cfg: RiskConfig) -> CheckResult:
    stop = ctx.starting_equity * cfg.daily_loss_stop_pct
    total_daily = ctx.daily_realized_pnl + ctx.daily_unrealized_pnl
    if total_daily <= -stop:
        return CheckResult.fail(f"daily loss stop hit: {total_daily} <= -{stop}")
    return CheckResult.pass_()


def check_cumulative_loss(ctx: PreTradeContext, cfg: RiskConfig) -> CheckResult:
    stop = ctx.starting_equity * cfg.cumulative_loss_stop_pct
    if ctx.cumulative_realized_pnl <= -stop:
        return CheckResult.fail(
            f"cumulative loss stop hit: {ctx.cumulative_realized_pnl} <= -{stop}"
        )
    return CheckResult.pass_()


def check_clock_drift(ctx: PreTradeContext, cfg: RiskConfig) -> CheckResult:
    if abs(ctx.clock_drift_ms) > cfg.max_clock_drift_ms:
        return CheckResult.fail(
            f"clock drift {ctx.clock_drift_ms}ms exceeds {cfg.max_clock_drift_ms}ms"
        )
    return CheckResult.pass_()


PRE_TRADE_CHECKS = (
    check_inputs_sane,
    check_system_active,
    check_reconciliation,
    check_per_symbol_exposure,
    check_total_exposure,
    check_liq_distance,
    check_order_rate,
    check_daily_loss,
    check_cumulative_loss,
    check_clock_drift,
)


def run_pre_trade_checks(ctx: PreTradeContext, cfg: RiskConfig) -> CheckResult:
    """Run all checks in order; return first failure or pass."""
    for check in PRE_TRADE_CHECKS:
        # Some checks need the config, some don't.
        try:
            result = check(ctx, cfg)  # type: ignore[call-arg]
        except TypeError:
            result = check(ctx)  # type: ignore[call-arg]
        if not result.ok:
            return result
    return CheckResult.pass_()
