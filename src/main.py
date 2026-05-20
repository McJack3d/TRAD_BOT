"""Entry point — wires all modules and runs the bot."""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from src.adapters.binance import BinanceAdapter
from src.config import BotConfig, Mode, Secrets
from src.data import MarketData
from src.execution.engine import ExecutionEngine
from src.killswitch import KillSwitch
from src.logging_setup import configure_logging, log
from src.monitoring import EmailNotifier, TelegramNotifier
from src.reconciliation import Reconciler
from src.risk import RiskManager
from src.state import Database
from src.state.models import StateSnapshot
from src.strategy import FundingArbStrategy


async def run(config_path: str, kill_file: str) -> None:
    secrets = Secrets()
    cfg = BotConfig.from_yaml(config_path)
    configure_logging(secrets.bot_log_level)
    log.info("bot.starting", mode=cfg.mode.value, config=config_path)

    db = Database(secrets.bot_db_path)
    await db.init(starting_equity=cfg.starting_equity_eur)

    exchange = BinanceAdapter(
        api_key=secrets.binance_api_key,
        api_secret=secrets.binance_api_secret,
        testnet=secrets.binance_testnet,
    )
    if cfg.mode == Mode.LIVE:
        await exchange.connect()
    elif secrets.binance_api_key:
        # Connect anyway for live data even in paper/dry-run.
        try:
            await exchange.connect()
        except Exception as e:
            log.warning("bot.exchange.connect_failed", error=str(e))

    telegram = TelegramNotifier(
        token=secrets.telegram_bot_token,
        chat_id=secrets.telegram_chat_id,
        db=db,
    )
    email = EmailNotifier(
        smtp_host=secrets.email_smtp_host,
        smtp_port=secrets.email_smtp_port,
        username=secrets.email_username,
        password=secrets.email_password,
        from_addr=secrets.email_from,
        to_addr=secrets.email_to,
    )

    async def notify(title: str, body: str) -> None:
        if cfg.monitoring.telegram_enabled:
            await telegram.send(title, body)
        if cfg.monitoring.email_enabled and any(
            k in title.upper() for k in ("HALT", "RECONCILIATION", "EMERGENCY")
        ):
            await email.send(f"[trad-bot] {title}", body)

    market_data = MarketData(
        exchange=exchange,
        symbols=[s.spot for s in cfg.symbols],
    )

    execution = ExecutionEngine(
        cfg=cfg,
        db=db,
        exchange=exchange,
        dry_run=cfg.mode in (Mode.PAPER, Mode.DRY_RUN),
    )

    risk = RiskManager(
        db=db,
        exchange=exchange,
        cfg=cfg.risk,
        starting_equity=cfg.starting_equity_eur,
        on_flatten=execution.emergency_flatten_all,
        on_notify=notify,
    )
    telegram.on_flatten = execution.emergency_flatten_all

    reconciler = Reconciler(
        db=db,
        exchange=exchange,
        cfg=cfg.reconciliation,
        on_notify=notify,
    )

    killswitch = KillSwitch(
        db=db,
        path=kill_file,
        on_flatten=execution.emergency_flatten_all,
    )

    strategy = FundingArbStrategy(
        cfg=cfg,
        db=db,
        market_data=market_data,
        risk=risk,
        execution=execution,
    )

    await market_data.start()
    await risk.start()
    await reconciler.start()
    await killswitch.start()
    if cfg.monitoring.telegram_enabled:
        await telegram.start()

    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        log.info("bot.signal.received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass  # Windows

    # Strategy tick loop — runs every minute; the funding-rate poller is
    # what produces fresh inputs.
    async def tick_loop() -> None:
        while not stop_event.is_set():
            try:
                await strategy.evaluate_all()
                await _snapshot_equity(db, exchange, cfg.starting_equity_eur)
            except Exception as e:
                log.exception("bot.tick.error", error=str(e))
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=60.0)
            except asyncio.TimeoutError:
                pass

    tick_task = asyncio.create_task(tick_loop())

    await stop_event.wait()
    log.info("bot.shutdown.start")

    tick_task.cancel()
    await market_data.stop()
    await risk.stop()
    await reconciler.stop()
    await killswitch.stop()
    if cfg.monitoring.telegram_enabled:
        await telegram.stop()
    await exchange.close()
    await db.close()
    log.info("bot.shutdown.done")


async def _snapshot_equity(
    db: Database, exchange, starting_equity: Decimal
) -> None:
    try:
        balances = await exchange.fetch_balances()
        usdt = sum(
            (b.total for b in balances.values() if b.asset == "USDT"),
            start=Decimal("0"),
        )
        snap = StateSnapshot(
            ts=datetime.now(UTC),
            equity_usdt=usdt or starting_equity,
            spot_balance_usdt=Decimal("0"),
            perp_balance_usdt=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            realized_pnl_daily=Decimal("0"),
            realized_pnl_cumulative=Decimal("0"),
        )
        await db.add_snapshot(snap)
    except Exception as e:
        log.warning("bot.snapshot.error", error=str(e))


def cli() -> None:
    parser = argparse.ArgumentParser(description="trad-bot")
    parser.add_argument("--config", default="config/paper.yaml")
    parser.add_argument("--kill-file", default="/var/lib/bot/KILL")
    args = parser.parse_args()

    if not Path(args.config).exists():
        print(f"config not found: {args.config}", file=sys.stderr)
        sys.exit(2)

    try:
        import uvloop  # type: ignore[import-not-found]

        uvloop.install()
    except ImportError:
        pass

    asyncio.run(run(args.config, args.kill_file))


if __name__ == "__main__":
    cli()
