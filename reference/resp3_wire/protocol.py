"""Value types produced by the parser.

Everything the parser produces is either a Python builtin or one of the four
types defined here. This module performs no I/O and imports nothing that does.

Three of these types exist because RESP3 carries information that has no
builtin counterpart: an attribute frame decorates a value, a verbatim string
names its own format, and a push frame arrives out of band. The fourth,
:class:`ErrorReply`, exists because an error can occupy a value position inside
an aggregate, where raising would make the surrounding reply unrepresentable.
"""

from __future__ import annotations

from typing import Any

__all__ = ["Attributed", "VerbatimBytes", "ErrorReply", "PushMessage", "unwrap"]


class Attributed:
    """A value that arrived decorated by a RESP3 attribute frame.

    Equality delegates to the wrapped value, in both directions, and ignores
    the attributes entirely::

        Attributed(b"x", {b"k": b"v"}) == b"x"                      -> True
        b"x" == Attributed(b"x", {b"k": b"v"})                      -> True
        Attributed(b"x", {}) == Attributed(b"x", {b"other": b"v"})  -> True

    Hashing delegates too, so an instance is interchangeable with its bare
    value as a dict key and as a set member. Where the wrapped value is
    unhashable, hashing raises :exc:`TypeError`, which is what hashing the bare
    value would do.

    The cost is that ``isinstance(x, bytes)`` is false for a decorated bulk
    string. Callers that need the bare value use ``x.value`` or :func:`unwrap`.
    """

    __slots__ = ("value", "attributes")

    value: Any
    attributes: dict[Any, Any]

    def __init__(self, value: Any, attributes: dict[Any, Any]) -> None:
        self.value = value
        self.attributes = attributes

    def __eq__(self, other: object) -> bool:
        # Delegating in both directions. Python tries the reflected form when
        # the left operand's __eq__ returns NotImplemented, which is how
        # b"x" == Attributed(b"x", ...) reaches this method.
        if isinstance(other, Attributed):
            other = other.value
        return bool(self.value == other)

    def __ne__(self, other: object) -> bool:
        return not self.__eq__(other)

    def __hash__(self) -> int:
        # Deliberately not guarded. An unhashable wrapped value must make the
        # wrapper unhashable, with the TypeError the bare value would raise.
        return hash(self.value)

    def __repr__(self) -> str:
        return f"Attributed({self.value!r}, {self.attributes!r})"


class VerbatimBytes(bytes):
    """A blob string that named its own format.

    RESP3 writes a verbatim string as ``=<len>\\r\\n<fmt>:<payload>\\r\\n``,
    where the declared length counts the three character format, the colon, and
    the payload. The format and its colon are stripped, so an instance compares
    equal to the plain bytes it carries::

        VerbatimBytes(b"Some string", format="txt") == b"Some string"  -> True

    ``format`` is a :class:`str` rather than :class:`bytes` because it is
    always three ASCII characters and is metadata rather than payload.
    """

    # A nonempty __slots__ is not permitted on a subclass of a variable length
    # builtin, so instances carry a dict for the one attribute.

    format: str

    def __new__(cls, data: bytes = b"", format: str = "txt") -> "VerbatimBytes":
        obj = super().__new__(cls, data)
        obj.format = format
        return obj

    def __repr__(self) -> str:
        return f"VerbatimBytes({bytes(self)!r}, format={self.format!r})"


class ErrorReply:
    """A server error occupying a value position.

    This is a value, not an exception, and the parser never raises it. Both the
    ``-`` and ``!`` wire forms produce it by identical rules; the distinction
    between them carries nothing actionable and is not preserved.

    ``code`` is the first whitespace delimited token of the error string,
    uppercased. ``message`` is the full error string including that token. Both
    are :class:`str`, decoded as UTF-8 with surrogate escaping, because error
    text is protocol level diagnostic rather than user payload.
    """

    __slots__ = ("code", "message")

    code: str
    message: str

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ErrorReply):
            return NotImplemented
        return self.code == other.code and self.message == other.message

    def __hash__(self) -> int:
        return hash((self.code, self.message))

    def __repr__(self) -> str:
        return f"ErrorReply({self.code!r}, {self.message!r})"


class PushMessage:
    """A RESP3 out of band ``>`` frame.

    ``kind`` is the first element of the frame decoded as UTF-8, for example
    ``invalidate`` or ``message``. ``data`` is the remaining elements, parsed
    by the ordinary rules.

    A push frame is a complete reply in its own right. It is never merged into
    the reply of a pending command and it is never wrapped by an attribute.

    Equality compares both fields. Unlike :class:`Attributed`, this type
    delegates nothing, because no value it could delegate to exists.
    """

    __slots__ = ("kind", "data")

    kind: str
    data: list[Any]

    def __init__(self, kind: str, data: list[Any]) -> None:
        self.kind = kind
        self.data = data

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, PushMessage):
            return NotImplemented
        return self.kind == other.kind and self.data == other.data

    # A push frame carries a list and is therefore unhashable, which defining
    # __eq__ already implies. Stated explicitly so it is not read as an
    # oversight.
    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return f"PushMessage({self.kind!r}, {self.data!r})"


def unwrap(value: object) -> object:
    """Return the value inside an :class:`Attributed`, or the value unchanged.

    Does not recurse into containers: unwrapping a list returns the same list,
    with any decorated elements still decorated.
    """
    if isinstance(value, Attributed):
        return value.value
    return value
