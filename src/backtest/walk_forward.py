"""Walk-forward backtesting.

Rolling 6-month train / 1-month test windows. Threshold parameters are
tuned only on the training window — never the test window. Final 30%
of history is held out for v1 validation and not touched here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from itertools import product

from src.backtest.engine import BacktestEngine, BacktestResult
from src.backtest.metrics import Metrics, compute_metrics
from src.config import BotConfig


@dataclass
class Window:
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime
    best_entry_threshold: Decimal
    best_exit_threshold: Decimal
    train_metrics: Metrics
    test_metrics: Metrics
    test_result: BacktestResult


# Conservative search grids — capped per spec §8.4 anti-overfit rules:
# max 3 tunables, max 2 decimals of precision.
ENTRY_GRID = [Decimal("0.0001"), Decimal("0.0002"), Decimal("0.0003"), Decimal("0.0005")]
EXIT_GRID = [Decimal("0.00003"), Decimal("0.00005"), Decimal("0.0001")]


def walk_forward(
    cfg: BotConfig,
    full_start: datetime,
    full_end: datetime,
    data_dir: str = "data/history",
) -> list[Window]:
    train_months = cfg.backtest.walk_forward_train_months
    test_months = cfg.backtest.walk_forward_test_months
    initial = cfg.backtest.initial_equity_eur

    windows: list[Window] = []
    cur = full_start
    while True:
        train_end = _add_months(cur, train_months)
        test_end = _add_months(train_end, test_months)
        if test_end > full_end:
            break

        best: tuple[Metrics, Decimal, Decimal] | None = None
        for ent, exi in product(ENTRY_GRID, EXIT_GRID):
            if exi >= ent:
                continue
            cfg_copy = cfg.model_copy(deep=True)
            cfg_copy.strategy.entry_funding_threshold = ent
            cfg_copy.strategy.exit_funding_threshold = exi
            engine = BacktestEngine(cfg_copy, data_dir=data_dir)
            r = engine.run(cur, train_end, initial)
            m = compute_metrics(r)
            if best is None or m.sharpe > best[0].sharpe:
                best = (m, ent, exi)

        if best is None:
            cur = _add_months(cur, test_months)
            continue

        train_metrics, ent, exi = best
        cfg_test = cfg.model_copy(deep=True)
        cfg_test.strategy.entry_funding_threshold = ent
        cfg_test.strategy.exit_funding_threshold = exi
        engine = BacktestEngine(cfg_test, data_dir=data_dir)
        test_result = engine.run(train_end, test_end, initial)
        test_metrics = compute_metrics(test_result)

        windows.append(
            Window(
                train_start=cur,
                train_end=train_end,
                test_start=train_end,
                test_end=test_end,
                best_entry_threshold=ent,
                best_exit_threshold=exi,
                train_metrics=train_metrics,
                test_metrics=test_metrics,
                test_result=test_result,
            )
        )
        cur = _add_months(cur, test_months)

    return windows


def _add_months(d: datetime, n: int) -> datetime:
    y, m = d.year, d.month + n
    while m > 12:
        y, m = y + 1, m - 12
    return d.replace(year=y, month=m)
