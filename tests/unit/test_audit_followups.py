"""Regression tests for the second round of audit follow-ups.

Covers:
- Risk manager halts (and flattens) when a margin top-up fails on a
  position that is approaching liquidation.
- Reconciler bounds the USDT change between consecutive runs: an
  unexplained drop (no recent orders) halts; the same drop with order
  activity is logged only.
- _wait_terminal cancels an order that never reaches a terminal state
  so nothing rests on the book untracked.
- open_pair sells surplus spot when the perp hedge fills smaller than
  the spot bought, recording a matched, delta-neutral position.
- The trend bot's spot executor recovers the true order state after a
  failed submit instead of blindly recording REJECTED.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from src.adapters.exchange_base import ExchangeOrder, ExchangePosition
from src.adapters.fake import FakeExchange
from src.config import BotConfig, Mode, RiskConfig, SymbolConfig
from src.execution import spot_only
from src.execution.engine import ExecutionEngine
from src.reconciliation.reconciler import Reconciler
from src.risk.manager import RiskManager
from src.state.db import Database
from src.state.models import (
    OrderStatus,
    PositionStatus,
    SystemStatusEnum,
)


def _cfg() -> BotConfig:
    return BotConfig(
        mode=Mode.PAPER,
        starting_equity_eur=Decimal("1000"),
        symbols=[
            SymbolConfig(spot="BTC/USDT", perp="BTC/USDT:USDT", min_qty=Decimal("0.00001"))
        ],
    )


def _seed_prices(ex: FakeExchange) -> None:
    ex.set_ticker("BTC/USDT", "spot", Decimal("30000"))
    ex.set_ticker("BTC/USDT:USDT", "perp", Decimal("30010"))


async def _db(tmp_path: Path) -> Database:
    d = Database(str(tmp_path / "followups.db"))
    await d.init(starting_equity=Decimal("1000"))
    return d


# ---- risk manager: margin top-up failure halts -----------------------


class _MarginFailExchange:
    """Short perp sitting between the top-up (30%) and halt (20%)
    thresholds; add_margin always fails."""

    def __init__(self) -> None:
        # liq distance: entry 100, mark 115, liq 120, qty -1 → 25% headroom.
        self._positions = [
            ExchangePosition(
                symbol="BTC/USDT:USDT",
                leg="perp",
                qty=Decimal("-1"),
                entry_price=Decimal("100"),
                mark_price=Decimal("115"),
                liquidation_price=Decimal("120"),
                margin=Decimal("20"),
                unrealized_pnl=Decimal("-15"),
            )
        ]

    async def fetch_positions(self):
        return self._positions

    async def fetch_balances(self):
        return {}

    async def add_margin(self, symbol: str, amount: Decimal) -> None:
        raise RuntimeError("insufficient transferable balance")


@pytest.mark.asyncio
async def test_margin_top_up_failure_halts_and_flattens(tmp_path):
    db = await _db(tmp_path)
    await db.touch_reconciliation_ok()
    flatten_calls: list[str] = []

    async def on_flatten(reason: str) -> None:
        flatten_calls.append(reason)

    risk = RiskManager(
        db=db,
        exchange=_MarginFailExchange(),
        cfg=RiskConfig(),
        starting_equity=Decimal("1000"),
        on_flatten=on_flatten,
    )
    await risk.tick()

    status = await db.get_status()
    assert status.status == SystemStatusEnum.HALTED
    assert "margin top-up failed" in status.halt_reason
    assert flatten_calls and "margin top-up failed" in flatten_calls[0]


# ---- reconciler: USDT drift between consecutive runs ------------------


@pytest.mark.asyncio
async def test_unexplained_balance_drop_halts(tmp_path):
    db = await _db(tmp_path)
    ex = FakeExchange(starting_usdt=Decimal("4000"))
    rec = Reconciler(
        db=db,
        exchange=ex,
        cfg=_cfg().reconciliation,
        perp_to_spot={"BTC/USDT:USDT": "BTC/USDT"},
    )

    first = await rec.run_once()
    assert first.ok

    # Drain USDT with no orders recorded — looks like a withdrawal or
    # external trading, which the bot can never explain.
    ex._debit("spot", "USDT", Decimal("500"))
    second = await rec.run_once()

    assert not second.ok
    assert any("USDT balance dropped" in d for d in second.drifts)
    status = await db.get_status()
    assert status.status == SystemStatusEnum.HALTED


@pytest.mark.asyncio
async def test_balance_drop_with_order_activity_tolerated(tmp_path):
    db = await _db(tmp_path)
    ex = FakeExchange(starting_usdt=Decimal("4000"))
    _seed_prices(ex)
    rec = Reconciler(
        db=db,
        exchange=ex,
        cfg=_cfg().reconciliation,
        perp_to_spot={"BTC/USDT:USDT": "BTC/USDT"},
    )

    first = await rec.run_once()
    assert first.ok

    # A real trade explains the spend: open a pair through the engine,
    # which records orders in the DB and moves USDT on the exchange.
    engine = ExecutionEngine(cfg=_cfg(), db=db, exchange=ex, dry_run=False)
    pos = await engine.open_pair("BTC/USDT", Decimal("300"))
    assert pos is not None

    second = await rec.run_once()
    assert not any("USDT balance dropped" in d for d in second.drifts)
    status = await db.get_status()
    assert status.status == SystemStatusEnum.ACTIVE


# ---- _wait_terminal: cancel non-terminal orders -----------------------


class _StuckOrderExchange(FakeExchange):
    """Orders never leave NEW; records whether cancel was requested."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cancelled: list[str] = []

    async def submit_order(self, symbol, leg, side, qty, client_order_id, price=None, reduce_only=False):
        order = await super().submit_order(symbol, leg, side, qty, client_order_id, price, reduce_only)
        stuck = ExchangeOrder(
            client_order_id=order.client_order_id,
            exchange_order_id=order.exchange_order_id,
            symbol=order.symbol,
            leg=order.leg,
            side=order.side,
            qty=order.qty,
            filled_qty=Decimal("0"),
            avg_price=Decimal("0"),
            status="new",
            fee_paid=Decimal("0"),
            fee_asset="USDT",
            ts=order.ts,
        )
        self._orders[client_order_id] = stuck
        return stuck

    async def cancel_order(self, client_order_id, symbol, leg):
        self.cancelled.append(client_order_id)


