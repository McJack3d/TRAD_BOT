# Handoff and Challenging Report — 2026-06-25T23:09:50+02:00

## 1. Observation

Direct code observations from the targeted files reveal several logical flaws and vulnerabilities:

### A. Chronological Sorting Mix-up in Consecutive Losses (src/risk/perp_guards.py)
In `src/risk/perp_guards.py` (lines 153-170), the sorting key prioritizes symbol-local `bar_index` over absolute timestamps:
```python
    def sort_key(t: Any) -> tuple[int, Any]:
        bar_idx = _get_field(
            t,
            [
                "exit_bar_index",
                "closed_bar_index",
                "exit_index",
                "closed_at_index",
                "exit_bar",
                "bar_index",
            ],
        )
        if bar_idx is not None:
            return (0, int(bar_idx))
        ts = _get_field(t, ["exit_ts", "closed_at", "exit_time", "timestamp", "ts"])
        if ts is not None:
            return (1, ts)
        return (2, 0)
```
When sorting trades across multiple assets (e.g. BTC/USDT and ETH/USDT) to find the most recent `max_consecutive_losses` trades for the account-level circuit breaker, this sorts them by their symbol-local bar index value (since `bar_idx` is not None).

### B. Cool-off Bypass via Stale Metadata (src/strategy/regime_live.py)
In `src/strategy/regime_live.py` (lines 456-465), when reconstructing trades to check for asset cool-off:
```python
                    exit_idx = None
                    exit_bar_str = meta.get(f"{symbol}_last_loss_exit_bar", None)
                    if exit_bar_str:
                        exit_idx = int(exit_bar_str)
                    elif p.closed_at is not None:
                        diffs = np.abs((df.index - pd.Timestamp(p.closed_at)).total_seconds())
                        if len(diffs) > 0:
                            exit_idx = int(np.argmin(diffs))
```
Every closed trade for the symbol is assigned `exit_idx = int(exit_bar_str)` if the key exists in metadata, overriding their actual close bar indices.

### C. Metadata Destruction on HALT Status (src/strategy/regime_live.py & src/state/db.py)
Metadata is serialized into `halt_reason` in `SystemStatus` (L726-735). However, when the bot halts, it calls `set_status(SystemStatusEnum.HALTED, reason=reason)` which completely overwrites the `halt_reason` column:
```python
    async def set_status(
        self, status: SystemStatusEnum, reason: str | None = None
    ) -> None:
        async with self._session() as s:
            await s.execute(
                update(SystemStatus)
                .where(SystemStatus.id == 1)
                .values(
                    status=status,
                    halt_reason=reason,
                    last_update=datetime.now(UTC),
                )
            )
            await s.commit()
```
This overwrites all strategy state metadata (e.g., `stop_price`, `entry_leg`). In `src/strategy/regime_switch.py` (L200-213), a position with `entry_leg == None` defaults to `RANGE` exit logic, causing immediate market exits during `TREND` or `NEUTRAL` regimes when re-enabled or tick evaluation resumes.

---

## 2. Logic Chain

1. **Bug 1: Consecutive Losses Sorting Bug**
   - *Observation A* shows that trades with a `bar_idx` are sorted with priority `(0, bar_idx)` while those with only timestamps are `(1, ts)`.
   - Different symbols (e.g., BTC/USDT at 10,000 bars and ETH/USDT at 500 bars) have independent historical lengths and bar indices.
   - Comparing `bar_idx` across different symbols sorts them by local index instead of UTC timestamps.
   - For example, if BTCUSDT closed a loss at bar 9990 (at 22:00 UTC) and ETHUSDT closed a profit at bar 495 (at 23:00 UTC), sorting by `bar_idx` incorrectly sorts BTCUSDT *after* ETHUSDT because `9990 > 495`.
   - The consecutive losses logic sees the sorted array ending in `[..., Profit, Loss, Loss, Loss]` instead of `[..., Loss, Profit, Loss, Loss]`, incorrectly triggering a system halt.

2. **Bug 2: Cool-off Bypass Bug**
   - *Observation B* shows that a stale `last_loss_exit_bar` is applied to subsequent closed trades of the same symbol, even if they were normal non-stop-loss exits.
   - For a subsequent trade closed at a loss at bar 110 (with `last_loss_exit_bar = 100` remaining in metadata), the trade is recorded as exited at bar 100.
   - Checking cool-off at bar 112 yields `elapsed = 112 - 100 = 12` (greater than `cooloff_bars = 6`).
   - The cool-off passes immediately even though only 2 bars have elapsed since the actual loss, bypassing the risk rules.

3. **Bug 3: Metadata Destruction Bug**
   - *Observation C* shows that calling `db.set_status` with a text halt reason overwrites `SystemStatus.halt_reason`, erasing all key-value serialized metadata.
   - An open position losing its metadata has its `entry_leg` loaded as `None` on subsequent ticks or bot restarts.
   - Under `src/strategy/regime_switch.py` state machine, `pos.entry_leg == None` falls back to `RANGE` exit logic. If the current regime is `Regime.TREND` or `Regime.NEUTRAL`, it hits `if regime != Regime.RANGE:` and returns `Action.EXIT`, causing an unwanted market exit.

---

## 3. Caveats

- We did not connect to a live exchange endpoint or test actual network packet drop/latency profiles.
- Tests were performed via static analysis and standalone sandbox environment replication.

---

## 4. Conclusion

- **Overall Risk Assessment**: **CRITICAL**
  - Bug 1 (Chronological Sorting) can prevent critical account-level circuit breakers from halting trading during severe loss streaks, or cause false halts.
  - Bug 2 (Cool-off Bypass) allows trading to resume immediately after a loss, potentially compounding losses.
  - Bug 3 (Metadata Destruction) causes desynchronization and premature market exits of valid positions.
- **Actionable Mitigations**:
  - Sort trades by absolute UTC timestamp (`exit_ts` or `closed_at`) rather than `bar_idx` in `check_consecutive_losses` and `check_asset_cooloff`.
  - Store position metadata in a dedicated table/column in the DB instead of overloading `halt_reason` in `SystemStatus`.
  - Ensure non-stop-loss exits clean up or do not propagate old stop-out indices.

---

## 5. Verification Method

To independently verify these bugs, run the replication script `sandbox_test.py` located in the root of the workspace:
```bash
python3 sandbox_test.py
```

### Invalidation Conditions:
- The script output should show both tests as `[SUCCESS] Bug reproduced`.
- If the output shows `[FAILURE]`, the logic has been successfully patched.
