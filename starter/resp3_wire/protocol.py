"""Value types produced by the parser.

Everything the parser produces is either a Python builtin or one of the four
types defined here. This module performs no I/O and imports nothing that does.

Three of these types exist because RESP3 carries information that has no
builtin counterpart: an attribute frame decorates a value, a verbatim string
names its own format, and a push frame arrives out of band. The fourth,
:class:`ErrorReply`, exists because an error can occupy a value position
inside an aggregate, where raising would make the surrounding reply
unrepresentable.
"""

from __future__ import annotations

from typing import Any

__all__ = ["Attributed", "VerbatimBytes", "ErrorReply", "PushMessage", "unwrap"]


class Attributed:
    """A value that arrived decorated by a RESP3 attribute frame.

    An attribute frame ``|N\\r\\n`` is followed by ``2N`` values forming its
    dictionary and then by the value it decorates. The dictionary is metadata
    about that value, not a reply in its own right, and must never be emitted
    as one.

    Equality delegates to the wrapped value, in both directions::

        Attributed(b"x", {b"k": b"v"}) == b"x"        -> True
        b"x" == Attributed(b"x", {b"k": b"v"})        -> True

    Attributes never take part in the comparison. Two instances wrapping equal
    values are equal whatever their attribute dictionaries hold::

        Attributed(b"x", {}) == Attributed(b"x", {b"other": b"v"})  -> True

    ``__hash__`` delegates as well, so an instance is interchangeable with its
    bare value as a dict key and as a set member::

        hash(Attributed(b"x", {})) == hash(b"x")
        {Attributed(b"x", {})} == {b"x"}
        {b"x": 1}[Attributed(b"x", {})] == 1

    Where the wrapped value is unhashable, hashing raises :exc:`TypeError`,
    which is what hashing the bare value would do.

    ``__repr__`` shows both the value and the attributes, since a bare value
    repr would hide the reason this wrapper exists.

    Implement ``__eq__``, ``__hash__``, and ``__repr__``. Note that defining
    ``__eq__`` on a class sets its ``__hash__`` to ``None`` unless you define
    ``__hash__`` too, which would make every instance unhashable.

    The cost of this design is that ``isinstance(x, bytes)`` is false for a
    decorated bulk string. Callers that need the bare value use ``x.value`` or
    :func:`unwrap`.
    """

    value: Any
    attributes: dict[Any, Any]

    def __init__(self, value: Any, attributes: dict[Any, Any]) -> None:
        self.value = value
        self.attributes = attributes


class VerbatimBytes(bytes):
    """A blob string that named its own format.

    RESP3 writes a verbatim string as ``=<len>\\r\\n<fmt>:<payload>\\r\\n``,
    where the declared length counts the three character format, the colon,
    and the payload. For ``=15\\r\\ntxt:Some string\\r\\n`` the length 15
    covers ``txt:`` plus the eleven byte payload.

    The format and its colon are stripped from the payload, so an instance
    compares equal to the plain bytes it carries::

        VerbatimBytes(b"Some string", format="txt") == b"Some string"  -> True

    ``format`` is a :class:`str` rather than :class:`bytes` because it is
    always three ASCII characters and is metadata rather than payload.

    A verbatim string whose declared length is under four bytes, or whose
    fourth byte is not a colon, is malformed.
    """

    format: str

    def __new__(cls, data: bytes = b"", format: str = "txt") -> "VerbatimBytes":
        obj = super().__new__(cls, data)
        obj.format = format
        return obj


class ErrorReply:
    """A server error occupying a value position.

    This is a value, not an exception, and the parser never raises it. Both
    the ``-`` and ``!`` wire forms produce it, by identical rules; the
    distinction between them carries nothing actionable and is not preserved.

    ``code`` is the first whitespace delimited token of the error string,
    uppercased, for example ``WRONGTYPE``. ``message`` is the full error
    string, including that token. Both are :class:`str`, decoded as UTF-8 with
    surrogate escaping, because error text is protocol level diagnostic rather
    than user payload.

    A caller normally never sees one of these at the top level of a reply,
    because :meth:`Connection.execute` converts a top level error into the
    matching exception and raises it. Nested ones survive, which is what makes
    a partially failed ``EXEC`` representable.
    """

    code: str
    message: str

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message


class PushMessage:
    """A RESP3 out of band ``>`` frame.

    Redis emits these for client side caching invalidation, pubsub delivery,
    and monitor output. They can arrive at any point in the stream, including
    between a command being written and its reply arriving.

    ``kind`` is the first element of the frame decoded as UTF-8, for example
    ``invalidate`` or ``message``. ``data`` is the remaining elements, parsed
    by the ordinary rules.

    A push frame is a complete reply in its own right. It is never merged into
    the reply of a pending command and it is never wrapped by an attribute. A
    ``>0\\r\\n`` frame is malformed, since there is no kind to read.

    Equality compares both fields. Unlike :class:`Attributed`, this type
    delegates nothing.

    Implement ``__eq__`` and ``__repr__``.
    """

    kind: str
    data: list[Any]

    def __init__(self, kind: str, data: list[Any]) -> None:
        self.kind = kind
        self.data = data


def unwrap(value: object) -> object:
    """Return the value inside an :class:`Attributed`, or the value unchanged.

    Does not recurse into containers: unwrapping a list returns the same list,
    with any decorated elements still decorated.
    """
    raise NotImplementedError("unwrap")