@pytest.mark.asyncio
async def test_wait_terminal_cancels_stuck_order(tmp_path):
    db = await _db(tmp_path)
    ex = _StuckOrderExchange(starting_usdt=Decimal("4000"))
    _seed_prices(ex)
    engine = ExecutionEngine(cfg=_cfg(), db=db, exchange=ex, dry_run=False)

    stuck = await ex.submit_order(
        "BTC/USDT", "spot", "buy", Decimal("0.01"), "cb-test123"
    )
    result = await engine._wait_terminal(stuck, "BTC/USDT", "spot", "cb-test123", max_wait_seconds=0)

    assert ex.cancelled == ["cb-test123"]
    assert result.filled_qty == 0


# ---- open_pair: trim surplus spot on partial perp fill ----------------


class _PartialPerpExchange(FakeExchange):
    """Perp orders fill at half the requested quantity."""

    async def submit_order(self, symbol, leg, side, qty, client_order_id, price=None, reduce_only=False):
        if leg == "perp":
            qty = qty / 2
        return await super().submit_order(symbol, leg, side, qty, client_order_id, price, reduce_only)


@pytest.mark.asyncio
async def test_open_pair_partial_perp_fill_trims_spot(tmp_path):
    db = await _db(tmp_path)
    ex = _PartialPerpExchange(starting_usdt=Decimal("4000"))
    _seed_prices(ex)
    engine = ExecutionEngine(cfg=_cfg(), db=db, exchange=ex, dry_run=False)

    pos = await engine.open_pair("BTC/USDT", Decimal("300"))
    assert pos is not None
    # The recorded position must be delta-neutral: spot matches the perp
    # hedge that actually filled, and the surplus spot was sold back.
    assert pos.spot_qty == abs(pos.perp_qty)
    sides = [(o.leg, o.side) for o in ex._orders.values()]
    assert ("spot", "sell") in sides  # surplus trim happened


# ---- spot_only: recover order state after failed submit ---------------


class _SubmitDropsExchange(FakeExchange):
    """submit_order applies the fill, then raises as if the response was
    lost in transit — the classic 'did it land?' failure."""

    async def submit_order(self, symbol, leg, side, qty, client_order_id, price=None, reduce_only=False):
        await super().submit_order(symbol, leg, side, qty, client_order_id, price, reduce_only)
        raise ConnectionError("response lost")


class _SpotUnreachableExchange(FakeExchange):
    async def submit_order(self, symbol, leg, side, qty, client_order_id, price=None, reduce_only=False):
        raise ConnectionError("network down")

    async def fetch_order(self, client_order_id, symbol, leg):
        raise ConnectionError("network still down")


@pytest.mark.asyncio
async def test_spot_only_recovers_landed_order(tmp_path):
    db = await _db(tmp_path)
    ex = _SubmitDropsExchange(starting_usdt=Decimal("2000"))
    _seed_prices(ex)

    result = await spot_only.go_in(ex, db, symbol="BTC/USDT")

    assert result is not None
    assert result.filled_qty > 0
    order = await db.get_order_by_client_id(result.client_order_id)
    assert order.status == OrderStatus.FILLED


@pytest.mark.asyncio
async def test_spot_only_marks_unknown_when_unreachable(tmp_path):
    db = await _db(tmp_path)
    ex = _SpotUnreachableExchange(starting_usdt=Decimal("2000"))
    _seed_prices(ex)

    result = await spot_only.go_in(ex, db, symbol="BTC/USDT")

    assert result is None
    orders = await db.recent_orders(datetime.now(UTC) - timedelta(hours=1))
    assert any(o.status == OrderStatus.UNKNOWN for o in orders)
