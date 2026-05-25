"""Shared dataclasses for the sentiment funnel.

A piece of text flows through the pipeline as:
    NewsItem  →  FinBertScore  →  LLMVerdict  →  StructuredSignal

Each downstream stage references the upstream stage's id, so the full
provenance of a trade can be reconstructed from the database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

Polarity = Literal["positive", "negative", "neutral"]
TimeHorizon = Literal["intraday", "short_term", "medium_term", "long_term", "unknown"]
Verdict = Literal["bullish", "bearish", "neutral", "noise"]


def _now() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return uuid4().hex


@dataclass(slots=True)
class NewsItem:
    """A single ingested text fragment.

    `symbols` is the set of tickers the item is believed to be about.
    `source` is a short identifier ("reuters", "sec-8k", "alpha-vantage").
    """

    id: str = field(default_factory=_new_id)
    source: str = ""
    url: str = ""
    title: str = ""
    body: str = ""
    published_at: datetime = field(default_factory=_now)
    ingested_at: datetime = field(default_factory=_now)
    symbols: tuple[str, ...] = ()
    meta: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class FinBertScore:
    """Stage 1 output — discriminative polarity reading."""

    item_id: str
    polarity: Polarity
    score: float  # -1 (bearish) .. +1 (bullish)
    confidence: float  # 0..1 — softmax max class probability
    scored_at: datetime = field(default_factory=_now)

    @property
    def passes(self) -> bool:
        """True if the reading is high-conviction enough to forward to
        Stage 2. The pipeline applies its own thresholds; this is a
        convenience used in tests."""
        return self.polarity != "neutral" and abs(self.score) >= 0.5


@dataclass(slots=True)
class LLMVerdict:
    """Stage 2 output — structured reasoning result.

    `conviction` and `temporal_impact` are the two knobs the signal
    engine actually uses; `rationale` is kept for audit only.
    """

    item_id: str
    verdict: Verdict
    conviction: float  # 0..1, the LLM's stated confidence
    temporal_impact: TimeHorizon
    structural: bool  # True if change is structural (vs. transient overreaction)
    source_credibility: float  # 0..1, LLM's read of the source
    rationale: str
    asset_score: dict[str, float] = field(default_factory=dict)  # ticker → -1..+1
    decided_at: datetime = field(default_factory=_now)


@dataclass(slots=True)
class StructuredSignal:
    """The thing the execution engine consumes.

    `score` is the composite sentiment factor in [-1, +1] for one
    symbol after weighting by source accuracy. `technical_ok` is
    populated by the signal engine after it checks SMA / RSI.
    """

    symbol: str
    score: float
    conviction: float
    temporal_impact: TimeHorizon
    structural: bool
    sources: tuple[str, ...]  # contributing source identifiers
    item_ids: tuple[str, ...]  # contributing NewsItem ids (audit trail)
    generated_at: datetime = field(default_factory=_now)
    technical_ok: bool | None = None
    technical_reason: str = ""
