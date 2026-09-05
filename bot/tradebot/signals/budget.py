"""A wall-clock budget for one collection pass.

Every source is a chain of HTTP calls with 15s timeouts and polite sleeps; with
everything hanging, one pass added up to ~10 minutes on the research thread,
which is the whole cycle. collect_all() arms a per-source budget and the
sources check it between requests, so the worst case is one hanging request
past the budget, not the whole chain.
"""
import time

_until = [None]


def arm(seconds):
    _until[0] = time.time() + seconds


def clear():
    _until[0] = None


def expired():
    return _until[0] is not None and time.time() >= _until[0]


# connect timeout, read timeout: a trickling server cannot hold a call open
# for long, and DNS failures surface in seconds rather than never.
TIMEOUT = (5, 10)
