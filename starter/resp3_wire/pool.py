"""A thread safe pool of connections."""

from __future__ import annotations

from contextlib import AbstractContextManager
from types import TracebackType

from .connection import Connection

__all__ = ["ConnectionPool"]


class ConnectionPool:
    """A pool of connections to one server.

    The pool is thread safe: multiple threads may call :meth:`acquire` and
    :meth:`release` concurrently. An individual :class:`Connection` is not
    thread safe, and the pool guarantees one is issued to at most one borrower
    at a time.

    Internal locking must not serialise command execution. A lock held across
    a socket read reduces the pool to a single connection's throughput and
    defeats the point of having one, so a lock should cover bookkeeping only:
    connecting, health checking, and executing all happen outside it.

    ``**connection_kwargs`` are passed through to each :class:`Connection`.

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
        **connection_kwargs: object,
    ) -> None:
        self._host = host
        self._port = port
        self._max_connections = max_connections
        self._protocol = protocol
        self._timeout = timeout
        self._health_check_interval = health_check_interval
        self._connection_kwargs = connection_kwargs

    # -- borrow and return -------------------------------------------------

    def acquire(self) -> Connection:
        """Borrow a connected :class:`Connection`.

        Reuses an idle connection when one is available and otherwise creates
        one, up to ``max_connections``. At capacity with none idle, this
        blocks until one is released or ``timeout`` elapses, then raises
        :exc:`TimeoutError`.

        When ``health_check_interval`` is nonzero and that many seconds have
        elapsed since an idle connection was last used, it is checked with
        ``PING`` before being handed out. One that fails the check is
        discarded and another is tried.

        Raises :exc:`ConnectionError` if the pool is closed.
        """
        raise NotImplementedError("ConnectionPool.acquire")

    def release(self, conn: Connection) -> None:
        """Return a connection to the idle set, or discard it.

        A connection is discarded rather than made idle again when its most
        recent use raised :exc:`ProtocolError`, :exc:`ConnectionError`, or
        :exc:`TimeoutError`. In each of those cases the stream position is
        unknown, and handing the connection to the next borrower would let it
        read the tail of somebody else's reply. That is silent cross-talk
        rather than a visible error, which is what makes it worth preventing
        rather than detecting.

        A server error does not cause a discard. The server completed its
        reply normally and the stream is intact.

        The connection tracks its own poisoned state and this consults it;
        callers are not required to.

        Releasing a connection this pool did not issue raises
        :exc:`ValueError`.
        """
        raise NotImplementedError("ConnectionPool.release")

    def connection(self) -> AbstractContextManager[Connection]:
        """Borrow a connection for the duration of a ``with`` block.

        The preferred interface. It releases on normal exit and on exception,
        including :exc:`KeyboardInterrupt`.
        """
        raise NotImplementedError("ConnectionPool.connection")

    def close(self) -> None:
        """Close every connection, idle and in use, and mark the pool closed.

        Idempotent. A subsequent :meth:`acquire` raises
        :exc:`ConnectionError`.
        """
        raise NotImplementedError("ConnectionPool.close")

    # -- introspection -----------------------------------------------------

    @property
    def size(self) -> int:
        """Total connections the pool holds, idle plus in use."""
        raise NotImplementedError("ConnectionPool.size")

    @property
    def idle(self) -> int:
        """Connections currently available to borrow."""
        raise NotImplementedError("ConnectionPool.idle")

    @property
    def in_use(self) -> int:
        """Connections currently borrowed."""
        raise NotImplementedError("ConnectionPool.in_use")

    # -- context manager ---------------------------------------------------

    def __enter__(self) -> "ConnectionPool":
        raise NotImplementedError("ConnectionPool.__enter__")

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        raise NotImplementedError("ConnectionPool.__exit__")
