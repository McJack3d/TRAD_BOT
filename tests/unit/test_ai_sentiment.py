"""Tests for the AI crypto-news sentiment source.

Network-free: a fake fetcher returns canned RSS, and the default stub
FinBERT scorer (keyword bag) needs no model. Verifies the source
conforms to the SentimentReading contract the trend bot expects.
"""

from __future__ import annotations

import pytest

from src.sentiment.ai_sentiment import (
    AiSentiment,
    CombinedSentiment,
    build_sentiment_source,
)
from src.sentiment.base import SentimentReading


def _rss(*titles: str) -> str:
    items = "".join(
        f"<item><title>{t}</title><link>https://x/{i}</link>"
        f"<description>{t}</description>"
        f"<pubDate>Mon, 25 May 2026 12:00:00 +0000</pubDate></item>"
        for i, t in enumerate(titles)
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel>{items}</channel></rss>'


async def _fetcher_for(xml: str):
    async def _f(url: str) -> str:
        return xml

    return _f


@pytest.mark.asyncio
async def test_bullish_headlines_give_positive_factor():
    xml = _rss(
        "Bitcoin surges to record high as ETF inflows beat expectations",
        "Ethereum rallies, strong growth and upgrade approval",
        "Crypto market soars on breakthrough adoption",
    )
    src = AiSentiment(feeds=["u"], fetcher=await _fetcher_for(xml), min_items=1)
    reading = await src.current()
    assert isinstance(reading, SentimentReading)
    assert reading.source == "ai_news"
    assert reading.value > 0
    assert -1.0 <= reading.value <= 1.0


@pytest.mark.asyncio
async def test_bearish_headlines_give_negative_factor():
    xml = _rss(
        "Bitcoin plunges as lawsuit and investigation weigh on market",
        "Ethereum slumps, weak demand and downgrade fears",
        "Crypto crashes on fraud probe and recall of tokens",
    )
    src = AiSentiment(feeds=["u"], fetcher=await _fetcher_for(xml), min_items=1)
    reading = await src.current()
    assert reading.value < 0


@pytest.mark.asyncio
async def test_too_few_headlines_returns_neutral():
    xml = _rss("Bitcoin surges record beat")  # 1 item
    src = AiSentiment(feeds=["u"], fetcher=await _fetcher_for(xml), min_items=3)
    reading = await src.current()
    assert reading.value == 0.0
    assert "No data" in reading.label


@pytest.mark.asyncio
async def test_dead_feed_does_not_raise():
    async def boom(url: str) -> str:
        raise RuntimeError("feed down")

    src = AiSentiment(feeds=["u1", "u2"], fetcher=boom, min_items=1)
    reading = await src.current()  # must not raise
    assert reading.value == 0.0


@pytest.mark.asyncio
async def test_factor_is_clamped():
    # Many strongly-bullish headlines must still clamp to <= 1.0.
    xml = _rss(*(["Bitcoin surges record beat rally soar breakthrough"] * 20))
    src = AiSentiment(feeds=["u"], fetcher=await _fetcher_for(xml), min_items=1)
    reading = await src.current()
    assert -1.0 <= reading.value <= 1.0


@pytest.mark.asyncio
async def test_combined_averages_sources():
    class _Fixed:
        def __init__(self, v, name):
            self._v = v
            self._name = name

        async def current(self):
            from datetime import UTC, datetime

            return SentimentReading(self._v, "x", self._v, self._name, datetime.now(UTC))

    combo = CombinedSentiment([_Fixed(0.8, "a"), _Fixed(-0.2, "b")])
    reading = await combo.current()
    assert reading.value == pytest.approx(0.3)  # (0.8 + -0.2) / 2
    assert reading.source == "combo"


@pytest.mark.asyncio
async def test_combined_skips_failing_source():
    class _Boom:
        async def current(self):
            raise RuntimeError("nope")

    class _Ok:
        async def current(self):
            from datetime import UTC, datetime

            return SentimentReading(0.5, "x", 0.5, "ok", datetime.now(UTC))

    combo = CombinedSentiment([_Boom(), _Ok()])
    reading = await combo.current()
    assert reading.value == pytest.approx(0.5)


def test_build_sentiment_source_names():
    from src.sentiment.ai_sentiment import AiSentiment as _AI
    from src.sentiment.fear_greed import FearGreedSentiment

    assert isinstance(build_sentiment_source("ai"), _AI)
    assert isinstance(build_sentiment_source("fear_greed"), FearGreedSentiment)
    assert isinstance(build_sentiment_source("combo"), CombinedSentiment)


def test_build_sentiment_source_rejects_unknown():
    with pytest.raises(ValueError, match="unknown sentiment source"):
        build_sentiment_source("astrology")
