"""Tests for the Stage 1 / Stage 2 sentiment funnel."""

from __future__ import annotations

import pytest

from src.ibkr_sentiment.sentiment.finbert import StubFinBertScorer, _bow_score
from src.ibkr_sentiment.sentiment.llm_gatekeeper import (
    StubLLMGatekeeper,
    parse_verdict_json,
)
from src.ibkr_sentiment.sentiment.models import NewsItem
from src.ibkr_sentiment.sentiment.pipeline import (
    PipelineConfig,
    SentimentPipeline,
    aggregate_signals,
    stage1_filter,
)


@pytest.mark.asyncio
async def test_stub_finbert_polarity_signs_are_correct():
    items = [
        NewsItem(title="Acme beats expectations", body="record growth", symbols=("ACME",)),
        NewsItem(title="Acme misses guidance", body="lawsuit and recall", symbols=("ACME",)),
        NewsItem(title="Acme reorganises", body="management restructured", symbols=("ACME",)),
    ]
    scores = await StubFinBertScorer().score(items)
    assert scores[0].polarity == "positive"
    assert scores[0].score > 0
    assert scores[1].polarity == "negative"
    assert scores[1].score < 0
    assert scores[2].polarity == "neutral"


def test_bow_score_handles_empty_text():
    label, score, conf = _bow_score("")
    assert label == "neutral"
    assert score == 0.0
    assert conf == 0.5


@pytest.mark.asyncio
async def test_stage1_filter_drops_neutral_and_low_confidence():
    cfg = PipelineConfig(polarity_threshold=0.5, confidence_threshold=0.6)
    items = [
        NewsItem(title="strong beat record growth surge", body="", symbols=("AAPL",)),
        NewsItem(title="quiet day at the office", body="", symbols=("AAPL",)),
    ]
    scores = await StubFinBertScorer().score(items)
    decisions = stage1_filter(items, scores, cfg)
    assert decisions[0].forwarded is True
    assert decisions[1].forwarded is False
    assert decisions[1].reason in {"neutral_polarity", "no_score"} or decisions[1].reason.startswith("confidence")


@pytest.mark.asyncio
async def test_stub_llm_mirrors_finbert_polarity():
    item = NewsItem(title="beat record", body="surge", symbols=("AAPL",))
    score = (await StubFinBertScorer().score([item]))[0]
    verdict = await StubLLMGatekeeper().analyze(item, score)
    assert verdict.verdict == "bullish"
    assert verdict.asset_score["AAPL"] > 0
    assert 0.0 <= verdict.conviction <= 1.0


def test_parse_verdict_json_robust_to_garbage():
    item = NewsItem(symbols=("AAPL",))
    v = parse_verdict_json(item, "not json at all")
    assert v.verdict == "noise"
    assert v.conviction == 0.0


def test_parse_verdict_json_clamps_scores():
    item = NewsItem(symbols=("AAPL",))
    blob = """
    Sure! Here's the analysis:
    {
      "verdict": "bullish",
      "conviction": 1.7,
      "temporal_impact": "short_term",
      "structural": true,
      "source_credibility": -0.3,
      "rationale": "x",
      "asset_score": {"AAPL": 5.0, "MSFT": -3.0}
    }
    """
    v = parse_verdict_json(item, blob)
    assert v.verdict == "bullish"
    assert v.conviction == 1.0
    assert v.source_credibility == 0.0
    assert v.asset_score["AAPL"] == 1.0
    assert v.asset_score["MSFT"] == -1.0


def test_parse_verdict_json_unknown_verdict_becomes_noise():
    item = NewsItem(symbols=("AAPL",))
    blob = '{"verdict": "very_bullish", "conviction": 0.9}'
    v = parse_verdict_json(item, blob)
    assert v.verdict == "noise"


@pytest.mark.asyncio
async def test_aggregator_weights_by_conviction_and_credibility():
    item_low = NewsItem(symbols=("AAPL",))
    item_high = NewsItem(symbols=("AAPL",))
    from src.ibkr_sentiment.sentiment.models import LLMVerdict

    verdicts = [
        (
            item_low,
            LLMVerdict(
                item_id=item_low.id,
                verdict="bullish",
                conviction=0.6,
                temporal_impact="short_term",
                structural=False,
                source_credibility=0.5,
                rationale="",
                asset_score={"AAPL": -0.4},
            ),
        ),
        (
            item_high,
            LLMVerdict(
                item_id=item_high.id,
                verdict="bullish",
                conviction=0.95,
                temporal_impact="short_term",
                structural=True,
                source_credibility=0.95,
                rationale="",
                asset_score={"AAPL": 0.9},
            ),
        ),
    ]
    signals = aggregate_signals(verdicts, cfg=PipelineConfig(min_conviction=0.5))
    assert len(signals) == 1
    sig = signals[0]
    assert sig.symbol == "AAPL"
    # The high-conviction +0.9 verdict should dominate the low-conviction -0.4.
    assert sig.score > 0.3
    assert sig.structural is True


@pytest.mark.asyncio
async def test_pipeline_end_to_end_stub():
    pipeline = SentimentPipeline(
        scorer=StubFinBertScorer(),
        gatekeeper=StubLLMGatekeeper(),
        cfg=PipelineConfig(polarity_threshold=0.2, confidence_threshold=0.4, min_conviction=0.4),
    )
    items = [
        NewsItem(title="AAPL beats record surge growth approval", body="", symbols=("AAPL",)),
        NewsItem(title="MSFT misses lawsuit weak loss", body="", symbols=("MSFT",)),
        NewsItem(title="Some random article", body="", symbols=("AAPL",)),
    ]
    decisions, verdicts, signals = await pipeline.run(items)
    assert len(decisions) == 3
    # 2 forwarded (AAPL+, MSFT-), 1 dropped (neutral)
    assert sum(d.forwarded for d in decisions) == 2
    by_sym = {s.symbol: s for s in signals}
    assert "AAPL" in by_sym and by_sym["AAPL"].score > 0
    assert "MSFT" in by_sym and by_sym["MSFT"].score < 0


@pytest.mark.asyncio
async def test_pipeline_drops_noise_verdicts():
    """A 'noise' verdict from the LLM must not produce a signal."""

    class NoiseLLM:
        async def analyze(self, item, finbert):
            from src.ibkr_sentiment.sentiment.models import LLMVerdict

            return LLMVerdict(
                item_id=item.id,
                verdict="noise",
                conviction=0.0,
                temporal_impact="unknown",
                structural=False,
                source_credibility=0.0,
                rationale="x",
            )

        async def analyze_many(self, pairs):
            return [await self.analyze(i, f) for i, f in pairs]

    pipeline = SentimentPipeline(
        scorer=StubFinBertScorer(),
        gatekeeper=NoiseLLM(),
        cfg=PipelineConfig(polarity_threshold=0.2, confidence_threshold=0.4),
    )
    items = [NewsItem(title="AAPL beats record surge growth", body="", symbols=("AAPL",))]
    _, verdicts, signals = await pipeline.run(items)
    assert verdicts and verdicts[0].verdict == "noise"
    assert signals == []
