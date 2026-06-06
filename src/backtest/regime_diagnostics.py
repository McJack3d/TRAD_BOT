"""Why does the regime-switch strategy trade so rarely?

`diagnose_regime` walks the indicator arrays once and tallies, bar by
bar, *where the funnel narrows*:

  1. Regime occupancy — what fraction of (warmed-up) bars are TREND vs
     RANGE vs NEUTRAL. If NEUTRAL dominates, the "ADX AND realized-vol
     must agree" gate is the bottleneck.
  2. Trend leg — of the TREND bars, how many have the EMAs aligned
     (would enter) vs not. Isolates "regime starves the leg" from
     "leg rejects the signal".
  3. Range leg — of the RANGE bars, how many see a qualifying band
     touch AND an RSI extreme, broken out so we can see whether it's
     the band touch or the RSI filter that's killing entries.
  4. Realized entries — a full state-machine walk counting actual
     opens, since holding a position blocks new entries.

Pure analysis over precomputed arrays — no fills, no costs.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.strategy.regime import Regime
from src.strategy.regime_switch import (
    Action,
    RegimeSwitchParams,
    SwitchPosition,
    evaluate_at,
    open_from_signal,
    precompute,
)


def _regime_mask(regime_arr: np.ndarray, target: Regime) -> np.ndarray:
    return np.array([r == target for r in regime_arr], dtype=bool)


def diagnose_regime(
    df: pd.DataFrame, params: RegimeSwitchParams | None = None
) -> dict:
    """Return a structured fire-rate diagnosis for `df`."""
    p = params or RegimeSwitchParams()
    pre = precompute(df, p)
    n = len(df)
    close = pre.close

    # "Warmed up" == every indicator the strategy uses is defined,
    # including the regime detector's long rv_lookback.
    warm = ~(
        np.isnan(pre.adx)
        | np.isnan(pre.rv_pct)
        | np.isnan(pre.atr)
        | np.isnan(pre.ema_slow)
        | np.isnan(pre.bb_lower)
        | np.isnan(pre.rsi)
    )
    warm_bars = int(warm.sum())

    is_trend = _regime_mask(pre.regime, Regime.TREND) & warm
    is_range = _regime_mask(pre.regime, Regime.RANGE) & warm
    is_neutral = _regime_mask(pre.regime, Regime.NEUTRAL) & warm

    # ---- trend leg alignment -----------------------------------------
    ema_up = (pre.ema_fast > pre.ema_slow) & (close > pre.ema_slow)
    ema_down = (pre.ema_fast < pre.ema_slow) & (close < pre.ema_slow)
    t_total = int(is_trend.sum())
    t_up = int((is_trend & ema_up).sum())
    t_down = int((is_trend & ema_down).sum())
    t_flat = t_total - t_up - t_down

    # ---- range leg touches -------------------------------------------
    touch_lower = close < pre.bb_lower
    touch_upper = close > pre.bb_upper
    oversold = pre.rsi < p.rsi_os
    overbought = pre.rsi > p.rsi_ob
    r_total = int(is_range.sum())
    r_lower_os = int((is_range & touch_lower & oversold).sum())
    r_lower_only = int((is_range & touch_lower & ~oversold).sum())
    r_upper_ob = int((is_range & touch_upper & overbought).sum())
    r_upper_only = int((is_range & touch_upper & ~overbought).sum())
    r_no_touch = int((is_range & ~(touch_lower | touch_upper)).sum())

    # ---- realized entries from a full state-machine walk -------------
    pos = SwitchPosition.flat()
    realized = 0
    for i in range(n):
        sig = evaluate_at(pre, i, pos, p)
        if pos.side == 0 and sig.action in (Action.ENTER_LONG, Action.ENTER_SHORT):
            realized += 1
            pos = open_from_signal(sig, pre, i, float(close[i]))
            pos.qty = 1.0
        elif pos.side != 0 and sig.action == Action.EXIT:
            pos = SwitchPosition.flat()

    def _pct(x: int, total: int) -> float:
        return (x / total) if total else 0.0

    return {
        "n_bars": n,
        "warmup_bars": n - warm_bars,
        "warm_bars": warm_bars,
        "regime": {
            "trend": t_total,
            "range": r_total,
            "neutral": int(is_neutral.sum()),
            "trend_pct": _pct(t_total, warm_bars),
            "range_pct": _pct(r_total, warm_bars),
            "neutral_pct": _pct(int(is_neutral.sum()), warm_bars),
        },
        "trend_leg": {
            "trend_bars": t_total,
            "ema_long_aligned": t_up,
            "ema_short_aligned": t_down,
            "ema_unaligned": t_flat,
            "would_enter": t_up + t_down,
            "enter_rate": _pct(t_up + t_down, t_total),
        },
        "range_leg": {
            "range_bars": r_total,
            "lower_touch_and_oversold": r_lower_os,
            "lower_touch_not_oversold": r_lower_only,
            "upper_touch_and_overbought": r_upper_ob,
            "upper_touch_not_overbought": r_upper_only,
            "no_band_touch": r_no_touch,
            "would_enter": r_lower_os + r_upper_ob,
            "enter_rate": _pct(r_lower_os + r_upper_ob, r_total),
        },
        "realized_entries": realized,
    }


def bottleneck_verdict(d: dict) -> str:
    """One-line plain-English diagnosis of the main constraint."""
    reg = d["regime"]
    if reg["neutral_pct"] >= 0.85:
        return (
            f"NEUTRAL dominates ({reg['neutral_pct']:.0%}) — the 'ADX AND vol "
            "must agree' gate is the bottleneck. Loosen it (lower adx_trend_min "
            "or rv_high_pctile, or require only one to agree)."
        )
    if reg["trend_pct"] > 0 and d["trend_leg"]["enter_rate"] < 0.05:
        return (
            "TREND fires but the EMA leg almost never aligns inside it — the "
            "trend entry rule is the bottleneck."
        )
    if reg["range_pct"] > 0 and d["range_leg"]["range_bars"] > 0 and d["range_leg"]["would_enter"] == 0:
        touched = (
            d["range_leg"]["lower_touch_not_oversold"]
            + d["range_leg"]["upper_touch_not_overbought"]
        )
        if touched > 0:
            return (
                "RANGE bars touch the bands but RSI is never extreme enough — "
                "the RSI filter (rsi_os/rsi_ob) is the bottleneck for the range leg."
            )
        return "RANGE bars never touch the bands — band width / regime mismatch."
    if d["realized_entries"] < 30:
        return (
            "Regimes and entry rules fire at a plausible rate, but realized "
            "trades are still low — likely cool-off / position-holding eating "
            "opportunities, or simply a quiet 6-month window."
        )
    return "No single dominant bottleneck — entries look reasonably distributed."
