"""bot-core daemon (runtime skill, deterministic layer). Owns execution, risk,
state, Telegram, monitoring, heartbeat. No model calls anywhere in here."""
import time

from . import (alerts, approval, config, execution, heartbeat, journal,
               marketdata, monitor, state, telegram)


def status_text():
    marks, _ = marketdata.marks([p["asset_id"] for p in state.positions()])
    value = state.total_value(marks)
    lines = [f"STATUS mode={state.get_mode()} phase={state.phase()}",
             f"Value: ${value:.2f}  Cash: " +
             " ".join(f"{v}=${u:.2f}" for v, u in state.cash().items())]
    for p in state.positions():
        m = marks.get(p["asset_id"])
        pnl = (m * p["qty"] - p["cost_basis_usd"]) if m else None
        lines.append(f"{p['asset_id']}: qty {p['qty']:.4g} cost ${p['cost_basis_usd']:.2f}"
                     + (f" pnl ${pnl:+.2f}" if pnl is not None else " (no mark)"))
    pend = journal.query("SELECT COUNT(*) c FROM pending_approvals WHERE status='pending'")
    live = journal.query("SELECT asset_id FROM whitelist WHERE revoked_ts IS NULL")
    lines.append(f"Approved ({len(live)}):")
    for w in live:
        ok, why = state.whitelist_state(w["asset_id"])
        lines.append(f"  {w['asset_id']}: {why}" + ("" if ok else " [inactive]"))
    lines.append(f"Pending approvals: {pend[0]['c']}")
    return "\n".join(lines)


def report_text(arg=None):
    day = time.time() - 86400
    f = journal.query("SELECT COUNT(*) c FROM forecasts WHERE ts>?", (day,))[0]["c"]
    fills = journal.query("SELECT COUNT(*) c, SUM(CASE WHEN side='sell' THEN 1 ELSE 0 END) s "
                          "FROM fills WHERE ts>?", (day,))[0]
    return (f"REPORT 24h: forecasts {f}, fills {fills['c'] or 0} "
            f"(exits {fills['s'] or 0}). Mode {state.get_mode()}, phase {state.phase()}.")


def why_text(asset):
    r = journal.query(
        "SELECT evidence_state FROM forecasts WHERE asset_id LIKE ? ORDER BY ts DESC LIMIT 1",
        (f"%{asset}%",))
    return r[0]["evidence_state"][:3500] if r else f"No recent analysis: {asset}"


def on_approved_buy(pending):
    """YES on a buy code -- called ON THE TELEGRAM LISTENER THREAD.

    It must return immediately. Executing here would block the listener for the
    length of a fill (a Base buy is ~285s worst case: 90s approve + 180s swap +
    settle reads), and for that whole window STOP and FLATTEN cannot reach the
    bot, while supervise_telegram -- seeing no successful poll -- would
    eventually halt trading because you approved a buy. So the tap only marks
    the ticket; the core loop does the work."""
    if not pending.get("ticket_id"):
        return
    state.set_ticket_status(pending["ticket_id"], "approved")
    journal.log_event("buy_approved_queued", detail=str(pending["ticket_id"]))


def run_approved_tickets(value, fresh):
    """Core-loop side of an approval. Gates 1-4 run here against the state at
    execution time, which may be minutes and one STOP later than the tap."""
    for t in state.tickets("approved"):
        if execution.execute_approved(t, value, fresh) == "blocked":
            alerts.ops(f"{t['asset_id']} approved but conditions changed since the "
                       "alert. Not bought. It stays approved and will auto-buy if it "
                       "requalifies.")


TG_HALT_SOURCE = "telegram_down"


def supervise_telegram(holder, handler):
    """The kill switch is only real while the poller is answering. A dead or
    silent poller means STOP/FLATTEN cannot reach us, so buying stops until it
    comes back; exits and monitoring are untouched."""
    poller = holder["p"]
    if not poller.is_alive():
        journal.log_event("telegram_poller_dead")
        poller.stop_flag = True
        poller = telegram.Poller(handler)
        poller.start()
        holder["p"] = poller
    if poller.healthy():
        if state.get_kv("halt_source") == TG_HALT_SOURCE:
            state.set_kv("halt_source", "")
            if state.get_mode() == "SELL_ONLY":
                state.set_mode("NORMAL", reason="telegram poller recovered")
                alerts.ops("Telegram poller recovered. Mode NORMAL.")
        return
    if state.get_mode() == "NORMAL":
        state.set_kv("halt_source", TG_HALT_SOURCE)
        state.set_mode("SELL_ONLY", reason="telegram poller unresponsive")
        journal.log_event("telegram_watchdog",
                          detail=f"stale {int(poller.stale_sec())}s")
        alerts.ops(f"Telegram poller unresponsive for {int(poller.stale_sec())}s. "
                   "Buying halted (SELL_ONLY); exits still active.")


def main():
    state.init()
    # kv is durable: a stale marker must not let the watchdog lift the
    # cold-start SELL_ONLY that state.init() sets pending reconciliation.
    state.set_kv("halt_source", "")
    journal.log_event("core_start")
    cmds = approval.Commands(on_approved_buy, execution.flatten_all,
                             status_text, report_text, why_text)
    alerts.bind_sender(telegram.send)
    holder = {"p": telegram.Poller(cmds.handle)}
    holder["p"].start()
    alerts.ops(f"bot-core started. Mode {state.get_mode()}, phase {state.phase()}.")

    last = {"hb": 0, "value": 0, "monitor": 0, "tg": 0}
    while True:
        now = time.time()
        due_hb = now - last["hb"] >= config.HEARTBEAT_SEC
        if due_hb:
            heartbeat.ping_start()
        try:
            if now - last["tg"] >= config.TELEGRAM_WATCHDOG_SEC:
                supervise_telegram(holder, cmds.handle)
                last["tg"] = now
            if now - last["monitor"] >= config.MONITOR_INTERVAL_TOKEN_SEC:
                monitor.check_positions()
                last["monitor"] = now
            if now - last["value"] >= config.VALUE_SAMPLE_SEC:
                value, fresh = monitor.portfolio_tick()
                last["value"] = now
                run_approved_tickets(value, fresh)
                # pick up new tickets from the agent layer
                for t in state.tickets("new"):
                    if t["action"] in ("BUY_NOW", "ADD"):
                        # An ADD targets a held (therefore already approved)
                        # asset, so gate 5 passes on the whitelist; the risk
                        # limits still see the combined position.
                        execution.process_ticket(t, value, fresh)
                    elif t["action"] == "SELL_NOW":
                        frac = execution.clamp_fraction(t.get("sell_fraction"))
                        execution.execute_sell(t["asset_id"], "agent SELL NOW", frac)
                        state.set_ticket_status(t["ticket_id"], "done")
        except Exception as e:
            journal.log_event("core_loop_error", detail=repr(e))
            if due_hb:
                heartbeat.fail(repr(e))   # a loop that raised is not healthy
                last["hb"] = now
        else:
            if due_hb:
                heartbeat.ping()          # green only after the work is done
                last["hb"] = now
        time.sleep(1)


if __name__ == "__main__":
    main()
