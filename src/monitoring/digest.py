"""Daily and weekly digest scheduler.

Wakes at the configured UTC hour. Daily digest summarizes the previous
24h. Weekly digest fires on the configured day-of-week. Uses
`EmailNotifier` to send.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from src.config import BotConfig
from src.logging_setup import log
from src.monitoring.email import EmailNotifier
from src.state import Database
from src.state.models import FundingPayment, Position, PositionStatus, StateSnapshot


class DigestScheduler:
    def __init__(self, cfg: BotConfig, db: Database, email: EmailNotifier):
        self.cfg = cfg
        self.db = db
        self.email = email
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while True:
            wait = self._seconds_until_next_run()
            try:
                await asyncio.sleep(wait)
                await self._send_daily()
                now = datetime.now(UTC)
                if now.weekday() == self.cfg.monitoring.weekly_digest_utc_dow:
                    await self._send_weekly()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("digest.error", error=str(e))

    def _seconds_until_next_run(self) -> float:
        now = datetime.now(UTC)
        target = now.replace(
            hour=self.cfg.monitoring.daily_digest_utc_hour,
            minute=0,
            second=0,
            microsecond=0,
        )
        if target <= now:
            target += timedelta(days=1)
        return (target - now).total_seconds()

    async def _send_daily(self) -> None:
        body = await self.build_digest(window_hours=24, title="Daily")
        await self.email.send("[trad-bot] Daily digest", body)

    async def _send_weekly(self) -> None:
        body = await self.build_digest(window_hours=24 * 7, title="Weekly")
        await self.email.send("[trad-bot] Weekly digest", body)

    async def build_digest(self, window_hours: int, title: str) -> str:
        """Generate the digest body. Public for tests."""
        since = datetime.now(UTC) - timedelta(hours=window_hours)
        async with self.db.session() as s:
            funding_rows = (
                await s.execute(
                    select(FundingPayment).where(FundingPayment.funding_time >= since)
                )
            ).scalars().all()
            closed_positions = (
                await s.execute(
                    select(Position).where(
                        (Position.status == PositionStatus.CLOSED)
                        & (Position.closed_at >= since)
                    )
                )
            ).scalars().all()
            open_positions = (
                await s.execute(select(Position).where(Position.status == PositionStatus.OPEN))
            ).scalars().all()
            latest_snap = (
                await s.execute(
                    select(StateSnapshot).order_by(StateSnapshot.ts.desc()).limit(1)
                )
            ).scalar_one_or_none()

        total_funding = sum((p.payment for p in funding_rows), start=Decimal("0"))
        total_realized = sum((p.realized_pnl for p in closed_positions), start=Decimal("0"))

        lines = [
            f"{title} digest — {datetime.now(UTC).isoformat()}",
            f"Window: last {window_hours}h",
            "",
            f"Equity:           {latest_snap.equity_usdt if latest_snap else 'n/a'}",
            f"Realized PnL:     {total_realized:.4f} USDT",
            f"Funding received: {total_funding:.4f} USDT ({len(funding_rows)} events)",
            f"Positions closed: {len(closed_positions)}",
            f"Positions open:   {len(open_positions)}",
        ]
        if open_positions:
            lines.append("")
            lines.append("Open positions:")
            for p in open_positions:
                lines.append(
                    f"  {p.symbol}: spot={p.spot_qty} perp={p.perp_qty} "
                    f"opened={p.opened_at.isoformat()}"
                )
        return "\n".join(lines)
