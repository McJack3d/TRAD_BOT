"""Shared fixtures for the test suite."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from src.config import (
    BotConfig,
    FeesConfig,
    Mode,
    MonitoringConfig,
    ReconciliationConfig,
    RiskConfig,
    StrategyConfig,
    SymbolConfig,
)
from src.state import Database


@pytest.fixture
def risk_cfg() -> RiskConfig:
    return RiskConfig()


@pytest.fixture
def strategy_cfg() -> StrategyConfig:
    return StrategyConfig()


@pytest.fixture
def reconciliation_cfg() -> ReconciliationConfig:
    return ReconciliationConfig()


@pytest.fixture
def fees_cfg() -> FeesConfig:
    return FeesConfig()


@pytest.fixture
def monitoring_cfg() -> MonitoringConfig:
    return MonitoringConfig()


@pytest.fixture
def symbols() -> list[SymbolConfig]:
    return [
        SymbolConfig(spot="BTC/USDT", perp="BTC/USDT:USDT"),
        SymbolConfig(spot="ETH/USDT", perp="ETH/USDT:USDT"),
    ]


@pytest.fixture
def bot_cfg(symbols, strategy_cfg, risk_cfg) -> BotConfig:
    return BotConfig(
        mode=Mode.PAPER,
        starting_equity_eur=Decimal("1000"),
        symbols=symbols,
        strategy=strategy_cfg,
        risk=risk_cfg,
    )


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    d = Database(str(tmp_path / "test.db"))
    await d.init(starting_equity=Decimal("1000"))
    yield d
    await d.close()
