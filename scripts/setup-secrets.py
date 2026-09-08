#!/usr/bin/env python3
"""Set research-layer secrets one value at a time. Run as root:

    python3 /opt/tradebot/scripts/setup-secrets.py

Asks for each value on its own line; Enter keeps what is there. Secret
values are typed hidden. Nothing is printed back. Also repairs a file whose
last line lacked a newline, which glued an appended KEY=... onto the previous
line where no grep or sed could see it.
"""
import getpass
import os
import re
import subprocess
import sys

SECRETS = "/etc/tradebot/secrets.env"
FIELDS = [
    ("ANTHROPIC_API_KEY", True, "Anthropic API key (console.anthropic.com -> API Keys)"),
    ("TG_API_ID", False, "Telegram api_id (my.telegram.org, digits only)"),
    ("TG_API_HASH", True, "Telegram api_hash (my.telegram.org)"),
    ("TG_CHANNELS", False, "Telegram channels, comma-separated, no @"),
]
KNOWN = re.compile(r"(?<!^)(?<![A-Z_])((?:ANTHROPIC|TG|REDDIT|NEYNAR|BIRDEYE|SIGNAL|BASE|SOLANA)_[A-Z_]+=)")


def load():
    lines = []
    with open(SECRETS) as f:
        raw = f.read()
    for line in raw.split("\n"):
        # split "…solana.comTG_API_ID=x" into two lines
        while True:
            m = KNOWN.search(line)
            if not m:
                break
            lines.append(line[:m.start()])
            line = line[m.start():]
        lines.append(line)
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def current(lines, key):
    for line in lines:
        if line.startswith(key + "="):
            return line[len(key) + 1:]
    return None


def put(lines, key, value):
    for i, line in enumerate(lines):
        if line.startswith(key + "="):
            lines[i] = f"{key}={value}"
            return
    lines.append(f"{key}={value}")


def looks_placeholder(v):
    return v is None or v == "" or v.endswith("_HERE")


def main():
    if os.geteuid() != 0:
        print("run as root")
        return 1
    lines = load()
    print("For each item: type the value and press Enter, or just press Enter to keep it.")
    print("Nothing you type is shown or stored anywhere but the secrets file.\n")
    for key, hidden, label in FIELDS:
        cur = current(lines, key)
        state = "NOT SET" if looks_placeholder(cur) else "set"
        prompt = f"{label} [{state}]: "
        v = (getpass.getpass(prompt) if hidden else input(prompt)).strip()
        if v:
            if key == "TG_API_ID" and not v.isdigit():
                print("  api_id is digits only; skipped")
                continue
            if key == "TG_CHANNELS":
                v = ",".join(x.strip().lstrip("@") for x in v.split(",") if x.strip())
            put(lines, key, v)
            print("  saved")
        elif looks_placeholder(cur):
            print("  still not set")
    with open(SECRETS, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(SECRETS, 0o640)
    print("\nwriting agent.env and restarting...")
    r = subprocess.run(["bash", "/opt/tradebot/scripts/split-credentials.sh"])
    if r.returncode != 0:
        return r.returncode
    subprocess.run(["systemctl", "restart", "tradebot-core", "tradebot-agent"])
    missing = [k for k, _, _ in FIELDS if looks_placeholder(current(lines, k))]
    if missing:
        print("still missing:", ", ".join(missing))
    else:
        print("all four set. Next: sudo -u agent /opt/tradebot/venv/bin/python "
              "/opt/tradebot/scripts/tg_login.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
