"""Two-sided funding carry — the math.

This is the heart of the strategy spec'd in `docs/FUNDING_CARRY_2SIDED.md`.
Pure functions, no I/O, fully testable: given a funding rate, a borrow
rate, an open position (or None), and the config, return ENTER / EXIT /
HOLD on either leg. The execution path and the adapter are separate;
this module decides what to do, not how to do it.

Two legs:

  * **Positive carry** (delta-neutral cash-and-carry): long spot + short
    perp. Receive `funding` per 8h when funding > 0. No borrow.

  * **Negative carry** (the new leg): long perp + borrow-and-short spot.
    Receive `|funding|` per 8h when funding < 0. Pay margin interest at
    the live borrow rate.

The decisive insight (sign-off review §9, point 2):

    enter_negative  ⇔  |funding| − borrow_rate_8h ≥ neg_entry_threshold
                       AND borrow_rate_apr < max_borrow_rate_apr

Net-of-borrow, NOT gross. The legacy 25% APR cap was *above* the entry
threshold (25 % / 1095 ≈ 0.023 %/8h), so a trade could open with borrow
already exceeding income. That bug is fixed in the parameter defaults
and re-asserted in tests.

Universe enforcement (spec §9 point 3) — the negative leg is restricted
to BTC + ETH at the math boundary, not just by config. Defence in depth:
if a future config widens the universe accidentally, the strategy still
refuses to short-borrow anything else.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum

SETTLEMENTS_PER_YEAR = 1095  # 3 × 365 — funding settles every 8h
NEGATIVE_LEG_UNIVERSE: frozenset[str] = frozenset({"BTC/USDT", "ETH/USDT"})


class CarrySide(str, Enum):
    POSITIVE = "positive_carry"  # long spot + short perp; receive +funding
    NEGATIVE = "negative_carry"  # long perp + borrowed-short spot; receive -funding


@dataclass(slots=True)
class CarryConfig:
    """Authoritative parameter set after sign-off (spec §9). The carry
    module enforces these even if upstream config supplies different
    values — defence in depth against accidental loosening."""

    pos_entry_threshold: Decimal = Decimal("0.0002")  # 0.02 %/8h positive funding
    neg_entry_threshold: Decimal = Decimal("0.0003")  # 0.03 %/8h NET (after borrow)
    exit_threshold: Decimal = Decimal("0.00005")      # hysteresis floor for both legs
    min_dwell_hours: int = 24
    max_borrow_rate_apr: Decimal = Decimal("0.15")    # 15 % APR (≈ 0.0137 %/8h)


@dataclass(slots=True)
class CarryPosition:
    """The minimal carry-state the math needs to decide an exit. Mirrors
    the live `Position` model's relevant fields without coupling to the
    DB layer."""

    symbol: str
    side: CarrySide
    opened_at: datetime
    notional: Decimal


@dataclass(slots=True)
class CarrySignal:
    action: str  # "enter", "exit", "hold"
    side: CarrySide | None
    reason: str
    net_carry_8h: Decimal = Decimal("0")


# ---- core math --------------------------------------------------------


def borrow_rate_per_8h(borrow_rate_apr: Decimal) -> Decimal:
    """Convert an APR borrow quote into per-8h cost. Negative or invalid
    inputs clamp to 0 (borrowing money never *pays* you)."""
    if borrow_rate_apr <= 0:
        return Decimal("0")
    return borrow_rate_apr / Decimal(SETTLEMENTS_PER_YEAR)


def net_carry_positive(funding_rate: Decimal) -> Decimal:
    """Per-8h net for the long-spot/short-perp leg.

    Funding > 0 → shorts receive → net = funding_rate.
    Funding ≤ 0 → no positive-leg edge → return the raw funding (caller
    decides whether to act). Fees are entry/exit only and live in the
    backtester/execution, not here.
    """
    return funding_rate


def net_carry_negative(funding_rate: Decimal, borrow_rate_apr: Decimal) -> Decimal:
    """Per-8h NET for the long-perp/short-spot leg.

    Negative funding → longs receive |funding|. Borrowing the spot to
    short it costs borrow_rate_apr / 1095 per 8h. Net is the difference.
    A positive funding rate would mean the bot pays funding while also
    paying borrow — strictly worse — so net is forced negative there.
    """
    if funding_rate >= 0:
        # On the negative leg we'd be long perp during positive funding —
        # pay both funding AND borrow. Caller never enters here, but
        # report the truthful net so it can't be mistaken for an edge.
        return -funding_rate - borrow_rate_per_8h(borrow_rate_apr)
    return -funding_rate - borrow_rate_per_8h(borrow_rate_apr)


# ---- decision functions ----------------------------------------------


def _ensure_aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def evaluate_positive_leg(
    symbol: str,
    funding_rate: Decimal,
    position: CarryPosition | None,
    cfg: CarryConfig,
    now: datetime,
) -> CarrySignal:
    """Decide enter/exit/hold for the positive-carry leg."""
    if position is None:
        if funding_rate >= cfg.pos_entry_threshold:
            return CarrySignal(
                action="enter",
                side=CarrySide.POSITIVE,
                reason=(
                    f"positive carry: funding {funding_rate} ≥ entry "
                    f"{cfg.pos_entry_threshold}"
                ),
                net_carry_8h=net_carry_positive(funding_rate),
            )
        return CarrySignal("hold", None, "funding below positive entry threshold")

    if position.side != CarrySide.POSITIVE:
        return CarrySignal("hold", None, "wrong-side position; evaluated elsewhere")

    dwell = now - _ensure_aware(position.opened_at)
    if dwell < timedelta(hours=cfg.min_dwell_hours):
        return CarrySignal(
            "hold", CarrySide.POSITIVE,
            f"min-dwell not satisfied ({dwell} < {cfg.min_dwell_hours}h)",
            net_carry_8h=net_carry_positive(funding_rate),
        )

    if funding_rate <= cfg.exit_threshold:
        return CarrySignal(
            "exit", CarrySide.POSITIVE,
            f"funding {funding_rate} ≤ exit {cfg.exit_threshold}",
            net_carry_8h=net_carry_positive(funding_rate),
        )
    return CarrySignal("hold", CarrySide.POSITIVE, "carry still above exit",
                       net_carry_8h=net_carry_positive(funding_rate))


def evaluate_negative_leg(
    symbol: str,
    funding_rate: Decimal,
    borrow_rate_apr: Decimal,
    position: CarryPosition | None,
    cfg: CarryConfig,
    now: datetime,
) -> CarrySignal:
    """Decide enter/exit/hold for the negative-carry leg.

    Hard rules (sign-off review §9):
      * Universe-gate: BTC+ETH only at the math layer (defence in depth).
      * Net-of-borrow entry: `|funding| − borrow_8h ≥ neg_entry_threshold`.
      * Borrow-rate cap: refuse entry when `borrow_apr ≥ max_borrow_rate_apr`.
      * Continuous monitor: close an open position if borrow exceeds the cap.
    """
    if symbol not in NEGATIVE_LEG_UNIVERSE:
        return CarrySignal(
            "hold", None,
            f"{symbol} not in negative-leg universe ({sorted(NEGATIVE_LEG_UNIVERSE)})",
        )

    net = net_carry_negative(funding_rate, borrow_rate_apr)

    if position is None:
        if funding_rate >= 0:
            return CarrySignal("hold", None, "funding non-negative — no negative-leg edge",
                               net_carry_8h=net)
        if borrow_rate_apr >= cfg.max_borrow_rate_apr:
            return CarrySignal(
                "hold", None,
                f"borrow {borrow_rate_apr} ≥ cap {cfg.max_borrow_rate_apr}",
                net_carry_8h=net,
            )
        if net >= cfg.neg_entry_threshold:
            return CarrySignal(
                "enter", CarrySide.NEGATIVE,
                (
                    f"negative carry: net {net:.6f} = |{funding_rate}| − "
                    f"{borrow_rate_per_8h(borrow_rate_apr):.6f} ≥ entry "
                    f"{cfg.neg_entry_threshold}"
                ),
                net_carry_8h=net,
            )
        return CarrySignal("hold", None,
                           "net carry below negative entry threshold",
                           net_carry_8h=net)

    if position.side != CarrySide.NEGATIVE:
        return CarrySignal("hold", None, "wrong-side position; evaluated elsewhere")

    # Borrow-rate kill switch trumps everything except min dwell. We
    # still respect min-dwell to avoid open-and-immediately-close churn
    # when the borrow rate ticks above the cap by a hair.
    dwell = now - _ensure_aware(position.opened_at)
    if dwell < timedelta(hours=cfg.min_dwell_hours):
        return CarrySignal(
            "hold", CarrySide.NEGATIVE,
            f"min-dwell not satisfied ({dwell} < {cfg.min_dwell_hours}h)",
            net_carry_8h=net,
        )
    if borrow_rate_apr >= cfg.max_borrow_rate_apr:
        return CarrySignal(
            "exit", CarrySide.NEGATIVE,
            f"borrow {borrow_rate_apr} ≥ cap {cfg.max_borrow_rate_apr} — kill switch",
            net_carry_8h=net,
        )
    if net <= cfg.exit_threshold:
        return CarrySignal(
            "exit", CarrySide.NEGATIVE,
            f"net carry {net} ≤ exit {cfg.exit_threshold}",
            net_carry_8h=net,
        )
    return CarrySignal("hold", CarrySide.NEGATIVE, "carry still above exit",
                       net_carry_8h=net)


def evaluate_both_legs(
    symbol: str,
    funding_rate: Decimal,
    borrow_rate_apr: Decimal,
    position: CarryPosition | None,
    cfg: CarryConfig,
    now: datetime,
) -> CarrySignal:
    """Pick the leg that matches funding's sign. If a position is open,
    the side of the position dictates which leg evaluates."""
    if position is not None:
        if position.side == CarrySide.POSITIVE:
            return evaluate_positive_leg(symbol, funding_rate, position, cfg, now)
        return evaluate_negative_leg(
            symbol, funding_rate, borrow_rate_apr, position, cfg, now
        )

    if funding_rate >= 0:
        return evaluate_positive_leg(symbol, funding_rate, None, cfg, now)
    return evaluate_negative_leg(
        symbol, funding_rate, borrow_rate_apr, None, cfg, now
    )
