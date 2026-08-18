"""Command pipelining over a single connection."""

from __future__ import annotations

from .connection import Connection

__all__ = ["Pipeline"]


class Pipeline:
    """Queues commands, sends them together, and reads their replies in order.

    Obtained either directly or, preferably, from
    :meth:`Connection.pipeline`::

        results = conn.pipeline().push("SET", "k", "v").push("GET", "k").execute()

    ``MULTI`` and ``EXEC`` are ordinary commands here. This package provides no
    transaction abstraction: a caller pipelines ``MULTI``, the queued commands,
    and ``EXEC``, and receives ``EXEC``'s array as one element of the result
    list.
    """

    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def push(self, *args: bytes | str | int | float) -> "Pipeline":
        """Queue one command and return ``self``, so calls chain.

        Encodes and buffers. Performs no I/O and never blocks. Arguments are
        encoded by the same rules as :meth:`Connection.execute`.
        """
        raise NotImplementedError("Pipeline.push")

    def execute(self) -> list[object]:
        """Flush the queue, read every reply, and return them in queue order.

        Writes every buffered command in one write where the OS permits, then
        reads exactly as many replies as there were commands. An empty
        pipeline returns ``[]`` and performs no I/O. The queue is cleared, so
        a pipeline is reusable.

        A server error for an individual command does not raise. It appears in
        the result list as the matching exception instance, which lets a
        caller inspect per command outcomes::

            results = pipe.push("SET", "k", "v").push("LPUSH", "k", "v").execute()
            # results[0] == b"OK"
            # isinstance(results[1], WrongTypeError)

        A :exc:`ProtocolError`, :exc:`ConnectionError`, or :exc:`TimeoutError`
        raises from here and poisons the connection, because reply alignment
        is lost and no remaining result can be trusted.

        Push frames arriving mid-pipeline are discarded and do not consume a
        reply slot.

        Note the asymmetry against nested errors, which is deliberate:
        pipeline slots carry exceptions, while error values nested inside an
        aggregate, such as inside ``EXEC``'s array, stay
        :class:`~resp3_wire.protocol.ErrorReply` values.
        """
        raise NotImplementedError("Pipeline.execute")

    def reset(self) -> None:
        """Discard queued commands without sending them."""
        raise NotImplementedError("Pipeline.reset")

    def __len__(self) -> int:
        """How many commands are queued."""
        raise NotImplementedError("Pipeline.__len__")
