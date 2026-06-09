"""Unit tests for the regime-switching live execution engine daemon."""

from __future__ import annotations

import asyncio
from datetime import datetime, UTC, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest
import pandas as pd
import numpy as np
from sqlalchemy import select

from src.adapters.fake import FakeExchange
from src.state.db import Database
from src.state.models import (
    Position,
    PositionStatus,
    Order,
    OrderStatus,
    Fill,
    SystemStatus,
    SystemStatusEnum,
    StateSnapshot,
    FundingPayment,
)
from src.strategy.regime_live import RegimeLiveBot, time_until_next_bar_close
from src.strategy.regime_switch import (
    RegimeSwitchParams,
    SwitchPosition,
    SwitchSignal,
    Action,
    EntryLeg,
)
from src.adapters.exchange_base import Side


def _ohlc(close: list[float]) -> pd.DataFrame:
    close_arr = np.array(close, dtype=float)
    df = pd.DataFrame(
        {
            "open": close_arr,
            "high": close_arr + 1.0,
            "low": close_arr - 1.0,
            "close": close_arr,
            "volume": np.full(len(close_arr), 1000.0),
        }
    )
    # Generate timestamp index
    base_time = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)
    df.index = [base_time + timedelta(minutes=15 * i) for i in range(len(close_arr))]
    df.index.name = "timestamp"
    return df


async def _setup_bot(tmp_path: Path, db: Database) -> tuple[RegimeLiveBot, FakeExchange]:
    ex = FakeExchange(starting_usdt=Decimal("2000"))
    ex.set_ticker("BTC/USDT:USDT", "perp", Decimal("60000"))
    ex.set_funding("BTC/USDT:USDT", Decimal("0.0001"), Decimal("60000"))

    bot = RegimeLiveBot(
        exchange=ex,
        db=db,
        symbols=["BTC/USDT:USDT"],
        mode="paper",
    )
    await bot.enable()
    return bot, ex


@pytest.mark.asyncio
async def test_init_and_config_loading(tmp_path: Path, db: Database) -> None:
    bot, _ = await _setup_bot(tmp_path, db)
    assert bot.mode == "PAPER"
    assert bot.timeframe == "15m"
    assert bot.risk_per_trade_pct == Decimal("0.01")

    # Test YAML config loading
    cfg_file = tmp_path / "test_config.yaml"
    with open(cfg_file, "w") as f:
        f.write("""
mode: dry_run
starting_equity_usdt: 1500
symbols:
  - perp: ETH/USDT:USDT
    spot: ETH/USDT
    min_qty: 0.005
    qty_step: 0.005
strategy:
  timeframe: 1h
  risk_per_trade_pct: 0.02
  max_leverage: 2.0
  adx_trend_min: 30.0
risk:
  cooloff_bars: 8
  per_asset_daily_pct: 0.02
  max_consecutive_losses: 5
fees:
  perp_taker_bps: 5.0
  assumed_slippage_bps: 3.0
""")

    bot2 = RegimeLiveBot(
        exchange=bot.exchange,
        db=db,
        symbols=["BTC/USDT:USDT"],
        config_path=str(cfg_file),
    )
    assert bot2.mode == "DRY_RUN"
    assert bot2.starting_equity_usdt == Decimal("1500")
    assert bot2.symbols == ["ETH/USDT:USDT"]
    assert bot2.timeframe == "1h"
    assert bot2.risk_per_trade_pct == Decimal("0.02")
    assert bot2.max_leverage == Decimal("2.0")
    assert bot2.strategy_params.adx_trend_min == 30.0
    assert bot2.cooloff_bars == 8
    assert bot2.per_asset_daily_pct == Decimal("0.02")
    assert bot2.max_consecutive_losses == 5
    assert bot2.perp_taker_bps == Decimal("5.0")
    assert bot2.assumed_slippage_bps == Decimal("3.0")


