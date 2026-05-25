"""Tests for the signal mapping + dollar-neutral basket builder."""

from __future__ import annotations

from decimal import Decimal

from src.ibkr_sentiment.sentiment.models import StructuredSignal
from src.ibkr_sentiment.signal_engine.dollar_neutral import (
    build_dollar_neutral_basket,
    diff_targets,
)
from src.ibkr_sentiment.signal_engine.mapping import Side, decide
from src.ibkr_sentiment.signal_engine.technical import TechnicalSnapshot


def _snap(symbol: str, last: float, sma: float, rsi: float) -> TechnicalSnapshot:
    return TechnicalSnapshot(
        symbol=symbol,
        last_close=Decimal(str(last)),
        sma=Decimal(str(sma)),
        rsi=rsi,
    )


def _signal(symbol: str, score: float, conviction: float = 0.8) -> StructuredSignal:
    return StructuredSignal(
        symbol=symbol,
        score=score,
        conviction=conviction,
        temporal_impact="short_term",
        structural=False,
        sources=("rss",),
        item_ids=("i1",),
    )


def test_decide_classifies_above_threshold_as_long_when_technicals_pass():
    signals = [_signal("AAPL", 0.7)]
    snaps = {"AAPL": _snap("AAPL", last=110, sma=100, rsi=50)}
    decisions = decide(
        signals, snaps,
        long_threshold=0.5, short_threshold=-0.5,
        sma_confirm_pct=0.0, rsi_long_min=30, rsi_short_max=70,
        technical_confirm_required=True,
    )
    assert decisions[0].side == Side.LONG


def test_decide_demotes_to_flat_when_technical_rejects():
    signals = [_signal("AAPL", 0.7)]
    snaps = {"AAPL": _snap("AAPL", last=80, sma=100, rsi=50)}  # below SMA
    decisions = decide(
        signals, snaps,
        long_threshold=0.5, short_threshold=-0.5,
        sma_confirm_pct=0.0, rsi_long_min=30, rsi_short_max=70,
        technical_confirm_required=True,
    )
    assert decisions[0].side == Side.FLAT
    assert decisions[0].rejected is True


def test_decide_short_side():
    signals = [_signal("AAPL", -0.7)]
    snaps = {"AAPL": _snap("AAPL", last=80, sma=100, rsi=40)}
    decisions = decide(
        signals, snaps,
        long_threshold=0.5, short_threshold=-0.5,
        sma_confirm_pct=0.0, rsi_long_min=30, rsi_short_max=70,
        technical_confirm_required=True,
    )
    assert decisions[0].side == Side.SHORT


def test_basket_respects_per_name_cap():
    from src.ibkr_sentiment.signal_engine.mapping import SymbolDecision

    decisions = [
        SymbolDecision(
            symbol="AAPL", side=Side.LONG, composite_score=0.9, conviction=1.0,
            last_price=Decimal("100"), technical_reason="ok",
        ),
        SymbolDecision(
            symbol="MSFT", side=Side.SHORT, composite_score=-0.9, conviction=1.0,
            last_price=Decimal("100"), technical_reason="ok",
        ),
    ]
    targets = build_dollar_neutral_basket(
        decisions,
        nlv=Decimal("10000"),
        max_gross_pct=Decimal("1.0"),
        max_position_pct=Decimal("0.1"),  # 10% per name = $1000
    )
    longs = [t for t in targets if t.side == Side.LONG]
    shorts = [t for t in targets if t.side == Side.SHORT]
    assert longs and shorts
    assert longs[0].notional <= Decimal("1000")
    assert shorts[0].notional <= Decimal("1000")


def test_basket_dollar_neutral_with_balanced_decisions():
    from src.ibkr_sentiment.signal_engine.mapping import SymbolDecision

    decisions = [
        SymbolDecision(
            symbol="AAPL", side=Side.LONG, composite_score=0.8, conviction=1.0,
            last_price=Decimal("100"), technical_reason="ok",
        ),
        SymbolDecision(
            symbol="MSFT", side=Side.SHORT, composite_score=-0.8, conviction=1.0,
            last_price=Decimal("100"), technical_reason="ok",
        ),
    ]
    targets = build_dollar_neutral_basket(
        decisions,
        nlv=Decimal("10000"),
        max_gross_pct=Decimal("1.0"),
        max_position_pct=Decimal("0.5"),
    )
    longs = sum(abs(t.notional) for t in targets if t.side == Side.LONG)
    shorts = sum(abs(t.notional) for t in targets if t.side == Side.SHORT)
    # Long and short notional sums should be equal when conviction +
    # |score| are symmetric.
    assert abs(longs - shorts) <= Decimal("1")


def test_basket_emits_flat_for_rejected_decisions():
    from src.ibkr_sentiment.signal_engine.mapping import SymbolDecision

    decisions = [
        SymbolDecision(
            symbol="ZZZ", side=Side.FLAT, composite_score=0.1, conviction=1.0,
            last_price=Decimal("10"), technical_reason="dead band",
        )
    ]
    targets = build_dollar_neutral_basket(
        decisions,
        nlv=Decimal("10000"),
        max_gross_pct=Decimal("1.0"),
        max_position_pct=Decimal("0.1"),
    )
    assert len(targets) == 1
    assert targets[0].side == Side.FLAT
    assert targets[0].target_qty == Decimal("0")


def test_diff_targets_closes_orphan_positions():
    from src.ibkr_sentiment.signal_engine.dollar_neutral import TargetPosition

    # No target for OLD, current holds 10 shares → diff should close it.
    targets = [
        TargetPosition(
            symbol="AAPL", side=Side.LONG, target_qty=Decimal("5"),
            notional=Decimal("500"), reason="",
        )
    ]
    current = {"AAPL": Decimal("0"), "OLD": Decimal("10")}
    deltas = diff_targets(current, targets)
    by_sym = {d.symbol: d for d in deltas}
    assert by_sym["AAPL"].target_qty == Decimal("5")
    assert by_sym["OLD"].target_qty == Decimal("-10")


def test_diff_targets_skips_when_already_at_target():
    from src.ibkr_sentiment.signal_engine.dollar_neutral import TargetPosition

    targets = [
        TargetPosition(
            symbol="AAPL", side=Side.LONG, target_qty=Decimal("5"),
            notional=Decimal("500"), reason="",
        )
    ]
    current = {"AAPL": Decimal("5")}
    deltas = diff_targets(current, targets)
    assert deltas == []
