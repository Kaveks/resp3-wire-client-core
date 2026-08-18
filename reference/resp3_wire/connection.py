"""A single connection to a Redis server.

A connection owns one socket and one parser, negotiates its protocol version on
connect, and knows whether its stream is still trustworthy.

The parser is sans-io, so everything that touches the socket lives here. The
division is what lets the parser be driven by arbitrary byte chunks in a test
without a server, and it is why this module reads bytes and hands them over
rather than letting the parser pull.
"""

from __future__ import annotations

import socket
from types import TracebackType
from typing import TYPE_CHECKING, Any

from .errors import ConnectionError, ProtocolError, TimeoutError, exception_for
from .parser import NEED_MORE, RespParser
from .protocol import ErrorReply, PushMessage, unwrap

if TYPE_CHECKING:  # pragma: no cover - avoids an import cycle at runtime
    from .pipeline import Pipeline

__all__ = ["Connection"]

_RECV_SIZE = 65536


def _encode_arg(value: object) -> bytes:
    """Encode one command argument as the payload of a bulk string."""
    # bool is a subclass of int, so it must be rejected before the int branch.
    # Sending True as 1 silently turns a type confusion into a valid command.
    if value is True or value is False:
        raise TypeError(
            "bool is not a valid command argument; pass an explicit int or bytes"
        )
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    if isinstance(value, (int, float)):
        return repr(value).encode("ascii")
    raise TypeError(f"cannot encode command argument of type {type(value).__name__}")


def _encode_command(args: tuple[object, ...]) -> bytes:
    """Encode a command as a RESP array of bulk strings."""
    out = bytearray()
    out += b"*%d\r\n" % len(args)
    for value in args:
        payload = _encode_arg(value)
        out += b"$%d\r\n" % len(payload)
        out += payload
        out += b"\r\n"
    return bytes(out)


def _as_info_dict(reply: object) -> dict[Any, Any]:
    """Normalize a HELLO reply into a dict.

    Under RESP3 the reply is a map and arrives as a dict already. A server that
    answers with a flat array instead requires pairing consecutive elements.
    Either way `server_info` is a dict with `bytes` keys.
    """
    reply = unwrap(reply)
    if isinstance(reply, dict):
        return reply
    if isinstance(reply, list):
        if len(reply) % 2:
            raise ProtocolError(
                f"HELLO reply is a flat array of odd length {len(reply)}"
            )
        pairs = iter(reply)
        return dict(zip(pairs, pairs))
    raise ProtocolError(
        f"HELLO reply is neither a map nor a flat array: {type(reply).__name__}"
    )


