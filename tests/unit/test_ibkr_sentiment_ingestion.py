"""Tests for the ingestion layer (RSS parsing, dedup, symbol tagging)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from src.ibkr_sentiment.sentiment.ingestion import (
    Deduper,
    IngestionService,
    detect_symbols,
    parse_rss,
)
from src.ibkr_sentiment.sentiment.models import NewsItem

RSS_SAMPLE = """<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <title>Test feed</title>
    <item>
      <title>AAPL beats record growth</title>
      <link>https://example.com/aapl-beats</link>
      <description>Apple posted record results overnight.</description>
      <pubDate>Mon, 25 May 2026 12:30:00 +0000</pubDate>
    </item>
    <item>
      <title>Random non-financial news</title>
      <link>https://example.com/random</link>
      <description>Nothing to see here.</description>
      <pubDate>Mon, 25 May 2026 12:31:00 +0000</pubDate>
    </item>
  </channel>
</rss>
"""


def test_parse_rss_extracts_items():
    items = parse_rss(RSS_SAMPLE)
    assert len(items) == 2
    assert items[0].title.startswith("AAPL")
    assert items[0].url == "https://example.com/aapl-beats"


def test_parse_rss_returns_empty_on_garbage():
    assert parse_rss("not xml") == []


def test_detect_symbols_dollar_prefix_and_universe():
    text = "Earnings for $AAPL and MSFT were strong. Generic acronym ALL too."
    symbols = detect_symbols(text, ["AAPL", "MSFT"])
    assert "AAPL" in symbols
    assert "MSFT" in symbols
    assert "ALL" not in symbols  # not in universe → ignored


def test_deduper_blocks_repeats_within_window():
    d = Deduper(window=timedelta(minutes=10))
    a = NewsItem(source="rss", url="u")
    b = NewsItem(source="rss", url="u")
    assert d.is_new(a) is True
    assert d.is_new(b) is False


@pytest.mark.asyncio
async def test_ingestion_fetches_and_tags_symbols():
    async def fake_fetcher(_url: str) -> str:
        return RSS_SAMPLE

    svc = IngestionService(
        feeds=["http://example/feed.rss"],
        universe=["AAPL"],
        fetcher=fake_fetcher,
        dedup_window_minutes=10,
    )
    items = await svc.fetch_once()
    # Only the AAPL item survives the symbol filter.
    assert len(items) == 1
    assert items[0].symbols == ("AAPL",)


@pytest.mark.asyncio
async def test_ingestion_dedups_across_polls():
    async def fake_fetcher(_url: str) -> str:
        return RSS_SAMPLE

    svc = IngestionService(
        feeds=["http://example/feed.rss"],
        universe=["AAPL"],
        fetcher=fake_fetcher,
    )
    first = await svc.fetch_once()
    second = await svc.fetch_once()
    assert len(first) == 1
    assert second == []  # same URL → dedup blocks it
