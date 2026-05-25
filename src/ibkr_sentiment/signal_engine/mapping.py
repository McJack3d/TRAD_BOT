"""Sentiment → tradable signal mapping.

Takes a list of `StructuredSignal` (one per symbol, already aggregated
from the LLM verdicts) and decides which become LONG / SHORT / FLAT
candidates after technical confirmation.

The output is a list of `SymbolDecision` — one per symbol the universe
has seen any signal for. The dollar-neutral basket builder then
assembles those into a final order list.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from src.ibkr_sentiment.sentiment.models import StructuredSignal
from src.ibkr_sentiment.signal_engine.technical import (
    TechnicalSnapshot,
    long_technical_ok,
    short_technical_ok,
)


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


@dataclass(slots=True)
class SymbolDecision:
    symbol: str
    side: Side
    composite_score: float  # the raw [-1, +1] sentiment score
    conviction: float
    last_price: Decimal
    technical_reason: str
    rejected: bool = False
    rejected_reason: str = ""


def decide(
    signals: Iterable[StructuredSignal],
    snapshots: dict[str, TechnicalSnapshot],
    *,
    long_threshold: float,
    short_threshold: float,
    sma_confirm_pct: float,
    rsi_long_min: float,
    rsi_short_max: float,
    technical_confirm_required: bool,
) -> list[SymbolDecision]:
    """Classify each symbol into LONG / SHORT / FLAT.

    A symbol is FLAT when the composite score doesn't clear either
    threshold OR when the requested side's technical guard rejects it.
    """
    out: list[SymbolDecision] = []
    for sig in signals:
        snap = snapshots.get(sig.symbol)
        last_price = snap.last_close if snap is not None else Decimal("0")
        if sig.score >= long_threshold:
            if snap is None:
                out.append(
                    SymbolDecision(
                        symbol=sig.symbol,
                        side=Side.FLAT,
                        composite_score=sig.score,
                        conviction=sig.conviction,
                        last_price=last_price,
                        technical_reason="no technical snapshot",
                        rejected=True,
                        rejected_reason="no_snapshot",
                    )
                )
                continue
            check = long_technical_ok(
                snap,
                sma_confirm_pct=sma_confirm_pct,
                rsi_long_min=rsi_long_min,
                required=technical_confirm_required,
            )
            out.append(
                SymbolDecision(
                    symbol=sig.symbol,
                    side=Side.LONG if check.ok else Side.FLAT,
                    composite_score=sig.score,
                    conviction=sig.conviction,
                    last_price=last_price,
                    technical_reason=check.reason,
                    rejected=not check.ok,
                    rejected_reason="" if check.ok else "long_technical",
                )
            )
        elif sig.score <= short_threshold:
            if snap is None:
                out.append(
                    SymbolDecision(
                        symbol=sig.symbol,
                        side=Side.FLAT,
                        composite_score=sig.score,
                        conviction=sig.conviction,
                        last_price=last_price,
                        technical_reason="no technical snapshot",
                        rejected=True,
                        rejected_reason="no_snapshot",
                    )
                )
                continue
            check = short_technical_ok(
                snap,
                sma_confirm_pct=sma_confirm_pct,
                rsi_short_max=rsi_short_max,
                required=technical_confirm_required,
            )
            out.append(
                SymbolDecision(
                    symbol=sig.symbol,
                    side=Side.SHORT if check.ok else Side.FLAT,
                    composite_score=sig.score,
                    conviction=sig.conviction,
                    last_price=last_price,
                    technical_reason=check.reason,
                    rejected=not check.ok,
                    rejected_reason="" if check.ok else "short_technical",
                )
            )
        else:
            out.append(
                SymbolDecision(
                    symbol=sig.symbol,
                    side=Side.FLAT,
                    composite_score=sig.score,
                    conviction=sig.conviction,
                    last_price=last_price,
                    technical_reason="score in dead band",
                )
            )
    return out
