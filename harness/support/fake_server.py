"""A scripted TCP server for the negotiation cases.

`docs/HARNESS.md` section 3.2 allocates three cases to negotiation paths that a
real Redis cannot produce: a server that rejects `HELLO`, a server that answers
it with a flat array instead of a map, and the absence of any `HELLO` at all
under `protocol=2`. Redis 7.4 answers `HELLO 3` correctly and always will, so
these are driven against a server written for the purpose.

The expectations come from `docs/API.md` section 5, not from redis-py. That is
the point: redis-py has no more access to a server that refuses `HELLO` than the
harness does.

The server reads whole RESP commands rather than whatever a `recv` happens to
return, so a case never depends on how the client's writes were segmented.
"""

from __future__ import annotations

import socket
import threading
from types import TracebackType
from typing import Callable

__all__ = ["ScriptedServer", "read_command"]


def read_command(sock: socket.socket, buffer: bytearray) -> list[bytes] | None:
    """Read one complete RESP array of bulk strings, or None at end of stream.

    `buffer` carries bytes between calls, so a command split across two packets
    is assembled rather than truncated.
    """

    def fill() -> bool:
        chunk = sock.recv(65536)
        if not chunk:
            return False
        buffer.extend(chunk)
        return True

    def line() -> bytes | None:
        while True:
            index = buffer.find(b"\r\n")
            if index >= 0:
                out = bytes(buffer[:index])
                del buffer[: index + 2]
                return out
            if not fill():
                return None

    header = line()
    if header is None:
        return None
    if not header.startswith(b"*"):
        raise AssertionError(f"client wrote a non-array command header: {header!r}")
    args: list[bytes] = []
    for _ in range(int(header[1:])):
        size_line = line()
        if size_line is None:
            return None
        size = int(size_line[1:])
        while len(buffer) < size + 2:
            if not fill():
                return None
        args.append(bytes(buffer[:size]))
        del buffer[: size + 2]
    return args


class ScriptedServer:
    """A one-connection server that answers commands from a handler.

    `handler(args, index)` returns the raw bytes to write for the `index`-th
    command, or None to write nothing. Every command the client sent is recorded
    in `commands`, so a case can assert on what was *not* written as easily as
    on what was.

    Used as a context manager; the listening socket and the thread are both
    closed on exit.
    """

    def __init__(self, handler: Callable[[list[bytes], int], bytes | None]) -> None:
        self._handler = handler
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port: int = int(self._sock.getsockname()[1])
        self.commands: list[list[bytes]] = []
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._stopped = threading.Event()

    def _serve(self) -> None:
        try:
            conn, _ = self._sock.accept()
        except OSError:
            return
        buffer = bytearray()
        with conn:
            conn.settimeout(10.0)
            while not self._stopped.is_set():
                try:
                    args = read_command(conn, buffer)
                except (OSError, ValueError):
                    return
                if args is None:
                    return
                index = len(self.commands)
                self.commands.append(args)
                try:
                    reply = self._handler(args, index)
                except Exception:  # noqa: BLE001 - the case asserts, not the server
                    return
                if reply:
                    try:
                        conn.sendall(reply)
                    except OSError:
                        return

    def __enter__(self) -> "ScriptedServer":
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._stopped.set()
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=5.0)

    def command_names(self) -> list[bytes]:
        """The uppercased first word of every command the client wrote."""
        return [c[0].upper() for c in self.commands if c]
