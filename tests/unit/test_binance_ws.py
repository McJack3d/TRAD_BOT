"""WebSocket message parsing — no network."""

from __future__ import annotations

from decimal import Decimal

from src.adapters.fake import FakeExchange
from src.data.market_data import MarketData
from src.data.binance_ws import BinanceWebSocket


def _ws() -> tuple[BinanceWebSocket, MarketData]:
    ex = FakeExchange()
    md = MarketData(ex, ["BTC/USDT", "ETH/USDT"], ticker_poll_seconds=999, funding_poll_seconds=999)
    ws = BinanceWebSocket(
        market_data=md,
        symbols=["BTC/USDT", "ETH/USDT"],
        perp_symbols=["BTC/USDT:USDT", "ETH/USDT:USDT"],
    )
    return ws, md


def test_spot_book_ticker_updates_snapshot() -> None:
    ws, md = _ws()
    ws._handle_spot(
        {
            "stream": "btcusdt@bookTicker",
            "data": {"s": "BTCUSDT", "b": "29995.0", "B": "1", "a": "30005.0", "A": "1"},
        }
    )
    snap = md.get("BTC/USDT")
    assert snap.spot_bid == Decimal("29995.0")
    assert snap.spot_ask == Decimal("30005.0")


def test_perp_mark_price_updates_funding() -> None:
    ws, md = _ws()
    ws._handle_perp(
        {
            "stream": "btcusdt@markPrice@1s",
            "data": {
                "e": "markPriceUpdate",
                "s": "BTCUSDT",
                "p": "30100.5",
                "r": "0.0003",
                "T": 1716200000000,
            },
        }
    )
    snap = md.get("BTC/USDT")
    assert snap.mark_price == Decimal("30100.5")
    assert snap.funding_rate == Decimal("0.0003")
    assert snap.next_funding_time is not None


def test_unknown_symbol_ignored() -> None:
    ws, md = _ws()
    ws._handle_spot({"data": {"s": "DOGEUSDT", "b": "0.1", "a": "0.11"}})
    assert md.get("BTC/USDT").spot_bid == Decimal("0")


def test_perp_book_ticker_updates_snapshot() -> None:
    ws, md = _ws()
    ws._handle_perp(
        {"data": {"s": "BTCUSDT", "b": "30001.0", "B": "1", "a": "30009.0", "A": "1"}}
    )
    snap = md.get("BTC/USDT")
    assert snap.perp_bid == Decimal("30001.0")
    assert snap.perp_ask == Decimal("30009.0")
