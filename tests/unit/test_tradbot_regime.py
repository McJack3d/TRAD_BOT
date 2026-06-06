"""Tests for the regime-backtest CLI wiring."""

from __future__ import annotations

import argparse

from scripts import tradbot_regime


def test_subparsers_match_handlers():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    tradbot_regime.register_subparsers(sub)
    for name in tradbot_regime.HANDLERS:
        assert name in sub.choices
    for name in sub.choices:
        if name.startswith("regime-"):
            assert name in tradbot_regime.HANDLERS


def test_menu_items_well_formed():
    items = tradbot_regime.menu_items()
    assert len(items) == 3
    keys = [k for k, _, _, _ in items]
    assert len(keys) == len(set(keys))
    for key, label, fn, ns in items:
        assert isinstance(key, str)
        assert isinstance(label, str) and label
        assert callable(fn)
        assert hasattr(ns, "__dict__")


def test_build_args_defaults_sensible():
    ns = tradbot_regime._build_args(
        "BTC/USDT,ETH/USDT", "1h", months=3, sweep=False
    )
    assert ns.symbols == ["BTC/USDT", "ETH/USDT"]
    assert ns.timeframes == ["1h"]
    assert ns.months == 3
    # Sizing policy from the spec — must not drift silently.
    assert ns.risk_pct == 0.01
    assert ns.max_leverage == 3.0
    assert ns.fee_bps == 4.0
    assert ns.slippage_bps == 2.0