@pytest.mark.asyncio
async def test_enable_disable_toggle(tmp_path: Path, db: Database) -> None:
    bot, _ = await _setup_bot(tmp_path, db)
    await bot.enable()
    assert await bot.is_enabled()
    await bot.disable()
    assert not await bot.is_enabled()


@pytest.mark.asyncio
async def test_tick_when_disabled_or_halted(tmp_path: Path, db: Database) -> None:
    bot, ex = await _setup_bot(tmp_path, db)
    await bot.disable()

    # If disabled, tick does nothing (doesn't fetch/evaluate)
    bot.df_override = _ohlc([60000.0] * 205)
    with patch("src.strategy.regime_live.evaluate_live") as mock_eval:
        await bot.tick()
        mock_eval.assert_not_called()

    # If halted, tick does nothing
    await bot.enable()
    await db.set_status(SystemStatusEnum.HALTED, "test halt")
    with patch("src.strategy.regime_live.evaluate_live") as mock_eval:
        await bot.tick()
        mock_eval.assert_not_called()


@pytest.mark.asyncio
async def test_enter_long_signal(tmp_path: Path, db: Database) -> None:
    bot, ex = await _setup_bot(tmp_path, db)
    bot.df_override = _ohlc([60000.0] * 205)

    # Mock signal
    signal = SwitchSignal(
        action=Action.ENTER_LONG,
        leg=EntryLeg.TREND,
        reason="test enter trend long",
        stop_price=58000.0,
    )

    with patch("src.strategy.regime_live.evaluate_live", return_value=signal):
        await bot.tick()

    # Check position in DB
    pos = await bot.get_active_position("BTC/USDT:USDT")
    assert pos is not None
    assert pos.status == PositionStatus.OPEN
    assert pos.spot_qty == Decimal("0")
    assert pos.perp_qty > Decimal("0")
    assert pos.perp_entry_price > Decimal("0")

    # Check orders and fills recorded
    async with db.session() as s:
        orders = (await s.execute(select(Order))).scalars().all()
        fills = (await s.execute(select(Fill))).scalars().all()
    assert len(orders) == 1
    assert orders[0].status == OrderStatus.FILLED
    assert len(fills) == 1
    assert fills[0].qty == orders[0].qty

    # Check meta updated
    meta = await bot._get_meta()
    assert meta.get("BTC/USDT:USDT_stop_price") == "58000.0"
    assert meta.get("BTC/USDT:USDT_entry_leg") == "trend"


@pytest.mark.asyncio
async def test_enter_short_signal(tmp_path: Path, db: Database) -> None:
    bot, ex = await _setup_bot(tmp_path, db)
    bot.df_override = _ohlc([60000.0] * 205)

    signal = SwitchSignal(
        action=Action.ENTER_SHORT,
        leg=EntryLeg.RANGE,
        reason="test enter range short",
        stop_price=62000.0,
    )

    with patch("src.strategy.regime_live.evaluate_live", return_value=signal):
        await bot.tick()

    pos = await bot.get_active_position("BTC/USDT:USDT")
    assert pos is not None
    assert pos.perp_qty < Decimal("0")  # short is negative

    meta = await bot._get_meta()
    assert meta.get("BTC/USDT:USDT_stop_price") == "62000.0"
    assert meta.get("BTC/USDT:USDT_entry_leg") == "range"


