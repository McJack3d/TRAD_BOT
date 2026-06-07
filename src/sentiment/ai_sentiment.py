"""AI crypto-news sentiment source for the trend bot.

Implements the same `SentimentSource` protocol as `FearGreedSentiment`,
so it drops into `SimpleBot(sentiment_source=...)` and tilts the SMA
entry/exit thresholds exactly like Fear & Greed does — it never trades
on its own.

Pipeline (a lightweight reuse of the IBKR sentiment funnel's generic
NLP, not the IBKR-specific parts):

    crypto RSS headlines
        │  parse
        ▼
    NewsItem[]
        │  FinBERT polarity (stub keyword scorer by default; the real
        ▼  ProsusAI/finbert model with the `sentiment` extra installed)
    per-headline signed score + confidence
        │  confidence-weighted mean, clamped to [-1, +1]
        ▼
    SentimentReading(value, label, raw, source="ai_news", ts)

Why confidence-weighting: a headline FinBERT is sure about should move
the market factor more than an ambiguous one. Why market-wide (one
factor, not per-asset): the trend bot is BTC-only and its sentiment
hook expects a single factor — same shape as Fear & Greed.

Honest caveat: news sentiment on the majors is heavily priced-in and
correlates with price. Like Fear & Greed, the only way to know if it
*adds* signal is to backtest SMA-alone vs SMA+AI-sentiment. This module
makes that comparison possible; it does not assert the edge exists.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from src.sentiment.base import SentimentReading, clamp_factor

# Public crypto-news RSS feeds (no API key required).
DEFAULT_CRYPTO_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://cryptoslate.com/feed/",
]

Fetcher = Callable[[str], Awaitable[str]]


async def _httpx_fetcher(url: str) -> str:
    import httpx

    headers = {"User-Agent": "trad-bot/1.0 (+research)"}
    async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text


def _label(factor: float, n: int) -> str:
    if factor >= 0.5:
        mood = "Strongly Bullish"
    elif factor >= 0.15:
        mood = "Bullish"
    elif factor > -0.15:
        mood = "Neutral"
    elif factor > -0.5:
        mood = "Bearish"
    else:
        mood = "Strongly Bearish"
    return f"{mood} ({n} headlines)"


class AiSentiment:
    """AI crypto-news sentiment source.

    `scorer` defaults to the deterministic keyword `StubFinBertScorer`
    (no heavy deps, no GPU) so the bot and tests work out of the box.
    Pass a real `FinBertScorer` (the `sentiment` extra) for production,
    or use `build_ai_sentiment("finbert")`.
    """

    def __init__(
        self,
        feeds: list[str] | None = None,
        scorer=None,
        *,
        max_items: int = 40,
        min_items: int = 3,
        fetcher: Fetcher | None = None,
    ):
        self.feeds = list(feeds) if feeds is not None else list(DEFAULT_CRYPTO_FEEDS)
        self.max_items = max_items
        self.min_items = min_items
        self.fetcher = fetcher or _httpx_fetcher
        self._scorer = scorer  # lazily defaulted in `_get_scorer`

    def _get_scorer(self):
        if self._scorer is None:
            from src.ibkr_sentiment.sentiment.finbert import StubFinBertScorer

            self._scorer = StubFinBertScorer()
        return self._scorer

    async def _fetch_headlines(self):
        from src.ibkr_sentiment.sentiment.ingestion import parse_rss

        items = []
        for url in self.feeds:
            try:
                body = await self.fetcher(url)
            except Exception:
                continue  # a dead feed must never sink the reading
            items.extend(parse_rss(body))
            if len(items) >= self.max_items:
                break
        return items[: self.max_items]

    async def current(self) -> SentimentReading:
        now = datetime.now(UTC)
        items = await self._fetch_headlines()
        if len(items) < self.min_items:
            return SentimentReading(
                value=0.0,
                label=f"No data ({len(items)} headlines)",
                raw=0.0,
                source="ai_news",
                ts=now,
            )
        scores = await self._get_scorer().score(items)
        num = sum(s.score * s.confidence for s in scores)
        den = sum(s.confidence for s in scores)
        factor = clamp_factor(num / den) if den > 0 else 0.0
        return SentimentReading(
            value=factor,
            label=_label(factor, len(scores)),
            raw=factor,
            source="ai_news",
            ts=now,
        )


class CombinedSentiment:
    """Average several sentiment sources into one factor.

    Fear & Greed is price-derived; AI news is (more) independent.
    Combining them is the cheapest way to get a less price-correlated
    signal. A source that errors is skipped, not fatal.
    """

    def __init__(self, sources: list, source_name: str = "combo"):
        if not sources:
            raise ValueError("CombinedSentiment needs at least one source")
        self.sources = sources
        self.source_name = source_name

    async def current(self) -> SentimentReading:
        now = datetime.now(UTC)
        readings = []
        for src in self.sources:
            try:
                readings.append(await src.current())
            except Exception:
                continue
        if not readings:
            return SentimentReading(0.0, "No data (combo)", 0.0, self.source_name, now)
        factor = clamp_factor(sum(r.value for r in readings) / len(readings))
        parts = ", ".join(f"{r.source} {r.value:+.2f}" for r in readings)
        return SentimentReading(
            value=factor,
            label=f"{_label(factor, len(readings))} [{parts}]",
            raw=factor,
            source=self.source_name,
            ts=now,
        )


def build_ai_sentiment(provider: str = "stub", feeds: list[str] | None = None) -> AiSentiment:
    """Factory: 'stub' (keyword scorer, default) or 'finbert' (real
    ProsusAI/finbert via the `sentiment` extra)."""
    scorer = None
    if provider == "finbert":
        from src.ibkr_sentiment.sentiment.finbert import FinBertScorer

        scorer = FinBertScorer()
    elif provider not in ("stub", "", None):
        raise ValueError(f"unknown AI sentiment provider: {provider}")
    return AiSentiment(feeds=feeds, scorer=scorer)


def build_sentiment_source(name: str):
    """Resolve a sentiment-source name to an instance, for the CLI.

    'fear_greed' (default) | 'ai' | 'combo' (fear_greed + ai).
    """
    name = (name or "fear_greed").lower()
    if name == "fear_greed":
        from src.sentiment.fear_greed import FearGreedSentiment

        return FearGreedSentiment()
    if name == "ai":
        return build_ai_sentiment("stub")
    if name == "combo":
        from src.sentiment.fear_greed import FearGreedSentiment

        return CombinedSentiment([FearGreedSentiment(), build_ai_sentiment("stub")])
    raise ValueError(f"unknown sentiment source: {name}")
