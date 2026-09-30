"""JSON encoding on the hot paths (API responses, checkpoints).

orjson (C, ~10x faster than the stdlib encoder, and it bypasses FastAPI's
recursive jsonable_encoder) when installed; the stdlib json module
otherwise, so the app never depends on it being present."""

from __future__ import annotations

import json

try:
    import orjson

    _OPTS = orjson.OPT_NON_STR_KEYS

    def dumps(obj) -> bytes:
        return orjson.dumps(obj, option=_OPTS, default=_default)

    def loads(data):
        return orjson.loads(data)

    FAST = True
except ImportError:            # pragma: no cover - depends on the environment
    def dumps(obj) -> bytes:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"),
                          default=_default).encode("utf-8")

    def loads(data):
        return json.loads(data)

    FAST = False


def _default(obj):
    if isinstance(obj, (set, frozenset, tuple)):
        return list(obj)
    raise TypeError(f"not JSON serializable: {type(obj).__name__}")
