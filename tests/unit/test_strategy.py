"""Strategy signal-evaluation tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from src.config import StrategyConfig
from src.strategy.signals import (
    EntrySignal,
    ExitSignal,
    HoldSignal,
    PositionView,
    evaluate_signal,
)


def test_no_position_high_funding_enters() -> None:
    sig = evaluate_signal(
        symbol="BTC/USDT",
        funding_rate=Decimal("0.0003"),
        cfg=StrategyConfig(),
        position=None,
        proposed_notional=Decimal("100"),
    )
    assert isinstance(sig, EntrySignal)


def test_no_position_low_funding_holds() -> None:
    sig = evaluate_signal(
        symbol="BTC/USDT",
        funding_rate=Decimal("0.0001"),
        cfg=StrategyConfig(),
        position=None,
        proposed_notional=Decimal("100"),
    )
    assert isinstance(sig, HoldSignal)


def test_no_position_at_exactly_entry_threshold_enters() -> None:
    cfg = StrategyConfig()
    sig = evaluate_signal(
        symbol="BTC/USDT",
        funding_rate=cfg.entry_funding_threshold,
        cfg=cfg,
        position=None,
        proposed_notional=Decimal("100"),
    )
    assert isinstance(sig, EntrySignal)


def test_position_within_dwell_holds() -> None:
    now = datetime.now(UTC)
    pos = PositionView(symbol="BTC/USDT", opened_at=now - timedelta(hours=1), notional=Decimal("100"))
    sig = evaluate_signal(
        symbol="BTC/USDT",
        funding_rate=Decimal("0.000001"),
        cfg=StrategyConfig(),
        position=pos,
        proposed_notional=Decimal("100"),
        now=now,
    )
    assert isinstance(sig, HoldSignal)


def test_position_after_dwell_low_funding_exits() -> None:
    now = datetime.now(UTC)
    pos = PositionView(
        symbol="BTC/USDT", opened_at=now - timedelta(hours=25), notional=Decimal("100")
    )
    sig = evaluate_signal(
        symbol="BTC/USDT",
        funding_rate=Decimal("0.00001"),
        cfg=StrategyConfig(),
        position=pos,
        proposed_notional=Decimal("100"),
        now=now,
    )
    assert isinstance(sig, ExitSignal)


def test_position_after_dwell_still_high_funding_holds() -> None:
    now = datetime.now(UTC)
    pos = PositionView(
        symbol="BTC/USDT", opened_at=now - timedelta(hours=25), notional=Decimal("100")
    )
    sig = evaluate_signal(
        symbol="BTC/USDT",
        funding_rate=Decimal("0.0003"),
        cfg=StrategyConfig(),
        position=pos,
        proposed_notional=Decimal("100"),
        now=now,
    )
    assert isinstance(sig, HoldSignal)


def test_hysteresis_avoids_flip_flop() -> None:
    """Funding between exit and entry thresholds should not exit
    a position once held — that's the whole point of the gap."""
    now = datetime.now(UTC)
    pos = PositionView(
        symbol="BTC/USDT", opened_at=now - timedelta(hours=25), notional=Decimal("100")
    )
    cfg = StrategyConfig()
    mid = (cfg.entry_funding_threshold + cfg.exit_funding_threshold) / 2
    sig = evaluate_signal(
        symbol="BTC/USDT",
        funding_rate=mid,
        cfg=cfg,
        position=pos,
        proposed_notional=Decimal("100"),
        now=now,
    )
    assert isinstance(sig, HoldSignal)