class Connection:
    """A connection to a Redis server.

    Construction performs no I/O. An instance is inert until :meth:`connect` is
    called, and may be reconnected after :meth:`close`.

    ``protocol`` is the preferred protocol version, 2 or 3. Any other value
    raises :exc:`ValueError` at construction.

    ``timeout`` applies to each individual socket operation, not to a whole
    command. ``None`` blocks indefinitely. ``connect_timeout`` defaults to
    ``timeout`` when not given.

    Supports the context manager protocol, closing on exit. Entering does not
    connect, since construction performing no I/O would be a hollow guarantee
    if ``with`` quietly undid it.
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
        if protocol not in (2, 3):
            raise ValueError(f"protocol must be 2 or 3, got {protocol!r}")
        self._host = host
        self._port = port
        self._protocol = protocol
        self._timeout = timeout
        self._connect_timeout = timeout if connect_timeout is None else connect_timeout
        self._db = db
        self._client_name = client_name

        self._sock: socket.socket | None = None
        self._parser = RespParser()
        self._protocol_version: int | None = None
        self._server_info: dict[Any, Any] = {}
        self._poisoned = False
        self._pushes_discarded = 0

    def __repr__(self) -> str:
        state = "connected" if self._sock is not None else "disconnected"
        if self._poisoned:
            state += ", poisoned"
        return f"<Connection {self._host}:{self._port} {state}>"

    # -- lifecycle ---------------------------------------------------------

    def connect(self) -> None:
        """Open the socket, disable Nagle, and negotiate the protocol.

        Calling this on an already connected instance is a no-op. On failure it
        raises :exc:`ConnectionError` and leaves the instance disconnected.

        Negotiation is described in :meth:`_negotiate`. A ``ServerError`` reply
        to ``HELLO`` is a fallback rather than a failure and raises nothing; a
        :exc:`ProtocolError` or :exc:`ConnectionError` propagates.
        """
        if self._sock is not None:
            return
        try:
            sock = socket.create_connection(
                (self._host, self._port), timeout=self._connect_timeout
            )
        except socket.timeout as exc:
            raise ConnectionError(
                f"timed out connecting to {self._host}:{self._port}"
            ) from exc
        except OSError as exc:
            raise ConnectionError(
                f"could not connect to {self._host}:{self._port}: {exc}"
            ) from exc

        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.settimeout(self._timeout)
        except OSError as exc:
            _quietly_close(sock)
            raise ConnectionError(f"could not configure socket: {exc}") from exc

        self._sock = sock
        self._parser.reset()
        self._poisoned = False
        self._pushes_discarded = 0

        try:
            self._negotiate()
        except BaseException:
            # A failed negotiation leaves nothing usable behind.
            self.close()
            raise

    def close(self) -> None:
        """Shut down the socket and reset the parser.

        Idempotent, and never raises. A closed connection may be reconnected by
        calling :meth:`connect` again.
        """
        sock, self._sock = self._sock, None
        if sock is not None:
            _quietly_close(sock)
        self._parser.reset()

    @property
    def is_connected(self) -> bool:
        """Whether the socket is currently open."""
        return self._sock is not None

    @property
    def is_poisoned(self) -> bool:
        """Whether this connection's stream position is no longer known.

        Set when a :exc:`ProtocolError`, :exc:`ConnectionError`, or
        :exc:`TimeoutError` occurred during use. A server error does not poison
        a connection, because the server completed its reply normally.
        """
        return self._poisoned

    @property
    def protocol_version(self) -> int:
        """The negotiated protocol version, 2 or 3.

        Raises :exc:`RuntimeError` if read before :meth:`connect`, since there
        is no negotiated version until then. The value survives :meth:`close`,
        which reports what the last connection negotiated.
        """
        if self._protocol_version is None:
            raise RuntimeError("protocol_version is not known until connect() is called")
        return self._protocol_version

    @property
    def server_info(self) -> dict[Any, Any]:
        """The parsed ``HELLO`` response, always a dict with `bytes` keys.

        Empty when negotiation fell back to RESP2 without a usable ``HELLO``
        reply, and when ``protocol`` is 2.
        """
        return self._server_info

    @property
    def pushes_discarded(self) -> int:
        """How many push frames have been discarded on this connection.

        Monotonically increasing, and reset by :meth:`connect`. The count
        reflects frames discarded so far, not frames attributable to a given
        :meth:`execute`: a server may send the command's own reply before the
        push frame, in which case the push is still unread when `execute`
        returns and is discarded by the following call.
        """
        return self._pushes_discarded

    # -- commands ----------------------------------------------------------

    def execute(self, *args: bytes | str | int | float) -> object:
        """Send one command and read one reply.

        Arguments are encoded as a RESP array of bulk strings: ``bytes`` pass
        through, ``str`` encodes as UTF-8, ``int`` and ``float`` encode via
        ``repr``, and anything else raises :exc:`TypeError`. A ``bool`` raises
        :exc:`TypeError` rather than encoding as an integer.

        A top level error reply is converted to the matching exception and
        raised. A nested one is returned intact as an
        :class:`~resp3_wire.protocol.ErrorReply`, which is what makes a
        partially failed ``EXEC`` representable.

        Push frames arriving while this waits are discarded and reading
        continues until a non push reply arrives.

        Raises :exc:`ConnectionError` on a disconnected or poisoned connection.
        It does not reconnect implicitly; reconnection is the pool's business.
        """
        reply = self._roundtrip(args)
        error = unwrap(reply)
        if isinstance(error, ErrorReply):
            raise exception_for(error.code, error.message)
        return reply

    def pipeline(self) -> "Pipeline":
        """Return a new :class:`~resp3_wire.pipeline.Pipeline` on this connection."""
        from .pipeline import Pipeline

        return Pipeline(self)

    # -- context manager ---------------------------------------------------

    def __enter__(self) -> "Connection":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- internals ---------------------------------------------------------

    def _negotiate(self) -> None:
        """Establish the protocol version and populate ``server_info``.

        With ``protocol`` 3 this sends ``HELLO 3``, with ``SETNAME`` appended
        when a client name was configured, and reads one reply. A successful
        reply selects RESP3. A ``ServerError`` reply means the server does not
        know ``HELLO`` or does not support version 3, and is a fallback to
        RESP2 rather than an error.

        With ``protocol`` 2 no ``HELLO`` is sent at all.

        ``SELECT`` follows when ``db`` is nonzero. Under RESP3 ``SETNAME`` rode
        along with ``HELLO``; otherwise it is issued separately.
        """
        if self._protocol == 3:
            args: list[object] = ["HELLO", "3"]
            if self._client_name is not None:
                args += ["SETNAME", self._client_name]
            reply = self._roundtrip(tuple(args))
            if isinstance(unwrap(reply), ErrorReply):
                # The server does not speak HELLO, or refuses version 3.
                self._protocol_version = 2
                self._server_info = {}
            else:
                self._protocol_version = 3
                self._server_info = _as_info_dict(reply)
        else:
            self._protocol_version = 2
            self._server_info = {}

        if self._db:
            self.execute("SELECT", self._db)
        if self._client_name is not None and self._protocol_version == 2:
            # SETNAME could not ride along with a HELLO that was not sent or
            # that the server rejected.
            self.execute("CLIENT", "SETNAME", self._client_name)

    def _roundtrip(self, args: tuple[object, ...]) -> object:
        """Write one command and read one reply, without raising on an error reply.

        Negotiation needs to see a `ServerError` reply as a value so it can fall
        back, which is why this sits below :meth:`execute` rather than inside it.
        """
        if not args:
            # A zero length command is never answered, so sending one would
            # block until the socket timeout rather than fail.
            raise ValueError("execute() requires at least a command name")
        if self._sock is None:
            raise ConnectionError("connection is not connected")
        if self._poisoned:
            raise ConnectionError(
                "connection is poisoned; its stream position is unknown"
            )
        payload = _encode_command(args)
        self._send(payload)
        return self._read_reply()

    def _send(self, payload: bytes) -> None:
        assert self._sock is not None
        try:
            self._sock.sendall(payload)
        except socket.timeout as exc:
            self._poisoned = True
            raise TimeoutError("timed out writing a command") from exc
        except OSError as exc:
            self._poisoned = True
            raise ConnectionError(f"failed writing a command: {exc}") from exc

    def _read_reply(self) -> object:
        """Read until one non push reply is complete.

        Any failure here poisons the connection. A timeout in particular leaves
        an unknown number of bytes in flight, so the stream position is lost
        even though the socket is still open.
        """
        assert self._sock is not None
        while True:
            try:
                value = self._parser.gets()
            except ProtocolError:
                self._poisoned = True
                raise
            if value is NEED_MORE:
                self._parser.feed(self._recv())
                continue
            if isinstance(value, PushMessage):
                self._pushes_discarded += 1
                continue
            return value

    def _recv(self) -> bytes:
        assert self._sock is not None
        try:
            data = self._sock.recv(_RECV_SIZE)
        except socket.timeout as exc:
            self._poisoned = True
            raise TimeoutError("timed out waiting for a reply") from exc
        except OSError as exc:
            self._poisoned = True
            raise ConnectionError(f"failed reading a reply: {exc}") from exc
        if not data:
            self._poisoned = True
            raise ConnectionError("server closed the connection")
        return data


def _quietly_close(sock: socket.socket) -> None:
    """Close a socket without letting its failure surface. Used only by close()."""
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass
