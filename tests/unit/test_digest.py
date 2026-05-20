"""Digest builder smoke test."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from src.config import BotConfig, Mode, SymbolConfig
from src.monitoring.digest import DigestScheduler
from src.monitoring.email import EmailNotifier
from src.state.db import Database
from src.state.models import FundingPayment, Position, PositionStatus, StateSnapshot


class _SilentEmail(EmailNotifier):
    def __init__(self):
        super().__init__("", 0, "", "", "", "")
        self.sent: list[tuple[str, str]] = []

    @property
    def enabled(self) -> bool:
        return True

    async def send(self, subject: str, body: str) -> None:
        self.sent.append((subject, body))


@pytest.fixture
def digest_cfg() -> BotConfig:
    return BotConfig(
        mode=Mode.PAPER,
        starting_equity_eur=Decimal("1000"),
        symbols=[SymbolConfig(spot="BTC/USDT", perp="BTC/USDT:USDT")],
    )


async def test_digest_includes_funding_and_positions(
    digest_cfg, tmp_path: Path
) -> None:
    db = Database(str(tmp_path / "x.db"))
    await db.init(starting_equity=Decimal("1000"))

    now = datetime.now(UTC)
    await db.add_funding_payment(
        FundingPayment(
            symbol="BTC/USDT",
            funding_time=now - timedelta(hours=2),
            funding_rate=Decimal("0.0003"),
            notional=Decimal("500"),
            payment=Decimal("0.15"),
            mark_price=Decimal("30000"),
        )
    )
    pos = Position(
        symbol="BTC/USDT",
        status=PositionStatus.OPEN,
        spot_qty=Decimal("0.0166"),
        perp_qty=Decimal("-0.0166"),
        spot_entry_price=Decimal("30000"),
        perp_entry_price=Decimal("30010"),
        initial_margin=Decimal("250"),
    )
    await db.create_position(pos)
    await db.add_snapshot(
        StateSnapshot(
            equity_usdt=Decimal("1000.15"),
            spot_balance_usdt=Decimal("500"),
            perp_balance_usdt=Decimal("500"),
            unrealized_pnl=Decimal("0"),
            realized_pnl_daily=Decimal("0"),
            realized_pnl_cumulative=Decimal("0"),
        )
    )

    email = _SilentEmail()
    scheduler = DigestScheduler(cfg=digest_cfg, db=db, email=email)
    body = await scheduler.build_digest(window_hours=24, title="Daily")
    assert "Funding received: 0.1500" in body
    assert "Positions open:   1" in body
    assert "BTC/USDT" in body
    await db.close()
