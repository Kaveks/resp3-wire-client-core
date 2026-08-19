"""A tagged encoding for reply values crossing a process boundary.

The harness interpreter imports the client package and must not have redis-py
on its path, so the oracle's expected values are produced by a separate
interpreter and travel here as JSON. Plain JSON would lose exactly what the
comparator cares about: bytes would become text, a bool would be
indistinguishable from an int once round-tripped through some encoders, and
`inf` has no JSON literal.

Every value therefore carries its type class as a tag, which is the same
information `support/compare.py` classifies on. Decoding rebuilds real Python
values, so the comparator sees `bytes`, `int`, `float`, `list`, `dict`, `set`,
and `None`, not a parallel representation of them.

This module is imported by both interpreters and depends only on the standard
library.
"""

from __future__ import annotations

import base64
from typing import Any

__all__ = ["encode_value", "decode_value", "encode_args", "decode_args", "RedisPyError"]


class RedisPyError:
    """An error redis-py raised, reduced to the only field that compares.

    Per D11, exception class identity cannot cross the library boundary and
    message text is redis-py's own concern, so only the code survives.
    """

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        self.code = code

    def __repr__(self) -> str:
        return f"RedisPyError({self.code!r})"


def encode_value(value: Any, code_of: Any = None) -> Any:
    """Encode a reply value from redis-py into a tagged JSON-safe structure.

    `code_of` extracts the error code from an exception. The backend supplies
    one that consults redis-py's own exception table, because the message text
    alone does not carry the code for every error. Nested errors inside an
    EXEC array reach this function too, so the extractor must be threaded
    through rather than applied only at the top level.
    """
    if value is None:
        return ["none"]
    if value is True or value is False:
        return ["bool", value]
    if isinstance(value, int):
        # Big numbers exceed what some JSON readers keep exact, so integers
        # travel as text and are rebuilt with Python's arbitrary precision.
        return ["int", str(value)]
    if isinstance(value, float):
        # repr round-trips exactly, including inf and -inf.
        return ["float", repr(value)]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return ["bytes", base64.b64encode(bytes(value)).decode("ascii")]
    if isinstance(value, str):
        return ["bytes", base64.b64encode(value.encode("utf-8")).decode("ascii")]
    if isinstance(value, (set, frozenset)):
        return ["set", [encode_value(v, code_of) for v in value]]
    if isinstance(value, (list, tuple)):
        return ["list", [encode_value(v, code_of) for v in value]]
    if isinstance(value, dict):
        return ["dict", [[encode_value(k, code_of), encode_value(v, code_of)]
                         for k, v in value.items()]]
    if isinstance(value, BaseException):
        return ["exc", code_of(value) if code_of else _code_of(value)]
    return ["other", type(value).__name__, repr(value)]


def decode_value(node: Any) -> Any:
    """Rebuild a real Python value from :func:`encode_value`'s output."""
    tag = node[0]
    if tag == "none":
        return None
    if tag == "bool":
        return bool(node[1])
    if tag == "int":
        return int(node[1])
    if tag == "float":
        return float(node[1])
    if tag == "bytes":
        return base64.b64decode(node[1].encode("ascii"))
    if tag == "list":
        return [decode_value(v) for v in node[1]]
    if tag == "set":
        return {decode_value(v) for v in node[1]}
    if tag == "dict":
        return {decode_value(k): decode_value(v) for k, v in node[1]}
    if tag == "exc":
        return RedisPyError(node[1])
    return _Opaque(node[1], node[2])


class _Opaque:
    """A value neither side has a representation for. Always a divergence."""

    __slots__ = ("type_name", "text")

    def __init__(self, type_name: str, text: str) -> None:
        self.type_name = type_name
        self.text = text

    def __repr__(self) -> str:
        return f"<opaque {self.type_name} {self.text}>"


def _code_of(exc: BaseException) -> str:
    """The error code of a redis-py exception: the first token, uppercased."""
    text = str(exc)
    parts = text.split(None, 1)
    return parts[0].upper() if parts else ""


def encode_args(args: tuple[Any, ...]) -> list[Any]:
    """Encode command arguments for transport to the backend."""
    out = []
    for a in args:
        if isinstance(a, bytes):
            out.append(["b", base64.b64encode(a).decode("ascii")])
        elif isinstance(a, str):
            out.append(["s", a])
        elif isinstance(a, bool):
            raise TypeError("bool is not a valid command argument")
        elif isinstance(a, int):
            out.append(["i", str(a)])
        elif isinstance(a, float):
            out.append(["f", repr(a)])
        else:
            raise TypeError(f"cannot encode argument of type {type(a).__name__}")
    return out


def decode_args(nodes: list[Any]) -> list[Any]:
    """Rebuild command arguments in the backend interpreter."""
    out = []
    for tag, raw in nodes:
        if tag == "b":
            out.append(base64.b64decode(raw.encode("ascii")))
        elif tag == "s":
            out.append(raw)
        elif tag == "i":
            out.append(int(raw))
        else:
            out.append(float(raw))
    return out
