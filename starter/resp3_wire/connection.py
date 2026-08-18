"""A single connection to a Redis server.

A connection owns one socket and one parser, negotiates its protocol version
on connect, and knows whether its stream is still trustworthy.
"""

from __future__ import annotations

from types import TracebackType
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import cycle broken for type hints only
    from .pipeline import Pipeline

__all__ = ["Connection"]


class Connection:
    """A connection to a Redis server.

    Construction performs no I/O. An instance is inert until :meth:`connect`
    is called, and may be reconnected after :meth:`close`.

    ``protocol`` is the preferred protocol version, 2 or 3. Any other value
    raises :exc:`ValueError` at construction.

    ``timeout`` applies to each individual socket operation, not to a whole
    command. ``None`` blocks indefinitely. ``connect_timeout`` defaults to
    ``timeout`` when not given.

    Supports the context manager protocol, closing on exit.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 6379,
        *,
        protocol: int = 3,
        timeout: float | None = 5.0,
        connect_timeout: float | None = None,
        db: int = 0,
        client_name: str | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._protocol = protocol
        self._timeout = timeout
        self._connect_timeout = connect_timeout
        self._db = db
        self._client_name = client_name

    # -- lifecycle ---------------------------------------------------------

    def connect(self) -> None:
        """Open the socket, disable Nagle, and negotiate the protocol.

        Negotiation, when ``protocol`` is 3, sends ``HELLO 3`` with
        ``SETNAME`` arguments appended if a client name was configured, and
        reads one reply. There are three outcomes.

        A successful reply, a map under RESP3, sets
        :attr:`protocol_version` to 3 and populates :attr:`server_info`.

        A server error reply means the server does not support ``HELLO`` or
        does not support protocol 3, which includes servers predating Redis 6
        that answer with an unknown command error. Fall back: set
        :attr:`protocol_version` to 2, leave :attr:`server_info` empty, and
        continue. This is not an error and raises nothing.

        A :exc:`ProtocolError` or :exc:`ConnectionError` propagates. A
        malformed ``HELLO`` reply means a broken server or a broken parser,
        not a negotiation failure, and must not be silently downgraded.

        When ``protocol`` is 2, no ``HELLO`` is sent at all.

        ``SELECT`` is issued afterwards when ``db`` is nonzero. Under RESP3,
        ``SETNAME`` rides along with ``HELLO`` rather than being issued
        separately.

        Fallback is per connection. Calling this on an already connected
        instance is a no-op. On failure it raises :exc:`ConnectionError` and
        leaves the instance disconnected.
        """
        raise NotImplementedError("Connection.connect")

    def close(self) -> None:
        """Shut down the socket and reset the parser.

        Idempotent, and never raises. A closed connection may be reconnected
        by calling :meth:`connect` again.
        """
        raise NotImplementedError("Connection.close")

    @property
    def is_connected(self) -> bool:
        """Whether the socket is currently open."""
        raise NotImplementedError("Connection.is_connected")

    @property
    def is_poisoned(self) -> bool:
        """Whether this connection's stream position is no longer known.

        Set when a :exc:`ProtocolError`, :exc:`ConnectionError`, or
        :exc:`TimeoutError` occurred during use. A server error does not
        poison a connection, because the server completed its reply normally.

        A poisoned connection raises :exc:`ConnectionError` on any further
        :meth:`execute`.
        """
        raise NotImplementedError("Connection.is_poisoned")

    @property
    def protocol_version(self) -> int:
        """The negotiated protocol version, 2 or 3.

        Raises :exc:`RuntimeError` if read before :meth:`connect`, since there
        is no negotiated version until then.
        """
        raise NotImplementedError("Connection.protocol_version")

    @property
    def server_info(self) -> dict[bytes, object]:
        """The parsed ``HELLO`` response.

        An empty dict when negotiation fell back to RESP2 without a usable
        ``HELLO`` reply, and when ``protocol`` is 2. Keys are :class:`bytes`,
        like every other string-ish value this package produces.
        """
        raise NotImplementedError("Connection.server_info")

    @property
    def pushes_discarded(self) -> int:
        """How many push frames have been discarded on this connection.

        Monotonically increasing, and reset by :meth:`connect`. Exposed for
        diagnostics: a caller cannot otherwise tell that out of band traffic
        arrived, since push frames are dropped rather than surfaced.
        """
        raise NotImplementedError("Connection.pushes_discarded")

    # -- commands ----------------------------------------------------------

    def execute(self, *args: bytes | str | int | float) -> object:
        """Send one command and read one reply.

        The arguments are encoded as a RESP array of bulk strings. ``bytes``
        pass through, ``str`` encodes as UTF-8, ``int`` and ``float`` encode
        via ``repr``. Anything else raises :exc:`TypeError`, and a ``bool``
        raises :exc:`TypeError` rather than encoding as an integer, because
        silently sending ``True`` as ``1`` hides bugs.

        The return value is the parsed reply, with one transformation: a top
        level error reply is converted to the matching exception and raised.
        A nested one is returned intact, as an
        :class:`~resp3_wire.protocol.ErrorReply`. That asymmetry is required,
        because ``EXEC`` returns an array in which individual commands may
        have failed and raising on the first would make a partially failed
        transaction unrepresentable.

        Decorated values are returned as
        :class:`~resp3_wire.protocol.Attributed` instances. This does not
        unwrap them.

        A push frame arriving while this waits for a reply is discarded,
        :attr:`pushes_discarded` increments, and reading continues until a non
        push reply arrives. Treating a push frame as a command reply would
        desynchronise the connection permanently.

        Raises :exc:`ConnectionError` on a disconnected or poisoned
        connection. It does not reconnect implicitly; reconnection is the
        pool's responsibility.
        """
        raise NotImplementedError("Connection.execute")

    def pipeline(self) -> "Pipeline":
        """Return a new :class:`~resp3_wire.pipeline.Pipeline` on this connection."""
        raise NotImplementedError("Connection.pipeline")

    # -- context manager ---------------------------------------------------

    def __enter__(self) -> "Connection":
        raise NotImplementedError("Connection.__enter__")

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        raise NotImplementedError("Connection.__exit__")
