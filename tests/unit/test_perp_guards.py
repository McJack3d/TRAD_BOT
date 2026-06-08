"""Unit tests for perpetual risk guards."""

from __future__ import annotations

from decimal import Decimal
from datetime import datetime, UTC
import pytest
from pydantic import ValidationError

from src.state.models import SystemStatusEnum
from src.state.db import Database
from src.risk.perp_guards import (
    PerpRiskConfig,
    PerpRiskParams,
    _get_field,
    check_asset_cooloff,
    check_asset_daily_stop,
    check_consecutive_losses,
    check_and_apply_consecutive_losses,
    check_account_daily_stop,
    check_account_cumulative_stop,
)


def test_perp_risk_config_defaults() -> None:
    cfg = PerpRiskConfig()
    assert cfg.cooloff_bars == 6
    assert cfg.per_asset_daily_pct == Decimal("0.015")
    assert cfg.max_consecutive_losses == 4

    # Validation check for negative cooloff
    with pytest.raises(ValidationError):
        PerpRiskConfig(cooloff_bars=-1)

    # Validation check for non-positive daily pct
    with pytest.raises(ValidationError):
        PerpRiskConfig(per_asset_daily_pct=Decimal("0.000"))

    with pytest.raises(ValidationError):
        PerpRiskConfig(per_asset_daily_pct=Decimal("-0.01"))

    # Validation check for non-positive consecutive losses
    with pytest.raises(ValidationError):
        PerpRiskConfig(max_consecutive_losses=0)


def test_perp_risk_params_defaults() -> None:
    params = PerpRiskParams()
    assert params.cooloff_bars == 6
    assert params.per_asset_daily_pct == Decimal("0.015")
    assert params.max_consecutive_losses == 4


def test_get_field_helper() -> None:
    # Test dictionary path
    d = {"symbol": "BTC/USDT", "net_pnl": Decimal("10")}
    assert _get_field(d, ["symbol"]) == "BTC/USDT"
    assert _get_field(d, ["net_pnl", "realized_pnl"]) == Decimal("10")
    assert _get_field(d, ["missing"], "default_val") == "default_val"

    # Test object path
    class DummyTrade:
        def __init__(self, asset: str, pnl: float):
            self.asset = asset
            self.pnl = pnl

    obj = DummyTrade("ETH/USDT", -5.5)
    assert _get_field(obj, ["asset", "symbol"]) == "ETH/USDT"
    assert _get_field(obj, ["pnl", "net_pnl"]) == -5.5
    assert _get_field(obj, ["missing"], "default") == "default"


def test_check_asset_cooloff_no_trades() -> None:
    res = check_asset_cooloff("BTC/USDT", [], 100, 6)
    assert res.ok


def test_check_asset_cooloff_no_matching_symbol() -> None:
    trades = [
        {"symbol": "ETH/USDT", "net_pnl": Decimal("-10"), "exit_bar_index": 90}
    ]
    res = check_asset_cooloff("BTC/USDT", trades, 95, 6)
    assert res.ok


def test_check_asset_cooloff_last_trade_profit() -> None:
    trades = [
        {"symbol": "BTC/USDT", "net_pnl": Decimal("5"), "exit_bar_index": 90}
    ]
    res = check_asset_cooloff("BTC/USDT", trades, 95, 6)
    assert res.ok


def test_check_asset_cooloff_loss_expired() -> None:
    trades = [
        {"symbol": "BTC/USDT", "net_pnl": Decimal("-5"), "exit_bar_index": 90}
    ]
    res = check_asset_cooloff("BTC/USDT", trades, 96, 6)
    assert res.ok


def test_check_asset_cooloff_loss_active() -> None:
    trades = [
        {"symbol": "BTC/USDT", "net_pnl": Decimal("-5"), "exit_bar_index": 90}
    ]
    res = check_asset_cooloff("BTC/USDT", trades, 95, 6)
    assert not res.ok
    assert "cool-off" in res.reason


def test_check_asset_cooloff_missing_pnl() -> None:
    trades = [
        {"symbol": "BTC/USDT", "exit_bar_index": 90}
    ]
    res = check_asset_cooloff("BTC/USDT", trades, 95, 6)
    assert res.ok