@pytest.mark.asyncio
async def test_position_sizing_and_leverage_cap(tmp_path: Path, db: Database) -> None:
    bot, ex = await _setup_bot(tmp_path, db)
    bot.df_override = _ohlc([60000.0] * 205)
    # Customize ATR to be very small, which implies large size that will hit leverage cap
    # precompute(df, p).atr[-1] will be computed based on df
    # Let's mock atr to be extremely small (e.g. 10.0)
    # Wait, we can patch precompute's return value to inject small atr!
    from src.strategy.regime_switch import SwitchPrecomputed
    pre_mock = SwitchPrecomputed(
        close=np.array([60000.0] * 205),
        high=np.array([60001.0] * 205),
        low=np.array([59999.0] * 205),
        ema_fast=np.array([60000.0] * 205),
        ema_slow=np.array([60000.0] * 205),
        bb_lower=np.array([59000.0] * 205),
        bb_mid=np.array([60000.0] * 205),
        bb_upper=np.array([61000.0] * 205),
        rsi=np.array([50.0] * 205),
        atr=np.array([10.0] * 205),  # very small ATR
        regime=np.array(["trend"] * 205),
        adx=np.array([30.0] * 205),
        rv_pct=np.array([0.7] * 205),
    )

    signal = SwitchSignal(
        action=Action.ENTER_LONG,
        leg=EntryLeg.TREND,
        reason="test enter trend long",
        stop_price=59980.0,  # stop distance = 20.0
    )

    # Equity = 1000. risk budget = 1% = 10.0. Qty = 10 / 20 = 0.5.
    # Leverage cap: equity * 3 / 60000 = 0.05!
    # So size should be capped at 0.05 instead of 0.5!

    with patch("src.strategy.regime_live.evaluate_live", return_value=signal), \
         patch("src.strategy.regime_switch.precompute", return_value=pre_mock):
        await bot.tick()

    pos = await bot.get_active_position("BTC/USDT:USDT")
    assert pos is not None
    # Capped at 0.05
    assert pos.perp_qty == Decimal("0.05")


@pytest.mark.asyncio
async def test_sizing_below_min_qty(tmp_path: Path, db: Database) -> None:
    bot, ex = await _setup_bot(tmp_path, db)
    bot.df_override = _ohlc([60000.0] * 205)

    # Let's set high stop_distance to make calculated qty extremely small
    signal = SwitchSignal(
        action=Action.ENTER_LONG,
        leg=EntryLeg.TREND,
        reason="test enter trend long",
        stop_price=1000.0,  # stop distance = 59000.0
    )

    # Qty = 10 / 59000 = 0.000169...
    # Let's set a config with min_qty = 0.1
    from src.config import SymbolConfig
    bot.symbol_configs = [SymbolConfig(spot="BTC/USDT", perp="BTC/USDT:USDT", min_qty=Decimal("0.1"))]

    with patch("src.strategy.regime_live.evaluate_live", return_value=signal):
        await bot.tick()

    # No position should be opened because size < min_qty
    pos = await bot.get_active_position("BTC/USDT:USDT")
    assert pos is None


@pytest.mark.asyncio
async def test_exit_signal(tmp_path: Path, db: Database) -> None:
    bot, ex = await _setup_bot(tmp_path, db)
    bot.df_override = _ohlc([60000.0] * 205)

    # First open a position
    open_sig = SwitchSignal(Action.ENTER_LONG, EntryLeg.TREND, "enter", stop_price=58000.0)
    with patch("src.strategy.regime_live.evaluate_live", return_value=open_sig):
        await bot.tick()

    pos = await bot.get_active_position("BTC/USDT:USDT")
    assert pos is not None

    # Now tick with EXIT signal
    exit_sig = SwitchSignal(Action.EXIT, EntryLeg.TREND, "exit signal triggered")
    with patch("src.strategy.regime_live.evaluate_live", return_value=exit_sig):
        await bot.tick()

    # Position should be closed
    closed_pos = await bot.get_active_position("BTC/USDT:USDT")
    assert closed_pos is None

    # Check DB Position status
    async with db.session() as s:
        db_positions = (await s.execute(select(Position))).scalars().all()
    assert len(db_positions) == 1
    assert db_positions[0].status == PositionStatus.CLOSED
    assert db_positions[0].realized_pnl != Decimal("0")


