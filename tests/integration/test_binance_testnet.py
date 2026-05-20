"""Integration tests against Binance testnet. Skipped unless keys present."""

from __future__ import annotations

import os
from decimal import Decimal

import pytest

pytestmark = pytest.mark.integration

KEY = os.environ.get("BINANCE_API_KEY", "")
SECRET = os.environ.get("BINANCE_API_SECRET", "")


@pytest.fixture
async def exchange():
    if not (KEY and SECRET):
        pytest.skip("BINANCE_API_KEY/SECRET not set")
    pytest.importorskip("ccxt")
    from src.adapters.binance import BinanceAdapter

    ex = BinanceAdapter(api_key=KEY, api_secret=SECRET, testnet=True)
    await ex.connect()
    yield ex
    await ex.close()


async def test_fetch_server_time(exchange):
    t = await exchange.fetch_server_time()
    assert t > 0


async def test_fetch_ticker_btc(exchange):
    t = await exchange.fetch_ticker("BTC/USDT", "spot")
    assert t.last > Decimal("0")


async def test_fetch_funding_rate_btc_perp(exchange):
    f = await exchange.fetch_funding_rate("BTC/USDT:USDT")
    assert f.mark_price > 0