def test_check_asset_cooloff_missing_exit_bar() -> None:
    trades = [
        {"symbol": "BTC/USDT", "net_pnl": Decimal("-5")}
    ]
    res = check_asset_cooloff("BTC/USDT", trades, 95, 6)
    assert res.ok


def test_check_asset_cooloff_sorting_by_bar_index() -> None:
    trades = [
        {"symbol": "BTC/USDT", "net_pnl": Decimal("10"), "exit_bar_index": 95},
        {"symbol": "BTC/USDT", "net_pnl": Decimal("-5"), "exit_bar_index": 90},
    ]
    # If sorted correctly, the last trade is at 95 (profit), so no cool-off
    res = check_asset_cooloff("BTC/USDT", trades, 97, 6)
    assert res.ok

    trades_reverse = [
        {"symbol": "BTC/USDT", "net_pnl": Decimal("-5"), "exit_bar_index": 95},
        {"symbol": "BTC/USDT", "net_pnl": Decimal("10"), "exit_bar_index": 90},
    ]
    # If sorted correctly, the last trade is at 95 (loss), so cool-off is active at 97
    res_reverse = check_asset_cooloff("BTC/USDT", trades_reverse, 97, 6)
    assert not res_reverse.ok


def test_check_asset_cooloff_sorting_by_timestamp() -> None:
    t1 = datetime(2026, 6, 8, 10, 0, tzinfo=UTC)
    t2 = datetime(2026, 6, 8, 11, 0, tzinfo=UTC)
    trades = [
        {"symbol": "BTC/USDT", "net_pnl": Decimal("10"), "exit_ts": t2},
        {"symbol": "BTC/USDT", "net_pnl": Decimal("-5"), "exit_ts": t1},
    ]
    # Sorting by timestamp should put t2 (profit) as last trade.
    # Note that we didn't specify exit_bar_index, so it falls back to timestamp but doesn't have bar index.
    # But wait, if exit_bar is None, check_asset_cooloff returns pass_().
    # Let's specify exit_bar_index as well to test sorting fallback works.
    trades_with_bar = [
        {"symbol": "BTC/USDT", "net_pnl": Decimal("10"), "exit_ts": t2, "exit_bar_index": 95},
        {"symbol": "BTC/USDT", "net_pnl": Decimal("-5"), "exit_ts": t1, "exit_bar_index": 90},
    ]
    res = check_asset_cooloff("BTC/USDT", trades_with_bar, 97, 6)
    assert res.ok


def test_check_asset_daily_stop() -> None:
    # daily_pnl > -stop_limit -> pass
    # equity: 1000, daily_pct: 0.015 -> limit: 15. daily_pnl: -14.99 -> pass
    res = check_asset_daily_stop("BTC/USDT", Decimal("-10"), Decimal("-4.99"), Decimal("1000"), Decimal("0.015"))
    assert res.ok

    # daily_pnl <= -stop_limit -> fail
    # daily_pnl: -15.00 -> fail
    res2 = check_asset_daily_stop("BTC/USDT", Decimal("-10"), Decimal("-5.00"), Decimal("1000"), Decimal("0.015"))
    assert not res2.ok
    assert "daily stop hit" in res2.reason


def test_check_consecutive_losses_fewer_trades() -> None:
    trades = [
        {"net_pnl": Decimal("-10")},
        {"net_pnl": Decimal("-5")},
    ]
    res = check_consecutive_losses(trades, 3)
    assert res.ok


def test_check_consecutive_losses_breaker_inactive() -> None:
    trades = [
        {"net_pnl": Decimal("-10")},
        {"net_pnl": Decimal("5")},
        {"net_pnl": Decimal("-5")},
    ]
    res = check_consecutive_losses(trades, 3)
    assert res.ok


def test_check_consecutive_losses_breaker_active() -> None:
    trades = [
        {"net_pnl": Decimal("-10"), "exit_bar_index": 10},
        {"net_pnl": Decimal("-5"), "exit_bar_index": 11},
        {"net_pnl": Decimal("-2"), "exit_bar_index": 12},
    ]
    res = check_consecutive_losses(trades, 3)
    assert not res.ok
    assert "Consecutive losses limit reached" in res.reason