@pytest.mark.asyncio
async def test_stop_loss_breach_intrabar(tmp_path: Path, db: Database) -> None:
    bot, ex = await _setup_bot(tmp_path, db)
    bot.df_override = _ohlc([60000.0] * 205)

    # Open long position
    open_sig = SwitchSignal(Action.ENTER_LONG, EntryLeg.TREND, "enter", stop_price=59000.0)
    with patch("src.strategy.regime_live.evaluate_live", return_value=open_sig):
        await bot.tick()

    # Drop price below stop_price
    ex.set_ticker("BTC/USDT:USDT", "perp", Decimal("58900"))

    # Tick, even with HOLD signal, should trigger stop loss breach and exit
    hold_sig = SwitchSignal(Action.HOLD, EntryLeg.TREND, "hold")
    with patch("src.strategy.regime_live.evaluate_live", return_value=hold_sig):
        await bot.tick()

    # Position should be closed
    assert await bot.get_active_position("BTC/USDT:USDT") is None
    meta = await bot._get_meta()
    assert meta.get("BTC/USDT:USDT_last_loss_exit_bar") == "204"


@pytest.mark.asyncio
async def test_cooloff_guard(tmp_path: Path, db: Database) -> None:
    bot, ex = await _setup_bot(tmp_path, db)
    bot.cooloff_bars = 6

    # 1. Simulate stopped out loss in meta at bar 200
    await bot._set_meta(**{
        "BTC/USDT:USDT_last_loss_exit_bar": "200"
    })
    # Add a closed loss position in DB
    p_loss = Position(
        symbol="BTC/USDT:USDT",
        status=PositionStatus.CLOSED,
        perp_qty=Decimal("1.0"),
        perp_entry_price=Decimal("60000"),
        realized_pnl=Decimal("-5"),
        closed_at=datetime.now(UTC),
    )
    await db.create_position(p_loss)

    # 2. Call tick at bar 203 (within 6 bars cool-off)
    bot.df_override = _ohlc([60000.0] * 204)  # current bar index = 203
    enter_sig = SwitchSignal(Action.ENTER_LONG, EntryLeg.TREND, "enter", stop_price=58000.0)
    with patch("src.strategy.regime_live.evaluate_live", return_value=enter_sig):
        await bot.tick()

    # Entry should be blocked (no open positions in DB other than the closed one)
    assert await bot.get_active_position("BTC/USDT:USDT") is None

    # 3. Call tick at bar 207 (expired cool-off: 207 - 200 = 7 >= 6)
    bot.df_override = _ohlc([60000.0] * 208)  # current bar index = 207
    with patch("src.strategy.regime_live.evaluate_live", return_value=enter_sig):
        await bot.tick()

    # Entry should succeed
    assert await bot.get_active_position("BTC/USDT:USDT") is not None


@pytest.mark.asyncio
async def test_asset_daily_stop_guard(tmp_path: Path, db: Database) -> None:
    bot, ex = await _setup_bot(tmp_path, db)
    bot.per_asset_daily_pct = Decimal("0.015")  # 1.5%

    # Equity = 2000. Limit = 30.
    # Create closed position today with -40 loss
    p_loss = Position(
        symbol="BTC/USDT:USDT",
        status=PositionStatus.CLOSED,
        perp_qty=Decimal("1.0"),
        perp_entry_price=Decimal("60000"),
        realized_pnl=Decimal("-40"),
        closed_at=datetime.now(UTC),
    )
    await db.create_position(p_loss)

    bot.df_override = _ohlc([60000.0] * 205)
    enter_sig = SwitchSignal(Action.ENTER_LONG, EntryLeg.TREND, "enter", stop_price=58000.0)
    with patch("src.strategy.regime_live.evaluate_live", return_value=enter_sig):
        await bot.tick()

    # Entry should be blocked due to daily stop
    assert await bot.get_active_position("BTC/USDT:USDT") is None


