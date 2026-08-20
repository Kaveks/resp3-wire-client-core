"""A single connection to a Redis server.

A connection owns one socket and one parser, negotiates its protocol version on
connect, and knows whether its stream is still trustworthy.

The parser is sans-io, so everything that touches the socket lives here. The
division is what lets the parser be driven by arbitrary byte chunks in a test
without a server, and it is why this module reads bytes and hands them over
rather than letting the parser pull.
"""

from __future__ import annotations

import select
import socket
import threading
from types import TracebackType
from typing import TYPE_CHECKING, Any, Final

from .cache import MISS, ReplyCache
from .errors import ConnectionError, ProtocolError, TimeoutError, exception_for
from .parser import NEED_MORE, RespParser
from .protocol import ErrorReply, PushMessage, unwrap

if TYPE_CHECKING:  # pragma: no cover - avoids an import cycle at runtime
    from .pipeline import Pipeline

__all__ = ["Connection"]

_RECV_SIZE = 65536

# docs/API.md section 7A.2: read-only commands with exactly one key, in
# position 1. The contract is explicit that deciding which commands are
# cacheable is a lookup table and that a lookup table is not what the
# requirement is about, so this stays small and obvious. Anything absent is
# simply not cached, which is never a failure.
_CACHEABLE: Final = frozenset({
    b"GET", b"GETRANGE", b"STRLEN", b"SUBSTR",
    b"TYPE", b"TTL", b"PTTL", b"EXPIRETIME",
    b"HGET", b"HGETALL", b"HLEN", b"HSTRLEN", b"HKEYS", b"HVALS",
    b"LRANGE", b"LLEN", b"LINDEX", b"LPOS",
    b"SMEMBERS", b"SCARD", b"SISMEMBER",
    b"ZSCORE", b"ZCARD", b"ZRANGE", b"ZRANGEBYLEX",
})

