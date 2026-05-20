"""End-to-end paper-mode test.

Drives a full cycle through the actual strategy → risk → execution → state
pipeline using `FakeExchange`. Verifies:
- High funding triggers entry → position recorded in DB.
- Min dwell prevents immediate exit.
- After dwell + low funding, exit closes the position cleanly.
- Reconciliation diff between DB and fake exchange stays at zero drift.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from src.adapters.fake import FakeExchange
from src.config import (
    BotConfig,
    FeesConfig,
    Mode,
    RiskConfig,
    StrategyConfig,
    SymbolConfig,
)
from src.data.market_data import MarketData
from src.execution.engine import ExecutionEngine
from src.reconciliation.reconciler import Reconciler
from src.risk.manager import RiskManager
from src.state.db import Database
from src.state.models import PositionStatus, SystemStatusEnum
from src.strategy.funding_arb import FundingArbStrategy


@pytest.fixture
def e2e_cfg() -> BotConfig:
    return BotConfig(
        mode=Mode.PAPER,
        starting_equity_eur=Decimal("1000"),
        symbols=[SymbolConfig(spot="BTC/USDT", perp="BTC/USDT:USDT", min_qty=Decimal("0.00001"))],
        strategy=StrategyConfig(
            entry_funding_threshold=Decimal("0.0002"),
            exit_funding_threshold=Decimal("0.00005"),
            min_dwell_hours=24,
        ),
        risk=RiskConfig(),
        fees=FeesConfig(),
    )


async def _wire(cfg: BotConfig, db_path: Path) -> dict:
    db = Database(str(db_path))
    await db.init(starting_equity=cfg.starting_equity_eur)
    ex = FakeExchange(starting_usdt=Decimal("4000"))  # split 2k spot / 2k perp; target is 500/sym
    # Seed prices and funding. Perp ticker is keyed by the perp symbol.
    ex.set_ticker("BTC/USDT", "spot", Decimal("30000"))
    ex.set_ticker("BTC/USDT:USDT", "perp", Decimal("30010"))
    ex.set_funding("BTC/USDT", Decimal("0.0003"), Decimal("30010"))
    md = MarketData(ex, [s.spot for s in cfg.symbols], ticker_poll_seconds=999, funding_poll_seconds=999)
    # Manually seed snapshots so we don't depend on the polling loop in this test.
    snap = md.get("BTC/USDT")
    snap.spot_bid = Decimal("29995")
    snap.spot_ask = Decimal("30005")
    snap.perp_bid = Decimal("30005")
    snap.perp_ask = Decimal("30015")
    snap.funding_rate = Decimal("0.0003")
    snap.mark_price = Decimal("30010")

    execution = ExecutionEngine(cfg=cfg, db=db, exchange=ex, dry_run=False)
    risk = RiskManager(db=db, exchange=ex, cfg=cfg.risk, starting_equity=cfg.starting_equity_eur)
    perp_to_spot = {s.perp: s.spot for s in cfg.symbols}
    reconciler = Reconciler(db=db, exchange=ex, cfg=cfg.reconciliation, perp_to_spot=perp_to_spot)
    strategy = FundingArbStrategy(cfg=cfg, db=db, market_data=md, risk=risk, execution=execution)

    # Reconciler must have a recent OK timestamp before risk continuous tick;
    # for pre-trade we sidestep with ctx.reconciliation_ok=True.
    await db.touch_reconciliation_ok()
    return {
        "db": db,
        "ex": ex,
        "md": md,
        "execution": execution,
        "risk": risk,
        "reconciler": reconciler,
        "strategy": strategy,
    }


async def test_high_funding_opens_position(e2e_cfg, tmp_path: Path) -> None:
    w = await _wire(e2e_cfg, tmp_path / "e2e.db")
    db, strategy = w["db"], w["strategy"]

    await strategy.evaluate_all()

    positions = await db.open_positions()
    assert len(positions) == 1
    pos = positions[0]
    assert pos.symbol == "BTC/USDT"
    assert pos.status == PositionStatus.OPEN
    assert pos.spot_qty > 0
    assert pos.perp_qty < 0  # short
    await db.close()


async def test_min_dwell_prevents_immediate_exit(e2e_cfg, tmp_path: Path) -> None:
    w = await _wire(e2e_cfg, tmp_path / "e2e.db")
    db, ex, md, strategy = w["db"], w["ex"], w["md"], w["strategy"]

    await strategy.evaluate_all()
    assert len(await db.open_positions()) == 1

    # Funding collapses but we're inside dwell — should stay open.
    md.get("BTC/USDT").funding_rate = Decimal("0.00001")
    ex.set_funding("BTC/USDT", Decimal("0.00001"), Decimal("30010"))
    await strategy.evaluate_all()
    assert len(await db.open_positions()) == 1
    await db.close()


async def test_exit_after_dwell_with_low_funding(e2e_cfg, tmp_path: Path) -> None:
    w = await _wire(e2e_cfg, tmp_path / "e2e.db")
    db, ex, md, strategy = w["db"], w["ex"], w["md"], w["strategy"]

    await strategy.evaluate_all()
    positions = await db.open_positions()
    assert len(positions) == 1

    # Backdate the position so dwell is satisfied.
    from sqlalchemy import update as sa_update

    from src.state.models import Position

    async with db.session() as s:
        await s.execute(
            sa_update(Position)
            .where(Position.id == positions[0].id)
            .values(opened_at=datetime.now(UTC) - timedelta(hours=48))
        )
        await s.commit()

    md.get("BTC/USDT").funding_rate = Decimal("0.00001")
    ex.set_funding("BTC/USDT", Decimal("0.00001"), Decimal("30010"))
    await strategy.evaluate_all()

    assert len(await db.open_positions()) == 0
    await db.close()


async def test_reconciler_matches_after_open(e2e_cfg, tmp_path: Path) -> None:
    w = await _wire(e2e_cfg, tmp_path / "e2e.db")
    db, strategy, reconciler = w["db"], w["strategy"], w["reconciler"]

    await strategy.evaluate_all()
    result = await reconciler.run_once()
    assert result.ok, f"unexpected drift: {result.drifts}"
    await db.close()


async def test_kill_path_flatten_via_execution(e2e_cfg, tmp_path: Path) -> None:
    w = await _wire(e2e_cfg, tmp_path / "e2e.db")
    db, execution, strategy = w["db"], w["execution"], w["strategy"]

    await strategy.evaluate_all()
    assert len(await db.open_positions()) == 1

    await execution.emergency_flatten_all("test")
    assert len(await db.open_positions()) == 0
    await db.close()


async def test_halted_status_blocks_new_entries(e2e_cfg, tmp_path: Path) -> None:
    w = await _wire(e2e_cfg, tmp_path / "e2e.db")
    db, strategy = w["db"], w["strategy"]

    await db.set_status(SystemStatusEnum.HALTED, reason="test halt")
    await strategy.evaluate_all()
    assert len(await db.open_positions()) == 0
    await db.close()
