"""Tests for the funding-arb monitor CLI surface + the trend-bot
drawdown helper."""

from __future__ import annotations

import argparse
from decimal import Decimal

import pytest

from scripts import tradbot, tradbot_farb
from src.state.models import StateSnapshot


def test_farb_subparsers_match_handlers():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    tradbot_farb.register_subparsers(sub)
    for name in tradbot_farb.HANDLERS:
        assert name in sub.choices
    for name in sub.choices:
        if name.startswith("farb-"):
            assert name in tradbot_farb.HANDLERS


def test_farb_menu_items_well_formed():
    items = tradbot_farb.menu_items()
    assert len(items) == 3
    keys = [k for k, _, _, _ in items]
    assert len(keys) == len(set(keys))
    for key, label, fn, ns in items:
        assert isinstance(key, str) and isinstance(label, str) and label
        assert callable(fn)
        assert hasattr(ns, "__dict__")


def test_bar_gauge_colours_by_fraction():
    # Below half → green; near full → red; over budget clamps to 100%.
    assert "green" in tradbot_farb._bar(5, 20)
    assert "red" in tradbot_farb._bar(19, 20)
    assert "100%" in tradbot_farb._bar(50, 20)
    assert tradbot_farb._bar(1, 0) == "—"


@pytest.mark.asyncio
async def test_equity_peak_drawdown(db):
    # No snapshots → peak is just the live equity, drawdown 0.
    peak, dd = await tradbot._equity_peak_drawdown(db, Decimal("1000"))
    assert peak == Decimal("1000")
    assert dd == 0.0

    # Record a high of 1200, then ask with current 1080 → -10% drawdown.
    await db.add_snapshot(
        StateSnapshot(
            equity_usdt=Decimal("1200"),
            spot_balance_usdt=Decimal("0"),
            perp_balance_usdt=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            realized_pnl_daily=Decimal("0"),
            realized_pnl_cumulative=Decimal("0"),
        )
    )
    peak, dd = await tradbot._equity_peak_drawdown(db, Decimal("1080"))
    assert peak == Decimal("1200")
    assert dd == pytest.approx(-0.10, abs=1e-9)

    # A fresh high above the recorded peak → drawdown 0, peak updates.
    peak, dd = await tradbot._equity_peak_drawdown(db, Decimal("1300"))
    assert peak == Decimal("1300")
    assert dd == 0.0
