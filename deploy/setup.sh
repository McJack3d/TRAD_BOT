#!/usr/bin/env bash
# Provision a fresh Ubuntu 24.04 host to run trad-bot.
# Run as root or via sudo on a Hetzner CAX11 / DO Singapore droplet.

set -euo pipefail

BOT_USER=botuser
BOT_DIR=/opt/trad-bot
KILL_DIR=/var/lib/bot

apt-get update
apt-get install -y \
    python3.11 python3.11-venv python3.11-dev \
    build-essential \
    git \
    chrony \
    ufw \
    fail2ban \
    unattended-upgrades

systemctl enable --now chrony
systemctl enable --now fail2ban

# Firewall: deny inbound except SSH, allow outbound.
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp comment "SSH"
ufw --force enable

# Auto security updates.
dpkg-reconfigure -plow unattended-upgrades || true

# Bot user.
if ! id "$BOT_USER" &>/dev/null; then
    useradd --system --create-home --shell /usr/sbin/nologin "$BOT_USER"
fi

mkdir -p "$BOT_DIR" "$KILL_DIR"
chown -R "$BOT_USER:$BOT_USER" "$BOT_DIR" "$KILL_DIR"

# Place the systemd unit if installer copied it.
if [[ -f "$BOT_DIR/deploy/systemd/bot.service" ]]; then
    install -m 0644 "$BOT_DIR/deploy/systemd/bot.service" /etc/systemd/system/trad-bot.service
    systemctl daemon-reload
fi

echo "Setup complete. Next steps:"
echo "  1. Copy code into $BOT_DIR (as $BOT_USER)."
echo "  2. sudo -u $BOT_USER python3.11 -m venv $BOT_DIR/.venv"
echo "  3. sudo -u $BOT_USER $BOT_DIR/.venv/bin/pip install -e $BOT_DIR"
echo "  4. Copy .env into $BOT_DIR (chmod 0600, owned by $BOT_USER)."
echo "  5. systemctl enable --now trad-bot"
echo "  6. journalctl -fu trad-bot"