# Entered by MULTI and left by EXEC or DISCARD. Nothing inside a transaction is
# cached or served from cache: the replies are QUEUED placeholders, and EXEC's
# array is not attributable to any single key.
_MULTI_ENTER: Final = frozenset({b"MULTI"})
_MULTI_LEAVE: Final = frozenset({b"EXEC", b"DISCARD", b"RESET"})


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
        cache: ReplyCache | None = None,
        predrain: Any = None,
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
        # The pool's cache, shared across every connection it owns, and a hook
        # that drains invalidations sitting unread on the pool's *other*
        # connections. Both are None when caching is off, which is the default
        # and the configuration every other channel exercises.
        self._cache = cache
        self._predrain = predrain
        self._in_multi = False
        # Guards this socket, and only this socket. A Connection is used by one
        # thread at a time per docs/API.md section 6.4, so this is uncontended
        # in normal use; it exists so that a *peer* sweeping this connection for
        # invalidations can tell the difference between "between commands" and
        # "mid-reply" without guessing. It is never held while any pool lock is,
        # and it serialises one connection rather than command execution across
        # the pool, which is the thing section 6.4 forbids.
        self._io_lock = threading.RLock()

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
        continues until a non push reply arrives, except for ``invalidate``
        frames on a caching connection, which are consumed.

        With a pool cache configured, a read-only single-key command may be
        served from cache and its reply may populate the cache. The ordering
        below is the whole of the caching requirement and is not incidental:

          - invalidations pending anywhere in the pool are consumed *before* a
            hit is served, because the frame that makes this value stale
            usually arrives on a connection this one does not own;
          - the key's invalidation generation is recorded *before* the command
            is sent, and the reply is offered back with it afterwards, so a
            frame that arrived while the reply was in flight refuses the offer
            rather than being processed after a stale entry is already
            readable.

        Raises :exc:`ConnectionError` on a disconnected or poisoned connection.
        """
        if not args:
            raise ValueError("execute() requires at least a command name")

        cache = self._cache
        name = _encode_arg(args[0]).upper()
        key = _encode_arg(args[1]) if len(args) >= 2 else None
        cacheable = (
            cache is not None
            and key is not None
            and not self._in_multi
            and name in _CACHEABLE
        )

        encoded = b""
        generation = (0, 0)
        if cacheable:
            assert cache is not None and key is not None
            encoded = _encode_command(args)
            # Another connection may be holding an unread invalidation for this
            # key. Nothing here holds a lock while that is read.
            swept = True
            if self._predrain is not None:
                swept = self._predrain(self)
            self.drain_invalidations()
            if swept:
                hit = cache.get(encoded)
                if hit is not MISS:
                    return hit
            generation = cache.generation(key)

        reply = self._roundtrip(args)

        if name in _MULTI_ENTER:
            self._in_multi = True
        elif name in _MULTI_LEAVE:
            self._in_multi = False

        error = unwrap(reply)
        if isinstance(error, ErrorReply):
            raise exception_for(error.code, error.message)

        if cacheable:
            assert cache is not None and key is not None
            # An invalidation for this key may already be buffered behind the
            # reply. Consuming it now is what turns the offer below into a
            # refusal instead of a stale entry.
            self.drain_invalidations()
            cache.offer(encoded, key, reply, generation)
        return reply

    def drain_invalidations(self, blocking: bool = True) -> bool:
        """Consume push frames already available on this socket. Never blocks on I/O.

        Returns whether the sweep happened at all, not how much it found:
        the caller needs to know whether this socket has been accounted for, and
        finding nothing on it is an answer while not having looked is not. Used
        two ways: by a caching
        `execute` around the read it is about to trust, and by the pool to sweep
        every connection it owns, because an invalidation arrives on whichever
        connection read the key and that is rarely the one asking.

        `blocking` governs the wait for this connection's own I/O lock, not for
        the socket. A peer sweeps with `blocking=False`: if the owner is
        mid-reply the sweep is skipped, which loses nothing, because a
        connection mid-reply is already consuming its own invalidations as it
        reads. If the owner is merely holding the connection between commands,
        the sweep proceeds, which is the case that made this necessary.

        Anything readable while the lock is held is out of band by construction.
        A command reply appearing instead means the stream is desynchronised,
        which poisons the connection rather than being ignored.
        """
        if self._sock is None or self._poisoned or self._cache is None:
            return True
        if not self._io_lock.acquire(blocking):
            return False
        try:
            self._drain_locked()
        finally:
            self._io_lock.release()
        return True

    def _drain_locked(self) -> int:
        if self._sock is None or self._poisoned:
            return 0
        seen = 0
        while True:
            try:
                value = self._parser.gets()
            except ProtocolError:
                self._poisoned = True
                raise
            if value is NEED_MORE:
                if self._parser.has_buffered_input:
                    # Half a frame is already in. TCP split it, and the rest is
                    # on its way: the server has committed to sending it. Giving
                    # up here leaves the invalidation unprocessed and the entry
                    # it should have evicted readable, which is how a purely
                    # sequential case was observed serving a stale value.
                    data = self._recv_rest_of_frame()
                else:
                    data = self._recv_ready()
                if not data:
                    return seen
                self._parser.feed(data)
                continue
            if isinstance(value, PushMessage):
                self._handle_push(value)
                seen += 1
                continue
            self._poisoned = True
            raise ProtocolError(
                f"a command reply arrived with nothing waiting for it: {value!r}"
            )

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

        if self._cache is not None:
            # docs/API.md section 7A.3. After negotiation and before any
            # command, so no reply can be cached before the server is willing to
            # tell us it went stale.
            self._roundtrip(("CLIENT", "TRACKING", "ON"))
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
        with self._io_lock:
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
                self._handle_push(value)
                continue
            return value

    def _handle_push(self, message: PushMessage) -> None:
        """Consume an invalidation, or discard any other push frame.

        docs/API.md section 7A.4: an `invalidate` frame is consumed rather than
        discarded and does not increment `pushes_discarded`; any other push
        frame still does.
        """
        if self._cache is not None and message.kind == "invalidate":
            payload = message.data[0] if message.data else None
            if payload is None:
                # A null in place of the key array means drop everything, which
                # Redis sends on FLUSHALL and on tracking table overflow.
                self._cache.invalidate_all()
            else:
                keys = payload if isinstance(payload, list) else [payload]
                self._cache.invalidate(
                    [k for k in keys if isinstance(k, (bytes, bytearray))]
                )
            return
        self._pushes_discarded += 1

    def _recv_rest_of_frame(self) -> bytes:
        """Wait for the remainder of a frame already part-way in.

        Blocking is correct here in a way it is not in `_recv_ready`: the bytes
        are not speculative. The socket's own timeout still bounds it, so a
        server that dies mid-frame poisons the connection rather than hanging it.
        """
        assert self._sock is not None
        try:
            data = self._sock.recv(_RECV_SIZE)
        except socket.timeout as exc:
            self._poisoned = True
            raise TimeoutError("timed out mid-frame while draining") from exc
        except OSError as exc:
            self._poisoned = True
            raise ConnectionError(f"failed reading while draining: {exc}") from exc
        if not data:
            self._poisoned = True
            raise ConnectionError("server closed the connection mid-frame")
        return data

    def _recv_ready(self) -> bytes:
        """Whatever is already readable, or empty bytes if nothing is.

        `select` with a zero timeout rather than a non-blocking recv, so the
        socket's own timeout setting is left alone: it belongs to the blocking
        reads that command execution depends on.
        """
        assert self._sock is not None
        try:
            ready, _, _ = select.select([self._sock], [], [], 0)
            if not ready:
                return b""
            data = self._sock.recv(_RECV_SIZE)
        except OSError as exc:
            self._poisoned = True
            raise ConnectionError(f"failed reading while draining: {exc}") from exc
        if not data:
            self._poisoned = True
            raise ConnectionError("server closed the connection")
        return data

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
