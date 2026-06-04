"""Regression tests for the funding-arb daemon's risk leaks.

These lock in the fixes for the audited issues:

  * Daily + cumulative loss-stops were dead code because the daemon
    wrote `realized_pnl_daily = realized_pnl_cumulative = 0` into every
    snapshot. `build_state_snapshot` now derives real PnL, so the stops
    fire.
  * The pre-trade liquidation-distance, reconciliation-freshness, and
    clock-drift checks were stubbed with constants. They now reflect
    real state.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from src.config import BotConfig, RiskConfig, StrategyConfig, SymbolConfig
from src.risk.checks import check_liq_distance, run_pre_trade_checks
from src.risk.manager import RiskManager
from src.state.models import (
    FundingPayment,
    Position,
    PositionStatus,
    SystemStatusEnum,
)
from src.state.pnl import build_state_snapshot, compute_realized_pnl
from src.strategy.funding_arb import FundingArbStrategy


class _StubExchange:
    """Minimal exchange for the snapshot builder and risk manager."""

    def __init__(self, positions=None, balances=None, server_ms=None):
        self._positions = positions or []
        self._balances = balances or {}
        self._server_ms = server_ms

    async def fetch_positions(self):
        return self._positions

    async def fetch_balances(self):
        return self._balances

    async def fetch_server_time(self) -> int:
        if self._server_ms is None:
            raise RuntimeError("server time unavailable")
        return self._server_ms


class _StubMarketData:
    """Duck-typed stand-in — only `.snapshots` membership and `.get` are
    touched, and only when there are open positions."""

    snapshots: dict = {}

    def get(self, symbol):  # pragma: no cover - not hit with no open positions
        raise AssertionError("get() should not be called with no open positions")


# ---- realized PnL aggregation ---------------------------------------


@pytest.mark.asyncio
async def test_compute_realized_pnl_sums_positions_and_funding(db):
    now = datetime.now(UTC)
    pos = await db.create_position(
        Position(symbol="BTC/USDT", status=PositionStatus.OPEN, spot_qty=Decimal("0.1"))
    )
    await db.close_position(pos.id, realized_pnl=Decimal("-50"))
    await db.add_funding_payment(
        FundingPayment(
            symbol="BTC/USDT",
            funding_time=now,
            funding_rate=Decimal("0.0001"),
            notional=Decimal("1000"),
            payment=Decimal("5"),
            mark_price=Decimal("100"),
        )
    )
    daily, cumulative = await compute_realized_pnl(db, now)
    assert daily == Decimal("-45")  # -50 close + 5 funding
    assert cumulative == Decimal("-45")


@pytest.mark.asyncio
async def test_build_snapshot_has_real_pnl_not_zero(db):
    now = datetime.now(UTC)
    pos = await db.create_position(Position(symbol="ETH/USDT"))
    await db.close_position(pos.id, realized_pnl=Decimal("-30"))
    ex = _StubExchange()
    snap = await build_state_snapshot(db, ex, Decimal("1000"), now=now)
    assert snap.realized_pnl_daily == Decimal("-30")
    assert snap.realized_pnl_cumulative == Decimal("-30")
    # equity = starting + cumulative_realized + unrealized
    assert snap.equity_usdt == Decimal("970")


@pytest.mark.asyncio
async def test_realized_pnl_daily_excludes_prior_days(db):
    """A loss booked yesterday must not count toward today's daily stop."""
    now = datetime.now(UTC)
    pos = await db.create_position(Position(symbol="BTC/USDT"))
    await db.close_position(pos.id, realized_pnl=Decimal("-200"))
    # Backdate the close to 2 days ago.
    from sqlalchemy import update

    from src.state.models import Position as P

    async with db.session() as s:
        await s.execute(
            update(P).where(P.id == pos.id).values(closed_at=now - timedelta(days=2))
        )
        await s.commit()
    daily, cumulative = await compute_realized_pnl(db, now)
    assert daily == Decimal("0")  # nothing closed today
    assert cumulative == Decimal("-200")  # but cumulative still sees it


# ---- continuous monitor loss-stops ----------------------------------


def _risk(db, starting=Decimal("1000"), flatten_calls=None):
    async def on_flatten(reason: str) -> None:
        if flatten_calls is not None:
            flatten_calls.append(reason)

    return RiskManager(
        db=db,
        exchange=_StubExchange(),
        cfg=RiskConfig(),
        starting_equity=starting,
        on_flatten=on_flatten,
    )


