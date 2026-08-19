"""The comparator.

Python equality is the wrong primitive for this comparison. `bool` subclasses
`int`, so `True == 1` and an implementation returning `1` for a RESP3 boolean
would pass. Set equality goes through hashing, which would make the comparator
depend on `Attributed.__hash__` delegating and turn a broken delegation into a
scatter of unrelated failures. `nan != nan`. And equality yields a bare `True`
or `False`, where a failing case needs to say which position in a nested
structure diverged.

This is therefore a recursive structural comparison that classifies both sides
before comparing them, and reports a path on mismatch.

Three asymmetries exist, all from D11, all because redis-py is the reference
for values it can actually produce and no reference at all for the rest:

  - `Attributed` is unwrapped on the agent side only, since redis-py raises on
    attribute frames rather than surfacing them.
  - agent `set` against redis-py `list` is permitted, since redis-py returns
    `list` for every RESP3 set by deliberate design. The contract's requirement
    that a `~` frame yield a `set` is enforced by a dedicated case that asserts
    the type directly, not by this comparator.
  - errors compare by code only, since exception classes cannot cross a library
    boundary and a nested `EXEC` error is an exception on one side and an
    `ErrorReply` on the other.
"""

from __future__ import annotations

import math
from typing import Any

from resp3_wire import Attributed, ErrorReply, PushMessage, RedisError, VerbatimBytes

from .wire_codec import RedisPyError

__all__ = [
    "Divergence", "compare", "canonical_key", "type_class", "render_path",
    "strict_equal", "strict_describe",
]

NONE = "NONE"
BOOL = "BOOL"
INT = "INT"
FLOAT = "FLOAT"
BYTES = "BYTES"
LIST = "LIST"
DICT = "DICT"
SET = "SET"
ERROR = "ERROR"
EXC = "EXC"
PUSH = "PUSH"


class Divergence(AssertionError):
    """Raised when actual and expected differ, carrying the position."""

    def __init__(self, path: tuple, message: str) -> None:
        super().__init__(f"{render_path(path)}: {message}")
        self.path = path


class Key:
    """A dict key in a rendered path."""

    __slots__ = ("value",)

    def __init__(self, value: Any) -> None:
        self.value = value


class SetElem:
    """A canonicalized set member in a rendered path."""

    __slots__ = ("value",)

    def __init__(self, value: Any) -> None:
        self.value = value


class Attr:
    """Descent into an attribute dictionary."""

    __slots__ = ()


def render_path(path: tuple) -> str:
    out = "root"
    for element in path:
        if isinstance(element, Key):
            out += f"[{element.value!r}]"
        elif isinstance(element, SetElem):
            out += f"{{{element.value!r}}}"
        elif isinstance(element, Attr):
            out += ".attributes"
        else:
            out += f"[{element}]"
    return out


def type_class(value: Any) -> str:
    """Assign a type class. BOOL is tested before INT, which is the point."""
    if value is None:
        return NONE
    if type(value) is bool:
        return BOOL
    if type(value) is int:
        return INT
    if type(value) is float:
        return FLOAT
    if isinstance(value, bytes):
        # isinstance, so VerbatimBytes compares as bytes. Its format is not
        # compared here because redis-py has no counterpart; the chunking
        # channel asserts format fidelity instead.
        return BYTES
    if type(value) is list:
        return LIST
    if type(value) is dict:
        return DICT
    if type(value) is set or type(value) is frozenset:
        return SET
    if isinstance(value, ErrorReply):
        return ERROR
    if isinstance(value, (RedisError, RedisPyError)):
        return EXC
    if isinstance(value, PushMessage):
        return PUSH
    return f"UNKNOWN({type(value).__name__})"


def _error_code(value: Any) -> str:
    if isinstance(value, (ErrorReply, RedisPyError)):
        return value.code
    code = getattr(value, "code", None)
    if code is not None:
        return str(code)
    text = str(value)
    parts = text.split(None, 1)
    return parts[0].upper() if parts else ""


def canonical_key(value: Any) -> tuple:
    """A total ordering key for set members.

    Returns (type class name, sortable representation). The first element
    disambiguates across classes, so sorting never compares values of different
    types against each other.
    """
    cls = type_class(value)
    if cls == BYTES:
        return (cls, bytes(value))
    if cls in (INT, FLOAT, BOOL):
        return (cls, value)
    if cls == NONE:
        return (cls, b"")
    if cls in (ERROR, EXC):
        return (cls, _error_code(value))
    return (cls, repr(value))


def _unwrap_recording(value: Any, path: tuple, seen: dict) -> Any:
    """Strip Attributed from the agent side, recording what was stripped.

    Recorded attributes are not compared against anything. They let a case
    assert that an attribute appeared where expected, and let this comparator
    enforce the one thing it does enforce about attributes: that a dictionary
    of them never arrives as a value in its own right. An `actual` that is a
    bare dict where `expected` is a scalar is a class mismatch, which is
    exactly the signature of an implementation emitting attributes as replies.
    """
    while isinstance(value, Attributed):
        seen[path] = value.attributes
        value = value.value
    return value


