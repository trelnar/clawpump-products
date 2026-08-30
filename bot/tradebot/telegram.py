"""approval-gate transport: Telegram Bot API via long polling (no inbound
port). Only TELEGRAM_USER_ID may command. Exact-match parsing only; message
text is untrusted and never reaches a model as instructions."""
import threading
import time

import requests

from . import config, journal

API = "https://api.telegram.org/bot{token}/{method}"


def _call(method, **params):
    r = requests.post(API.format(token=config.TELEGRAM_TOKEN, method=method),
                      json=params, timeout=35)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"telegram {method}: {data}")
    return data["result"]


def send(text, buttons=None):
    """buttons: [[(label, callback_data), ...], ...]"""
    try:
        kw = {"chat_id": config.TELEGRAM_USER_ID, "text": text[:4096]}
        if buttons:
            kw["reply_markup"] = {"inline_keyboard": [
                [{"text": lb, "callback_data": cb} for lb, cb in row] for row in buttons]}
        _call("sendMessage", **kw)
        return True
    except Exception as e:
        journal.log_event("telegram_send_fail", detail=str(e))
        return False


class Poller(threading.Thread):
    """Long-poll getUpdates; hand every authorized command string to handler."""

    def __init__(self, handler):
        super().__init__(daemon=True)
        self.handler = handler
        self.offset = 0
        self.stop_flag = False

    def run(self):
        while not self.stop_flag:
            try:
                updates = _call("getUpdates", offset=self.offset, timeout=30,
                                allowed_updates=["message", "callback_query"])
            except Exception as e:
                journal.log_event("telegram_poll_fail", detail=str(e))
                time.sleep(5)
                continue
            for u in updates:
                self.offset = max(self.offset, u["update_id"] + 1)
                self._dispatch(u)

    def _dispatch(self, u):
        if "callback_query" in u:
            cq = u["callback_query"]
            sender = cq["from"]["id"]
            text = (cq.get("data") or "").strip()
            try:
                _call("answerCallbackQuery", callback_query_id=cq["id"])
            except Exception:
                pass
        elif "message" in u:
            sender = u["message"]["from"]["id"]
            text = (u["message"].get("text") or "").strip()
        else:
            return
        journal.log_approval(code=None, asset_id=None, kind="inbound",
                             event="received", raw_text=text[:500], sender=str(sender))
        if sender != config.TELEGRAM_USER_ID:
            journal.log_event("unregistered_sender", detail=str(sender))
            return  # ignore silently; never reply with data
        try:
            self.handler(text)
        except Exception as e:
            journal.log_event("command_handler_error", detail=f"{text[:80]}: {e}")
