"""Tests for the IBKR sentiment bot's SQLite-backed state layer."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from src.ibkr_sentiment.broker.base import OrderResult, OrderStatus
from src.ibkr_sentiment.sentiment.models import (
    FinBertScore,
    LLMVerdict,
    NewsItem,
    StructuredSignal,
)
from src.ibkr_sentiment.sentiment.pipeline import FunnelDecision
from src.ibkr_sentiment.signal_engine.dollar_neutral import TargetPosition
from src.ibkr_sentiment.signal_engine.mapping import Side
from src.ibkr_sentiment.state.db import IbkrSentimentDB


@pytest.fixture
async def db(tmp_path: Path) -> IbkrSentimentDB:
    d = IbkrSentimentDB(f"sqlite+aiosqlite:///{tmp_path}/ibsent.db")
    await d.init()
    yield d
    await d.close()


@pytest.mark.asyncio
async def test_init_creates_schema(tmp_path: Path):
    d = IbkrSentimentDB(f"sqlite+aiosqlite:///{tmp_path}/x.db")
    await d.init()
    # Re-init should be idempotent.
    await d.init()
    await d.close()


@pytest.mark.asyncio
async def test_record_news_and_finbert(db: IbkrSentimentDB):
    item = NewsItem(title="t", body="b", symbols=("AAPL",))
    await db.record_news(item)
    score = FinBertScore(
        item_id=item.id, polarity="positive", score=0.8, confidence=0.9
    )
    await db.record_finbert([FunnelDecision(item, score, True, "ok")])


@pytest.mark.asyncio
async def test_record_verdicts_and_signals(db: IbkrSentimentDB):
    item = NewsItem(symbols=("AAPL",))
    verdict = LLMVerdict(
        item_id=item.id,
        verdict="bullish",
        conviction=0.8,
        temporal_impact="short_term",
        structural=True,
        source_credibility=0.7,
        rationale="x",
        asset_score={"AAPL": 0.8},
    )
    await db.record_verdicts([verdict])
    sig = StructuredSignal(
        symbol="AAPL", score=0.6, conviction=0.8,
        temporal_impact="short_term", structural=True,
        sources=("rss",), item_ids=(item.id,),
    )
    await db.record_signals([sig])
    fetched = await db.signals_since(datetime.now(UTC) - timedelta(days=1))
    assert len(fetched) == 1
    assert fetched[0].symbol == "AAPL"


@pytest.mark.asyncio
async def test_trade_persistence_and_count(db: IbkrSentimentDB):
    target = TargetPosition(
        symbol="AAPL", side=Side.LONG, target_qty=Decimal("5"),
        notional=Decimal("500"), reason="",
    )
    result = OrderResult(
        client_order_id="cid-1",
        broker_order_id="bid-1",
        status=OrderStatus.FILLED,
        filled_qty=Decimal("5"),
        avg_fill_price=Decimal("100"),
    )
    await db.record_trade(target, result)
    n = await db.trade_count_last(timedelta(hours=1))
    assert n == 1


@pytest.mark.asyncio
async def test_source_accuracy_smoothed_weight(db: IbkrSentimentDB):
    # Brand-new source: weight should be 0.5 (prior).
    weights = await db.source_weights()
    assert "reuters" not in weights
    await db.update_source_accuracy("reuters", hit=True)
    await db.update_source_accuracy("reuters", hit=False)
    weights = await db.source_weights()
    # smoothing: (1 + 5) / (2 + 10) = 0.5
    assert weights["reuters"] == 0.5


@pytest.mark.asyncio
async def test_equity_snapshot_round_trip(db: IbkrSentimentDB):
    await db.record_equity(
        net_liquidation=Decimal("10500"),
        gross_exposure=Decimal("8000"),
        net_exposure=Decimal("1500"),
        open_positions=3,
    )
    row = await db.latest_equity()
    assert row is not None
    assert row.net_liquidation == Decimal("10500")
    assert row.open_positions == 3
