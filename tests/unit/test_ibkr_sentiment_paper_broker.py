"""Tests for the paper broker."""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.ibkr_sentiment.broker.base import OrderRequest, OrderSide, OrderType
from src.ibkr_sentiment.broker.paper import PaperBroker


@pytest.mark.asyncio
async def test_paper_broker_buy_then_sell_updates_cash_and_position():
    b = PaperBroker(starting_cash=Decimal("10000"))
    await b.connect()
    b.set_quote("AAPL", bid=Decimal("100"), ask=Decimal("100"))
    buy = await b.place_order(
        OrderRequest(
            symbol="AAPL",
            side=OrderSide.BUY,
            qty=Decimal("10"),
            order_type=OrderType.MARKET,
        )
    )
    assert buy.filled_qty == Decimal("10")
    positions = await b.positions()
    assert len(positions) == 1
    assert positions[0].symbol == "AAPL"
    assert positions[0].qty == Decimal("10")

    sell = await b.place_order(
        OrderRequest(
            symbol="AAPL",
            side=OrderSide.SELL,
            qty=Decimal("10"),
            order_type=OrderType.MARKET,
        )
    )
    assert sell.filled_qty == Decimal("10")
    assert await b.positions() == []


@pytest.mark.asyncio
async def test_paper_broker_short_then_cover_updates_position_sign():
    b = PaperBroker(starting_cash=Decimal("10000"))
    await b.connect()
    b.set_quote("MSFT", bid=Decimal("100"), ask=Decimal("100"))
    short = await b.place_order(
        OrderRequest(
            symbol="MSFT",
            side=OrderSide.SELL,
            qty=Decimal("5"),
            order_type=OrderType.MARKET,
        )
    )
    assert short.filled_qty == Decimal("5")
    pos = await b.positions()
    assert pos[0].qty == Decimal("-5")
    # Cover
    await b.place_order(
        OrderRequest(
            symbol="MSFT",
            side=OrderSide.BUY,
            qty=Decimal("5"),
            order_type=OrderType.MARKET,
        )
    )
    assert await b.positions() == []


@pytest.mark.asyncio
async def test_account_summary_reflects_open_positions():
    b = PaperBroker(starting_cash=Decimal("10000"))
    await b.connect()
    b.set_quote("AAPL", bid=Decimal("100"), ask=Decimal("100"))
    await b.place_order(
        OrderRequest(
            symbol="AAPL",
            side=OrderSide.BUY,
            qty=Decimal("10"),
            order_type=OrderType.MARKET,
        )
    )
    summary = await b.account_summary()
    assert summary.gross_position_value > 0
    assert summary.net_liquidation > 0


@pytest.mark.asyncio
async def test_flatten_all_closes_every_open_position():
    b = PaperBroker(starting_cash=Decimal("10000"))
    await b.connect()
    b.set_quote("AAPL", bid=Decimal("100"), ask=Decimal("100"))
    b.set_quote("MSFT", bid=Decimal("50"), ask=Decimal("50"))
    await b.place_order(OrderRequest(symbol="AAPL", side=OrderSide.BUY, qty=Decimal("2")))
    await b.place_order(OrderRequest(symbol="MSFT", side=OrderSide.SELL, qty=Decimal("3")))
    assert len(await b.positions()) == 2
    results = await b.flatten_all()
    assert len(results) == 2
    assert await b.positions() == []
