# Handoff Report: Codebase Exploration for Binance Perp Bot

## 1. Observation

### Perp Execution Capability in Adapters
*   **File Path**: `src/adapters/exchange_base.py`
    *   Line 128: `async def set_leverage(self, symbol: str, leverage: int) -> None`
    *   Lines 130-140:
        ```python
        @abstractmethod
        async def submit_order(
            self,
            symbol: str,
            leg: Leg,
            side: Side,
            qty: Decimal,
            client_order_id: str,
            price: Decimal | None = None,
            reduce_only: bool = False,
        ) -> ExchangeOrder: ...
        ```
    *   Line 110: `async def fetch_positions(self) -> list[ExchangePosition]: ...`
    *   There is no explicit method defined for closing a perpetual position directly on the `ExchangeAdapter` interface.
*   **File Path**: `src/adapters/binance.py`
    *   Lines 111-136: Implements `fetch_positions` by querying CCXT's `self.perp.fetch_positions()` and mapping positions to `ExchangePosition` instances (only when `contracts > 0`).
    *   Lines 169-176: Implements `set_leverage` on the CCXT futures client:
        ```python
        async def set_leverage(self, symbol: str, leverage: int) -> None:
            await self.perp.set_leverage(leverage, symbol)
            # Force isolated margin mode for short legs.
            try:
                await self.perp.set_margin_mode("ISOLATED", symbol)
            except Exception:
                # ccxt raises if margin mode is already set; ignore.
                pass
        ```
    *   Lines 178-201: Implements `submit_order`. When `leg == "perp"` and `reduce_only` is true, it passes `params["reduceOnly"] = True` to the CCXT futures order submission:
        ```python
        if reduce_only and leg == "perp":
            params["reduceOnly"] = True
        ```
*   **File Path**: `src/adapters/fake.py`
    *   Lines 227-278: Implements `_apply_perp_fill` which handles simulated fills for perps, calculates estimated liquidation prices, manages margins, and tracks positions in memory under `self._positions`.
    *   Line 261-266: In memory close-position simulation happens when position quantity goes to zero:
        ```python
        new_qty = pos.qty + signed
        if new_qty == 0:
            # Closed; release margin + realize PnL.
            pnl = (pos.entry_price - price) * pos.qty  # short qty negative
            self._credit("perp", margin_asset, pos.margin + pnl)
            del self._positions[symbol]
            return
        ```

### Database Structure
*   **File Path**: `src/state/models.py`
    *   Lines 67-89: The `Position` model is structured around a delta-neutral spot-long + perp-short pair.
        ```python
        class Position(Base):
            """A delta-neutral pair position (spot long + perp short)."""
            __tablename__ = "positions"
            id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
            symbol: Mapped[str] = mapped_column(String(32), index=True)
            status: Mapped[PositionStatus] = mapped_column(SAEnum(PositionStatus), default=PositionStatus.OPEN)
            spot_qty: Mapped[Decimal] = mapped_column(DECIMAL(28, 12), default=Decimal("0"))
            perp_qty: Mapped[Decimal] = mapped_column(DECIMAL(28, 12), default=Decimal("0"))
            spot_entry_price: Mapped[Decimal] = mapped_column(DECIMAL(28, 12), default=Decimal("0"))
            perp_entry_price: Mapped[Decimal] = mapped_column(DECIMAL(28, 12), default=Decimal("0"))
            initial_margin: Mapped[Decimal] = mapped_column(DECIMAL(28, 12), default=Decimal("0"))
            opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
            closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
            realized_pnl: Mapped[Decimal] = mapped_column(DECIMAL(28, 12), default=Decimal("0"))
            funding_collected: Mapped[Decimal] = mapped_column(DECIMAL(28, 12), default=Decimal("0"))
        ```
    *   Lines 90-114: The `Order` model tracks order metadata. It includes fields `leg: Mapped[Leg]` (either `SPOT` or `PERP`), `side`, `qty`, `price`, `status` (OrderStatus enum), `filled_qty`, and `avg_fill_price`.
    *   Lines 116-128: The `Fill` model records discrete trades matching to an `Order`.
    *   Lines 131-147: `FundingPayment` logs individual historical funding rate items.
    *   Lines 164-179: `SystemStatus` tracks global system state (`active`, `paused`, `halted`), the `halt_reason`, starting equity, and last reconciliation.
*   **File Path**: `src/state/db.py`
    *   Implements an asynchronous wrapper using SQLite and SQLAlchemy (`sqlite+aiosqlite`). Contains DAO methods such as `open_positions()`, `create_position()`, `close_position()`, `add_order()`, `update_order_status()`, and `add_fill()`.

### Daemons & Bots Structure
*   **File Path**: `src/simple_bot.py`
    *   Defines the `SimpleBot` class.
    *   Lines 126-260: The `tick` method implements the evaluation loop:
        1. Checks if enabled via `await self.is_enabled()`.
        2. Retrieves current state, peak price, and cooldown flags from DB (stored serialized inside `SystemStatus.halt_reason` via `self._get_meta()` / `self._set_meta()`).
        3. Runs trailing stop check if IN (long spot). If triggered, calls `go_out()`, updates metadata, and sets cooldown.
        4. Runs signal evaluation via `sig = await self.evaluate()`.
        5. If signal state differs from current state, places spot orders using `go_in()` or `go_out()`.
        6. Updates metadata and returns the signal.
    *   Lines 320-341: Meta persistence routines:
        ```python
        async def _get_meta(self) -> dict[str, str]:
            async with self.db.session() as s:
                row = (await s.execute(select(SystemStatus).where(SystemStatus.id == 1))).scalar_one_or_none()
                raw = row.halt_reason if row else None
            ...
        async def _set_meta(self, **kwargs: str) -> None:
            ...
            encoded = "|".join(f"{k}:{v}" for k, v in meta.items())
            ...
        ```
    *   SimpleBot is run as a one-shot evaluation script. The CLI runner in `scripts/tradbot.py` invokes `await bot.tick()` when called with `evaluate`.
