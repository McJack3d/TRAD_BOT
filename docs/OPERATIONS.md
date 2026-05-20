# Operations runbook

This is the step-by-step operator's guide for `trad-bot`. It assumes the
v1 spec (`docs/trading_bot_spec_v1.md`) has been read and that the §15
acceptance criteria for full-live deployment are understood.

---

## 1. Local development

### 1.1 First-time setup

```bash
git clone <repo> trad-bot && cd trad-bot
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # leave keys blank for now
```

### 1.2 Run tests

```bash
pytest                              # all unit tests (~5s, 87 cases)
pytest tests/unit/test_risk_manager.py -v   # the 41 adversarial cases
ruff check src tests scripts        # lint
```

### 1.3 Backtest

```bash
# 1. Download historical funding rates and OHLCV (slow first time).
python -m scripts.download_history --config config/backtest.yaml --start 2020-01-01

# 2. Run a single-shot backtest with current parameters.
python -m scripts.run_backtest --config config/backtest.yaml

# 3. Run walk-forward (rolling 6mo train / 1mo test).
python -m scripts.run_backtest --config config/backtest.yaml --walk-forward
```

### 1.4 Paper mode (FakeExchange, no real orders)

```bash
python -m src.main --config config/paper.yaml --kill-file /tmp/KILL
# Triggers signal evaluation, simulated fills, and DB writes. No keys needed.
```

---

## 2. Production deployment (Singapore VPS)

### 2.1 Provision the host

Recommended: Hetzner CAX11 (ARM, 2 vCPU, 4GB RAM, Singapore — ~€5/mo) or
DigitalOcean Singapore droplet. Ubuntu 24.04 LTS.

```bash
# As root on the fresh VPS:
git clone <repo> /opt/trad-bot
cd /opt/trad-bot
bash deploy/setup.sh
```

This installs Python 3.11, chrony (clock sync), UFW (firewall, deny
inbound except SSH), fail2ban, unattended-upgrades, creates the
`botuser` system account, and copies the systemd unit.

### 2.2 Install the package

```bash
sudo -u botuser python3.11 -m venv /opt/trad-bot/.venv
sudo -u botuser /opt/trad-bot/.venv/bin/pip install -e /opt/trad-bot
sudo install -m 0600 -o botuser -g botuser .env /opt/trad-bot/.env
```

### 2.3 Pre-flight checks

```bash
# Clock drift < 100ms?
sudo -u botuser /opt/trad-bot/.venv/bin/chrony tracking | grep "System time"

# Config loads?
sudo -u botuser /opt/trad-bot/.venv/bin/python -c \
  "from src.config import BotConfig; BotConfig.from_yaml('/opt/trad-bot/config/live.yaml')"

# Reach Binance?
curl -sS https://api.binance.com/api/v3/ping
```

### 2.4 First run — DRY-RUN for 5–7 days (spec Phase 5)

```bash
# Temporarily point the systemd unit at paper.yaml or pass --config.
sudo systemctl enable --now trad-bot
sudo journalctl -fu trad-bot
```

What "clean dry-run" means (§7 gate):
- Zero unhandled exceptions in journald.
- `reconciler.ok` heartbeat in logs every 60s for the full window.
- At least one WebSocket reconnect survived cleanly.
- Telegram `/status` responds; `/kill` flattens and halts.

### 2.5 Promote to micro-live (€100–150)

1. Set `BINANCE_TESTNET=false` in `.env`.
2. Set the live API key (no-withdrawal, IP-whitelisted to the VPS).
3. Deposit €100–150 worth of USDT split across spot + perp accounts.
4. Edit `config/live.yaml`: `starting_equity_eur: "150"` (or similar).
5. `sudo systemctl restart trad-bot`.
6. Watch closely for 14 days. Read `/status` daily; verify the daily
   email digest arrives.

### 2.6 Promote to full live (€1,000)

Only when Phase 5 acceptance (§15) holds. Update `.env` deposit,
restart the unit, observe.

---

## 3. Day-to-day operations

### 3.1 Status check (any time)

Via Telegram: `/status`, `/positions`, `/funding`.

Via SSH:
```bash
sudo journalctl -fu trad-bot
sudo systemctl status trad-bot
sqlite3 /opt/trad-bot/data/bot.db "SELECT status, halt_reason, last_reconciliation_ok FROM system_status;"
```

### 3.2 Soft pause (no new orders, keep existing)

Telegram: `/halt`

### 3.3 Full kill (flatten + halt)

Three independent paths — use whichever you can reach fastest:

1. **Telegram**: `/kill`
2. **VPS file**: `sudo touch /var/lib/bot/KILL` (detected within 5s)
3. **Exchange**: revoke the API key in Binance dashboard

### 3.4 Resume after a halt

1. Read the halt reason: `/status`.
2. Investigate in journald: `sudo journalctl -u trad-bot --since "1 hour ago"`.
3. Verify positions on Binance match `sqlite3 ... 'SELECT * FROM positions WHERE status="open"'`.
4. If clean: Telegram `/resume CONFIRM`.

### 3.5 Monthly tax export

```bash
sudo -u botuser /opt/trad-bot/.venv/bin/python \
    -m scripts.tax_export --year 2026 --month 5
# CSV lands at data/tax_export/2026-05.csv
```

---

## 4. Common incidents

### 4.1 Reconciliation drift (HALTED)

The bot's view of positions disagrees with what Binance reports.

1. **Don't resume yet.** Investigate first.
2. Compare:
   ```bash
   sqlite3 data/bot.db "SELECT symbol, spot_qty, perp_qty FROM positions WHERE status='open';"
   ```
   vs Binance dashboard.
3. Likely cause: a partial fill landed after the bot crashed. Either
   manually flatten on Binance to match the DB, or manually update the
   DB row to match Binance. Then `/resume CONFIRM`.

### 4.2 Liquidation distance halt

A short leg got too close to liquidation.

1. The bot has already attempted margin top-up and failed (or topped up but didn't help).
2. Check the price; if a spike has reverted, the position may now be safe.
3. Manually flatten or top up margin on Binance, then `/resume CONFIRM`.

### 4.3 Daily / cumulative loss stop hit

1. This is the bot doing its job. Don't blindly `/resume`.
2. Re-read backtest expectations; if reality is materially worse than
   backtest, the edge may have compressed — re-run walk-forward before
   resuming.

### 4.4 Clock drift > 100ms

```bash
sudo systemctl restart chrony
chronyc tracking
```

If drift persists, raise priority on chrony or switch NTP source.

### 4.5 WS dropouts

Expected occasionally. REST polling keeps the snapshot warm. If WS
fails to reconnect for > 60s, the alert fires — usually transient. If
sustained, check `journalctl` for the exception.

---

## 5. Upgrades

```bash
sudo systemctl stop trad-bot
sudo -u botuser git -C /opt/trad-bot pull
sudo -u botuser /opt/trad-bot/.venv/bin/pip install -e /opt/trad-bot
sudo systemctl start trad-bot
```

If schema changes, run any Alembic migrations (v1 has no migrations —
SQLAlchemy `create_all` handles the initial schema; production changes
will need migrations added).

---

## 6. Backups

The SQLite DB is the only thing that can't be regenerated. Daily backup:

```bash
sudo -u botuser sqlite3 /opt/trad-bot/data/bot.db \
    ".backup /opt/trad-bot/data/backups/$(date -u +%F).db"
```

Add to root crontab as a nightly job. Push to off-site (rsync, B2,
S3) — without the DB the tax export and PnL history are gone.
