"""The incremental RESP2 and RESP3 parser.

This module is sans-io. It never touches a socket, never blocks, and is driven
entirely by byte chunks handed to it. It imports nothing from ``socket``,
``select``, ``asyncio``, ``ssl``, or ``subprocess``, and that property is
checked structurally against this module's abstract syntax tree.

The parser accepts every type byte from both protocol versions regardless of
what was negotiated. A RESP2 connection simply never receives a ``,`` or ``%``
frame, and enforcing the mode here would add a failure path that buys nothing.

A parser instance is not thread safe and belongs to exactly one connection.
"""

from __future__ import annotations

from typing import Final

__all__ = ["RespParser", "NEED_MORE"]


class _NeedMore:
    """The type of :data:`NEED_MORE`. Not instantiated by callers."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "NEED_MORE"


NEED_MORE: Final[_NeedMore] = _NeedMore()
"""Returned by :meth:`RespParser.gets` when no complete reply is buffered.

A unique sentinel, distinguishable from every legitimate reply value. It is
deliberately not ``None``, because ``None`` is itself a legitimate reply: it is
what ``_\\r\\n``, ``$-1\\r\\n``, and ``*-1\\r\\n`` all produce.
"""


class RespParser:
    """Turns byte chunks into reply values.

    The usage pattern is feed, then drain::

        parser.feed(chunk)
        while (value := parser.gets()) is not NEED_MORE:
            handle(value)

    The invariant that governs this class is that the result must not depend on
    where the chunk boundaries fell. For any byte sequence and any partition of
    it, feeding the chunks in order with a drain after each must produce the
    same sequence of values as feeding the whole sequence and draining once.

    That holds with no exceptions: one byte at a time, a split between the CR
    and the LF of a terminator, a split inside the digits of a length prefix, a
    split inside a verbatim string's format prefix, and splits at arbitrary
    depths of nesting. Metadata survives too, so a :class:`VerbatimBytes` keeps
    its format and an :class:`Attributed` keeps its attributes wherever the
    boundaries land.

    Two design consequences follow, and both are worth deciding before writing
    any code. First, :meth:`gets` must carry its progress across calls rather
    than reparsing a frame from the start each time it is called, or the cost
    of parsing one frame becomes quadratic in the number of chunks it arrived
    in. Second, the buffer must not be recopied on every consume, for the same
    reason.
    """

    def __init__(self) -> None:
        """Create a parser with an empty buffer and no partial frame."""

    def feed(self, data: bytes) -> None:
        """Append bytes to the internal buffer.

        Never blocks, never raises on incomplete input, and accepts a chunk of
        any size including empty. Feeding does not parse; parsing happens in
        :meth:`gets`.
        """
        raise NotImplementedError("RespParser.feed")

    def gets(self) -> object:
        """Return the next complete reply, or :data:`NEED_MORE`.

        Callers drain by calling this repeatedly until it returns
        :data:`NEED_MORE`. Returning that sentinel must leave the parser able
        to resume from exactly where it stopped once more bytes arrive; no
        input may be consumed unless the value it belongs to has been produced.

        An attribute frame is never returned. It decorates the value that
        follows it at the same depth, and if no such value has arrived yet the
        input is incomplete and this returns :data:`NEED_MORE`.

        A push frame is returned like any other complete reply. Filtering
        pushes is a caller's concern, not this class's.

        Raises :exc:`ProtocolError` on malformed wire data. It never raises on
        a server error reply, which is a value.
        """
        raise NotImplementedError("RespParser.gets")

    def reset(self) -> None:
        """Discard all buffered state and return to the initial condition.

        Used when a connection is discarded mid-reply. Any partially received
        frame, any pending attribute, and any unconsumed bytes are dropped, and
        the memory backing them is released.
        """
        raise NotImplementedError("RespParser.reset")