def test_check_consecutive_losses_sorting() -> None:
    # Mixed order: if not sorted, last 3 might look like they have a profit,
    # but sorted by exit_bar_index:
    # 10: -10
    # 11: -5
    # 12: -2
    # 13: 10 (profit) - wait, if 13 is profit, last 3 are (11, 12, 13) -> no breach.
    # Let's construct trades:
    # 10: 10 (profit)
    # 11: -5 (loss)
    # 12: -2 (loss)
    # 13: -1 (loss)
    # Sorted order of last 3: (11: loss, 12: loss, 13: loss) -> breach.
    # Let's pass them unsorted:
    trades = [
        {"net_pnl": Decimal("-1"), "exit_bar_index": 13},
        {"net_pnl": Decimal("10"), "exit_bar_index": 10},
        {"net_pnl": Decimal("-2"), "exit_bar_index": 12},
        {"net_pnl": Decimal("-5"), "exit_bar_index": 11},
    ]
    res = check_consecutive_losses(trades, 3)
    assert not res.ok


@pytest.mark.asyncio
async def test_check_and_apply_consecutive_losses_pass(db: Database) -> None:
    trades = [
        {"net_pnl": Decimal("-10")},
        {"net_pnl": Decimal("5")},
    ]
    # Under limits -> passes, status remains ACTIVE
    res = await check_and_apply_consecutive_losses(db, trades, 3)
    assert res.ok
    status = await db.get_status()
    assert status.status == SystemStatusEnum.ACTIVE


@pytest.mark.asyncio
async def test_check_and_apply_consecutive_losses_fail(db: Database) -> None:
    trades = [
        {"net_pnl": Decimal("-10"), "exit_bar_index": 10},
        {"net_pnl": Decimal("-5"), "exit_bar_index": 11},
        {"net_pnl": Decimal("-2"), "exit_bar_index": 12},
    ]
    # Breaker triggered -> fails, status set to HALTED
    res = await check_and_apply_consecutive_losses(db, trades, 3)
    assert not res.ok
    status = await db.get_status()
    assert status.status == SystemStatusEnum.HALTED
    assert "Consecutive losses limit reached" in status.halt_reason


def test_check_account_daily_stop() -> None:
    # daily loss > -limit -> pass
    # starting_equity = 1000, daily_loss_stop_pct = 0.02 -> limit: 20
    # daily total pnl: -19.99 -> pass
    res = check_account_daily_stop(Decimal("-10"), Decimal("-9.99"), Decimal("1000"), Decimal("0.02"))
    assert res.ok

    # daily total pnl <= -limit -> fail
    res2 = check_account_daily_stop(Decimal("-10"), Decimal("-10.00"), Decimal("1000"), Decimal("0.02"))
    assert not res2.ok
    assert "Account daily loss stop hit" in res2.reason


def test_check_account_cumulative_stop() -> None:
    # cumulative loss > -limit -> pass
    # starting_equity = 1000, cumulative_loss_stop_pct = 0.10 -> limit: 100
    # cumulative realized pnl: -99.99 -> pass
    res = check_account_cumulative_stop(Decimal("-99.99"), Decimal("1000"), Decimal("0.10"))
    assert res.ok

    # cumulative realized pnl <= -limit -> fail
    res2 = check_account_cumulative_stop(Decimal("-100.00"), Decimal("1000"), Decimal("0.10"))
    assert not res2.ok
    assert "Account cumulative loss stop hit" in res2.reason


def test_check_consecutive_losses_sorting_by_timestamp() -> None:
    t1 = datetime(2026, 6, 8, 10, 0, tzinfo=UTC)
    t2 = datetime(2026, 6, 8, 11, 0, tzinfo=UTC)
    t3 = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)
    trades = [
        {"net_pnl": Decimal("-1"), "exit_ts": t3},
        {"net_pnl": Decimal("10"), "exit_ts": t1},
        {"net_pnl": Decimal("-2"), "exit_ts": t2},
    ]
    # Sorted by ts: t1 (profit), t2 (loss), t3 (loss).
    # Last 2: t2 (loss), t3 (loss) -> breach if N=2.
    res = check_consecutive_losses(trades, 2)
    assert not res.ok

