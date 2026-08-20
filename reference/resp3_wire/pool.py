"""A thread safe pool of connections.

The design constraint that shapes this module is that the lock protects
bookkeeping and nothing else. Opening a socket, negotiating, running a health
check, and closing are all performed with the lock released, because a lock
held across a socket operation reduces the pool to a single connection's
throughput and defeats the reason for having one.

That is why acquisition is written as a loop that alternates between a short
critical section and unlocked I/O, rather than as a single guarded block. A
slot is reserved under the lock, the connection is opened outside it, and the
bookkeeping is reconciled under the lock afterwards.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from contextlib import contextmanager
from types import TracebackType
from typing import Any, Iterator

from .cache import ReplyCache
from .connection import Connection
from .errors import ConnectionError, RedisError, TimeoutError

__all__ = ["ConnectionPool"]

# Applied when `timeout` is None. Socket operations may block indefinitely, but
# acquisition may not: a pool that can wait forever at capacity is a deadlock
# rather than a configuration.
_UNBOUNDED_ACQUIRE_LIMIT = 30.0


class ConnectionPool:
    """A pool of connections to one server.

    The pool is thread safe: multiple threads may call :meth:`acquire` and
    :meth:`release` concurrently. An individual :class:`Connection` is not
    thread safe, and the pool guarantees one is issued to at most one borrower
    at a time.

    ``**connection_kwargs`` are passed through to each :class:`Connection` and
    take precedence over the pool's own ``protocol`` and ``timeout``.

    Supports the context manager protocol.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 6379,
        *,
        max_connections: int = 16,
        protocol: int = 3,
        timeout: float | None = 5.0,
        health_check_interval: float = 0.0,
        cache_size: int = 0,
        **connection_kwargs: Any,
    ) -> None:
        if max_connections < 1:
            raise ValueError(f"max_connections must be at least 1, got {max_connections!r}")
        if cache_size < 0:
            raise ValueError(f"cache_size must not be negative, got {cache_size!r}")
        if cache_size and protocol != 3:
            # docs/API.md section 7A.3. Under RESP2 the server has no channel on
            # which to deliver an invalidation, so a cache there could only ever
            # serve stale data. Refusing at construction is the only honest
            # answer; the alternative is a cache that is silently wrong.
            raise ValueError(
                f"client-side caching requires protocol=3; got protocol={protocol!r} "
                f"with cache_size={cache_size!r}"
            )
        self._host = host
        self._port = port
        self._max_connections = max_connections
        self._protocol = protocol
        self._timeout = timeout
        self._health_check_interval = health_check_interval
        self._connection_kwargs = connection_kwargs
        # Pool-wide per D34, not per connection. The connection that receives an
        # invalidation is usually not the connection that cached the value.
        self._cache = ReplyCache(cache_size) if cache_size > 0 else None

        self._cond = threading.Condition()
        # (connection, monotonic timestamp of its last use)
        self._idle: deque[tuple[Connection, float]] = deque()
        self._in_use: set[Connection] = set()
        # Every connection this pool created and has not discarded. Backs the
        # ValueError on releasing a foreign connection.
        self._owned: set[Connection] = set()
        # Slots claimed by a thread that is opening a connection right now.
        # Counted against capacity so concurrent acquires cannot overshoot
        # max_connections while all of them are outside the lock connecting.
        self._reserved = 0
        self._closed = False

    def __repr__(self) -> str:
        with self._cond:
            return (
                f"<ConnectionPool {self._host}:{self._port} "
                f"idle={len(self._idle)} in_use={len(self._in_use)} "
                f"max={self._max_connections}{' closed' if self._closed else ''}>"
            )

    # -- borrow and return -------------------------------------------------

    def acquire(self) -> Connection:
        """Borrow a connected :class:`Connection`.

        Reuses an idle connection when one is available and otherwise creates
        one, up to ``max_connections``. At capacity with none idle, this blocks
        until one is released or the acquisition bound elapses, then raises
        :exc:`TimeoutError`.

        The bound is ``timeout``, or 30 seconds when ``timeout`` is ``None``.

        When ``health_check_interval`` is nonzero and that many seconds have
        elapsed since an idle connection was last used, it is checked with
        ``PING`` before being handed out. One that fails is discarded and
        another is tried.

        Raises :exc:`ConnectionError` if the pool is closed.
        """
        bound = _UNBOUNDED_ACQUIRE_LIMIT if self._timeout is None else self._timeout
        deadline = time.monotonic() + bound
        while True:
            reuse: Connection | None = None
            last_used = 0.0
            create = False

            with self._cond:
                if self._closed:
                    raise ConnectionError("connection pool is closed")
                if self._idle:
                    reuse, last_used = self._idle.popleft()
                    self._in_use.add(reuse)
                elif self._total_locked() + self._reserved < self._max_connections:
                    self._reserved += 1
                    create = True
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(
                            f"no connection available within {bound:.3f}s; "
                            f"pool is at its limit of {self._max_connections}"
                        )
                    self._cond.wait(remaining)
                    continue

            # Everything below runs with the lock released.
            if create:
                return self._open_reserved()

            assert reuse is not None
            if self._is_stale(reuse, last_used):
                self._discard(reuse)
                continue
            return reuse

    def release(self, conn: Connection) -> None:
        """Return a connection to the idle set, or discard it.

        A connection is discarded rather than made idle again when its most
        recent use raised :exc:`ProtocolError`, :exc:`ConnectionError`, or
        :exc:`TimeoutError`. In each of those cases the stream position is
        unknown, and handing it to the next borrower would let that borrower
        read the tail of somebody else's reply, which is silent cross-talk
        rather than a visible error.

        A server error does not cause a discard: the server completed its reply
        normally and the stream is intact.

        The connection tracks its own poisoned state and this consults it;
        callers are not required to.

        Releasing a connection this pool did not issue, or one it is not
        currently lending out, raises :exc:`ValueError`. The second case is a
        double release, which would otherwise place one connection in the idle
        set twice and hand it to two borrowers at once.
        """
        discard = False
        with self._cond:
            if conn not in self._owned:
                raise ValueError("connection was not issued by this pool")
            if self._closed:
                # close() has already emptied _in_use, so the borrowed check
                # below would misread this as a double release. A borrower
                # unwinding a with block after another thread closed the pool
                # must not see a spurious error on the way out.
                self._owned.discard(conn)
                self._in_use.discard(conn)
                discard = True
            elif conn not in self._in_use:
                raise ValueError("connection is not currently borrowed from this pool")
            else:
                self._in_use.discard(conn)
                if conn.is_poisoned or not conn.is_connected:
                    self._owned.discard(conn)
                    discard = True
                else:
                    self._idle.append((conn, time.monotonic()))
            self._cond.notify()
        if discard:
            conn.close()

    @contextmanager
    def connection(self) -> Iterator[Connection]:
        """Borrow a connection for the duration of a ``with`` block.

        The preferred interface. It releases on normal exit and on exception,
        including :exc:`KeyboardInterrupt`, because the release sits in a
        ``finally``.
        """
        conn = self.acquire()
        try:
            yield conn
        finally:
            self.release(conn)

    def close(self) -> None:
        """Close every connection, idle and in use, and mark the pool closed.

        Idempotent. A subsequent :meth:`acquire` raises :exc:`ConnectionError`.
        A borrower still holding a connection may still release it; the release
        discards rather than raising, so an in-flight ``with`` block unwinds
        cleanly.
        """
        with self._cond:
            already = self._closed
            self._closed = True
            doomed = [c for c, _ in self._idle] + list(self._in_use)
            self._idle.clear()
            self._in_use.clear()
            # _owned is deliberately not cleared, so a late release from a
            # borrower still inside a with block discards rather than raising.
            self._cond.notify_all()
        if already:
            return
        # Section 7A.1: the counters reset only here.
        self._cache = None
        for conn in doomed:
            conn.close()

    # -- introspection -----------------------------------------------------

    @property
    def size(self) -> int:
        """Total connections the pool holds, idle plus in use."""
        with self._cond:
            return self._total_locked()

    @property
    def idle(self) -> int:
        """Connections currently available to borrow."""
        with self._cond:
            return len(self._idle)

    @property
    def in_use(self) -> int:
        """Connections currently borrowed."""
        with self._cond:
            return len(self._in_use)

    # -- client-side caching -----------------------------------------------

    @property
    def cache_stats(self) -> dict[str, int]:
        """Hits, misses, invalidations, and current entry count.

        Counters are monotonic and reset only on :meth:`close`. They exist so
        the caching channel can observe that a cache is in use without reaching
        into internals: a cache that never hits is indistinguishable from no
        cache by result alone.
        """
        if self._cache is None:
            return {"hits": 0, "misses": 0, "invalidations": 0, "entries": 0}
        return self._cache.stats()

    def cache_clear(self) -> None:
        """Drop every cached entry. Counters are unaffected."""
        if self._cache is not None:
            self._cache.clear()

    def _drain_peers(self, borrower: Connection) -> bool:
        """Consume invalidations sitting unread on every connection this pool owns.

        This is the pool-wide half of the requirement, and D34 is why it cannot
        be narrower. An invalidation for a key arrives on whichever connection
        read that key, which is rarely the connection now asking for it. That
        connection may be idle in the pool with nobody reading it, or held by a
        worker that is between commands. Sweeping only the idle ones leaves the
        second case serving stale values, which is what the "a worker holding a
        connection does not block eviction" case exists to catch.

        The pool's own lock is held only long enough to copy the set of
        connections. Every socket read below happens with it released, because
        `docs/API.md` section 6.4 forbids holding a lock across socket I/O and
        section 7A.5 says cache correctness is not an exemption. Each connection
        is swept under its own I/O lock, taken without blocking: a connection
        mid-reply is skipped rather than waited for, and it is already consuming
        its own invalidations as it reads.

        Returns whether every peer was actually swept. A peer that was skipped
        may be holding an invalidation nobody has read, so the caller must not
        serve a cached value on the strength of an incomplete sweep. Waiting for
        that peer instead would couple one connection's cache hit to another
        connection's in-flight command, which is the coupling section 6.4
        forbids; taking the miss costs nothing, because section 7A.5 says
        serving a miss where a hit was possible is not a failure.
        """
        if self._cache is None:
            return True
        with self._cond:
            if self._closed:
                return True
            peers = [c for c in self._owned if c is not borrower]
        complete = True
        for conn in peers:
            try:
                if not conn.drain_invalidations(blocking=False):
                    complete = False
            except RedisError:
                # A peer that died while being swept is not this borrower's
                # problem; it will be discarded when its holder next uses it or
                # when the health check reaches it.
                continue
        return complete

    # -- context manager ---------------------------------------------------

    def __enter__(self) -> "ConnectionPool":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- internals ---------------------------------------------------------

    def _total_locked(self) -> int:
        """Idle plus in use. The caller must hold the lock."""
        return len(self._idle) + len(self._in_use)

    def _open_reserved(self) -> Connection:
        """Open a connection for a slot already reserved, with the lock released."""
        try:
            kwargs: dict[str, Any] = {
                "protocol": self._protocol,
                "timeout": self._timeout,
            }
            kwargs.update(self._connection_kwargs)
            if self._cache is not None:
                kwargs["cache"] = self._cache
                kwargs["predrain"] = self._drain_peers
            conn = Connection(self._host, self._port, **kwargs)
            conn.connect()
        except BaseException:
            with self._cond:
                self._reserved -= 1
                self._cond.notify()
            raise

        with self._cond:
            self._reserved -= 1
            if self._closed:
                self._cond.notify()
                conn.close()
                raise ConnectionError("connection pool is closed")
            self._owned.add(conn)
            self._in_use.add(conn)
        return conn

    def _is_stale(self, conn: Connection, last_used: float) -> bool:
        """Whether an idle connection must not be handed out.

        Performs the health check with the lock released, since it is a command
        round trip.
        """
        if conn.is_poisoned or not conn.is_connected:
            return True
        interval = self._health_check_interval
        if interval <= 0 or time.monotonic() - last_used < interval:
            return False
        try:
            return conn.execute("PING") != b"PONG"
        except RedisError:
            return True

    def _discard(self, conn: Connection) -> None:
        """Drop a borrowed connection from the pool entirely."""
        with self._cond:
            self._in_use.discard(conn)
            self._owned.discard(conn)
            self._cond.notify()
        conn.close()
