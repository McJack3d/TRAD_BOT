"""Monthly tax-export CSV (France PFU + form 3916-bis).

One row per trade event (fill or funding payment), with both UTC and
Europe/Paris timestamps and all fields the spec §12.2 requires.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select

from src.config import Secrets
from src.logging_setup import configure_logging, log
from src.state import Database
from src.state.models import Fill, FundingPayment, Order

PARIS = ZoneInfo("Europe/Paris")


async def _export(db_path: str, year: int, month: int, out_path: str) -> None:
    db = Database(db_path)
    await db.init()
    start = datetime(year, month, 1, tzinfo=UTC)
    end_year, end_month = (year + 1, 1) if month == 12 else (year, month + 1)
    end = datetime(end_year, end_month, 1, tzinfo=UTC)

    rows: list[dict] = []
    async with db.session() as s:
        fills_q = await s.execute(
            select(Fill, Order).join(Order, Fill.order_id == Order.id).where(
                (Fill.ts >= start) & (Fill.ts < end)
            )
        )
        for fill, order in fills_q.all():
            rows.append(
                {
                    "ts_utc": fill.ts.isoformat(),
                    "ts_paris": fill.ts.astimezone(PARIS).isoformat(),
                    "symbol": order.symbol,
                    "side": order.side.value,
                    "leg": order.leg.value,
                    "quantity": str(fill.qty),
                    "price_usdt": str(fill.price),
                    "fee_usdt": str(fill.fee),
                    "fee_asset": fill.fee_asset,
                    "exchange_trade_id": fill.exchange_trade_id,
                    "client_order_id": order.client_order_id,
                    "category": "trade",
                }
            )

        fund_q = await s.execute(
            select(FundingPayment).where(
                (FundingPayment.funding_time >= start) & (FundingPayment.funding_time < end)
            )
        )
        for fp in fund_q.scalars().all():
            rows.append(
                {
                    "ts_utc": fp.funding_time.isoformat(),
                    "ts_paris": fp.funding_time.astimezone(PARIS).isoformat(),
                    "symbol": fp.symbol,
                    "side": "n/a",
                    "leg": "perp",
                    "quantity": str(fp.notional),
                    "price_usdt": str(fp.mark_price),
                    "fee_usdt": "0",
                    "fee_asset": "USDT",
                    "exchange_trade_id": "",
                    "client_order_id": "",
                    "category": "funding",
                    "funding_rate": str(fp.funding_rate),
                    "funding_payment_usdt": str(fp.payment),
                }
            )

    rows.sort(key=lambda r: r["ts_utc"])

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        log.warning("tax_export.no_rows", year=year, month=month)
        out.touch()
        await db.close()
        return

    fieldnames = sorted({k for r in rows for k in r})
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    log.info("tax_export.done", rows=len(rows), out=str(out))
    await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Export monthly tax CSV")
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--month", type=int, required=True)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    secrets = Secrets()
    configure_logging(secrets.bot_log_level)
    out = args.out or f"data/tax_export/{args.year:04d}-{args.month:02d}.csv"
    asyncio.run(_export(secrets.bot_db_path, args.year, args.month, out))


if __name__ == "__main__":
    main()
