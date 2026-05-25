"""Tests for SMA + RSI confirmation."""

from __future__ import annotations

from decimal import Decimal

from src.ibkr_sentiment.signal_engine.technical import (
    evaluate_technicals,
    long_technical_ok,
    relative_strength_index,
    short_technical_ok,
    simple_moving_average,
)


def test_sma_returns_none_with_too_few_bars():
    assert simple_moving_average([1, 2, 3], 5) is None


def test_sma_correct_over_window():
    closes = list(range(1, 11))  # 1..10
    assert simple_moving_average(closes, 5) == sum([6, 7, 8, 9, 10]) / 5


def test_rsi_returns_none_with_too_few_bars():
    assert relative_strength_index([1, 2, 3], 14) is None


def test_rsi_all_up_gives_100():
    closes = list(range(1, 20))
    assert relative_strength_index(closes, 14) == 100.0


def test_rsi_all_down_gives_zero():
    closes = list(range(20, 1, -1))
    assert relative_strength_index(closes, 14) == 0.0


def test_long_technical_blocks_when_close_below_sma():
    snap = evaluate_technicals(
        "AAPL",
        list(range(100, 0, -1)),  # falling series
        sma_window=10,
        rsi_window=14,
    )
    check = long_technical_ok(
        snap, sma_confirm_pct=0.0, rsi_long_min=35.0, required=True
    )
    assert check.ok is False
    assert "SMA" in check.reason or "RSI" in check.reason


def test_long_technical_passes_when_uptrend_and_rsi_ok():
    closes = list(range(1, 60))  # smooth uptrend
    snap = evaluate_technicals(
        "AAPL", closes, sma_window=10, rsi_window=14
    )
    check = long_technical_ok(
        snap, sma_confirm_pct=0.0, rsi_long_min=20.0, required=True
    )
    assert check.ok is True


def test_short_technical_blocks_when_rsi_above_ceiling():
    closes = list(range(1, 60))  # rising → RSI saturates near 100
    snap = evaluate_technicals(
        "AAPL", closes, sma_window=10, rsi_window=14
    )
    check = short_technical_ok(
        snap, sma_confirm_pct=0.0, rsi_short_max=60.0, required=True
    )
    assert check.ok is False


def test_technical_disabled_always_passes():
    closes = [1.0]
    snap = evaluate_technicals("AAPL", closes, sma_window=10, rsi_window=14)
    long_ok = long_technical_ok(
        snap, sma_confirm_pct=0.0, rsi_long_min=35.0, required=False
    )
    short_ok = short_technical_ok(
        snap, sma_confirm_pct=0.0, rsi_short_max=65.0, required=False
    )
    assert long_ok.ok and short_ok.ok


def test_evaluate_technicals_handles_empty():
    snap = evaluate_technicals("AAPL", [], sma_window=10, rsi_window=14)
    assert snap.last_close == Decimal("0")
    assert snap.sma is None
    assert snap.rsi is None