@pytest.mark.asyncio
async def test_consecutive_losses_breaker(tmp_path: Path, db: Database) -> None:
    bot, ex = await _setup_bot(tmp_path, db)
    bot.max_consecutive_losses = 4

    # Add 4 closed loss positions today
    for _ in range(4):
        p_loss = Position(
            symbol="BTC/USDT:USDT",
            status=PositionStatus.CLOSED,
            perp_qty=Decimal("1.0"),
            perp_entry_price=Decimal("60000"),
            realized_pnl=Decimal("-2"),
            closed_at=datetime.now(UTC),
        )
        await db.create_position(p_loss)

    # Open a position first to verify it gets closed on halt
    p_open = Position(
        symbol="BTC/USDT:USDT",
        status=PositionStatus.OPEN,
        perp_qty=Decimal("1.0"),
        perp_entry_price=Decimal("60000"),
    )
    await db.create_position(p_open)

    bot.df_override = _ohlc([60000.0] * 205)
    await bot.tick()

    # Bot should be halted in DB
    status = await db.get_status()
    assert status.status == SystemStatusEnum.HALTED
    assert "Consecutive losses" in status.halt_reason

    # Open position should be closed
    assert await bot.get_active_position("BTC/USDT:USDT") is None


@pytest.mark.asyncio
async def test_account_daily_stop_breaker(tmp_path: Path, db: Database) -> None:
    bot, ex = await _setup_bot(tmp_path, db)
    bot.daily_loss_stop_pct = Decimal("0.02")  # 2% of starting equity (1000) = 20

    # Realize -25 PnL today
    p_loss = Position(
        symbol="BTC/USDT:USDT",
        status=PositionStatus.CLOSED,
        perp_qty=Decimal("1.0"),
        perp_entry_price=Decimal("60000"),
        realized_pnl=Decimal("-25"),
        closed_at=datetime.now(UTC),
    )
    await db.create_position(p_loss)

    bot.df_override = _ohlc([60000.0] * 205)
    await bot.tick()

    status = await db.get_status()
    assert status.status == SystemStatusEnum.HALTED
    assert "Account daily loss stop hit" in status.halt_reason


@pytest.mark.asyncio
async def test_account_cumulative_stop_breaker(tmp_path: Path, db: Database) -> None:
    bot, ex = await _setup_bot(tmp_path, db)
    bot.cumulative_loss_stop_pct = Decimal("0.10")  # 10% of starting equity = 100

    # Realize -110 cumulative PnL
    p_loss = Position(
        symbol="BTC/USDT:USDT",
        status=PositionStatus.CLOSED,
        perp_qty=Decimal("1.0"),
        perp_entry_price=Decimal("60000"),
        realized_pnl=Decimal("-110"),
        closed_at=datetime.now(UTC) - timedelta(days=2),  # not today
    )
    await db.create_position(p_loss)

    bot.df_override = _ohlc([60000.0] * 205)
    await bot.tick()

    status = await db.get_status()
    assert status.status == SystemStatusEnum.HALTED
    assert "Account cumulative loss stop hit" in status.halt_reason


@pytest.mark.asyncio
async def test_calendar_and_weekend_blackout(tmp_path: Path, db: Database) -> None:
    bot, ex = await _setup_bot(tmp_path, db)
    bot.weekend_blackout = True

    # 1. Saturday 10:00 UTC
    sat_time = datetime(2026, 6, 13, 10, 0, tzinfo=UTC)  # 2026-06-13 is Saturday
    assert bot.is_entry_blocked_by_calendar(sat_time)

    # 2. Wednesday 10:00 UTC
    wed_time = datetime(2026, 6, 10, 10, 0, tzinfo=UTC)  # Wednesday
    assert not bot.is_entry_blocked_by_calendar(wed_time)

    # 3. Macro event check on Wednesday
    bot.macro_events = [datetime(2026, 6, 10, 10, 30, tzinfo=UTC)]
    # 09:45 is blocked (within 30m before)
    assert bot.is_entry_blocked_by_calendar(datetime(2026, 6, 10, 10, 10, tzinfo=UTC))
    # 11:30 is not blocked
    assert not bot.is_entry_blocked_by_calendar(datetime(2026, 6, 10, 11, 30, tzinfo=UTC))


