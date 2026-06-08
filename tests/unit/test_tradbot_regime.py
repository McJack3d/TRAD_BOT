"""Tests for the regime-backtest CLI wiring."""

from __future__ import annotations

import argparse
import io

import pytest
from rich.console import Console

from scripts import tradbot_regime
from src.data import history as histmod


def test_subparsers_match_handlers():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    tradbot_regime.register_subparsers(sub)
    for name in tradbot_regime.HANDLERS:
        assert name in sub.choices
    for name in sub.choices:
        if name.startswith("regime-"):
            assert name in tradbot_regime.HANDLERS


def test_menu_items_well_formed():
    items = tradbot_regime.menu_items()
    assert len(items) == 11  # 4 backtests + 7 live operations
    keys = [k for k, _, _, _ in items]
    assert len(keys) == len(set(keys))
    for key, label, fn, ns in items:
        assert isinstance(key, str)
        assert isinstance(label, str) and label
        assert callable(fn)
        assert hasattr(ns, "__dict__")


def test_build_args_defaults_sensible():
    ns = tradbot_regime._build_args(
        "BTC/USDT,ETH/USDT", "1h", months=3, sweep=False
    )
    assert ns.symbols == ["BTC/USDT", "ETH/USDT"]
    assert ns.timeframes == ["1h"]
    assert ns.months == 3
    # Sizing policy from the spec — must not drift silently.
    assert ns.risk_pct == 0.01
    assert ns.max_leverage == 3.0
    assert ns.fee_bps == 4.0
    assert ns.slippage_bps == 2.0
    # The handlers must pass a `debug` attr through to the backtest CLI.
    assert hasattr(ns, "debug")


@pytest.mark.asyncio
async def test_regime_quick_runs_end_to_end_without_asyncio_nesting(
    tmp_path, monkeypatch
):
    """REGRESSION: the menu handler awaits the async backtest chain. The
    v1 code called a SYNC data loader (which did asyncio.run internally)
    from inside this already-running loop, crashing with 'asyncio.run()
    cannot be called from a running event loop' and mislabelling it as a
    Binance geo-block. This proves the whole chain runs cleanly."""
    # Keep the parquet cache inside tmp by running from there.
    monkeypatch.chdir(tmp_path)

    async def fake_ohlcv(symbol, timeframe, since_ms, until_ms):
        # A clean uptrend, enough bars for the backtest to run.
        base, step = 1_700_000_000_000, 3_600_000
        rows = []
        price = 100.0
        for i in range(700):
            price += 0.2
            rows.append([base + i * step, price, price + 0.3, price - 0.3, price, 1000.0])
        return rows

    monkeypatch.setattr(histmod, "_OHLCV_FETCHER", fake_ohlcv)

    console = Console(file=io.StringIO())
    rc = await tradbot_regime.cmd_regime_quick(argparse.Namespace(), console)
    assert rc == 0  # ran end-to-end, produced a scorecard, no crash


