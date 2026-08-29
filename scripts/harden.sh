#!/bin/bash
# Vultr startup script — first-boot hardening for the trading bot VPS.
# Runs as root at first boot. Paste into Vultr: Orchestration > Startup Scripts (type: Boot).
# Verify afterwards: cat /var/log/tradebot-harden.log
set -euxo pipefail
exec > >(tee -a /var/log/tradebot-harden.log) 2>&1

BOT_USER=bot

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get -y upgrade
apt-get -y install ufw fail2ban unattended-upgrades chrony curl git jq sqlite3

# --- service user -----------------------------------------------------------
id -u "$BOT_USER" >/dev/null 2>&1 || adduser --disabled-password --gecos "" "$BOT_USER"
usermod -aG sudo "$BOT_USER"
install -d -m 0700 -o "$BOT_USER" -g "$BOT_USER" "/home/$BOT_USER/.ssh"
# Carry over any key added at deploy time so the bot user is reachable too.
if [ -f /root/.ssh/authorized_keys ]; then
  install -m 0600 -o "$BOT_USER" -g "$BOT_USER" \
    /root/.ssh/authorized_keys "/home/$BOT_USER/.ssh/authorized_keys"
fi

# --- secrets directory ------------------------------------------------------
install -d -m 0700 -o root -g root /etc/tradebot
touch /etc/tradebot/secrets.env
chmod 0600 /etc/tradebot/secrets.env

# --- ssh: keys only, no passwords, no root login ----------------------------
cat > /etc/ssh/sshd_config.d/99-tradebot.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
PubkeyAuthentication yes
X11Forwarding no
MaxAuthTries 3
EOF
sshd -t && systemctl restart ssh

# --- firewall: deny inbound except ssh --------------------------------------
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw --force enable

# --- fail2ban ---------------------------------------------------------------
cat > /etc/fail2ban/jail.local <<'EOF'
[sshd]
enabled = true
maxretry = 3
bantime = 1h
findtime = 10m
EOF
systemctl enable --now fail2ban

# --- automatic security updates --------------------------------------------
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
systemctl enable --now unattended-upgrades

# --- time -------------------------------------------------------------------
timedatectl set-timezone UTC
systemctl enable --now chrony

echo "HARDENING COMPLETE $(date -u +%FT%TZ)" | tee /etc/tradebot/.harden-ok
