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
    lines.append(f"Whitelist: {len(journal.query('SELECT 1 FROM whitelist WHERE revoked_ts IS NULL'))}"
                 f"  Pending approvals: {pend[0]['c']}")
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
    """YES on a buy code: re-validate price/age, then execute or re-queue."""
    t = journal.query("SELECT * FROM tickets WHERE ticket_id=?", (pending["ticket_id"],))
    if not t:
        return
    ticket = t[0]
    price = marketdata.price(ticket["asset_id"])
    lo, hi = ticket.get("buy_zone_lo"), ticket.get("buy_zone_hi")
    stale = time.time() - ticket["ts"] > config.TICKET_MAX_AGE_SEC
    if price is None or stale or (lo and hi and not (lo <= price <= hi)):
        state.set_ticket_status(ticket["ticket_id"], "revalidate")
        alerts.ops(f"Not executed - price left buy zone. {ticket['asset_id']} whitelisted; "
                   "will auto-buy if it requalifies.")
        return
    execution.execute_buy(ticket, price)


def main():
    state.init()
    journal.log_event("core_start")
    cmds = approval.Commands(on_approved_buy, execution.flatten_all,
                             status_text, report_text, why_text)
    alerts.bind_sender(telegram.send)
    poller = telegram.Poller(cmds.handle)
    poller.start()
    alerts.ops(f"bot-core started. Mode {state.get_mode()}, phase {state.phase()}.")

    last = {"hb": 0, "value": 0, "monitor": 0}
    while True:
        now = time.time()
        try:
            if now - last["hb"] >= config.HEARTBEAT_SEC:
                heartbeat.ping()
                last["hb"] = now
            if now - last["monitor"] >= config.MONITOR_INTERVAL_TOKEN_SEC:
                monitor.check_positions()
                last["monitor"] = now
            if now - last["value"] >= config.VALUE_SAMPLE_SEC:
                value, fresh = monitor.portfolio_tick()
                last["value"] = now
                # pick up new tickets from the agent layer
                for t in state.tickets("new"):
                    if t["action"] == "BUY_NOW":
                        execution.process_ticket(t, value, fresh)
                    elif t["action"] == "SELL_NOW":
                        execution.execute_sell(t["asset_id"], "agent SELL NOW")
                        state.set_ticket_status(t["ticket_id"], "done")
        except Exception as e:
            journal.log_event("core_loop_error", detail=repr(e))
        time.sleep(1)


if __name__ == "__main__":
    main()
