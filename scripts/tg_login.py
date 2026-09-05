#!/usr/bin/env python3
"""One-time interactive Telegram login for the channel monitor (tgmon).

Creates the Telethon session file at TG_SESSION (default
/var/lib/tradebot/tgmon.session) so the tradebot-tgmon service can start
without prompting. Run it ONCE, as the `agent` user, from a terminal:

    sudo -u agent /opt/tradebot/venv/bin/python /opt/tradebot/scripts/tg_login.py

It will ask for the phone number of the Telegram account, then the login
code Telegram sends to that account, then the 2FA password if one is set.
Those are typed here and never printed back. Use a throwaway account, not
the one that owns the bot -- the session file is a full login.
"""
import os
import sys

sys.path.insert(0, "/opt/tradebot/bot")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _env  # noqa: E402
_env.load("/etc/tradebot/agent.env")

from tradebot import config  # noqa: E402


def main():
    if not (config.TG_API_ID and config.TG_API_HASH):
        print("TG_API_ID / TG_API_HASH are not set in /etc/tradebot/agent.env "
              "(they come from https://my.telegram.org -> API development tools).")
        return 1
    from telethon.sync import TelegramClient
    with TelegramClient(config.TG_SESSION, int(config.TG_API_ID), config.TG_API_HASH) as c:
        me = c.get_me()
        print(f"logged in as @{me.username or me.id}; session saved to {config.TG_SESSION}")
        missing = []
        for ch in config.TG_CHANNELS:
            try:
                c.get_entity(ch)
            except Exception as e:
                missing.append(f"{ch}: {str(e)[:60]}")
        if config.TG_CHANNELS:
            print(f"channels resolvable: {len(config.TG_CHANNELS) - len(missing)}"
                  f"/{len(config.TG_CHANNELS)}")
        for m in missing:
            print("  cannot resolve", m)
    return 0


if __name__ == "__main__":
    sys.exit(main())