@pytest.mark.asyncio
async def test_regime_status_live(tmp_path, db, monkeypatch):
    from unittest.mock import AsyncMock, patch
    from scripts import tradbot_regime
    from src.config import BotConfig
    from src.state.models import Position, PositionStatus, StateSnapshot, SystemStatusEnum
    from decimal import Decimal

    cfg = BotConfig.from_yaml("config/regime_switch.yaml")
    cfg.starting_equity_usdt = Decimal("1000")

    # Write a test snapshot
    await db.add_snapshot(StateSnapshot(
        equity_usdt=Decimal("1050"),
        spot_balance_usdt=Decimal("0"),
        perp_balance_usdt=Decimal("1000"),
        unrealized_pnl=Decimal("10"),
        realized_pnl_daily=Decimal("40"),
        realized_pnl_cumulative=Decimal("50"),
    ))

    # Write an open position
    await db.create_position(Position(
        symbol="BTC/USDT:USDT",
        status=PositionStatus.OPEN,
        perp_qty=Decimal("0.05"),
        perp_entry_price=Decimal("60000"),
        initial_margin=Decimal("1000"),
    ))

    db_path = str(tmp_path / "test.db")

    # Mock _load to return cfg, db, db_path
    async def mock_load():
        return cfg, db, db_path
    monkeypatch.setattr(tradbot_regime, "_load", mock_load)

    # Mock BinanceAdapter to return simulated ticker and allow connect/close
    from src.adapters.binance import BinanceAdapter
    from src.adapters.exchange_base import Ticker
    from datetime import datetime, UTC

    ticker_mock = Ticker(
        symbol="BTC/USDT:USDT",
        bid=Decimal("60100"),
        ask=Decimal("60100"),
        last=Decimal("60100"),
        ts=datetime.now(UTC)
    )

    mock_exchange = AsyncMock()
    mock_exchange.connect = AsyncMock()
    mock_exchange.close = AsyncMock()
    mock_exchange.fetch_ticker = AsyncMock(return_value=ticker_mock)

    with patch("src.adapters.binance.BinanceAdapter", return_value=mock_exchange):
        console = Console(file=io.StringIO(), width=150)
        rc = await tradbot_regime.cmd_regime_status(argparse.Namespace(), console)
        assert rc == 0
        output = console.file.getvalue()
        assert "DISABLED" in output or "ACTIVE" in output
        assert "BTC/USDT:USDT" in output
        assert "0.0500" in output
        assert "Daily realized PnL" in output


@pytest.mark.asyncio
async def test_regime_positions_live(tmp_path, db, monkeypatch):
    from scripts import tradbot_regime
    from src.config import BotConfig
    from src.state.models import Position, PositionStatus
    from decimal import Decimal

    cfg = BotConfig.from_yaml("config/regime_switch.yaml")
    await db.create_position(Position(
        symbol="ETH/USDT:USDT",
        status=PositionStatus.OPEN,
        perp_qty=Decimal("-1.0"),
        perp_entry_price=Decimal("3000"),
        initial_margin=Decimal("1000"),
    ))

    async def mock_load():
        return cfg, db, str(tmp_path / "test.db")
    monkeypatch.setattr(tradbot_regime, "_load", mock_load)

    console = Console(file=io.StringIO(), width=150)
    rc = await tradbot_regime.cmd_regime_positions(argparse.Namespace(), console)
    assert rc == 0
    output = console.file.getvalue()
    assert "ETH/USDT:USDT" in output
    assert "1.0000" in output
    assert "Short" in output


@pytest.mark.asyncio
async def test_regime_equity_live(tmp_path, db, monkeypatch):
    from scripts import tradbot_regime
    from src.config import BotConfig
    from src.state.models import StateSnapshot
    from decimal import Decimal

    cfg = BotConfig.from_yaml("config/regime_switch.yaml")
    await db.add_snapshot(StateSnapshot(
        equity_usdt=Decimal("1050"),
        spot_balance_usdt=Decimal("0"),
        perp_balance_usdt=Decimal("1000"),
        unrealized_pnl=Decimal("10"),
        realized_pnl_daily=Decimal("40"),
        realized_pnl_cumulative=Decimal("50"),
    ))

    async def mock_load():
        return cfg, db, str(tmp_path / "test.db")
    monkeypatch.setattr(tradbot_regime, "_load", mock_load)

    console = Console(file=io.StringIO())
    rc = await tradbot_regime.cmd_regime_equity(argparse.Namespace(limit=10), console)
    assert rc == 0
    output = console.file.getvalue()
    assert "$1,050.00" in output


