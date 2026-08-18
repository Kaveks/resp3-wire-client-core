"""Command pipelining over a single connection.

What makes this a pipeline rather than a loop is that every queued command is
written before any reply is read. The number of write syscalls is not the
point and is not observable; the ordering is.
"""

from __future__ import annotations

from .connection import Connection, _encode_command
from .errors import ConnectionError, exception_for
from .protocol import ErrorReply, unwrap

__all__ = ["Pipeline"]


class Pipeline:
    """Queues commands, sends them together, and reads their replies in order.

    Obtained either directly or, preferably, from :meth:`Connection.pipeline`::

        results = conn.pipeline().push("SET", "k", "v").push("GET", "k").execute()

    ``MULTI`` and ``EXEC`` are ordinary commands here. This package provides no
    transaction abstraction: a caller pipelines ``MULTI``, the queued commands,
    and ``EXEC``, and receives ``EXEC``'s array as one element of the result
    list.
    """

    def __init__(self, conn: Connection) -> None:
        self._conn = conn
        # Commands are encoded as they are pushed, so a bad argument is
        # rejected at the call site that supplied it rather than later, when
        # the offending command is no longer identifiable.
        self._commands: list[bytes] = []

    def __repr__(self) -> str:
        return f"<Pipeline queued={len(self._commands)}>"

    def push(self, *args: bytes | str | int | float) -> "Pipeline":
        """Queue one command and return ``self``, so calls chain.

        Encodes and buffers. Performs no I/O and never blocks. Arguments are
        encoded by the same rules as :meth:`Connection.execute`, including the
        rejection of ``bool``.

        Queueing a command with no arguments raises :exc:`ValueError`, for the
        same reason :meth:`Connection.execute` does: Redis sends no reply to an
        empty command array, so the batch would be one reply short and
        :meth:`execute` would block waiting for it.
        """
        if not args:
            raise ValueError("push() requires at least a command name")
        self._commands.append(_encode_command(args))
        return self

    def execute(self) -> list[object]:
        """Flush the queue, read every reply, and return them in queue order.

        Every buffered command is written before any reply is read, then
        exactly as many replies as there were commands are read back. An empty
        pipeline returns ``[]`` and performs no I/O. The queue is cleared, so a
        pipeline is reusable.

        A server error for an individual command does not raise. It occupies
        that command's slot in the result list as the matching exception
        instance::

            results = pipe.push("SET", "k", "v").push("LPUSH", "k", "v").execute()
            # results[0] == b"OK"
            # isinstance(results[1], WrongTypeError)

        A :exc:`ProtocolError`, :exc:`ConnectionError`, or :exc:`TimeoutError`
        raises from here and poisons the connection, because reply alignment is
        lost and no remaining result can be trusted.

        Push frames arriving mid-pipeline are discarded and do not consume a
        reply slot.

        The asymmetry against nested errors is deliberate. A pipeline slot
        carries an exception; an error nested inside an aggregate, such as
        inside ``EXEC``'s array, stays an
        :class:`~resp3_wire.protocol.ErrorReply` value. The first is a command
        that failed, the second is a value that happens to describe a failure.
        """
        if not self._commands:
            return []
        if not self._conn.is_connected:
            raise ConnectionError("connection is not connected")
        if self._conn.is_poisoned:
            raise ConnectionError(
                "connection is poisoned; its stream position is unknown"
            )

        # Consume the queue before any I/O, so a failure cannot leave commands
        # buffered that a later execute() would resend against a stream whose
        # position is already lost.
        commands, self._commands = self._commands, []

        self._conn._send(b"".join(commands))

        results: list[object] = []
        for _ in range(len(commands)):
            reply = self._conn._read_reply()
            error = unwrap(reply)
            if isinstance(error, ErrorReply):
                results.append(exception_for(error.code, error.message))
            else:
                results.append(reply)
        return results

    def reset(self) -> None:
        """Discard queued commands without sending them."""
        self._commands.clear()

    def __len__(self) -> int:
        """How many commands are queued."""
        return len(self._commands)
