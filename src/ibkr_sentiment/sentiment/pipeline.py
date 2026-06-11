"""End-to-end sentiment funnel.

  raw NewsItem
       │ FinBERT (Stage 1)  →  drop noisy / low-conviction items
       ▼
  high-conviction NewsItem
       │ LLM gatekeeper (Stage 2) →  reasoning + structured verdict
       ▼
  LLMVerdict (verdict, conviction, time horizon, structural, asset_score)
       │ Aggregator
       ▼
  StructuredSignal per symbol (composite [-1, +1] score with provenance)

The aggregator weights each verdict by:
  * conviction              — how sure the LLM was
  * source credibility      — LLM's read of the source
  * source weight           — historic market-moving accuracy (from DB)
  * structural multiplier   — structural changes weighted higher than
                              transient overreactions
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from src.ibkr_sentiment.sentiment.finbert import Scorer
from src.ibkr_sentiment.sentiment.llm_gatekeeper import LLMGatekeeper
from src.ibkr_sentiment.sentiment.models import (
    FinBertScore,
    LLMVerdict,
    NewsItem,
    StructuredSignal,
    TimeHorizon,
)


@dataclass
class FunnelDecision:
    """The Stage 1 decision: forward an item or drop it."""

    item: NewsItem
    score: FinBertScore
    forwarded: bool
    reason: str


@dataclass
class PipelineConfig:
    """Knobs for the funnel. Mirrors the relevant subset of
    FinBertConfig + LLMConfig — kept independent so the pipeline is
    usable without the full config tree (tests, ad-hoc scripts)."""

    polarity_threshold: float = 0.55
    confidence_threshold: float = 0.70
    min_conviction: float = 0.55
    default_source_weight: float = 0.5
    # Decay length for time-weighting aggregated signals (older verdicts
    # contribute less). Items older than `signal_window` are dropped.
    signal_window: timedelta = timedelta(hours=4)
    structural_weight_bonus: float = 0.25


_HORIZON_WEIGHT: dict[TimeHorizon, float] = {
    "intraday": 1.0,
    "short_term": 0.9,
    "medium_term": 0.7,
    "long_term": 0.4,
    "unknown": 0.5,
}


def stage1_filter(
    items: Iterable[NewsItem],
    scores: Iterable[FinBertScore],
    cfg: PipelineConfig,
) -> list[FunnelDecision]:
    """Apply Stage 1 thresholds; produce one FunnelDecision per item."""
    score_by_id: dict[str, FinBertScore] = {s.item_id: s for s in scores}
    out: list[FunnelDecision] = []
    for item in items:
        s = score_by_id.get(item.id)
        if s is None:
            out.append(
                FunnelDecision(
                    item=item,
                    score=FinBertScore(
                        item_id=item.id,
                        polarity="neutral",
                        score=0.0,
                        confidence=0.0,
                    ),
                    forwarded=False,
                    reason="no_score",
                )
            )
            continue
        if s.polarity == "neutral":
            out.append(FunnelDecision(item, s, False, "neutral_polarity"))
            continue
        if s.confidence < cfg.confidence_threshold:
            out.append(
                FunnelDecision(
                    item, s, False,
                    f"confidence {s.confidence:.2f} < {cfg.confidence_threshold:.2f}",
                )
            )
            continue
        if abs(s.score) < cfg.polarity_threshold:
            out.append(
                FunnelDecision(
                    item, s, False,
                    f"|score| {abs(s.score):.2f} < {cfg.polarity_threshold:.2f}",
                )
            )
            continue
        out.append(FunnelDecision(item, s, True, "forwarded"))
    return out


def aggregate_signals(
    verdicts: Iterable[tuple[NewsItem, LLMVerdict]],
    *,
    source_weights: dict[str, float] | None = None,
    cfg: PipelineConfig | None = None,
    now: datetime | None = None,
) -> list[StructuredSignal]:
    """Roll a batch of (NewsItem, LLMVerdict) pairs into one signal per
    symbol.

    A symbol's composite score is the weighted average of contributing
    asset_score values, with each weight =
        conviction * credibility * source_weight * horizon * structural_bonus * recency
    """
    cfg = cfg or PipelineConfig()
    now = now or datetime.now(UTC)
    source_weights = source_weights or {}

    per_symbol: dict[str, list[tuple[float, float, NewsItem, LLMVerdict]]] = (
        defaultdict(list)
    )
    for item, verdict in verdicts:
        if verdict.verdict == "noise" or verdict.conviction < cfg.min_conviction:
            continue
        age = (now - item.published_at).total_seconds() if item.published_at else 0
        if cfg.signal_window.total_seconds() > 0 and age > cfg.signal_window.total_seconds():
            continue
        recency = max(0.05, 1.0 - age / cfg.signal_window.total_seconds()) if cfg.signal_window.total_seconds() else 1.0
        src_w = source_weights.get(item.source, cfg.default_source_weight)
        horizon = _HORIZON_WEIGHT.get(verdict.temporal_impact, 0.5)
        struct_bonus = 1.0 + (cfg.structural_weight_bonus if verdict.structural else 0.0)
        base_weight = (
            verdict.conviction
            * max(0.05, verdict.source_credibility)
            * max(0.05, src_w)
            * horizon
            * struct_bonus
            * recency
        )
        for sym, sym_score in verdict.asset_score.items():
            per_symbol[sym].append((base_weight, sym_score, item, verdict))

    out: list[StructuredSignal] = []
    for sym, rows in per_symbol.items():
        total_w = sum(w for w, _, _, _ in rows)
        if total_w <= 0:
            continue
        composite = sum(w * s for w, s, _, _ in rows) / total_w
        avg_conv = sum(w * v.conviction for w, _, _, v in rows) / total_w
        structural = any(v.structural for _, _, _, v in rows)
        # Pick the most pessimistic horizon — long-horizon news shouldn't
        # be conflated with intraday spikes.
        horizons = [v.temporal_impact for _, _, _, v in rows]
        horizon = _dominant_horizon(horizons)
        sources = tuple(sorted({i.source for _, _, i, _ in rows}))
        item_ids = tuple(sorted({i.id for _, _, i, _ in rows}))
        out.append(
            StructuredSignal(
                symbol=sym,
                score=max(-1.0, min(1.0, composite)),
                conviction=min(1.0, avg_conv),
                temporal_impact=horizon,
                structural=structural,
                sources=sources,
                item_ids=item_ids,
            )
        )
    return out


_HORIZON_ORDER: list[TimeHorizon] = [
    "intraday",
    "short_term",
    "medium_term",
    "long_term",
    "unknown",
]


def _dominant_horizon(horizons: list[TimeHorizon]) -> TimeHorizon:
    if not horizons:
        return "unknown"
    counts = defaultdict(int)
    for h in horizons:
        counts[h] += 1
    # Most common; ties broken by `_HORIZON_ORDER`.
    return max(_HORIZON_ORDER, key=lambda h: (counts[h], -_HORIZON_ORDER.index(h)))


@dataclass
class SentimentPipeline:
    """Stitches the two stages together.

    Stateless across calls so it can be reused from the bot loop, a
    backfill job, or a one-shot debug script.
    """

    scorer: Scorer
    gatekeeper: LLMGatekeeper
    cfg: PipelineConfig = field(default_factory=PipelineConfig)

    async def run(
        self,
        items: list[NewsItem],
        *,
        source_weights: dict[str, float] | None = None,
        now: datetime | None = None,
    ) -> tuple[list[FunnelDecision], list[LLMVerdict], list[StructuredSignal]]:
        if not items:
            return [], [], []
        scores = await self.scorer.score(items)
        decisions = stage1_filter(items, scores, self.cfg)
        forwarded = [d for d in decisions if d.forwarded]
        verdicts: list[LLMVerdict] = []
        if forwarded:
            verdicts = await self.gatekeeper.analyze_many(
                [(d.item, d.score) for d in forwarded]
            )
        # Pair each verdict back to its source item for the aggregator.
        item_by_id = {i.id: i for i in items}
        pairs = [
            (item_by_id[v.item_id], v)
            for v in verdicts
            if v.item_id in item_by_id
        ]
        signals = aggregate_signals(
            pairs,
            source_weights=source_weights,
            cfg=self.cfg,
            now=now,
        )
        return decisions, verdicts, signals
