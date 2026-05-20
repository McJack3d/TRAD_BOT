"""End-to-end tests for SimpleBot trend-follower."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from src.adapters.fake import FakeExchange
from src.simple_bot import SimpleBot
from src.state.db import Database
from src.strategy.sma_trend import TrendState


async def _setup(tmp_path: Path, starting_usdt: Decimal = Decimal("1000")) -> tuple[SimpleBot, FakeExchange, Database]:
    db = Database(str(tmp_path / "simple.db"))
    await db.init(starting_equity=starting_usdt)
    ex = FakeExchange(starting_usdt=starting_usdt)
    ex.set_ticker("BTC/USDT", "spot", Decimal("60000"))
    bot = SimpleBot(exchange=ex, db=db, symbol="BTC/USDT", sma_window=50)
    return bot, ex, db


async def test_default_is_disabled_and_out(tmp_path: Path) -> None:
    bot, _, db = await _setup(tmp_path)
    assert not await bot.is_enabled()
    assert await bot.current_state() == TrendState.OUT
    await db.close()


async def test_enable_toggle_persists(tmp_path: Path) -> None:
    bot, _, db = await _setup(tmp_path)
    await bot.enable()
    assert await bot.is_enabled()
    await bot.disable()
    assert not await bot.is_enabled()
    await db.close()


async def test_tick_when_disabled_does_nothing(tmp_path: Path) -> None:
    bot, _, db = await _setup(tmp_path)
    bot.closes_override = [60000.0] * 50  # signal would be IN if enabled
    sig = await bot.tick()
    assert sig is None
    assert await bot.current_state() == TrendState.OUT
    await db.close()


async def test_signal_in_flips_position(tmp_path: Path) -> None:
    bot, ex, db = await _setup(tmp_path)
    await bot.enable()
    # 49 closes at 50k, last at 70k → close > SMA50 → IN.
    bot.closes_override = [50000.0] * 49 + [70000.0]
    sig = await bot.tick()
    assert sig is not None and sig.state == TrendState.IN
    assert await bot.current_state() == TrendState.IN
    # Balances: USDT spent, BTC acquired.
    balances = await ex.fetch_balances()
    assert balances["spot:BTC"].total > 0
    assert balances["spot:USDT"].total < Decimal("500")  # we had 500 in spot, mostly spent
    await db.close()


async def test_signal_out_flips_back(tmp_path: Path) -> None:
    bot, ex, db = await _setup(tmp_path)
    await bot.enable()
    bot.closes_override = [50000.0] * 49 + [70000.0]
    await bot.tick()  # enter IN
    bot.closes_override = [70000.0] * 49 + [50000.0]  # signal OUT
    await bot.tick()
    assert await bot.current_state() == TrendState.OUT
    balances = await ex.fetch_balances()
    assert balances["spot:BTC"].total == Decimal("0") or balances["spot:BTC"].total < Decimal("0.0001")
    await db.close()


async def test_no_change_when_signal_matches_position(tmp_path: Path) -> None:
    bot, ex, db = await _setup(tmp_path)
    await bot.enable()
    bot.closes_override = [50000.0] * 50  # close == SMA → OUT, position already OUT
    sig = await bot.tick()
    assert sig.state == TrendState.OUT
    # No orders submitted.
    from sqlalchemy import select

    from src.state.models import Order

    async with db.session() as s:
        orders = (await s.execute(select(Order))).scalars().all()
    assert len(orders) == 0
    await db.close()


async def test_flatten_now_sells_all(tmp_path: Path) -> None:
    bot, ex, db = await _setup(tmp_path)
    await bot.enable()
    bot.closes_override = [50000.0] * 49 + [70000.0]
    await bot.tick()  # IN
    assert (await ex.fetch_balances())["spot:BTC"].total > 0
    await bot.flatten_now()
    assert (await ex.fetch_balances()).get("spot:BTC", None) is None or \
        (await ex.fetch_balances())["spot:BTC"].total < Decimal("0.0001")
    assert await bot.current_state() == TrendState.OUT
    await db.close()


async def test_status_reports_holdings(tmp_path: Path) -> None:
    bot, _, db = await _setup(tmp_path)
    await bot.enable()
    bot.closes_override = [50000.0] * 49 + [70000.0]
    await bot.tick()
    status = await bot.status()
    assert status.enabled is True
    assert status.current_state == TrendState.IN
    assert status.btc_qty > 0
    assert status.last_price == Decimal("60000")
    await db.close()
