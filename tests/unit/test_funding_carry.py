"""Tests for the two-sided funding-carry math.

The high-value tests lock down sign-off review §9 corrections that
were *bugs* in the original spec defaults — if these regress, the
strategy is misconfigured by definition.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from src.strategy.funding_carry import (
    NEGATIVE_LEG_UNIVERSE,
    SETTLEMENTS_PER_YEAR,
    CarryConfig,
    CarryPosition,
    CarrySide,
    borrow_rate_per_8h,
    evaluate_both_legs,
    evaluate_negative_leg,
    evaluate_positive_leg,
    net_carry_negative,
    net_carry_positive,
)


NOW = datetime(2026, 6, 7, 12, 0, tzinfo=UTC)


# ---- the per-8h math --------------------------------------------------


def test_borrow_rate_per_8h_uses_1095_settlements():
    # 25% APR → 25/1095 ≈ 0.000228 per 8h.
    assert borrow_rate_per_8h(Decimal("0.25")) == Decimal("0.25") / Decimal(SETTLEMENTS_PER_YEAR)
    # 15% APR is the agreed cap. 15/1095 ≈ 0.0001370 per 8h —
    # safely below the 0.0002 positive entry threshold.
    rate = borrow_rate_per_8h(Decimal("0.15"))
    assert rate < Decimal("0.00014")
    assert rate > Decimal("0.00013")


def test_borrow_rate_zero_or_negative_clamps_to_zero():
    assert borrow_rate_per_8h(Decimal("0")) == 0
    assert borrow_rate_per_8h(Decimal("-0.01")) == 0


def test_net_carry_positive_passes_funding_through():
    assert net_carry_positive(Decimal("0.0003")) == Decimal("0.0003")


def test_net_carry_negative_subtracts_borrow_correctly():
    # Funding -0.0005 (i.e. -0.05%/8h), borrow 10% APR.
    # |funding| = 0.0005, borrow_8h = 0.10/1095 ≈ 0.0000913.
    # Net = 0.0005 - 0.0000913 ≈ 0.0004087.
    net = net_carry_negative(Decimal("-0.0005"), Decimal("0.10"))
    assert net == Decimal("0.0005") - (Decimal("0.10") / Decimal(SETTLEMENTS_PER_YEAR))
    assert net > Decimal("0.0004")
    assert net < Decimal("0.0005")


def test_net_carry_negative_when_funding_is_positive_is_strictly_negative():
    """If we were (hypothetically) on the negative leg during positive
    funding, we'd pay funding AND borrow — net must be negative."""
    net = net_carry_negative(Decimal("0.0001"), Decimal("0.10"))
    assert net < 0


# ---- THE BUG FROM THE SIGN-OFF REVIEW (point 2) ----------------------


def test_25pct_apr_borrow_exceeds_legacy_002_threshold():
    """Regression: locks in the corrected default. 25% APR borrow on
    the negative leg would be 0.023%/8h — ABOVE the legacy 0.02% positive
    threshold, so a 'mirror' threshold would let trades open already
    underwater. The agreed cap is 15%.

    This test fails loudly if anyone resets `max_borrow_rate_apr` to a
    value above 0.020/8h-equivalent."""
    cfg = CarryConfig()
    # Cap as 8h rate must be safely below the positive entry threshold.
    cap_8h = borrow_rate_per_8h(cfg.max_borrow_rate_apr)
    assert cap_8h < cfg.pos_entry_threshold, (
        f"Borrow cap {cfg.max_borrow_rate_apr} APR = {cap_8h}/8h is NOT "
        f"safely below the positive entry threshold {cfg.pos_entry_threshold}. "
        "This is the bug the review caught."
    )


def test_negative_entry_threshold_is_50pct_premium_over_positive():
    """Sign-off review §9 point 1: the negative leg carries asymmetric
    risk (borrow cost, recall, squeezes) so its NET threshold must demand
    a premium over the positive leg's gross threshold."""
    cfg = CarryConfig()
    assert cfg.neg_entry_threshold == cfg.pos_entry_threshold * Decimal("1.5")


