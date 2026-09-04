#!/usr/bin/env bash
# Give the research layer its own identity and its own credentials.
#
# Before: tradebot-agent ran as `bot` with the full secrets.env -- the Coinbase
# trade key and read access to both wallet keyfiles -- despite needing none of
# them. It only writes tickets to SQLite, and every ticket still passes all five
# gates in the core. The design is "the agent decides, the core enforces"; this
# makes the credential boundary match that split.
#
# Run as root on the VPS:  bash /opt/tradebot/scripts/split-credentials.sh
set -euo pipefail

SECRETS=/etc/tradebot/secrets.env
AGENT_ENV=/etc/tradebot/agent.env

[[ -r "$SECRETS" ]] || { echo "missing $SECRETS"; exit 1; }

# 1. an unprivileged user for the research layer, in the bot group for DB access
id -u agent >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin -g bot agent

# 2. agent.env: only what the research layer actually uses
umask 077
{
  echo "# Research layer only. Nothing here can move money."
  grep -E '^(ANTHROPIC_API_KEY|ANTHROPIC_WORKSPACE_ID|ANTHROPIC_MODEL|AGENT_|DISCOVERY_INTERVAL_SEC|TRADEBOT_DB|SOLANA_RPC|BASE_RPC)=' "$SECRETS" || true
} > "$AGENT_ENV"
chown root:bot "$AGENT_ENV"
chmod 640 "$AGENT_ENV"

# 3. trading credentials readable only by the core's user
chown root:bot "$SECRETS"; chmod 640 "$SECRETS"
for f in /etc/tradebot/solana_wallet.json /etc/tradebot/evm_wallet.key; do
  [[ -e "$f" ]] && { chown bot:bot "$f"; chmod 600 "$f"; }
done

# 4. the shared state DB must stay writable by both
chgrp -R bot /var/lib/tradebot && chmod -R g+rw /var/lib/tradebot

echo "--- agent.env (keys redacted) ---"
sed -E 's/=(.{0,6}).*/=\1…/' "$AGENT_ENV"
echo
echo "Now: systemctl daemon-reload && systemctl restart tradebot-agent"
echo "Then confirm the split actually holds:"
echo "  sudo -u agent cat /etc/tradebot/secrets.env   # must be Permission denied"
echo "  systemctl status tradebot-agent               # must be active"
