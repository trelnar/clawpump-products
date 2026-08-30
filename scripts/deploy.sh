#!/bin/bash
# Deploy/update the trading bot on the VPS. Run as root:
#   curl -fsSL https://raw.githubusercontent.com/trelnar/clawpump-products/claude/trading-bot-skills-sfqmfo/scripts/deploy.sh | bash
# Idempotent: safe to re-run for updates. Halts buying during deploy (vps-ops).
set -euo pipefail

REPO=https://github.com/trelnar/clawpump-products
BRANCH=claude/trading-bot-skills-sfqmfo
DIR=/opt/tradebot

echo "== fetching code =="
if [ -d $DIR/.git ]; then
  git -C $DIR fetch origin $BRANCH && git -C $DIR checkout -B $BRANCH origin/$BRANCH
else
  git clone --branch $BRANCH $REPO $DIR
fi

echo "== python env =="
apt-get -y install python3-venv python3-dev build-essential >/dev/null
[ -d $DIR/venv ] || python3 -m venv $DIR/venv
$DIR/venv/bin/pip install -q --upgrade pip
$DIR/venv/bin/pip install -q -r $DIR/bot/requirements.txt

echo "== dirs & secrets =="
install -d -m 0750 -o bot -g bot /var/lib/tradebot
install -d -m 0750 -o root -g bot /etc/tradebot
touch /etc/tradebot/secrets.env
chown root:bot /etc/tradebot/secrets.env
chmod 0640 /etc/tradebot/secrets.env
grep -q TELEGRAM_TOKEN /etc/tradebot/secrets.env || cat >> /etc/tradebot/secrets.env <<'TPL'
# Fill every value, then: systemctl restart tradebot-core tradebot-agent
TELEGRAM_TOKEN=
TELEGRAM_USER_ID=6674587758
HEALTHCHECK_URL=
COINBASE_API_KEY=
COINBASE_API_SECRET=
ANTHROPIC_API_KEY=
# Optional overrides:
# ANTHROPIC_MODEL=claude-opus-5
# SOLANA_RPC=
# BASE_RPC=
TPL

echo "== wallets =="
$DIR/venv/bin/python $DIR/scripts/gen_wallets.py
chown root:bot /etc/tradebot/solana_wallet.json /etc/tradebot/evm_wallet.key 2>/dev/null || true
chmod 0640 /etc/tradebot/solana_wallet.json /etc/tradebot/evm_wallet.key 2>/dev/null || true

echo "== services =="
cp $DIR/systemd/tradebot-core.service $DIR/systemd/tradebot-agent.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable tradebot-core tradebot-agent >/dev/null 2>&1 || true

echo
echo "Deployed. Next:"
echo "  1. nano /etc/tradebot/secrets.env   (fill every value)"
echo "  2. systemctl start tradebot-core    (agent starts after Phase 0)"
echo "  3. sudo -u bot bash -c 'set -a; . /etc/tradebot/secrets.env; set +a; $DIR/venv/bin/python $DIR/scripts/phase0.py'"
