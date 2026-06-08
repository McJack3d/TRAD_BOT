#!/usr/bin/env bash
# ---------------------------------------------------------------
# go_live.sh – Transition regime-bot from paper to LIVE
#
# Run this ON THE VPS after git pull:
#   chmod +x deploy/go_live.sh && ./deploy/go_live.sh
# ---------------------------------------------------------------
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

BOT_DIR="/home/ubuntu/TRAD_BOT"
ENV_FILE="${BOT_DIR}/.env"
CONFIG_FILE="${BOT_DIR}/config/regime_switch.yaml"
SERVICE_NAME="regime-bot"

echo -e "${YELLOW}═══════════════════════════════════════════════════${NC}"
echo -e "${YELLOW}  REGIME-BOT → LIVE TRANSITION SCRIPT${NC}"
echo -e "${YELLOW}═══════════════════════════════════════════════════${NC}"
echo ""

# 1. Pre-flight checks
echo -e "${YELLOW}[1/6] Pre-flight checks...${NC}"

if [ ! -f "$ENV_FILE" ]; then
    echo -e "${RED}ERROR: .env file not found at ${ENV_FILE}${NC}"
    exit 1
fi

if [ ! -f "$CONFIG_FILE" ]; then
    echo -e "${RED}ERROR: Config file not found at ${CONFIG_FILE}${NC}"
    exit 1
fi

# Check Binance keys are set
if ! grep -q "^BINANCE_API_KEY=.\+" "$ENV_FILE"; then
    echo -e "${RED}ERROR: BINANCE_API_KEY is empty in .env${NC}"
    echo -e "  Set your mainnet API key first: nano ${ENV_FILE}"
    exit 1
fi

if ! grep -q "^BINANCE_API_SECRET=.\+" "$ENV_FILE"; then
    echo -e "${RED}ERROR: BINANCE_API_SECRET is empty in .env${NC}"
    echo -e "  Set your mainnet API secret first: nano ${ENV_FILE}"
    exit 1
fi

echo -e "${GREEN}  ✓ .env exists with API credentials${NC}"

# 2. Verify config is set to live
echo -e "${YELLOW}[2/6] Verifying config/regime_switch.yaml...${NC}"
CURRENT_MODE=$(grep "^mode:" "$CONFIG_FILE" | awk '{print $2}')
echo -e "  Current mode: ${CURRENT_MODE}"
if [ "$CURRENT_MODE" != "live" ]; then
    echo -e "${YELLOW}  Config says '${CURRENT_MODE}', updating to 'live'...${NC}"
    sed -i 's/^mode:.*/mode: live/' "$CONFIG_FILE"
fi
echo -e "${GREEN}  ✓ Config mode = live${NC}"

# 3. Set BINANCE_TESTNET=false
echo -e "${YELLOW}[3/6] Ensuring BINANCE_TESTNET=false...${NC}"
if grep -q "^BINANCE_TESTNET=true" "$ENV_FILE"; then
    sed -i 's/^BINANCE_TESTNET=true/BINANCE_TESTNET=false/' "$ENV_FILE"
    echo -e "${GREEN}  ✓ Flipped BINANCE_TESTNET to false${NC}"
elif grep -q "^BINANCE_TESTNET=false" "$ENV_FILE"; then
    echo -e "${GREEN}  ✓ Already set to false${NC}"
else
    echo "BINANCE_TESTNET=false" >> "$ENV_FILE"
    echo -e "${GREEN}  ✓ Added BINANCE_TESTNET=false${NC}"
fi

# 4. Backup database
echo -e "${YELLOW}[4/6] Backing up database...${NC}"
if [ -f "${BOT_DIR}/data/bot.db" ]; then
    cp "${BOT_DIR}/data/bot.db" "${BOT_DIR}/data/bot.db.backup.$(date +%Y%m%d_%H%M%S)"
    echo -e "${GREEN}  ✓ Database backed up${NC}"
else
    echo -e "  (no database yet — first run will create it)"
fi

# 5. Restart systemd service
echo -e "${YELLOW}[5/6] Restarting ${SERVICE_NAME} service...${NC}"
sudo systemctl daemon-reload
sudo systemctl restart "$SERVICE_NAME"
sleep 2

if systemctl is-active --quiet "$SERVICE_NAME"; then
    echo -e "${GREEN}  ✓ ${SERVICE_NAME} is ACTIVE${NC}"
else
    echo -e "${RED}  ✗ ${SERVICE_NAME} failed to start!${NC}"
    echo "  Check logs: journalctl -u ${SERVICE_NAME} -n 50 --no-pager"
    exit 1
fi

# 6. Show status
echo -e "${YELLOW}[6/6] Service status:${NC}"
echo ""
systemctl status "$SERVICE_NAME" --no-pager -l | head -20
echo ""

echo -e "${GREEN}═══════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  ✅ REGIME-BOT IS NOW LIVE${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════${NC}"
echo ""
echo -e "  Monitor logs:  ${YELLOW}journalctl -fu regime-bot${NC}"
echo -e "  Check status:  ${YELLOW}python -m scripts.tradbot_regime status${NC}"
echo -e "  Emergency stop: ${YELLOW}touch data/KILL${NC}"
echo ""
echo -e "${RED}  ⚠️  REAL MONEY IS NOW AT RISK. Monitor closely.${NC}"