*   **File Path**: `src/main.py`
    *   The live daemon runner. Wires all components (including `FundingArbStrategy` and `ExecutionEngine`).
    *   Lines 166-180: Runs a persistent, asynchronous 60-second `tick_loop()`:
        ```python
        async def tick_loop() -> None:
            while not stop_event.is_set():
                try:
                    await strategy.evaluate_all()
                    snap = await build_state_snapshot(
                        db, exchange, cfg.starting_equity_usdt
                    )
                    await db.add_snapshot(snap)
                ...
                await asyncio.sleep(60.0)
        ```

### Risk Check Mechanisms
*   **File Path**: `src/risk/checks.py`
    *   Defines pure pre-trade checks using a `PreTradeContext` snapshot.
    *   Includes check functions: `check_inputs_sane`, `check_system_active`, `check_reconciliation`, `check_per_symbol_exposure`, `check_total_exposure`, `check_liq_distance`, `check_order_rate`, `check_daily_loss`, `check_cumulative_loss`, `check_clock_drift`.
*   **File Path**: `src/risk/manager.py`
    *   Defines the `RiskManager` class which runs a continuous loop checking positions every 10 seconds.
    *   Lines 117-143: Monitoring loop for liquidation distance:
        ```python
        # Liquidation-distance + margin top-up checks for each short perp.
        for p in positions:
            if p.leg != "perp" or p.qty >= 0:
                continue
            liq_dist = _liq_distance_pct(p)
            if liq_dist is None:
                continue
            if liq_dist < self.cfg.liquidation_halt_pct:
                await self._halt(f"liq distance {liq_dist:.4f} < {self.cfg.liquidation_halt_pct}")
                return
            if liq_dist < self.cfg.margin_top_up_pct:
                # Add margin top up...
        ```
    *   Note that it filters out non-short positions: `if p.leg != "perp" or p.qty >= 0: continue`.

### Verification commands
*   We attempted to run unit tests using `pytest -m "not integration"` and `python3 -m pytest -m "not integration"`. The latter command timed out due to the shell authorization prompt.

---

## 2. Logic Chain

1.  **Perp Execution Capabilities**:
    *   `ExchangeAdapter` possesses generic interfaces for `set_leverage` (isolation margin logic is handled in `BinanceAdapter` using CCXT's client), `fetch_positions`, and `submit_order` (which accepts a `reduce_only` flag).
    *   There is no custom `close_position` function on the adapter, which means the execution module must construct order payloads manually by submitting opposing orders (e.g. `submit_order` with `reduce_only=True`).
2.  **Database Structure**:
    *   The `Position` model has fields explicitly tracking spot and perp quantities and prices separately. If a pure perp strategy (long or short) is implemented, the spot fields (`spot_qty`, `spot_entry_price`) can simply be kept at `Decimal("0")` since they default to zero.
    *   Alternatively, we can subclass or add a new table structure specifically for directional perp positions, or just reuse the existing model.
3.  **Bot & Daemon Structure**:
    *   `SimpleBot` relies on a one-shot CLI execution pattern rather than a daemonized daemon, storing strategy meta (cooldowns, peaks, states) in `SystemStatus.halt_reason`.
    *   In contrast, the main daemon runs as a continuous loop via `src/main.py` waking up every 60 seconds. A similar daemonized structure should be used for the Regime-Switching Perp Bot if it is expected to execute live in real time.
4.  **Risk Mechanism**:
    *   The current `RiskManager` only tracks liquidation distance for short perps (`qty < 0`), as shown by the check `if p.qty >= 0: continue`.
    *   For a regime-switching bot that can be long or short perp, this must be extended because long positions also face liquidation risk. The math inside `_liq_distance_pct` holds for long positions, but the loop constraint must be removed or modified in the new `src/risk/perp_guards.py`.

---

## 3. Caveats

*   We could not run the test suite to completion due to system permission timeouts during shell execution.
*   We did not modify any files (under read-only exploration rules).

---

## 4. Conclusion

*   **Execution Capability**: We already have all the low-level functions needed to set leverage, submit orders (including reduce-only), and fetch positions. We do not have a dedicated `close_position` method in the exchange adapters; closing positions is achieved by calling `submit_order` with opposing sides and `reduce_only=True`.
*   **Database Structure**: The models `Position`, `Order`, `Fill`, `FundingPayment`, `StateSnapshot`, and `SystemStatus` are fully functional and can be reused. Directional perp positions can be represented in `Position` with `spot_qty=0`.
*   **Daemon/Bot Structure**: The SimpleBot operates as a one-shot command line script (`tradbot evaluate`), whereas the live execution bot will need to run as a persistent event-loop daemon (like `src/main.py`).
*   **Risk Mechanism**: The existing pre-trade checks are highly modular. However, `RiskManager`'s continuous monitoring must be extended in `src/risk/perp_guards.py` to calculate and check liquidation distances for long perp positions (`qty > 0`), which are currently ignored.

---

## 5. Verification Method

To independently verify the test suite state:
1.  Run the following command in the workspace root:
    ```bash
    pytest -m "not integration"
    ```
2.  Inspect `tests/unit/test_simple_bot.py` and `tests/unit/test_risk_manager.py` to confirm that tests mock the exchange adapters successfully.
