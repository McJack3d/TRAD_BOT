# Running the bots on Amazon Lightsail

A beginner-friendly runbook. Follow it top to bottom while logged into
the AWS Lightsail console and, later, SSH'd into the instance.

## Honest status (read first)

| Bot | Deployable today? | How |
|---|---|---|
| **BTC trend bot** (SMA) | ✅ yes, live-capable | daily cron (Part E) |
| **IBKR sentiment bot** | ⚠️ paper only | always-on process; needs IB Gateway for real trading |
| **Funding-arb daemon** (`src/main.py`) | ❌ no | imports the missing `src/data` package — do **not** enable `bot.service` yet |
| **Regime-switching perp bot** | ✅ yes, live-capable | systemd service `regime-bot` (Part F) |

The box is worth setting up now: it's where the regime bot lands once it
passes its backtest gates, and the trend bot can run on it today.

---

## Part A — Create the instance (Lightsail console)

1. Lightsail → **Create instance**.
2. **Region:** choose **Tokyo (ap-northeast-1)**. Binance's servers live
   in AWS Tokyo; any other region adds 100–300 ms to every API call.
3. **Platform:** Linux/Unix. **Blueprint:** **OS Only → Ubuntu 22.04 LTS**.
4. **SSH key:** create a new key pair (or use the default) and
   **download the `.pem`** — you need it to log in. Keep it safe.
5. **Plan:** **$5/mo (1 GB RAM, 2 vCPU)** is enough for the Binance bots.
   Pick $10/mo only if you'll also run the IBKR sentiment bot here.
6. Name it `tradbot` and **Create instance**.

## Part B — Static IP + firewall

1. Lightsail → **Networking** → **Create static IP** → attach it to the
   `tradbot` instance. (Free while attached.) **Write the IP down** — you
   whitelist it on Binance later.
2. On the instance's **Networking** tab, the default firewall allows SSH
   (22) only. Leave it that way — the bot needs **outbound** access, not
   inbound. Don't open 80/443 unless you later expose a dashboard.

## Part C — First SSH + system prep

On your Mac:

```bash
chmod 400 ~/Downloads/LightsailDefaultKey-ap-northeast-1.pem   # your key file
ssh -i ~/Downloads/LightsailDefaultKey-ap-northeast-1.pem ubuntu@YOUR_STATIC_IP
```

(You can also use the browser-based SSH button in the console.)

Then on the box:

```bash
sudo apt-get update && sudo apt-get upgrade -y
sudo apt-get install -y python3.11 python3.11-venv python3.11-dev \
    build-essential git chrony
sudo systemctl enable --now chrony   # keeps the clock accurate (matters for API auth)
```

## Part D — Clone, install, validate

```bash
cd ~
git clone https://github.com/McJack3d/TRAD_BOT.git
cd TRAD_BOT
git checkout main          # or the branch you want to run
python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[dev]"

# Sanity check — the bot code imports and the strategy logic passes:
python -m pytest tests/unit/test_sma_trend.py tests/unit/test_simple_bot.py -q
```

Create the env file (paper mode needs no keys):

```bash
cp .env.example .env
nano .env        # leave Binance keys blank for paper; fill them only for live
chmod 600 .env   # never world-readable
```

Confirm it runs in paper mode (real Binance prices, fake money):

```bash
python -m scripts.tradbot status
```

## Part E — Run the trend bot on a daily cron (paper now, live later)

The trend bot evaluates once a day, so it doesn't need a 24/7 process —
a cron is the right tool. (`tradbot install-cron` is macOS-only; on Linux
use cron directly.)

Enable trading once, then schedule the daily evaluation:

```bash
cd ~/TRAD_BOT && source .venv/bin/activate
python -m scripts.tradbot start          # enable the bot
crontab -e
```

Add this line (runs every day at 00:05 UTC):

```
5 0 * * * cd /home/ubuntu/TRAD_BOT && /home/ubuntu/TRAD_BOT/.venv/bin/python -m scripts.tradbot evaluate >> /home/ubuntu/TRAD_BOT/data/cron.log 2>&1
```

Check on it anytime:

```bash
python -m scripts.tradbot status        # position, equity, drawdown
python -m scripts.tradbot trades        # recent orders
tail -f ~/TRAD_BOT/data/cron.log        # what the overnight runs did
```

### Going live with the trend bot (only after you're satisfied with paper)

In `.env` set real **no-withdrawal, IP-whitelisted** Binance keys and:

```
SIMPLE_BOT_LIVE=true
BINANCE_API_KEY=...
BINANCE_API_SECRET=...
```

On Binance: API Management → edit key → **Restrict access to trusted IPs**
→ paste your Lightsail static IP. Disable withdrawals on the key.

## Part F — When the regime-switching bot is ready (the real goal)

After it passes the backtest acceptance gates in
`docs/REGIME_SWITCH_STRATEGY.md`, it'll ship with its own systemd service.
Deploying will be:

```bash
cd ~/TRAD_BOT && git pull
source .venv/bin/activate && pip install -e ".[dev]"
sudo cp deploy/systemd/regime-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now regime-bot
journalctl -fu regime-bot               # live logs
```

Until then, leave the existing `deploy/systemd/bot.service` **disabled** —
it targets the funding-arb daemon, which can't start without the missing
`src/data` package.

## Security checklist

- [ ] SSH key only (Lightsail disables password login by default — keep it that way).
- [ ] Firewall: inbound SSH (22) only.
- [ ] `chrony` running (accurate clock = valid API signatures).
- [ ] `.env` is `chmod 600`, Binance keys are **no-withdrawal** and **IP-whitelisted** to the static IP.
- [ ] Start in **paper** mode; flip to live only after you trust it.
- [ ] Keep the repo on a branch you control; `git pull` to update.

## Monitoring & upkeep

```bash
# Resource usage (make sure you're not near the RAM cap)
htop                       # sudo apt-get install -y htop

# Disk (SQLite DBs + logs live under ~/TRAD_BOT/data)
df -h && du -sh ~/TRAD_BOT/data

# Back up the paper/live database off-box occasionally
scp -i KEY.pem ubuntu@IP:~/TRAD_BOT/data/simple_bot.db ./backup/
```

Keep the OS patched: `sudo apt-get update && sudo apt-get upgrade -y`
every couple of weeks (or enable `unattended-upgrades`).
