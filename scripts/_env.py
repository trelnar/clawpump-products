"""Load /etc/tradebot/secrets.env into os.environ for standalone scripts.

The daemons get their environment from systemd's EnvironmentFile; a script run
by hand does not, and every one of them failed the first time it was run for
exactly that reason. Importing this before tradebot.config fixes it once.
"""
import os

DEFAULT = "/etc/tradebot/secrets.env"


def load(path=DEFAULT):
    """Fill in anything not already set. Never overrides a real environment."""
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except OSError as e:
        raise SystemExit(f"cannot read {path}: {e}\n"
                         f"Run this as a user that can (root, or bot).")
    n = 0
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and not os.environ.get(k):
            os.environ[k] = v
            n += 1
    return n