# ---- positive-leg decisions ------------------------------------------


def test_positive_enter_when_funding_clears_threshold():
    sig = evaluate_positive_leg(
        "BTC/USDT", Decimal("0.0003"), None, CarryConfig(), NOW
    )
    assert sig.action == "enter"
    assert sig.side == CarrySide.POSITIVE


def test_positive_hold_below_threshold():
    sig = evaluate_positive_leg(
        "BTC/USDT", Decimal("0.0001"), None, CarryConfig(), NOW
    )
    assert sig.action == "hold"
    assert sig.side is None


def test_positive_dwell_blocks_early_exit():
    pos = CarryPosition("BTC/USDT", CarrySide.POSITIVE, NOW - timedelta(hours=2), Decimal("1000"))
    sig = evaluate_positive_leg("BTC/USDT", Decimal("0.00001"), pos, CarryConfig(), NOW)
    assert sig.action == "hold"
    assert "min-dwell" in sig.reason


def test_positive_exits_after_dwell_when_funding_decays():
    pos = CarryPosition("BTC/USDT", CarrySide.POSITIVE, NOW - timedelta(hours=48), Decimal("1000"))
    sig = evaluate_positive_leg("BTC/USDT", Decimal("0.00001"), pos, CarryConfig(), NOW)
    assert sig.action == "exit"


# ---- negative-leg decisions: the asymmetric ones ---------------------


def test_negative_universe_gate_blocks_altcoins():
    """The negative leg never touches non-BTC/ETH symbols, regardless of
    config — defence in depth per spec §9 point 3."""
    sig = evaluate_negative_leg(
        "SOL/USDT", Decimal("-0.0010"), Decimal("0.05"),
        None, CarryConfig(), NOW,
    )
    assert sig.action == "hold"
    assert "universe" in sig.reason
    # Sanity: BTC/USDT and ETH/USDT are inside.
    assert "BTC/USDT" in NEGATIVE_LEG_UNIVERSE
    assert "ETH/USDT" in NEGATIVE_LEG_UNIVERSE


def test_negative_enters_when_net_clears_higher_threshold():
    """Funding -0.05%/8h, borrow 5% APR → net ≈ 0.0004543. Above the
    0.0003 negative threshold."""
    sig = evaluate_negative_leg(
        "BTC/USDT", Decimal("-0.0005"), Decimal("0.05"),
        None, CarryConfig(), NOW,
    )
    assert sig.action == "enter"
    assert sig.side == CarrySide.NEGATIVE
    assert sig.net_carry_8h > CarryConfig().neg_entry_threshold


def test_negative_blocks_when_net_clears_positive_but_not_negative_threshold():
    """The asymmetric threshold exists for a reason: a setup that would
    qualify on the positive side (>=0.0002) must NOT qualify on the
    negative side until it clears 0.0003."""
    # Find funding/borrow that nets exactly ~0.00025 — between thresholds.
    # |funding| = 0.0003, borrow 0.05/1095 ≈ 0.0000457 → net ≈ 0.0002543.
    sig = evaluate_negative_leg(
        "BTC/USDT", Decimal("-0.0003"), Decimal("0.05"),
        None, CarryConfig(), NOW,
    )
    assert Decimal("0.0002") <= sig.net_carry_8h < Decimal("0.0003")
    assert sig.action == "hold"


def test_negative_blocks_when_borrow_rate_above_cap():
    """The kill-switch trumps a juicy-looking funding rate when borrow
    is high — exactly the scenario the legacy 25% APR cap would have
    allowed."""
    # Funding -0.04%/8h is huge; borrow 20% APR is above the 15% cap.
    sig = evaluate_negative_leg(
        "BTC/USDT", Decimal("-0.0004"), Decimal("0.20"),
        None, CarryConfig(), NOW,
    )
    assert sig.action == "hold"
    assert "cap" in sig.reason


