"""One module per source. Each exposes:

    NAME        str
    KEYLESS     bool   -- runs with no credentials
    enabled()   bool   -- config and credentials present
    collect()   int    -- events recorded this pass; raises on failure

The registry in signals/__init__.py guards every call, so a source may raise
freely and never take down the others.
"""
