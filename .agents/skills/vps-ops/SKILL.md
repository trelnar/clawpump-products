---
name: vps-ops
description: Use when deploying, upgrading, securing, supervising, or recovering the VPS the bot runs on, when handling host-level incidents (crashes, missed heartbeats, stale feeds, disk or clock faults), or when running backups and rollbacks.
---

# VPS Operations

This skill defines the host. The bot trades real money from this machine, so host compromise means trading-account compromise. Withdrawal-disabled keys cap the damage; harden the host anyway.

Two rules apply everywhere:

- Any host-level fault degrades toward **sell-only**, never toward unattended buying. Sells and exit management continue through every fault mode that allows them.
- The bot must never modify this file, the systemd units, the firewall, or the secrets. Only the user changes host configuration.

## Base setup

Run the current Ubuntu LTS release. Configure once, before the first live trade:

| Item | Requirement |
|---|---|
| Service user | Non-root user (`tradebot`), no sudo, no login shell needed. The bot process, state databases (**portfolio-state**, **trade-journal**), and logs live under this user |
| SSH | Key authentication only: `PasswordAuthentication no`, `PermitRootLogin no` |
| Firewall | Default deny inbound. Allow only the SSH port and HTTPS (443) for the inbound-SMS webhook (**approval-gate**). Restrict 443 to the SMS provider's published IP ranges when available. Outbound open for venue APIs, price feeds, monitor pings, and backups |
| fail2ban | Enabled on SSH |
| Security updates | Unattended upgrades for the security pocket, with automatic reboot disabled. Kernel reboots are deploys — run them through the deploy procedure below |
| Time | NTP sync via chrony or systemd-timesyncd. Clock accuracy is load-bearing: API request signing, approval-code expiry (**approval-gate**), and the rolling 24-hour window (**risk-limits**) all depend on it |

## Secrets

