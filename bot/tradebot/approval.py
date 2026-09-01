"""approval-gate skill: per-asset whitelist + single-use codes + the command
parser. Exact match only, case-insensitive. Sells are never gated."""
import secrets
import time

from . import alerts, config, journal, state

HELP = ("Unrecognized. Commands: YES <code>, NO <code>, REVOKE <asset>, STOP, "
        "FLATTEN, RESUME, STATUS, REPORT, WHY <asset>")


def new_code():
    return secrets.token_hex(3).upper()  # 6 hex chars, unpredictable, single-use


def request_buy_approval(ticket, price, fields, fast=False):
    code = new_code()
    expiry = config.APPROVAL_EXPIRY_FAST_SEC if fast else config.APPROVAL_EXPIRY_SEC
    state.add_pending(code, "buy", ticket["asset_id"], ticket["ticket_id"], expiry)
    state.set_ticket_status(ticket["ticket_id"], "awaiting_approval")
    journal.log_approval(code=code, asset_id=ticket["asset_id"], kind="buy",
                         event="requested", raw_text=None, sender=None)
    alerts.approval_request(code, "BUY NOW", ticket["asset_id"], price, fields,
                            expiry // 60)
    return code


def request_resume_approval():
    code = new_code()
    state.add_pending(code, "resume", None, None, 6 * 3600)
    alerts.ops(f"Reply YES {code} to resume automatic buying.")
    return code


class Commands:
    """Wired by core: needs callbacks for execute/flatten/status/report/why."""

    def __init__(self, on_approved_buy, on_flatten, status_text, report_text, why_text):
        self.on_approved_buy = on_approved_buy
        self.on_flatten = on_flatten
        self.status_text = status_text
        self.report_text = report_text
        self.why_text = why_text
        self._flatten_code = None
        self._flatten_expiry = 0

    def handle(self, text):
        t = " ".join(text.upper().split())
        parts = t.split(" ")
        cmd = parts[0] if parts else ""
        arg = parts[1] if len(parts) > 1 else None

        if cmd == "YES" and arg:
            self._yes(arg, text)
        elif cmd == "NO" and arg:
            self._no(arg)
        elif cmd == "STOP" and not arg:
            state.set_mode("USER_STOP", reason="STOP command")
            alerts.ops("STOP acknowledged. Buying halted, open buy orders cancelled, "
                       "selling continues. Reply RESUME to re-enable.")
        elif cmd == "RESUME" and not arg:
            # RECON_FREEZE included: the fill sanity guard can land there, and a
            # mode with no way out is a mode that strands the bot.
            if state.get_mode() in ("USER_STOP", "EMERGENCY_HALT", "RECON_FREEZE"):
                request_resume_approval()
            else:
                alerts.ops(f"Nothing to resume (mode {state.get_mode()}).")
        elif cmd == "FLATTEN":
            self._flatten(arg)
        elif cmd == "REVOKE" and arg:
            state.whitelist_revoke(arg)
            alerts.ops(f"Revoked {arg}. It will require approval again.")
        elif cmd == "STATUS" and not arg:
            alerts.ops(self.status_text())
        elif cmd == "REPORT":
            alerts.ops(self.report_text(arg))
        elif cmd == "WHY" and arg:
            alerts.ops(self.why_text(arg))
        else:
            alerts.ops(HELP)

    # --- code flows ---------------------------------------------------------
    def _yes(self, code, raw):
        p = state.get_pending(code)
        if not p or p["status"] != "pending" or p["expires"] < time.time():
            journal.log_approval(code=code, kind="invalid_code", event="rejected",
                                 raw_text=raw, sender=None, asset_id=None)
            alerts.ops(f"Code {code} is not live.")
            return
        if p["kind"] == "buy" and state.get_mode() != "NORMAL":
            # Do not consume the code: RESUME, then this same YES still works.
            journal.log_approval(code=code, asset_id=p["asset_id"], kind="buy",
                                 event="blocked_halt", raw_text=raw, sender=None)
            alerts.ops(f"Not buying {p['asset_id']}: mode is {state.get_mode()}. "
                       f"RESUME first, then send YES {code} again.")
            return
        state.resolve_pending(code, "approved")
        journal.log_approval(code=code, asset_id=p["asset_id"], kind=p["kind"],
                             event="approved", raw_text=raw, sender=None)
        if p["kind"] == "buy":
            state.whitelist_add(p["asset_id"], "")  # approval whitelists the asset
            self.on_approved_buy(p)
        elif p["kind"] == "resume":
            state.set_mode("NORMAL", reason="user-approved resume")
            alerts.ops("Buying resumed.")
        elif p["kind"] == "flatten":
            alerts.ops("FLATTEN confirmed. Exiting everything.")
            self.on_flatten()
        elif p["kind"] == "phase":
            ph = state.phase() + 1
            state.set_kv("phase", str(ph))
            alerts.ops(f"Advanced to go-live phase {ph}.")

    def _no(self, code):
        p = state.get_pending(code)
        if p and p["status"] == "pending":
            state.resolve_pending(code, "rejected")
            if p["ticket_id"]:
                state.set_ticket_status(p["ticket_id"], "rejected")
            journal.log_approval(code=code, asset_id=p["asset_id"], kind=p["kind"],
                                 event="rejected", raw_text=None, sender=None)
            alerts.ops(f"Rejected {p['asset_id'] or p['kind']}.")
        else:
            alerts.ops(f"Code {code} is not live.")

    def _flatten(self, arg):
        if arg is None:
            self._flatten_code = new_code()
            self._flatten_expiry = time.time() + 300
            state.add_pending(self._flatten_code, "flatten", None, None, 300)
            alerts.ops(f"FLATTEN requested. This exits EVERY position. "
                       f"Confirm with: FLATTEN {self._flatten_code} (5 min)")
        else:
            p = state.get_pending(arg)
            if p and p["kind"] == "flatten" and p["status"] == "pending" and p["expires"] >= time.time():
                state.resolve_pending(arg, "approved")
                alerts.ops("FLATTEN confirmed. Exiting everything.")
                self.on_flatten()
            else:
                alerts.ops("FLATTEN code not live.")