@pytest.mark.asyncio
async def test_regime_enable_disable_live(tmp_path, db, monkeypatch):
    from scripts import tradbot_regime
    from src.config import BotConfig
    from src.state.models import SystemStatusEnum

    cfg = BotConfig.from_yaml("config/regime_switch.yaml")
    async def mock_load():
        return cfg, db, str(tmp_path / "test.db")
    monkeypatch.setattr(tradbot_regime, "_load", mock_load)

    console = Console(file=io.StringIO())
    rc = await tradbot_regime.cmd_regime_enable(argparse.Namespace(), console)
    assert rc == 0
    status = await db.get_status()
    assert status.status == SystemStatusEnum.ACTIVE
    assert "enabled:true" in status.halt_reason

    rc = await tradbot_regime.cmd_regime_disable(argparse.Namespace(), console)
    assert rc == 0
    status = await db.get_status()
    assert "enabled:false" in status.halt_reason


@pytest.mark.asyncio
async def test_regime_evaluate_live(tmp_path, db, monkeypatch):
    from unittest.mock import AsyncMock
    from scripts import tradbot_regime
    from src.strategy.regime_live import RegimeLiveBot

    mock_bot = AsyncMock(spec=RegimeLiveBot)
    mock_bot.tick = AsyncMock()
    mock_exchange = AsyncMock()

    async def mock_make_live_bot():
        return mock_bot, mock_exchange, db

    monkeypatch.setattr(tradbot_regime, "_make_live_bot", mock_make_live_bot)

    console = Console(file=io.StringIO())
    rc = await tradbot_regime.cmd_regime_evaluate(argparse.Namespace(), console)
    assert rc == 0
    mock_bot.tick.assert_called_once()


@pytest.mark.asyncio
async def test_regime_flatten_live(tmp_path, db, monkeypatch):
    from unittest.mock import AsyncMock
    from scripts import tradbot_regime
    from src.state.models import Position, PositionStatus
    from src.strategy.regime_live import RegimeLiveBot
    from src.adapters.exchange_base import ExchangeOrder, Side
    from datetime import datetime, UTC
    from decimal import Decimal
    from src.config import BotConfig

    cfg = BotConfig.from_yaml("config/regime_switch.yaml")
    p = await db.create_position(Position(
        symbol="BTC/USDT:USDT",
        status=PositionStatus.OPEN,
        perp_qty=Decimal("0.1"),
        perp_entry_price=Decimal("60000"),
        initial_margin=Decimal("2000"),
    ))

    mock_bot = AsyncMock(spec=RegimeLiveBot)
    # Simulate a close fill
    fill_order = ExchangeOrder(
        client_order_id="pS123",
        exchange_order_id="exc123",
        symbol="BTC/USDT:USDT",
        leg="perp",
        side="sell",
        qty=Decimal("0.1"),
        filled_qty=Decimal("0.1"),
        avg_price=Decimal("61000"),
        status="filled",
        fee_paid=Decimal("5"),
        fee_asset="USDT",
        ts=datetime.now(UTC),
    )
    mock_bot._close_perp = AsyncMock(return_value=fill_order)
    mock_exchange = AsyncMock()

    async def mock_load():
        return cfg, db, str(tmp_path / "test.db")

    async def mock_make_live_bot():
        return mock_bot, mock_exchange, db

    monkeypatch.setattr(tradbot_regime, "_load", mock_load)
    monkeypatch.setattr(tradbot_regime, "_make_live_bot", mock_make_live_bot)

    # 1. Without --yes
    console = Console(file=io.StringIO())
    rc = await tradbot_regime.cmd_regime_flatten(argparse.Namespace(yes=False), console)
    assert rc == 1
    assert "WARNING" in console.file.getvalue()

    # 2. With --yes
    console = Console(file=io.StringIO())
    rc = await tradbot_regime.cmd_regime_flatten(argparse.Namespace(yes=True), console)
    assert rc == 0
    mock_bot._close_perp.assert_called_once_with("BTC/USDT:USDT", "sell", Decimal("0.1"), p.id)

    # Check DB closed position
    pos_after = await db.get_position(p.id)
    assert pos_after.status == PositionStatus.CLOSED
    assert pos_after.realized_pnl == Decimal("100") # (61000 - 60000) * 0.1

