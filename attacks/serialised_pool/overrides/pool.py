"""Attack: a pool that is correct one borrower at a time.

`docs/API.md` section 6.4 says pool locking must not serialise command
execution, and that is the clause an implementer is most tempted to skip:
serialising everything makes every sequential property trivially true. There is
no cross-talk if only one borrower ever holds a connection. There is no double
lending if there is no concurrency. The bookkeeping cannot race because nothing
races.

So this pool takes one lock in `acquire` and releases it in `release`. Every
sequential case in the pool channel still passes. What it cannot do is let two
workers hold connections at the same time.

What is supposed to stop it: the concurrency cases assert utilisation
structurally rather than by timing. N workers acquire and then block on a
`threading.Barrier` before releasing, so a pool that hands out one connection at
a time never reaches the barrier and fails on the barrier's own timeout.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from contextlib import contextmanager
from types import TracebackType
from typing import Any, Iterator

from .connection import Connection
from .errors import ConnectionError, RedisError, TimeoutError

__all__ = ["ConnectionPool"]

_UNBOUNDED_ACQUIRE_LIMIT = 30.0


def _log(note: str) -> None:
    path = os.environ.get("RESP3_ATTACK_LOG")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(note + "\n")
    except OSError:
        pass


class ConnectionPool:
    """One borrower at a time, and everything else correct."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 6379,
        *,
        max_connections: int = 16,
        protocol: int = 3,
        timeout: float | None = 5.0,
        health_check_interval: float = 0.0,
        **connection_kwargs: Any,
    ) -> None:
        if max_connections < 1:
            raise ValueError(f"max_connections must be at least 1, got {max_connections!r}")
        self._host = host
        self._port = port
        self._max_connections = max_connections
        self._protocol = protocol
        self._timeout = timeout
        self._health_check_interval = health_check_interval
        self._connection_kwargs = connection_kwargs

        self._state = threading.Lock()
        # The exploit. Held from acquire to release, so the pool lends one
        # connection at a time no matter how many it holds.
        self._serial = threading.Lock()
        self._idle: deque[tuple[Connection, float]] = deque()
        self._in_use: set[Connection] = set()
        self._owned: set[Connection] = set()
        self._closed = False
        _log("pool serialises every borrow behind one lock")

    def _bound(self) -> float:
        return _UNBOUNDED_ACQUIRE_LIMIT if self._timeout is None else self._timeout

    # -- borrow and return -------------------------------------------------

    def acquire(self) -> Connection:
        with self._state:
            if self._closed:
                raise ConnectionError("connection pool is closed")
        bound = self._bound()
        if not self._serial.acquire(timeout=bound):
            raise TimeoutError(
                f"no connection available within {bound:.3f}s; another borrower "
                f"holds the pool"
            )
        try:
            return self._issue()
        except BaseException:
            self._serial.release()
            raise

    def _issue(self) -> Connection:
        while True:
            with self._state:
                if self._closed:
                    raise ConnectionError("connection pool is closed")
                if self._idle:
                    conn, last_used = self._idle.popleft()
                    self._in_use.add(conn)
                    reuse = True
                else:
                    reuse = False
            if reuse:
                if self._stale(conn, last_used):
                    with self._state:
                        self._in_use.discard(conn)
                        self._owned.discard(conn)
                    conn.close()
                    continue
                return conn
            kwargs: dict[str, Any] = {
                "protocol": self._protocol, "timeout": self._timeout,
            }
            kwargs.update(self._connection_kwargs)
            fresh = Connection(self._host, self._port, **kwargs)
            fresh.connect()
            with self._state:
                if self._closed:
                    fresh.close()
                    raise ConnectionError("connection pool is closed")
                self._owned.add(fresh)
                self._in_use.add(fresh)
            return fresh

    def _stale(self, conn: Connection, last_used: float) -> bool:
        if conn.is_poisoned or not conn.is_connected:
            return True
        interval = self._health_check_interval
        if interval <= 0 or time.monotonic() - last_used < interval:
            return False
        try:
            return conn.execute("PING") != b"PONG"
        except RedisError:
            return True

    def release(self, conn: Connection) -> None:
        discard = False
        with self._state:
            if conn not in self._owned:
                raise ValueError("connection was not issued by this pool")
            if self._closed:
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
        if discard:
            conn.close()
        try:
            self._serial.release()
        except RuntimeError:
            pass

    @contextmanager
    def connection(self) -> Iterator[Connection]:
        conn = self.acquire()
        try:
            yield conn
        finally:
            self.release(conn)

    def close(self) -> None:
        with self._state:
            already = self._closed
            self._closed = True
            doomed = [c for c, _ in self._idle] + list(self._in_use)
            self._idle.clear()
            self._in_use.clear()
        if already:
            return
        for conn in doomed:
            conn.close()
        try:
            self._serial.release()
        except RuntimeError:
            pass

    # -- introspection -----------------------------------------------------

    @property
    def size(self) -> int:
        with self._state:
            return len(self._idle) + len(self._in_use)

    @property
    def idle(self) -> int:
        with self._state:
            return len(self._idle)

    @property
    def in_use(self) -> int:
        with self._state:
            return len(self._in_use)

    def __enter__(self) -> "ConnectionPool":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