def compare(actual: Any, expected: Any, path: tuple = (), attrs: dict | None = None) -> None:
    """Compare two reply values structurally. Raises :class:`Divergence`."""
    if attrs is None:
        attrs = {}
    actual = _unwrap_recording(actual, path, attrs)

    a_cls = type_class(actual)
    e_cls = type_class(expected)

    # D11: redis-py returns list for every RESP3 set, so an agent set against a
    # redis-py list is not a class mismatch. Both canonicalize below.
    set_vs_list = a_cls == SET and e_cls == LIST
    # D11: an error is an ErrorReply on one side and an exception on the other.
    error_pair = a_cls in (ERROR, EXC) and e_cls in (ERROR, EXC)

    if a_cls != e_cls and not set_vs_list and not error_pair:
        raise Divergence(
            path,
            f"type class {a_cls} != {e_cls} "
            f"(actual {actual!r}, expected {expected!r})",
        )

    if error_pair:
        a_code, e_code = _error_code(actual), _error_code(expected)
        if a_code != e_code:
            raise Divergence(path, f"error code {a_code!r} != {e_code!r}")
        return

    if a_cls == NONE:
        return

    if a_cls == FLOAT:
        if math.isnan(actual) or math.isnan(expected):
            raise Divergence(
                path,
                "nan encountered; the command matrix must not produce one",
            )
        if actual != expected:
            raise Divergence(path, f"float {actual!r} != {expected!r}")
        return

    if a_cls in (BOOL, INT, BYTES):
        if a_cls == BYTES:
            if bytes(actual) != bytes(expected):
                raise Divergence(path, f"bytes {bytes(actual)!r} != {bytes(expected)!r}")
            return
        if actual != expected:
            raise Divergence(path, f"{a_cls.lower()} {actual!r} != {expected!r}")
        return

    if a_cls == LIST and e_cls == LIST:
        if len(actual) != len(expected):
            raise Divergence(
                path, f"list length {len(actual)} != {len(expected)}"
            )
        for i, (a, e) in enumerate(zip(actual, expected)):
            compare(a, e, path + (i,), attrs)
        return

    if a_cls == DICT:
        a_keys = {canonical_key(k): k for k in actual}
        e_keys = {canonical_key(k): k for k in expected}
        missing = sorted(set(e_keys) - set(a_keys))
        unexpected = sorted(set(a_keys) - set(e_keys))
        if missing:
            raise Divergence(path, f"missing keys {[e_keys[k] for k in missing]!r}")
        if unexpected:
            raise Divergence(path, f"unexpected keys {[a_keys[k] for k in unexpected]!r}")
        for ck, key in e_keys.items():
            compare(actual[a_keys[ck]], expected[key], path + (Key(key),), attrs)
        return

    if a_cls == SET or set_vs_list:
        # Canonicalized rather than compared by set operations, so the result
        # does not depend on Attributed.__hash__ delegating. That delegation is
        # load-bearing for callers and is asserted by its own dedicated case.
        a_items = sorted(
            (_unwrap_recording(v, path, attrs) for v in actual), key=canonical_key
        )
        e_items = sorted(expected, key=canonical_key)
        if len(a_items) != len(e_items):
            raise Divergence(
                path, f"set size {len(a_items)} != {len(e_items)}"
            )
        for a, e in zip(a_items, e_items):
            compare(a, e, path + (SetElem(canonical_key(e)[1]),), attrs)
        return

    if a_cls == PUSH:
        if actual.kind != expected.kind:
            raise Divergence(path, f"push kind {actual.kind!r} != {expected.kind!r}")
        compare(actual.data, expected.data, path + (Key("data"),), attrs)
        return

    raise Divergence(path, f"unsupported type class {a_cls}")


# ---------------------------------------------------------------------------
# Strict equality, used by the chunking channel only.
# ---------------------------------------------------------------------------
#
# docs/HARNESS.md section 4.1: the chunking invariant compares more than the
# comparator above does. It additionally compares VerbatimBytes.format and
# Attributed.attributes, deliberately reaching past the delegating equality
# that section 2.1 of the protocol contract defines.
#
# The reason is specific. `Attributed` compares equal to its bare value and a
# `VerbatimBytes` compares equal to plain bytes, so a parser that dropped an
# attribute dictionary or a format prefix only when a split landed inside it
# would satisfy `==` and pass. Comparing the metadata structurally is what
# closes that hole.


def strict_describe(value: Any) -> Any:
    """A fully explicit form of a reply value, with metadata made structural."""
    if isinstance(value, Attributed):
        return ("attributed", strict_describe(value.attributes),
                strict_describe(value.value))
    if isinstance(value, VerbatimBytes):
        return ("verbatim", value.format, bytes(value))
    if isinstance(value, ErrorReply):
        return ("error", value.code, value.message)
    if isinstance(value, PushMessage):
        return ("push", value.kind, [strict_describe(v) for v in value.data])
    if value is None:
        return ("none",)
    if type(value) is bool:
        return ("bool", value)
    if type(value) is int:
        return ("int", value)
    if type(value) is float:
        return ("float", "nan" if math.isnan(value) else repr(value))
    if type(value) is bytes:
        return ("bytes", value)
    if type(value) is list:
        return ("list", [strict_describe(v) for v in value])
    if type(value) is dict:
        return ("dict", sorted(
            ((strict_describe(k), strict_describe(v)) for k, v in value.items()),
            key=repr,
        ))
    if type(value) is set or type(value) is frozenset:
        return ("set", sorted((strict_describe(v) for v in value), key=repr))
    return ("other", type(value).__name__, repr(value))


def strict_equal(left: Any, right: Any) -> bool:
    """Whether two reply sequences match, metadata included."""
    return strict_describe(left) == strict_describe(right)
