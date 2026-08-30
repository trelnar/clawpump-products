"""vps-ops dead-man's switch: ping healthchecks.io. Silence -> external alert."""
import requests

from . import config, journal


def ping():
    if not config.HEALTHCHECK_URL:
        return False
    try:
        requests.post(config.HEALTHCHECK_URL, timeout=10)
        return True
    except Exception as e:
        journal.log_event("heartbeat_fail", detail=str(e))
        return False
