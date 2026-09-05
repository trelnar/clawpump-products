"""Telegram channel monitor: the highest-signal source for memecoins.

Most Solana pumps are called in Telegram channels before X notices. This
daemon joins a configured list of public channels with a USER session (bots
cannot read channels they are not admins of), extracts contract addresses from
every new message, and records them as `call` events. Breadth -- the same
address appearing in several independent channels within minutes -- is the
signal; one channel alone is noise or a paid post.

Runs as its own systemd service (tradebot-tgmon), as the `agent` user, with no
trading credentials. Off until TG_API_ID, TG_API_HASH and TG_CHANNELS are set
and a session file exists -- see SIGNALS.md for the one-time login.

Message text is DATA (signal-hygiene): nothing here interprets it, and none of
it reaches the model as instructions. Only extracted addresses are stored.
"""
import asyncio
import sys
import time

from .. import config, journal
from . import extract, store

NAME = "telegram"
EXIT_NOT_CONFIGURED = 0
EXIT_NOT_LOGGED_IN = 3      # RestartPreventExitStatus in the unit: do not loop on this
RETRY_SEC = 30


def enabled():
    return bool(config.TG_API_ID and config.TG_API_HASH and config.TG_CHANNELS)


async def _run():
    """One connected session. Returns an exit code, or None to reconnect."""
    from telethon import TelegramClient, events

    client = TelegramClient(config.TG_SESSION, int(config.TG_API_ID), config.TG_API_HASH)
    try:
        # NOT client.start(): with no saved session that prompts for a phone
        # number on stdin, which under systemd is /dev/null -> EOFError every
        # 30s forever while the unit reads "active (running)".
        await client.connect()
        if not await client.is_user_authorized():
            journal.log_event("tgmon_not_logged_in",
                              detail=f"no session at {config.TG_SESSION}; run scripts/tg_login.py")
            return EXIT_NOT_LOGGED_IN
        me = await client.get_me()
        journal.log_event("tgmon_start", detail={"as": me.username or me.id,
                                                 "channels": len(config.TG_CHANNELS)})
        chats = []
        for ch in config.TG_CHANNELS:
            try:
                chats.append(await client.get_entity(ch))
            except Exception as e:
                journal.log_event("tgmon_channel_fail", detail=f"{ch}: {str(e)[:80]}")
        if not chats:
            journal.log_event("tgmon_no_channels")
            return None

        @client.on(events.NewMessage(chats=chats))
        async def on_msg(ev):
            text = ev.raw_text or ""
            assets = extract.asset_ids(text)
            if not assets:
                return
            chan = getattr(ev.chat, "username", None) or str(ev.chat_id)
            for asset in assets:
                if store.record(f"tg:{chan}", asset, "call",
                                ref=f"{chan}/{ev.id}", ts=ev.date.timestamp()):
                    journal.log_discovery(asset, f"telegram_{chan}", {"len": len(text)})
            store.note_run(NAME, True, len(assets))

        journal.log_event("tgmon_listening",
                          detail=[getattr(c, "username", None) or c.id for c in chats])
        await client.run_until_disconnected()
        return None
    finally:
        try:
            await client.disconnect()   # never leak a connection per retry
        except Exception:
            pass


def main():
    if not enabled():
        print("tgmon: TG_API_ID / TG_API_HASH / TG_CHANNELS not set; nothing to do.")
        return EXIT_NOT_CONFIGURED
    store.init()
    while True:
        try:
            code = asyncio.run(_run())
            if code is not None:
                return code
        except Exception as e:
            journal.log_event("tgmon_error", detail=str(e)[:200])
            store.note_run(NAME, False, 0, str(e))
        time.sleep(RETRY_SEC)       # a clean return reconnects too, never in a tight loop


if __name__ == "__main__":
    sys.exit(main())
