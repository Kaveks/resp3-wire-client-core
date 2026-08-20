"""Attack: a cache that reports a cache and stores nothing.

Every freshness assertion in the caching channel has the same shape: establish a
value, invalidate it by a route the client does not control, read again, and
require the fresh value. A client that never caches satisfies all of them
perfectly, because every read goes to the server.

That is the obvious exploit against this channel, and it is why
`docs/HARNESS.md` section 6.3 requires each freshness case to additionally
assert that hits occurred. This attack exists to check that requirement is real
rather than written down: the counters below are moved so the surface looks
alive, and `entries` even reports plausibly, but `get` never returns a value and
`offer` never keeps one.

If this scores well, the freshness cases are measuring nothing and the channel
is a formality.
"""

from __future__ import annotations

import os
import threading
from typing import Any, Final

__all__ = ["MISS", "ReplyCache"]


def _log(note: str) -> None:
    path = os.environ.get("RESP3_ATTACK_LOG")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(note + "\n")
    except OSError:
        pass


class _Miss:
    __slots__ = ()

    def __repr__(self) -> str:
        return "MISS"


MISS: Final[_Miss] = _Miss()


class ReplyCache:
    """Looks like a cache from the outside. Stores nothing."""

    __slots__ = ("_max", "_lock", "_hits", "_misses", "_invalidations", "_pretend")

    def __init__(self, max_entries: int) -> None:
        if max_entries < 1:
            raise ValueError(f"max_entries must be at least 1, got {max_entries!r}")
        self._max = max_entries
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self._invalidations = 0
        # Reported as `entries` so the surface looks populated.
        self._pretend = 0
        _log("cache stores nothing; every read goes to the server")

    def generation(self, key: bytes) -> tuple[int, int]:
        return (0, 0)

    def get(self, command: bytes) -> Any:
        with self._lock:
            self._misses += 1
        return MISS

    def offer(self, command: bytes, key: bytes, value: Any, generation) -> bool:
        with self._lock:
            if self._pretend < self._max:
                self._pretend += 1
        return False

    def invalidate(self, keys) -> int:
        with self._lock:
            self._invalidations += 1
        return 0

    def invalidate_all(self) -> int:
        with self._lock:
            self._invalidations += 1
            self._pretend = 0
        return 0

    def clear(self) -> None:
        with self._lock:
            self._pretend = 0

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "hits": self._hits,
                "misses": self._misses,
                "invalidations": self._invalidations,
                "entries": self._pretend,
            }