- Store API keys and the SMS provider credentials in a root-readable-only env file (`/etc/tradebot/secrets.env`, owner `root:root`, mode `0600`, loaded via the systemd unit's `EnvironmentFile=`) or in a secrets manager. Never in the repo, never in **trade-journal**, never in logs, never in SMS.
- Every exchange key must be scoped to the dedicated sub-account or wallet, must have withdrawal permission disabled, and must be IP-allowlisted to the VPS static address. Keys must never touch main balances.
- Hot-wallet keys exist only if the user approves on-chain DEX automation, which is proposed, not yet approved (see **execution**). If approved, store the wallet key like any other secret — but a hot-wallet key is inherently withdrawal-capable, so the no-withdrawal-permission rule above applies to exchange keys only. The wallet is bounded by holding only its trading allocation, capped by the per-venue exposure limit in **risk-limits**.
- On startup, query each venue for the key's permission set. If withdrawal is enabled, or the scope exceeds the sub-account, do not trade on that venue; run it alert-only and send an ops alert per **alert-format**.
- Rotate any key immediately on suspicion of exposure, and on the schedule below. Rotation is a deploy: follow the deploy procedure.

| Parameter | Default (editable — edit the table; it's plain Markdown) |
|---|---|
| Routine key rotation | Every 90 days |
| Startup permission check | Every process start, per venue |

## Process supervision

Run the bot under systemd:

| Unit | Purpose | Policy |
|---|---|---|
| `tradebot.service` | Main bot process | `User=tradebot`, `Restart=always`, `RestartSec=10`, `WatchdogSec=60` (the bot must notify the watchdog each main-loop cycle so a hung process is killed and restarted, not just a dead one) |
| `tradebot-webhook.service` | Inbound-SMS webhook listener | Same restart policy. Can be part of the main process; a separate unit isolates webhook crashes |
| `tradebot-backup.timer` | Daily journal backup | See Backups |

Rules:

- On every start — clean, crash, or watchdog kill — the bot enters through **portfolio-state** cold-start recovery in `SELL_ONLY` mode. No flag, environment variable, or code path can skip it. Buying resumes only when cold-start reconciliation passes clean, and never clears `EMERGENCY_HALT`, `USER_STOP`, or `RECON_FREEZE`.
- If the service exceeds the crash-loop threshold (default in the dead-man's switch table below), let it stay down. The dead-man's switch covers notification. Do not fight a crash loop with more restarts.

## Dead-man's switch

The bot pings an external monitor (healthchecks.io or equivalent). The monitor — not the VPS — alerts when pings stop, so notification survives total host failure.

- Ping only at the end of a completed, healthy main-loop cycle. A ping must prove the loop works, not that a timer fires.
- Configure the monitor to send the user an SMS on a missed ping: `BOT OFFLINE — positions unmanaged`. This path must not depend on the VPS, the bot process, or credentials stored on the VPS.
- Test the switch by stopping the service and confirming the SMS arrives.

| Parameter | Default (editable) |
|---|---|
| Ping interval | 60 seconds |
| Monitor grace period | 3 minutes |
| Crash-loop threshold (service stays down) | 5 restarts in 10 minutes |
| Dead-man's switch test | Monthly |

## Stale-data watchdog

Per-asset mark staleness and haircuts live in **portfolio-state**. This watchdog covers feed-level failure:

| Condition | Default threshold (editable) | Action |
|---|---|---|
| One venue's market-data feed not refreshed | 3 minutes | Pause automatic buying on that venue. Sells continue and re-quote per **execution** |
| No feed refreshing from any source | 5 minutes | Pause all automatic buying. Ops alert |
| Clock drift vs NTP | 1 second | Pause automatic buying, force resync. Ops alert if drift persists 5 minutes |
| Disk usage | 85% warn / 95% critical | Warn: ops alert. Critical: pause automatic buying — state writes are at risk |

Buying paused by this watchdog resumes automatically when the condition clears; it is not a halt under **risk-limits** and needs no approval. Log every pause and resume to **trade-journal**.

## Logs

- Rotate application logs and journald daily; compress; keep them for the log-retention default in the Backups table. **trade-journal** is the durable record — logs are diagnostics, not history.
- Never write secrets, API keys, or approval codes to logs.

## Backups

Back up the **trade-journal** and **portfolio-state** SQLite databases off-VPS daily:

1. Snapshot with SQLite's online backup (`VACUUM INTO` or `.backup`). Never copy a live WAL database file directly.
2. Encrypt with a public key (age or GPG). The private key must not exist on the VPS.
3. Upload to off-VPS object storage. Verify the upload; on failure retry, and send an ops alert after 2 consecutive failed days.

| Parameter | Default (editable) |
|---|---|
| Backup schedule | Daily |
| Daily backup retention | 90 days |
| Monthly backup retention | Indefinite (**trade-journal** retention is indefinite) |
| Log retention | 14 days |
| Restore test | Quarterly: restore to a scratch location, run `PRAGMA integrity_check`, compare row counts |

A restored backup is a copy of history, not a new history. After any restore, **portfolio-state** must reconcile against venues before trading resumes.

## Deploys and upgrades

Deploy from tagged commits only. Never hand-edit code on the VPS. The hard-limit definitions in **risk-limits** are user-edited only; a deploy must never alter them programmatically.

Procedure for every deploy, including rollbacks and key rotations:

1. Set a deploy hold: pause automatic buying. Sells continue. Wait for in-flight orders to reach a terminal state or adopt them per the **execution** idempotency rules.
2. Install the new release beside the old one (versioned release directory; `current` symlink points at the live release).
3. Repoint the symlink and restart the service. The bot re-enters through **portfolio-state** cold-start recovery in `SELL_ONLY` mode.
4. Clear the deploy hold only after cold-start reconciliation passes clean.

Rollback must be one command: repoint `current` to the previous release and restart. Keep past releases on disk per the releases-kept default in the table below. A rollback goes through the same cold-start recovery.

### Shadow mode for decision logic

Any change to decision logic — discovery, scoring, sizing, entry or exit rules, gate ordering — must run in shadow mode per **backtest-replay** before it controls real orders:

- The shadow version receives live data and logs its would-be orders to **trade-journal**. It places nothing.
- Promote only when the **backtest-replay** Stage 2 comparison criteria pass. Stage 2 owns all shadow-mode durations and sample minimums — this skill sets no shadow numbers of its own.

| Parameter | Default (editable) |
|---|---|
| Releases kept on disk | 5 |
| Changes exempt from shadow | Infra, logging, and dependency updates that touch no decision logic. The deploy hold still applies |

## Incident runbook

Automatic actions fire without asking. Every incident logs to **trade-journal**. Ops SMS messages use **alert-format** with an `OPS:` prefix. Edit the table; it's plain Markdown.

| Symptom | Automatic action | SMS to user |
|---|---|---|
| Venue API errors sustained > 5 min | Pause buying on that venue; keep retrying sells per **execution**; other venues unaffected | `OPS: <venue> API down <duration>. Buying paused there. <n> positions on venue; exits retrying.` |
| Venue recovers | Resume that venue after reconciliation passes | `OPS: <venue> restored. Reconciled clean. Buying resumed.` |
| SMS webhook down (inbound probe fails) | Restart webhook unit. If still down 5 min: pause all automatic buying — the user's STOP path is broken | `OPS: Inbound SMS down. Buying paused until restored. Outbound alerts still work.` |
| Bot process down or hung; heartbeat missed | systemd restarts (cold start, `SELL_ONLY`). Monitor alerts independently | `BOT OFFLINE — positions unmanaged` (sent by the external monitor) |
| Crash loop (>5 restarts / 10 min) | Service stays down | Same monitor SMS; user intervenes over SSH |
| Reconciliation mismatch | `RECON_FREEZE` per **portfolio-state**: buying stops, sells continue | Discrepancy SMS per **portfolio-state**; re-alert every 30 min while unexplained |
| Emergency halt (20% / 24 h) | Halt per **risk-limits**; resume only via **approval-gate** | Halt SMS per **risk-limits** with trigger values and resume instruction |
| Feed stale / clock drift / disk critical | Pause buying per the watchdog table; auto-resume on clear | `OPS: <condition>. Buying paused. Sells unaffected.` |
| Withdrawal permission found enabled on a key | Set that venue alert-only; place no orders with the key | `OPS: <venue> key has withdrawal enabled. Venue disabled. Rotate the key.` |
| Backup failed 2 consecutive days | Keep retrying daily | `OPS: Journal backup failing since <date>. Last good backup <date>.` |
| fail2ban ban rate spike / repeated SSH auth failures | None (fail2ban handles bans) | `OPS: <n> SSH intrusion attempts banned in 24 h.` |

If more than one incident is active, each fault mode applies independently. Clearing one never clears another.
