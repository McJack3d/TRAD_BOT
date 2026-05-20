"""Config loading tests."""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.config import BotConfig, StrategyConfig


def test_strategy_threshold_must_be_positive() -> None:
    with pytest.raises(Exception):
        StrategyConfig(entry_funding_threshold=Decimal("-0.001"))


def test_load_paper_yaml() -> None:
    cfg = BotConfig.from_yaml("config/paper.yaml")
    assert cfg.mode.value == "paper"
    assert len(cfg.symbols) == 3
    assert cfg.strategy.perp_leverage == 2


def test_load_live_yaml() -> None:
    cfg = BotConfig.from_yaml("config/live.yaml")
    assert cfg.mode.value == "live"
    assert cfg.starting_equity_eur == Decimal("1000")


def test_load_backtest_yaml() -> None:
    cfg = BotConfig.from_yaml("config/backtest.yaml")
    assert cfg.mode.value == "backtest"
    assert cfg.backtest.start == "2020-01-01"
