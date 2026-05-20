"""Streamlit dashboard.

Run with:  streamlit run src/monitoring/dashboard.py

Exposed only over SSH tunnel from the VPS; do not bind publicly.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pandas as pd
import streamlit as st

from src.state import Database
from src.state.models import Position, StateSnapshot


@st.cache_resource
def _db() -> Database:
    return Database()


async def _load_positions() -> list[Position]:
    return await _db().open_positions()


async def _load_snapshots(limit: int = 1000) -> list[StateSnapshot]:
    from sqlalchemy import select

    async with _db().session() as s:
        res = await s.execute(
            select(StateSnapshot).order_by(StateSnapshot.ts.desc()).limit(limit)
        )
        return list(res.scalars().all())


def main() -> None:
    st.set_page_config(page_title="trad-bot", layout="wide")
    st.title("trad-bot — live dashboard")

    positions = asyncio.run(_load_positions())
    snapshots = list(reversed(asyncio.run(_load_snapshots())))

    col1, col2, col3 = st.columns(3)
    if snapshots:
        latest = snapshots[-1]
        col1.metric("Equity (USDT)", f"{Decimal(latest.equity_usdt):.2f}")
        col2.metric("Daily PnL", f"{Decimal(latest.realized_pnl_daily):.2f}")
        col3.metric("Cumulative PnL", f"{Decimal(latest.realized_pnl_cumulative):.2f}")

    st.subheader("Open positions")
    if positions:
        df = pd.DataFrame(
            [
                {
                    "symbol": p.symbol,
                    "spot_qty": float(p.spot_qty),
                    "perp_qty": float(p.perp_qty),
                    "spot_entry": float(p.spot_entry_price),
                    "perp_entry": float(p.perp_entry_price),
                    "funding_collected": float(p.funding_collected),
                    "opened_at": p.opened_at,
                }
                for p in positions
            ]
        )
        st.dataframe(df, use_container_width=True)
    else:
        st.info("No open positions.")

    st.subheader("Equity curve")
    if snapshots:
        df = pd.DataFrame(
            [{"ts": s.ts, "equity": float(s.equity_usdt)} for s in snapshots]
        )
        st.line_chart(df.set_index("ts")["equity"])


if __name__ == "__main__":
    main()