@pytest.mark.asyncio
async def test_calendar_does_not_block_exits(tmp_path: Path, db: Database) -> None:
    bot, ex = await _setup_bot(tmp_path, db)
    bot.weekend_blackout = True
    bot.df_override = _ohlc([60000.0] * 205)

    # Open position
    open_sig = SwitchSignal(Action.ENTER_LONG, EntryLeg.TREND, "enter", stop_price=58000.0)
    with patch("src.strategy.regime_live.evaluate_live", return_value=open_sig):
        await bot.tick()

    # Set mock time to Saturday
    sat_time = datetime(2026, 6, 13, 10, 0, tzinfo=UTC)
    exit_sig = SwitchSignal(Action.EXIT, EntryLeg.TREND, "exit reason")

    with patch("src.strategy.regime_live.evaluate_live", return_value=exit_sig), \
         patch("src.strategy.regime_live.datetime") as mock_dt:
        # mock datetime.now(UTC) to Saturday
        mock_dt.now.return_value = sat_time
        await bot.tick()

    # Position should be closed despite weekend
    assert await bot.get_active_position("BTC/USDT:USDT") is None


@pytest.mark.asyncio
async def test_live_mode_futures_fallback(tmp_path: Path, db: Database) -> None:
    bot, ex = await _setup_bot(tmp_path, db)
    bot.mode = "LIVE"
    bot.df_override = _ohlc([60000.0] * 205)

    # Force probe to fail by mocking fetch_positions to raise Exception
    with patch.object(ex, "fetch_positions", side_effect=Exception("API Error: -2015")):
        await bot.probe_futures_availability()

    assert bot.futures_available is False

    # Mock ENTER_LONG signal
    signal = SwitchSignal(
        action=Action.ENTER_LONG,
        leg=EntryLeg.TREND,
        reason="test enter long when futures unavailable",
        stop_price=58000.0,
    )

    with patch("src.strategy.regime_live.evaluate_live", return_value=signal):
        await bot.tick()

    # The order should have been simulated successfully and position opened in DB
    pos = await bot.get_active_position("BTC/USDT:USDT")
    assert pos is not None
    assert pos.status == PositionStatus.OPEN
    assert pos.perp_qty > Decimal("0")

    # Order in DB should be marked FILLED
    async with db.session() as s:
        orders = (await s.execute(select(Order))).scalars().all()
    assert len(orders) == 1
    assert orders[0].status == OrderStatus.FILLED


def test_time_until_next_bar_close() -> None:
    # timeframe "15m"
    res = time_until_next_bar_close("15m")
    assert res > 0.0


def test_main_cli_argument_parsing() -> None:
    from src.strategy.regime_live import main
    import argparse
    from unittest.mock import patch

    with patch("argparse.ArgumentParser.parse_args") as mock_args, \
         patch("src.strategy.regime_live.run") as mock_run, \
         patch("src.strategy.regime_live.Path") as mock_path:
        
        mock_path.return_value.exists.return_value = True
        mock_args.return_value = argparse.Namespace(
            config="config/regime_switch.yaml",
            kill_file="/var/lib/bot/KILL"
        )
        main()
        mock_run.assert_called_once_with("config/regime_switch.yaml", "/var/lib/bot/KILL")


def test_main_cli_missing_config_exits() -> None:
    from src.strategy.regime_live import main
    from unittest.mock import patch
    import sys
    import argparse

    with patch("argparse.ArgumentParser.parse_args") as mock_args, \
         patch("src.strategy.regime_live.run") as mock_run, \
         patch("src.strategy.regime_live.Path") as mock_path, \
         patch("sys.exit") as mock_exit:
        
        mock_path.return_value.exists.return_value = False
        mock_args.return_value = argparse.Namespace(
            config="config/non_existent.yaml",
            kill_file="/var/lib/bot/KILL"
        )
        main()
        mock_exit.assert_called_once_with(2)

