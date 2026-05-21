"""Streamlit UI for the SimpleBot trend follower.

Run with:
    streamlit run src/app/streamlit_app.py

Modes:
- Paper (default): uses `PaperBinanceAdapter` — real Binance prices,
  fake balances. Safe to run as long as you want.
- Live: `SIMPLE_BOT_LIVE=true` + BINANCE_API_KEY/SECRET → real money.

Closing the terminal kills the bot. Closing the browser tab does not —
the Streamlit server keeps running.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
from decimal import Decimal
from pathlib import Path

# Streamlit runs scripts from their own directory, so `src` isn't on the
# path. Insert the project root before any `src.*` imports.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Load .env (BINANCE_API_KEY etc.) from the project root so the user
# doesn't have to remember to `source` it every time.
try:
    from dotenv import load_dotenv

    load_dotenv(_PROJECT_ROOT / ".env")
except ImportError:
    pass

import pandas as pd
import streamlit as st

from src.simple_bot import SimpleBot
from src.state.db import Database
from src.state.models import StateSnapshot

DB_PATH = os.environ.get("SIMPLE_BOT_DB", "data/simple_bot.db")
LIVE = os.environ.get("SIMPLE_BOT_LIVE", "false").lower() == "true"
STARTING_USDT = Decimal(os.environ.get("SIMPLE_BOT_STARTING_USDT", "1000"))
SMA_WINDOW = int(os.environ.get("SIMPLE_BOT_SMA_WINDOW", "200"))
ENTRY_BUFFER = float(os.environ.get("SIMPLE_BOT_ENTRY_BUFFER", "0.01"))
EXIT_BUFFER = float(os.environ.get("SIMPLE_BOT_EXIT_BUFFER", "0.01"))
TRAILING_STOP = float(os.environ.get("SIMPLE_BOT_TRAILING_STOP", "0"))
SYMBOL = os.environ.get("SIMPLE_BOT_SYMBOL", "BTC/USDT")


# ---------------------------------------------------------------------
# Persistent background event loop.
#
# Streamlit re-runs the script on every interaction. Calling asyncio.run
# inside the rerun creates a fresh event loop each time, while cached
# resources (ccxt's aiohttp client, aiosqlite connection) still hold
# references to the *original* loop. The second click then hangs forever
# because the cached client's loop is dead. Workaround: keep a single
# loop alive on a background thread for the lifetime of the process.
# ---------------------------------------------------------------------
@st.cache_resource
def _background_loop() -> asyncio.AbstractEventLoop:
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, name="bot-loop", daemon=True)
    thread.start()
    return loop


def _run(coro):
    """Submit a coroutine to the persistent loop and block until it finishes."""
    fut = asyncio.run_coroutine_threadsafe(coro, _background_loop())
    return fut.result()


@st.cache_resource
def _resources():
    db = Database(DB_PATH)
    _run(db.init(starting_equity=STARTING_USDT))
    if LIVE:
        from src.adapters.binance import BinanceAdapter

        api_key = os.environ.get("BINANCE_API_KEY", "")
        api_secret = os.environ.get("BINANCE_API_SECRET", "")
        testnet = os.environ.get("BINANCE_TESTNET", "false").lower() == "true"
        if not (api_key and api_secret):
            st.error("LIVE mode requires BINANCE_API_KEY and BINANCE_API_SECRET")
            st.stop()
        ex = BinanceAdapter(api_key=api_key, api_secret=api_secret, testnet=testnet)
        _run(ex.connect())
    else:
        from src.adapters.paper_binance import PaperBinanceAdapter

        ex = PaperBinanceAdapter(starting_usdt=STARTING_USDT)
        try:
            _run(ex.connect())
        except Exception as e:
            st.warning(
                f"Couldn't reach Binance public API ({e}). The app will "
                "still load, but prices and signals won't update until "
                "connectivity is restored."
            )
    bot = SimpleBot(
        exchange=ex,
        db=db,
        symbol=SYMBOL,
        sma_window=SMA_WINDOW,
        entry_buffer_pct=ENTRY_BUFFER,
        exit_buffer_pct=EXIT_BUFFER,
        trailing_stop_pct=TRAILING_STOP,
    )
    return bot, ex, db


async def _snapshot_equity(db: Database, bot: SimpleBot) -> Decimal:
    """Record current equity (USDT + BTC*price) and return the value."""
    status = await bot.status()
    equity = status.usdt_qty + status.btc_qty * status.last_price
    await db.add_snapshot(
        StateSnapshot(
            equity_usdt=equity,
            spot_balance_usdt=status.usdt_qty,
            perp_balance_usdt=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            realized_pnl_daily=Decimal("0"),
            realized_pnl_cumulative=Decimal("0"),
        )
    )
    return equity


async def _load_equity_history(db: Database) -> pd.DataFrame:
    from sqlalchemy import select

    async with db.session() as s:
        rows = (
            await s.execute(
                select(StateSnapshot).order_by(StateSnapshot.ts.asc())
            )
        ).scalars().all()
    return pd.DataFrame(
        [{"ts": r.ts, "equity": float(r.equity_usdt)} for r in rows]
    )


def _fetch_status(bot: SimpleBot):
    """Hit Binance for fresh status. Cached in session_state."""
    with st.spinner("Fetching price + balances from Binance..."):
        try:
            status = _run(bot.status())
        except Exception as e:
            st.warning(
                f"Couldn't reach Binance to refresh status ({e}). "
                "Showing last cached status if available."
            )
            return st.session_state.get("status")
    st.session_state["status"] = status
    st.session_state["status_age"] = pd.Timestamp.now(tz="UTC")
    return status
    return status


def main() -> None:
    st.set_page_config(page_title="BTC trend bot", layout="wide")
    st.title(f"BTC trend bot — SMA-{SMA_WINDOW} trend follower")

    if LIVE:
        st.error("🚨 LIVE MODE — real money on Binance mainnet 🚨")
    else:
        st.success(
            "Paper mode: real Binance prices, fake balances. Run as long "
            "as you want — no capital at risk."
        )

    bot, exchange, db = _resources()

    # Fetch status once on first load; subsequent reruns reuse the cached
    # value so the page is instant. The "Refresh price" button re-fetches.
    if "status" not in st.session_state:
        status = _fetch_status(bot)
    else:
        status = st.session_state["status"]
    if status is None:
        st.info(
            "Couldn't fetch status from Binance. The page will work once "
            "connectivity recovers — click *Refresh price* to retry."
        )
        return

    # Top row: status metrics.
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Trading", "ON" if status.enabled else "OFF")
    c2.metric("Position", status.current_state.value.upper())
    c3.metric(f"{status.base_asset} price", f"{status.last_price:,.2f}" if status.last_price else "—")
    equity = status.usdt_qty + status.btc_qty * status.last_price
    c4.metric(f"Equity ({status.quote_asset})", f"{equity:,.2f}")

    age = st.session_state.get("status_age")
    if age is not None:
        delta_s = (pd.Timestamp.now(tz="UTC") - age).total_seconds()
        st.caption(f"Status last refreshed {int(delta_s)}s ago")

    st.divider()

    # Controls.
    st.subheader("Controls")
    b1, b2, b3, b4, b5 = st.columns(5)
    if b1.button("▶ Start trading", type="primary", use_container_width=True):
        _run(bot.enable())
        _fetch_status(bot)
        st.rerun()
    if b2.button("⏸ Stop trading", use_container_width=True):
        _run(bot.disable())
        _fetch_status(bot)
        st.rerun()
    if b3.button("🔄 Evaluate now", use_container_width=True):
        with st.spinner("Fetching daily closes + evaluating signal..."):
            try:
                sig = _run(bot.tick())
            except Exception as e:
                st.error(
                    f"Couldn't reach Binance to evaluate the signal: {e}. "
                    "This is usually a transient outage or a geo-block — "
                    "try again in a minute, or check that your network can "
                    "reach api.binance.com."
                )
                sig = "ERR"
        if sig is None:
            st.warning("Bot is disabled — click Start trading first.")
        elif sig != "ERR":
            try:
                _run(_snapshot_equity(db, bot))
            except Exception as e:
                st.warning(f"Trade executed but couldn't snapshot equity: {e}")
            st.success(f"Signal: {sig.state.value.upper()} — {sig.reason}")
            _fetch_status(bot)
            st.rerun()
    if b4.button(f"💵 Flatten to {status.quote_asset}", use_container_width=True):
        with st.spinner(f"Selling {status.base_asset} to {status.quote_asset}..."):
            try:
                _run(bot.flatten_now())
                _run(_snapshot_equity(db, bot))
            except Exception as e:
                st.error(f"Flatten failed: {e}")
                st.stop()
        _fetch_status(bot)
        st.rerun()
    if b5.button("⟳ Refresh price", use_container_width=True):
        try:
            _fetch_status(bot)
        except Exception as e:
            st.error(f"Couldn't refresh price: {e}")
        st.rerun()

    # Last signal info.
    if status.last_signal is not None:
        st.caption(
            f"Last signal: **{status.last_signal.state.value.upper()}** — "
            f"close ${status.last_signal.close:,.2f} vs SMA "
            f"${status.last_signal.sma:,.2f}. {status.last_signal.reason}"
        )
    if status.last_evaluated is not None:
        st.caption(f"Last evaluated: {status.last_evaluated.isoformat()}")

    st.divider()

    # Holdings + equity curve.
    left, right = st.columns([1, 2])

    with left:
        st.subheader("Holdings")
        holdings = pd.DataFrame(
            [
                {"asset": status.base_asset, "qty": float(status.btc_qty), "value_quote": float(status.btc_qty * status.last_price)},
                {"asset": status.quote_asset, "qty": float(status.usdt_qty), "value_quote": float(status.usdt_qty)},
            ]
        )
        st.dataframe(holdings, use_container_width=True, hide_index=True)

    with right:
        st.subheader("Equity curve (paper)")
        history = _run(_load_equity_history(db))
        if history.empty:
            st.info("No equity snapshots yet — click *Evaluate now* a few times.")
        else:
            st.line_chart(history.set_index("ts")["equity"])

    st.divider()

    # Quick-look historical backtest (collapsible to keep page snappy).
    with st.expander("📊 Historical backtest (last 5 years) — does the strategy actually work?"):
        if st.button("Run 5-year backtest now"):
            with st.spinner("Fetching BTC daily history and running backtest..."):
                from scripts.backtest_trend import _fetch_btc_daily
                from src.backtest.trend_backtest import backtest_sma_trend, summarize

                closes = asyncio.run(_fetch_btc_daily(5))
                result = backtest_sma_trend(
                    closes,
                    initial_equity=Decimal("1000"),
                    sma_window=SMA_WINDOW,
                )
                stats = summarize(result)

                col_a, col_b, col_c = st.columns(3)
                col_a.metric(
                    "Strategy APR", f"{stats['strategy_apr']:.1%}",
                    f"vs B&H {stats['buy_and_hold_apr']:.1%}",
                )
                col_b.metric(
                    "Strategy max DD", f"{stats['strategy_max_dd']:.1%}",
                    f"vs B&H {stats['buy_and_hold_max_dd']:.1%}",
                )
                col_c.metric("Trades", str(stats["n_trades"]))

                eq = result.equity_curve.copy()
                eq["ts"] = pd.to_datetime(eq["ts"], utc=True)
                st.line_chart(eq.set_index("ts")[["strategy_equity", "buy_and_hold_equity"]])

    with st.expander("Strategy details"):
        st.markdown(
            f"""
            **Asset:** {SYMBOL}
            **Rule:** Close > {SMA_WINDOW}-day SMA → hold {status.base_asset}; else hold {status.quote_asset}.
            **Evaluation:** click *Evaluate now*, or come back tomorrow.
            **No leverage. No shorting. No funding settlements.**

            Closing the browser tab does NOT stop the bot — the Streamlit
            server in your terminal keeps running. To fully stop:
            Ctrl+C the terminal.

            Set `SIMPLE_BOT_LIVE=true` + Binance API keys to switch to
            real-money mode.
            """
        )


if __name__ == "__main__":
    main()
