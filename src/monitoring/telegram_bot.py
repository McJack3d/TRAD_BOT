"""Telegram bot for real-time alerts and interactive commands.

Commands:
  /status   – current positions, P&L, system state
  /positions
  /funding
  /halt     – soft halt (no new orders)
  /kill     – flatten everything and halt
  /resume   – exit halted state (confirmation required)
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from src.logging_setup import log
from src.state.db import Database
from src.state.models import SystemStatusEnum

FlattenCallback = Callable[[str], Awaitable[None]]


class TelegramNotifier:
    def __init__(
        self,
        token: str,
        chat_id: str,
        db: Database,
        on_flatten: FlattenCallback | None = None,
    ):
        self.token = token
        self.chat_id = chat_id
        self.db = db
        self.on_flatten = on_flatten
        self._app = None

    async def start(self) -> None:
        if not self.token or not self.chat_id:
            log.warning("telegram.disabled.no_credentials")
            return
        try:
            from telegram import Update  # noqa: F401
            from telegram.ext import (  # noqa: F401
                Application,
                CommandHandler,
                ContextTypes,
            )
        except ImportError:
            log.error("telegram.import_failed")
            return

        from telegram.ext import Application, CommandHandler

        self._app = Application.builder().token(self.token).build()
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("positions", self._cmd_positions))
        self._app.add_handler(CommandHandler("funding", self._cmd_funding))
        self._app.add_handler(CommandHandler("halt", self._cmd_halt))
        self._app.add_handler(CommandHandler("kill", self._cmd_kill))
        self._app.add_handler(CommandHandler("resume", self._cmd_resume))
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling()
        log.info("telegram.started")

    async def stop(self) -> None:
        if self._app:
            try:
                await self._app.updater.stop()
                await self._app.stop()
                await self._app.shutdown()
            except Exception as e:
                log.warning("telegram.stop.error", error=str(e))

    async def send(self, title: str, body: str) -> None:
        if not self._app or not self.chat_id:
            log.info("telegram.no_app.send", title=title)
            return
        text = f"*{title}*\n{body}"
        try:
            await self._app.bot.send_message(chat_id=self.chat_id, text=text, parse_mode="Markdown")
        except Exception as e:
            log.warning("telegram.send.failed", error=str(e))

    # ---- command handlers --------------------------------------------

    async def _cmd_status(self, update, context) -> None:  # noqa: ANN001
        status = await self.db.get_status()
        snap = await self.db.latest_snapshot()
        positions = await self.db.open_positions()
        last_recon = (
            status.last_reconciliation_ok.isoformat()
            if status.last_reconciliation_ok
            else "never"
        )
        body = (
            f"Status: `{status.status.value}`\n"
            f"Open positions: {len(positions)}\n"
            f"Equity: {snap.equity_usdt if snap else 'n/a'}\n"
            f"Daily PnL: {snap.realized_pnl_daily if snap else 'n/a'}\n"
            f"Cumulative PnL: {snap.realized_pnl_cumulative if snap else 'n/a'}\n"
            f"Last reconciliation OK: {last_recon}\n"
            f"Halt reason: {status.halt_reason or '-'}"
        )
        await update.message.reply_text(body, parse_mode="Markdown")

    async def _cmd_positions(self, update, context) -> None:  # noqa: ANN001
        positions = await self.db.open_positions()
        if not positions:
            await update.message.reply_text("No open positions.")
            return
        lines = []
        for p in positions:
            lines.append(
                f"{p.symbol}: spot={p.spot_qty} perp={p.perp_qty} "
                f"opened={p.opened_at.isoformat()}"
            )
        await update.message.reply_text("\n".join(lines))

    async def _cmd_funding(self, update, context) -> None:  # noqa: ANN001
        # The strategy keeps live funding rates in memory; for the Telegram
        # path we just acknowledge — the dashboard is the canonical source.
        await update.message.reply_text("See dashboard for current funding rates.")

    async def _cmd_halt(self, update, context) -> None:  # noqa: ANN001
        await self.db.set_status(SystemStatusEnum.PAUSED, reason="manual /halt")
        await update.message.reply_text("System PAUSED. No new orders. /resume to revert.")

    async def _cmd_kill(self, update, context) -> None:  # noqa: ANN001
        await self.db.set_status(SystemStatusEnum.HALTED, reason="manual /kill")
        if self.on_flatten:
            await self.on_flatten("manual /kill")
        await update.message.reply_text("KILL: flattening all + HALT. /resume requires confirmation.")

    async def _cmd_resume(self, update, context) -> None:  # noqa: ANN001
        args = context.args if hasattr(context, "args") else []
        if not args or args[0] != "CONFIRM":
            await update.message.reply_text(
                "Confirm with `/resume CONFIRM`. Review halt_reason via /status first."
            )
            return
        await self.db.set_status(SystemStatusEnum.ACTIVE, reason=None)
        await update.message.reply_text("System RESUMED (ACTIVE).")