@pytest.mark.asyncio
async def test_daily_loss_stop_fires(db):
    await db.touch_reconciliation_ok()  # keep the stale gate happy
    snap = await build_state_snapshot(db, _StubExchange(), Decimal("1000"))
    # Inject a daily drawdown of -25 (realized -15 + unrealized -10) >
    # the 2% * 1000 = 20 stop.
    snap.realized_pnl_daily = Decimal("-15")
    snap.unrealized_pnl = Decimal("-10")
    await db.add_snapshot(snap)

    calls: list[str] = []
    risk = _risk(db, flatten_calls=calls)
    await risk.tick()

    status = await db.get_status()
    assert status.status == SystemStatusEnum.HALTED
    assert calls and "daily loss stop" in calls[0]


@pytest.mark.asyncio
async def test_cumulative_loss_stop_fires(db):
    await db.touch_reconciliation_ok()
    snap = await build_state_snapshot(db, _StubExchange(), Decimal("1000"))
    snap.realized_pnl_cumulative = Decimal("-120")  # > 10% * 1000 = 100
    await db.add_snapshot(snap)

    calls: list[str] = []
    risk = _risk(db, flatten_calls=calls)
    await risk.tick()

    status = await db.get_status()
    assert status.status == SystemStatusEnum.HALTED
    assert calls and "cumulative loss stop" in calls[0]


@pytest.mark.asyncio
async def test_no_halt_when_within_limits(db):
    await db.touch_reconciliation_ok()
    snap = await build_state_snapshot(db, _StubExchange(), Decimal("1000"))
    snap.realized_pnl_daily = Decimal("-5")  # within the 20 stop
    await db.add_snapshot(snap)

    risk = _risk(db)
    await risk.tick()

    status = await db.get_status()
    assert status.status == SystemStatusEnum.ACTIVE


# ---- pre-trade leverage / reconciliation / clock-drift --------------


def _cfg(leverage: int) -> BotConfig:
    return BotConfig(
        starting_equity_usdt=Decimal("1000"),
        symbols=[SymbolConfig(spot="BTC/USDT", perp="BTC/USDT:USDT")],
        strategy=StrategyConfig(perp_leverage=leverage),
    )


@pytest.mark.asyncio
async def test_pretrade_liq_distance_tracks_leverage(db):
    await db.touch_reconciliation_ok()
    now_ms = int(datetime.now(UTC).timestamp() * 1000)

    strat2 = FundingArbStrategy(
        cfg=_cfg(2), db=db, market_data=_StubMarketData(),
        risk=None, execution=None, exchange=_StubExchange(server_ms=now_ms),
    )
    ctx2 = await strat2._build_pretrade_ctx("BTC/USDT", Decimal("100"), Decimal("1000"))
    assert ctx2.proposed_short_liq_distance_pct == Decimal("0.5")
    assert ctx2.reconciliation_ok is True
    assert ctx2.clock_drift_ms < 1000  # server time ~ local time

    strat4 = FundingArbStrategy(
        cfg=_cfg(4), db=db, market_data=_StubMarketData(),
        risk=None, execution=None, exchange=_StubExchange(server_ms=now_ms),
    )
    ctx4 = await strat4._build_pretrade_ctx("BTC/USDT", Decimal("100"), Decimal("1000"))
    assert ctx4.proposed_short_liq_distance_pct == Decimal("0.25")
    # 0.25 < 0.30 floor → the liquidation check now actually rejects it.
    assert check_liq_distance(ctx4, RiskConfig()).ok is False


@pytest.mark.asyncio
async def test_pretrade_reconciliation_stale_blocks(db):
    """With no recent reconciliation, the pre-trade ctx must report
    reconciliation_ok=False (it used to be hardcoded True)."""
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    strat = FundingArbStrategy(
        cfg=_cfg(2), db=db, market_data=_StubMarketData(),
        risk=None, execution=None, exchange=_StubExchange(server_ms=now_ms),
    )
    # db freshly initialised → last_reconciliation_ok is None.
    ctx = await strat._build_pretrade_ctx("BTC/USDT", Decimal("100"), Decimal("1000"))
    assert ctx.reconciliation_ok is False
    assert run_pre_trade_checks(ctx, RiskConfig()).ok is False


@pytest.mark.asyncio
async def test_clock_drift_detected(db):
    await db.touch_reconciliation_ok()
    # Server clock 5 seconds ahead → drift ~5000ms, well over the 100ms cap.
    skewed_ms = int(datetime.now(UTC).timestamp() * 1000) + 5000
    strat = FundingArbStrategy(
        cfg=_cfg(2), db=db, market_data=_StubMarketData(),
        risk=None, execution=None, exchange=_StubExchange(server_ms=skewed_ms),
    )
    ctx = await strat._build_pretrade_ctx("BTC/USDT", Decimal("100"), Decimal("1000"))
    assert ctx.clock_drift_ms >= 4000
    assert run_pre_trade_checks(ctx, RiskConfig()).ok is False
