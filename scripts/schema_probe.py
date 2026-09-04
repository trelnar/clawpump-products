#!/usr/bin/env python3
"""Find the actual structured-output schema ceiling, instead of guessing at it.

Two schema versions were rejected with 400 "Schema is too complex", each time
stopping the research layer for hours. The limit is not documented, so measure
it: this binary-searches the number of candidate properties the API will accept
and reports the largest that compiles.

Rejected requests are not billed and max_tokens is 1, so this costs ~nothing.

    /opt/tradebot/venv/bin/python /opt/tradebot/scripts/schema_probe.py
"""
import copy
import json
import sys

sys.path.insert(0, "/opt/tradebot/bot")

from tradebot import config  # noqa: E402
from tradebot.agent import prompts, runner  # noqa: E402

FILLER = {"type": "string", "description": "probe filler property"}


def schema_with(n_extra):
    s = copy.deepcopy(prompts.FORECAST_SCHEMA)
    props = s["properties"]["candidates"]["items"]["properties"]
    for i in range(n_extra):
        props[f"probe_{i}"] = dict(FILLER)
    return s


def accepted(schema):
    try:
        runner.client().messages.create(
            model=config.ANTHROPIC_MODEL, max_tokens=1,
            messages=[{"role": "user", "content": "ping"}],
            output_config={"effort": "low",
                           "format": {"type": "json_schema", "schema": schema}})
        return True
    except Exception as e:
        if "complex" in repr(e).lower():
            return False
        raise            # a different error is not a size signal


def main():
    base = prompts.FORECAST_SCHEMA
    n_base = len(base["properties"]["candidates"]["items"]["properties"])
    print(f"current schema: {n_base} properties, {len(json.dumps(base))} chars")

    if not accepted(base):
        print("REJECTED as-is. The live schema is still too complex -- cut fields.")
        return 1
    print("current schema: ACCEPTED")

    lo, hi = 0, 1
    while accepted(schema_with(hi)):          # find an upper bound
        lo, hi = hi, hi * 2
        if hi > 256:
            break
    while lo + 1 < hi:                        # bisect
        mid = (lo + hi) // 2
        if accepted(schema_with(mid)):
            lo = mid
        else:
            hi = mid
    print(f"\nheadroom: {lo} more properties accepted, {lo + 1} rejected")
    print(f"ceiling  : ~{n_base + lo} properties at this shape "
          f"({len(json.dumps(schema_with(lo)))} chars)")
    print("\nUse that as the budget in bot/tests/test_contracts.py, with margin.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