def test_negative_continuous_kills_position_when_borrow_spikes():
    """If we're already in and the borrow rate crosses the cap, the
    monitor closes — even when net is technically still positive."""
    pos = CarryPosition(
        "BTC/USDT", CarrySide.NEGATIVE, NOW - timedelta(hours=48), Decimal("1000")
    )
    sig = evaluate_negative_leg(
        "BTC/USDT", Decimal("-0.0010"), Decimal("0.20"),  # borrow > 15% cap
        pos, CarryConfig(), NOW,
    )
    assert sig.action == "exit"
    assert "kill switch" in sig.reason


def test_negative_respects_min_dwell_even_at_borrow_spike():
    """Defensive: avoid open-and-immediately-close churn from a transient
    borrow blip in the first 24h."""
    pos = CarryPosition(
        "BTC/USDT", CarrySide.NEGATIVE, NOW - timedelta(hours=2), Decimal("1000")
    )
    sig = evaluate_negative_leg(
        "BTC/USDT", Decimal("-0.0010"), Decimal("0.20"),
        pos, CarryConfig(), NOW,
    )
    assert sig.action == "hold"
    assert "min-dwell" in sig.reason


def test_negative_exits_after_dwell_when_net_decays():
    pos = CarryPosition(
        "BTC/USDT", CarrySide.NEGATIVE, NOW - timedelta(hours=48), Decimal("1000")
    )
    # Tiny |funding|; borrow eats it.
    sig = evaluate_negative_leg(
        "BTC/USDT", Decimal("-0.00001"), Decimal("0.05"),
        pos, CarryConfig(), NOW,
    )
    assert sig.action == "exit"


def test_negative_holds_when_no_universe_position_but_open():
    """An open position whose symbol drifts out of the universe (e.g. a
    config change between restarts) is still managed — we exit it via
    the normal rules, not strand it."""
    pos = CarryPosition(
        "DOGE/USDT", CarrySide.NEGATIVE, NOW - timedelta(hours=48), Decimal("1000")
    )
    sig = evaluate_negative_leg(
        "DOGE/USDT", Decimal("-0.0010"), Decimal("0.05"),
        pos, CarryConfig(), NOW,
    )
    # Universe gate is at the new-entry boundary; existing positions get
    # the normal exit rule. (Keeps us from leaving the position naked.)
    assert sig.action == "hold"  # would be "exit" if borrow > cap or net low


# ---- dispatcher -------------------------------------------------------


def test_dispatcher_picks_positive_leg_for_positive_funding():
    sig = evaluate_both_legs(
        "BTC/USDT", Decimal("0.0003"), Decimal("0.05"),
        None, CarryConfig(), NOW,
    )
    assert sig.action == "enter"
    assert sig.side == CarrySide.POSITIVE


def test_dispatcher_picks_negative_leg_for_negative_funding():
    sig = evaluate_both_legs(
        "BTC/USDT", Decimal("-0.0005"), Decimal("0.05"),
        None, CarryConfig(), NOW,
    )
    assert sig.action == "enter"
    assert sig.side == CarrySide.NEGATIVE


def test_dispatcher_routes_to_position_side_when_open():
    """If we have a positive position open and funding flips negative, we
    still evaluate the *positive* leg (to decide whether to exit it),
    not the negative leg."""
    pos = CarryPosition("BTC/USDT", CarrySide.POSITIVE, NOW - timedelta(hours=48), Decimal("1000"))
    sig = evaluate_both_legs(
        "BTC/USDT", Decimal("-0.0001"), Decimal("0.05"),
        pos, CarryConfig(), NOW,
    )
    assert sig.side != CarrySide.NEGATIVE


def test_naive_datetime_position_is_treated_as_utc():
    """SQLite reads back tz-naive datetimes; the math must not crash on
    that — same bug class as the loss-stop tz fix on the existing daemon."""
    naive_opened = (NOW - timedelta(hours=48)).replace(tzinfo=None)
    pos = CarryPosition("BTC/USDT", CarrySide.POSITIVE, naive_opened, Decimal("1000"))
    sig = evaluate_positive_leg(
        "BTC/USDT", Decimal("0.00001"), pos, CarryConfig(), NOW,
    )
    assert sig.action == "exit"  # dwell satisfied, funding below exit
