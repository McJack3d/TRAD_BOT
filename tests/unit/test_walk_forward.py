"""Walk-forward harness smoke test on synthetic data."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd

from src.backtest.walk_forward import ENTRY_GRID, EXIT_GRID, walk_forward
from src.config import (
    BacktestConfig,
    BotConfig,
    FeesConfig,
    Mode,
    RiskConfig,
    StrategyConfig,
    SymbolConfig,
)


def _write_funding(data_dir: Path, symbol: str, start: datetime, months: int, rate: float) -> None:
    folder = data_dir / "funding" / symbol.replace("/", "_")
    folder.mkdir(parents=True, exist_ok=True)
    rows = []
    ts = start
    for _ in range(months * 30 * 3):  # ~3 events/day
        rows.append(
            {
                "ts": ts,
                "symbol": symbol,
                "funding_rate": rate,
                "mark_price": 30000.0,
            }
        )
        ts += timedelta(hours=8)
    df = pd.DataFrame(rows)
    out = folder / f"{start:%Y-%m}.parquet"
    df.to_parquet(out, index=False)


def test_walk_forward_yields_windows(tmp_path: Path) -> None:
    cfg = BotConfig(
        mode=Mode.BACKTEST,
        starting_equity_eur=Decimal("1000"),
        symbols=[SymbolConfig(spot="BTC/USDT", perp="BTC/USDT:USDT")],
        strategy=StrategyConfig(),
        risk=RiskConfig(),
        fees=FeesConfig(),
        backtest=BacktestConfig(
            initial_equity_eur=Decimal("1000"),
            walk_forward_train_months=2,
            walk_forward_test_months=1,
            data_dir=str(tmp_path / "h"),
        ),
    )

    start = datetime(2024, 1, 1, tzinfo=UTC)
    # Synthesize 9 months of strongly positive funding so the strategy trades.
    months = 9
    cur = start
    for _ in range(months):
        _write_funding(Path(cfg.backtest.data_dir), "BTC/USDT", cur, 1, rate=0.0003)
        if cur.month == 12:
            cur = cur.replace(year=cur.year + 1, month=1)
        else:
            cur = cur.replace(month=cur.month + 1)

    windows = walk_forward(
        cfg,
        full_start=start,
        full_end=start + timedelta(days=30 * months),
        data_dir=cfg.backtest.data_dir,
    )

    # 2-month train + 1-month test = 3-month stride. 9 months → 7 stride steps,
    # but each window needs test_end <= full_end, so we expect about 6-7 windows.
    assert len(windows) >= 4
    for w in windows:
        assert w.best_entry_threshold in ENTRY_GRID
        assert w.best_exit_threshold in EXIT_GRID
        assert w.best_exit_threshold < w.best_entry_threshold
        # On a constant-positive-funding tape we expect test Sharpe > 0
        # for at least one window — not all, because some test windows may
        # have only a few funding events depending on dwell timing.
    assert any(w.test_metrics.sharpe > 0 for w in windows)


def test_walk_forward_no_data_returns_empty(tmp_path: Path) -> None:
    cfg = BotConfig(
        mode=Mode.BACKTEST,
        starting_equity_eur=Decimal("1000"),
        symbols=[SymbolConfig(spot="BTC/USDT", perp="BTC/USDT:USDT")],
        backtest=BacktestConfig(
            walk_forward_train_months=2,
            walk_forward_test_months=1,
            data_dir=str(tmp_path / "empty"),
        ),
    )
    start = datetime(2024, 1, 1, tzinfo=UTC)
    windows = walk_forward(
        cfg,
        full_start=start,
        full_end=start + timedelta(days=90),
        data_dir=cfg.backtest.data_dir,
    )
    # Engine yields windows but all metrics are degenerate. We just verify
    # the harness doesn't crash on an empty data dir.
    for w in windows:
        assert w.test_metrics.n_trades == 0
