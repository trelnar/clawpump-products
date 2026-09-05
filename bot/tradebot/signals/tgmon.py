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


def enabled():
    return bool(config.TG_API_ID and config.TG_API_HASH and config.TG_CHANNELS)


async def _run():
    from telethon import TelegramClient, events

    client = TelegramClient(config.TG_SESSION, int(config.TG_API_ID), config.TG_API_HASH)
    await client.start()          # uses the saved session; never prompts under systemd
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
        return

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


def main():
    if not enabled():
        print("tgmon: TG_API_ID / TG_API_HASH / TG_CHANNELS not set; nothing to do.")
        return 0
    store.init()
    while True:
        try:
            asyncio.run(_run())
        except Exception as e:
            journal.log_event("tgmon_error", detail=str(e)[:200])
            store.note_run(NAME, False, 0, str(e))
            time.sleep(30)


if __name__ == "__main__":
    sys.exit(main())
