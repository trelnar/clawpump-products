"""vps-ops dead-man's switch: ping healthchecks.io. Silence -> external alert.

The ping must follow the work it vouches for. Pinging first reports green for
a loop that then failed to monitor a single position -- the exact failure the
switch exists to catch."""
import requests

from . import config, journal


def ping_start():
    """Tell healthchecks a cycle began, so a hang shows as a run that never
    finished rather than as a gap it might attribute to the network."""
    if not config.HEALTHCHECK_URL:
        return False
    try:
        requests.post(config.HEALTHCHECK_URL.rstrip("/") + "/start", timeout=10)
        return True
    except Exception:
        return False   # best effort; the real signal is the success ping


def fail(detail=""):
    """Explicitly report a failed cycle rather than waiting for the grace
    period. A loop that raised is not a healthy loop."""
    if not config.HEALTHCHECK_URL:
        return False
    try:
        requests.post(config.HEALTHCHECK_URL.rstrip("/") + "/fail",
                      data=str(detail)[:500].encode(), timeout=10)
        return True
    except Exception as e:
        journal.log_event("heartbeat_fail_report_failed", detail=str(e))
        return False


def ping():
    if not config.HEALTHCHECK_URL:
        return False
    try:
        r = requests.post(config.HEALTHCHECK_URL, timeout=10)
        r.raise_for_status()  # a deleted/wrong check URL returns 404 — that is a failure
        return True
    except Exception as e:
        journal.log_event("heartbeat_fail", detail=str(e))
        return False
