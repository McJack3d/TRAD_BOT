"""Stage 1 — FinBERT discriminative screen.

Two implementations:

  * `FinBertScorer` loads the real transformer model. Lazy import so
    the dependency is only required when you actually want the heavy
    model in the loop.
  * `StubFinBertScorer` is a deterministic keyword-driven scorer used
    for tests and for paper-mode dry runs where you don't want to pay
    the model-loading cost.

Both implement the same `Scorer` protocol so the pipeline can swap
between them transparently.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from src.ibkr_sentiment.sentiment.models import FinBertScore, NewsItem


class Scorer(Protocol):
    async def score(self, items: Iterable[NewsItem]) -> list[FinBertScore]: ...


# ---- stub scorer --------------------------------------------------------

# A short bag-of-words proxy for the FinBERT polarity classifier.
# Good enough for tests; obviously not good enough for live trading.
_POSITIVE = {
    "beat", "beats", "beat-and-raise", "beats expectations", "record",
    "surge", "surges", "soared", "soars", "rallied", "rallies", "upgrade",
    "upgraded", "raises guidance", "outperform", "strong", "growth",
    "wins", "approved", "approval", "breakthrough", "exceeds",
}
_NEGATIVE = {
    "miss", "misses", "missed", "slump", "slumps", "plunge", "plunged",
    "downgrade", "downgraded", "lowers guidance", "underperform", "fraud",
    "lawsuit", "investigation", "probe", "recall", "halts", "halt",
    "weak", "loss", "losses", "delays", "fired", "resign", "warns",
}

_TOKEN = re.compile(r"[a-zA-Z][a-zA-Z\-]+")


def _bow_score(text: str) -> tuple[str, float, float]:
    """Naive polarity + pseudo-confidence from word presence.

    Returns (polarity_label, signed_score, confidence) where
    confidence is a softmax-ish proxy `|score| + small floor`.
    """
    tokens = [t.lower() for t in _TOKEN.findall(text)]
    pos = sum(1 for t in tokens if t in _POSITIVE)
    neg = sum(1 for t in tokens if t in _NEGATIVE)
    total = pos + neg
    if total == 0:
        return "neutral", 0.0, 0.5
    score = (pos - neg) / total
    confidence = min(1.0, 0.5 + 0.5 * (total / 5.0))
    label = "positive" if score > 0.15 else "negative" if score < -0.15 else "neutral"
    return label, max(-1.0, min(1.0, score)), confidence


@dataclass
class StubFinBertScorer:
    """Keyword-based scorer used when transformers isn't installed
    or when running tests."""

    max_input_chars: int = 2_000

    async def score(self, items: Iterable[NewsItem]) -> list[FinBertScore]:
        out: list[FinBertScore] = []
        for item in items:
            text = f"{item.title}\n{item.body}"[: self.max_input_chars]
            label, score, conf = _bow_score(text)
            out.append(
                FinBertScore(
                    item_id=item.id,
                    polarity=label,  # type: ignore[arg-type]
                    score=score,
                    confidence=conf,
                )
            )
        return out


# ---- real FinBERT scorer ------------------------------------------------


class FinBertScorer:
    """Wraps the ProsusAI/finbert HuggingFace model.

    Heavy dependency (transformers + torch) — import lazily so the
    rest of the bot keeps working without them. Constructor raises a
    clear ImportError if the optional `sentiment` extra hasn't been
    installed.
    """

    def __init__(
        self,
        model_name: str = "ProsusAI/finbert",
        device: str = "cpu",
        max_input_chars: int = 2_000,
        batch_size: int = 16,
    ):
        try:
            import torch  # type: ignore[import-not-found]  # noqa: F401
            from transformers import (  # type: ignore[import-not-found]
                AutoModelForSequenceClassification,
                AutoTokenizer,
            )
        except ImportError as e:
            raise ImportError(
                "FinBertScorer requires the 'sentiment' extra. "
                "Install with: pip install -e '.[sentiment]'"
            ) from e

        self.tokenizer = AutoTokenizer.from_pretrained(model_name)
        self.model = AutoModelForSequenceClassification.from_pretrained(model_name)
        self.device = device
        self.model.to(device)
        self.model.eval()
        self.max_input_chars = max_input_chars
        self.batch_size = batch_size
        # ProsusAI/finbert label order: positive=0, negative=1, neutral=2.
        # We confirm by `id2label` to avoid silently flipping signs if HF
        # changes the order.
        id2label = {int(k): v.lower() for k, v in self.model.config.id2label.items()}
        self._label_index = {v: k for k, v in id2label.items()}
        for required in ("positive", "negative", "neutral"):
            if required not in self._label_index:
                raise ValueError(
                    f"FinBERT model missing expected label '{required}'"
                )

    async def score(self, items: Iterable[NewsItem]) -> list[FinBertScore]:
        import torch  # type: ignore[import-not-found]

        items_list = list(items)
        results: list[FinBertScore] = []
        for start in range(0, len(items_list), self.batch_size):
            batch = items_list[start : start + self.batch_size]
            texts = [
                f"{i.title}\n{i.body}"[: self.max_input_chars] for i in batch
            ]
            enc = self.tokenizer(
                texts, padding=True, truncation=True, return_tensors="pt"
            )
            enc = {k: v.to(self.device) for k, v in enc.items()}
            with torch.no_grad():
                logits = self.model(**enc).logits
            probs = torch.softmax(logits, dim=-1).cpu().numpy()
            for item, p in zip(batch, probs, strict=True):
                pos = float(p[self._label_index["positive"]])
                neg = float(p[self._label_index["negative"]])
                neu = float(p[self._label_index["neutral"]])
                # Signed sentiment in [-1, +1], confidence = max class prob.
                signed = pos - neg
                conf = max(pos, neg, neu)
                label = (
                    "positive"
                    if pos >= neg and pos >= neu
                    else "negative"
                    if neg >= neu
                    else "neutral"
                )
                results.append(
                    FinBertScore(
                        item_id=item.id,
                        polarity=label,  # type: ignore[arg-type]
                        score=signed,
                        confidence=conf,
                    )
                )
        return results
