"""Attack: answer from a table instead of reading the wire.

The connection is real, so the pool's bookkeeping and the server's view of the
connection count are both genuine, and commands are written so the server sees
them. Nothing is ever read back. Every reply comes from a table keyed on the
command name.

The table carries everything a table can carry: the replies that do not depend
on server state. `PING` is always `PONG`, `SET` is always `OK`, and `ECHO`
returns its own argument, which is exactly the tagging the pool channel uses to
detect cross-talk. `CLIENT ID` is faked with a per-connection counter, which is
what the distinct-connection case counts.

What a table cannot carry is any reply that depends on what the server holds,
which is what randomised keys and run-time expectations are for.
"""

from __future__ import annotations

import itertools
import os
import socket
import threading
from collections import deque
from types import TracebackType
from typing import TYPE_CHECKING, Any

from .errors import ConnectionError, TimeoutError, exception_for
from .parser import NEED_MORE, RespParser
from .protocol import ErrorReply, unwrap

if TYPE_CHECKING:  # pragma: no cover
    from .pipeline import Pipeline

__all__ = ["Connection"]

_IDS = itertools.count(1000)
_ID_LOCK = threading.Lock()


def _log(note: str) -> None:
    path = os.environ.get("RESP3_ATTACK_LOG")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(note + "\n")
    except OSError:
        pass


def _encode_arg(value: object) -> bytes:
    if value is True or value is False:
        raise TypeError("bool is not a valid command argument")
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    if isinstance(value, (int, float)):
        return repr(value).encode("ascii")
    raise TypeError(f"cannot encode {type(value).__name__}")


def _encode_command(args: tuple[object, ...]) -> bytes:
    out = bytearray(b"*%d\r\n" % len(args))
    for value in args:
        payload = _encode_arg(value)
        out += b"$%d\r\n%s\r\n" % (len(payload), payload)
    return bytes(out)


def _decode_command(payload: bytes) -> list[list[bytes]]:
    """Recover commands from an encoded batch, so pipelines can be faked too."""
    commands: list[list[bytes]] = []
    pos = 0
    while pos < len(payload):
        end = payload.find(b"\r\n", pos)
        if end < 0 or payload[pos:pos + 1] != b"*":
            break
        count = int(payload[pos + 1:end])
        pos = end + 2
        args: list[bytes] = []
        for _ in range(count):
            end = payload.find(b"\r\n", pos)
            size = int(payload[pos + 1:end])
            pos = end + 2
            args.append(payload[pos:pos + size])
            pos += size + 2
        commands.append(args)
    return commands


class Connection:
    """A socket that is written to and never read from."""

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
        self._sock: socket.socket | None = None
        self._parser = RespParser()
        self._protocol_version: int | None = None
        self._server_info: dict[Any, Any] = {}
        self._poisoned = False
        self._pushes_discarded = 0
        self._queued: deque = deque()
        with _ID_LOCK:
            self._fake_id = next(_IDS)

    # -- lifecycle ---------------------------------------------------------

    def connect(self) -> None:
        if self._sock is not None:
            return
        try:
            self._sock = socket.create_connection(
                (self._host, self._port), timeout=self._timeout
            )
        except OSError as exc:
            raise ConnectionError(f"could not connect: {exc}") from exc
        self._protocol_version = self._protocol
        self._server_info = (
            {b"server": b"redis", b"version": b"7.4.10", b"proto": 3}
            if self._protocol == 3 else {}
        )
        self._poisoned = False
        _log("answering from a table; no reply is ever read from the socket")

    def close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        self._parser.reset()

    @property
    def is_connected(self) -> bool:
        return self._sock is not None

    @property
    def is_poisoned(self) -> bool:
        return self._poisoned

    @property
    def protocol_version(self) -> int:
        if self._protocol_version is None:
            raise RuntimeError("protocol_version is not known until connect()")
        return self._protocol_version

    @property
    def server_info(self) -> dict[Any, Any]:
        return self._server_info

    @property
    def pushes_discarded(self) -> int:
        return self._pushes_discarded

    # -- the table ---------------------------------------------------------

    def _canned(self, args: list[bytes]) -> Any:
        name = args[0].upper()
        rest = args[1:]
        if name == b"PING":
            return b"PONG"
        if name == b"ECHO":
            return rest[0] if rest else b""
        if name in (b"SET", b"SELECT", b"WATCH", b"MULTI", b"DISCARD", b"RESET"):
            return b"OK"
        if name == b"HELLO":
            return dict(self._server_info)
        if name == b"CLIENT":
            sub = rest[0].upper() if rest else b""
            if sub == b"ID":
                return self._fake_id
            if sub == b"KILL":
                return 1
            return b"OK"
        if name in (b"UNLINK", b"DEL", b"EXISTS"):
            return 0
        if name in (b"INCR", b"STRLEN", b"HSET", b"SADD", b"RPUSH", b"ZADD"):
            return 1
        if name == b"DEBUG":
            return b"OK"
        if name == b"TYPE":
            return b"string"
        if name == b"EXEC":
            return []
        return None

    # -- commands ----------------------------------------------------------

    def execute(self, *args: bytes | str | int | float) -> object:
        if not args:
            raise ValueError("execute() requires at least a command name")
        if self._sock is None:
            raise ConnectionError("connection is not connected")
        if self._poisoned:
            raise ConnectionError("connection is poisoned")
        encoded = [_encode_arg(a) for a in args]
        self._write(_encode_command(tuple(args)))
        reply = self._canned(encoded)
        error = unwrap(reply)
        if isinstance(error, ErrorReply):
            raise exception_for(error.code, error.message)
        return reply

    def _write(self, payload: bytes) -> None:
        assert self._sock is not None
        try:
            self._sock.sendall(payload)
        except OSError as exc:
            self._poisoned = True
            raise ConnectionError(f"failed writing: {exc}") from exc

    # Pipeline drives these two directly, so the batch is faked the same way.
    def _send(self, payload: bytes) -> None:
        if self._sock is None:
            raise ConnectionError("connection is not connected")
        self._write(payload)
        for command in _decode_command(payload):
            self._queued.append(self._canned(command))

    def _read_reply(self) -> object:
        if not self._queued:
            raise ConnectionError("nothing queued")
        return self._queued.popleft()

    def pipeline(self) -> "Pipeline":
        from .pipeline import Pipeline

        return Pipeline(self)

    def __enter__(self) -> "Connection":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
