#!/usr/bin/env bash
# Give the research layer its own identity and its own credentials.
#
# tradebot-agent ingests untrusted scraped content and talks to an external API
# -- the largest attack surface here -- and needs none of the trading
# credentials: it only writes tickets to SQLite, and every ticket still passes
# all five gates in the core. The design is "the agent decides, the core
# enforces"; this makes the credential boundary match that split.
#
# The first version of this script did NOT work. It put `agent` in group `bot`
# so it could reach the shared database, and left secrets.env as root:bot 640 --
# group-readable by the very user it was meant to exclude. The database and the
# secrets need DIFFERENT groups, which is what this version does.
#
# Run as root:  bash /opt/tradebot/scripts/split-credentials.sh
set -euo pipefail

SECRETS=/etc/tradebot/secrets.env
AGENT_ENV=/etc/tradebot/agent.env
DATA=/var/lib/tradebot

[[ -r "$SECRETS" ]] || { echo "missing $SECRETS"; exit 1; }

# 1. a group for the shared database ONLY -- never for the secrets
getent group tbdata >/dev/null || groupadd --system tbdata

# 2. the research user: its own primary group, plus tbdata for the database.
#    Deliberately NOT in group `bot`, which owns the trading credentials.
#    The first version created the user with -g bot and no `agent` group, so
#    on an upgraded box the group must be created before the user is moved
#    into it -- `chown root:agent` failed on exactly that and aborted here.
getent group agent >/dev/null || groupadd --system agent
if id -u agent >/dev/null 2>&1; then
  usermod -g agent -G tbdata agent
else
  useradd --system --no-create-home --shell /usr/sbin/nologin -g agent -G tbdata agent
fi
usermod -aG tbdata bot

# 3. agent.env: only what the research layer uses. Readable by agent, no one else.
umask 077
{
  echo "# Research layer only. Nothing here can move money."
  grep -E '^(ANTHROPIC_API_KEY|ANTHROPIC_WORKSPACE_ID|ANTHROPIC_MODEL|AGENT_|DISCOVERY_INTERVAL_SEC|TRADEBOT_DB|SOLANA_RPC|BASE_RPC|TRADEBOT_LOG_STDOUT)=' "$SECRETS" || true
} > "$AGENT_ENV"
chown root:agent "$AGENT_ENV"; chmod 640 "$AGENT_ENV"

# 4. trading credentials: group `bot` only. agent is not in it.
chown root:bot "$SECRETS"; chmod 640 "$SECRETS"
for f in /etc/tradebot/solana_wallet.json /etc/tradebot/evm_wallet.key; do
  [[ -e "$f" ]] && { chown bot:bot "$f"; chmod 600 "$f"; }
done
chmod 750 /etc/tradebot; chown root:bot /etc/tradebot

# 5. the shared database: both users reach it through tbdata
chown -R bot:tbdata "$DATA"
chmod 2770 "$DATA"                       # setgid: new files inherit tbdata
find "$DATA" -type f -exec chmod 660 {} +

echo "--- agent.env keys present (values never printed) ---"
sed -E 's/=.*/=<set>/' "$AGENT_ENV"

echo
echo "--- verification (prints no secret material) ---"
for f in "$SECRETS" /etc/tradebot/solana_wallet.json /etc/tradebot/evm_wallet.key; do
  [[ -e "$f" ]] || continue
  if sudo -u agent test -r "$f"; then echo "FAIL  agent CAN read $f"; else echo "ok    agent cannot read $f"; fi
done
sudo -u agent test -r "$AGENT_ENV" && echo "ok    agent can read its own env" || echo "FAIL  agent cannot read $AGENT_ENV"
sudo -u agent test -w "$DATA" && echo "ok    agent can write the database" || echo "FAIL  agent cannot write $DATA"

# 6. install the unit files. deploy.sh COPIES them into systemd, so a git pull
#    alone never changes what is running; the corrected agent unit (its own
#    group, tbdata supplementary) sat uninstalled while the old one kept
#    running as before.
cp /opt/tradebot/systemd/tradebot-core.service /opt/tradebot/systemd/tradebot-agent.service \
   /etc/systemd/system/
systemctl daemon-reload
echo "units     : installed and reloaded"
echo "agent runs: $(systemctl show -p User -p Group --value tradebot-agent | paste -sd/)  (want agent/agent)"

echo
echo "Now: systemctl restart tradebot-core tradebot-agent"
echo "Every line above must read 'ok'. Never cat the secrets file to check."
