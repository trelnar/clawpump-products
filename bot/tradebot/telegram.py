"""approval-gate transport: Telegram Bot API via long polling (no inbound
port). Only TELEGRAM_USER_ID may command. Exact-match parsing only; message
text is untrusted and never reaches a model as instructions."""
import threading
import time

import requests

from . import config, journal, state

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
    """Long-poll getUpdates; hand every authorized command string to handler.

    The kill switch lives on this thread, so it must not be killable: every
    update is dispatched inside its own guard, the offset survives a restart,
    and `last_ok` lets the core loop notice a thread that has gone quiet."""

    OFFSET_KEY = "telegram_offset"

    def __init__(self, handler):
        super().__init__(daemon=True)
        self.handler = handler
        self.offset = int(state.get_kv(self.OFFSET_KEY, 0) or 0)
        self.stop_flag = False
        self.last_ok = time.time()

    def _commit_offset(self, offset):
        """Persist before dispatch: a command that crashes the process must not
        be redelivered forever on restart."""
        self.offset = offset
        try:
            state.set_kv(self.OFFSET_KEY, offset)
        except Exception as e:
            journal.log_event("telegram_offset_persist_fail", detail=str(e))

    def run(self):
        while not self.stop_flag:
            try:
                updates = _call("getUpdates", offset=self.offset, timeout=30,
                                allowed_updates=["message", "callback_query"])
                self.last_ok = time.time()
            except Exception as e:
                journal.log_event("telegram_poll_fail", detail=str(e))
                time.sleep(5)
                continue
            for u in updates:
                try:
                    self._commit_offset(max(self.offset, u["update_id"] + 1))
                    self._dispatch(u)
                except Exception as e:  # one malformed update must not end polling
                    journal.log_event("telegram_dispatch_error", detail=repr(e))

    def stale_sec(self):
        return time.time() - self.last_ok

    def healthy(self):
        return self.is_alive() and self.stale_sec() < config.TELEGRAM_STALE_SEC

    def _dispatch(self, u):
        if "callback_query" in u:
            cq = u["callback_query"] or {}
            sender = (cq.get("from") or {}).get("id")
            text = (cq.get("data") or "").strip()
            if cq.get("id"):
                try:
                    _call("answerCallbackQuery", callback_query_id=cq["id"])
                except Exception:
                    pass
        elif "message" in u:
            msg = u["message"] or {}
            sender = (msg.get("from") or {}).get("id")
            text = (msg.get("text") or "").strip()
        else:
            return
        if sender is None:
            journal.log_event("telegram_update_no_sender", detail=str(u)[:200])
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
