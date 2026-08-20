"""The pool-wide client-side cache.

`docs/API.md` section 7A. One requirement governs everything here: a cached read
must never return a value the server has already invalidated.

The structure that makes that achievable is the per-key generation counter.
Reading a reply and being clear to cache it are not the same event: an
invalidation for the key may already be sitting in the socket buffer when the
reply is parsed. So a caller records a key's generation *before* it sends the
command and offers the value back with that generation afterwards; if any
invalidation for the key landed in between, the generation has moved and the
offer is refused. Storing the value and processing the invalidation afterwards
would leave a stale entry readable for as long as it takes to get round to it,
which is exactly the failure D33 describes.

This module performs no I/O and holds its lock over nothing but dictionary
operations. `docs/API.md` section 6.4 forbids holding a lock across socket I/O,
and cache correctness is not an exemption.
"""

from __future__ import annotations

import threading
from typing import Any, Final

__all__ = ["MISS", "ReplyCache"]


class _Miss:
    """The type of :data:`MISS`. Distinguishable from every cached value."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "MISS"


MISS: Final[_Miss] = _Miss()
"""Returned by :meth:`ReplyCache.get` when the command is not cached.

Deliberately not ``None``: ``None`` is what a read of a missing key returns, and
that is a perfectly cacheable reply.
"""


class ReplyCache:
    """Command replies keyed by their encoded command, shared across a pool.

    Bounded at ``max_entries``; the oldest entry is evicted when the bound is
    reached. Counters are monotonic and are not affected by eviction or by
    :meth:`clear`, per section 7A.1.
    """

    __slots__ = ("_max", "_lock", "_entries", "_by_key", "_generation",
                 "_epoch", "_hits", "_misses", "_invalidations")

    def __init__(self, max_entries: int) -> None:
        if max_entries < 1:
            raise ValueError(f"max_entries must be at least 1, got {max_entries!r}")
        self._max = max_entries
        self._lock = threading.Lock()
        # encoded command -> reply. Insertion ordered, which is the eviction
        # order: dicts preserve it and nothing here reorders on access.
        self._entries: dict[bytes, Any] = {}
        # redis key -> the encoded commands whose replies depend on it, so an
        # invalidation naming a key can find every entry it makes stale.
        self._by_key: dict[bytes, set[bytes]] = {}
        # redis key -> how many times it has been invalidated. The counter is
        # what lets a caller detect an invalidation that arrived while it was
        # reading, rather than trusting that none did.
        self._generation: dict[bytes, int] = {}
        # Bumped by a flush-all, which names no keys. Without it an in-flight
        # read of a key the cache has never seen would find its per-key counter
        # still at zero after the flush and cache a value the server has just
        # dropped.
        self._epoch = 0
        self._hits = 0
        self._misses = 0
        self._invalidations = 0

    # -- reading -----------------------------------------------------------

    def generation(self, key: bytes) -> tuple[int, int]:
        """The key's invalidation state, to be handed back to :meth:`offer`.

        Two counters, because invalidation arrives two ways. A frame naming keys
        moves the per-key counter; a flush-all names none and moves the epoch.
        A caller that compared only the first would cache through a flush.
        """
        with self._lock:
            return (self._epoch, self._generation.get(key, 0))

    def get(self, command: bytes) -> Any:
        """The cached reply for an encoded command, or :data:`MISS`.

        Counts a hit or a miss either way. The caller is responsible for having
        drained pending invalidations before calling this; nothing here can know
        what is still sitting unread on a socket.
        """
        with self._lock:
            if command in self._entries:
                self._hits += 1
                return self._entries[command]
            self._misses += 1
            return MISS

    # -- writing -----------------------------------------------------------

    def offer(self, command: bytes, key: bytes, value: Any,
              generation: tuple[int, int]) -> bool:
        """Store a reply, unless the key was invalidated since `generation`.

        Returns whether it was stored. A refusal is not an error: it means the
        value went stale between being read and being offered, which is the race
        this cache exists to lose safely.
        """
        with self._lock:
            if (self._epoch, self._generation.get(key, 0)) != generation:
                return False
            if command not in self._entries and len(self._entries) >= self._max:
                oldest = next(iter(self._entries))
                self._forget_locked(oldest)
            self._entries[command] = value
            self._by_key.setdefault(key, set()).add(command)
            return True

    def _forget_locked(self, command: bytes) -> None:
        self._entries.pop(command, None)
        for key, commands in list(self._by_key.items()):
            commands.discard(command)
            if not commands:
                del self._by_key[key]

    # -- invalidation ------------------------------------------------------

    def invalidate(self, keys) -> int:
        """Drop every entry depending on any of `keys`, and bump their generations.

        The generation moves whether or not anything was cached, because a
        command may be in flight for a key with no entry yet. That in-flight
        read is precisely the one that must not be allowed to cache.
        """
        with self._lock:
            self._invalidations += 1
            dropped = 0
            for key in keys:
                self._generation[key] = self._generation.get(key, 0) + 1
                for command in self._by_key.pop(key, set()):
                    if self._entries.pop(command, None) is not None:
                        dropped += 1
            return dropped

    def invalidate_all(self) -> int:
        """Drop everything. Redis sends this on FLUSHALL and on table overflow.

        Every generation moves, including those of keys with no entry, so a read
        in flight during a flush cannot cache what it was about to.
        """
        with self._lock:
            self._invalidations += 1
            dropped = len(self._entries)
            self._entries.clear()
            self._by_key.clear()
            self._epoch += 1
            return dropped

    # -- maintenance -------------------------------------------------------

    def clear(self) -> None:
        """Drop every entry. Counters are unaffected, per section 7A.1."""
        with self._lock:
            self._entries.clear()
            self._by_key.clear()

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "hits": self._hits,
                "misses": self._misses,
                "invalidations": self._invalidations,
                "entries": len(self._entries),
            }
