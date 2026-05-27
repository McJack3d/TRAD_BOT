"""Tests for the unified tradbot CLI that hosts both bots.

These tests don't shell out to the CLI — they import the module
directly and assert that:
  * scripts.tradbot registers the ibsent-* subcommands
  * scripts.tradbot_ibsent's HANDLERS dict matches the registered names
  * the IBKR menu items are well-formed (key, label, callable, namespace)
  * `_make_bot()` returns a fully-wired bot in paper mode and the
    paper broker is connected
"""

from __future__ import annotations

import argparse

import pytest

from scripts import tradbot_ibsent


def test_subparsers_registers_all_ibsent_commands():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    tradbot_ibsent.register_subparsers(sub)
    # Every registered ibsent-* subcommand must have a handler.
    choices = sub.choices
    for name in tradbot_ibsent.HANDLERS:
        assert name in choices, f"{name} is in HANDLERS but not registered as a subparser"
    for name in choices:
        if name.startswith("ibsent-"):
            assert name in tradbot_ibsent.HANDLERS, (
                f"{name} is registered as a subparser but has no handler"
            )


def test_menu_items_are_well_formed():
    items = tradbot_ibsent.menu_items()
    assert len(items) >= 5, "menu should expose at least the core commands"
    keys = [k for k, _, _, _ in items]
    assert len(keys) == len(set(keys)), "menu keys must be unique"
    for key, label, fn, ns in items:
        assert isinstance(key, str)
        assert isinstance(label, str) and label
        assert callable(fn)
        assert hasattr(ns, "__dict__"), "namespace must be argparse-like"


@pytest.mark.asyncio
async def test_make_bot_returns_connected_paper_broker(tmp_path, monkeypatch):
    # Point at a throwaway DB so we don't touch data/ibkr_sentiment.db.
    monkeypatch.setenv("IBSENT_MODE", "paper")

    cfg_path = tmp_path / "ibsent.yaml"
    cfg_path.write_text(
        f"""
mode: paper
universe:
  - symbol: AAPL
    sector_etf: XLK
    min_qty: "1"
risk:
  starting_equity_usd: "50000"
llm:
  provider: stub
db_url: sqlite+aiosqlite:///{tmp_path}/ibsent.db
"""
    )
    monkeypatch.setattr(tradbot_ibsent, "IBSENT_CONFIG", str(cfg_path))

    bot, cfg = await tradbot_ibsent._make_bot()
    try:
        assert cfg.mode.value == "paper"
        assert await bot.broker.is_connected()
        # Ingestion poller must NOT be running for one-shot CLI commands.
        assert bot.ingestion is None
        summary = await bot.broker.account_summary()
        assert summary.net_liquidation > 0
    finally:
        await bot.stop()
