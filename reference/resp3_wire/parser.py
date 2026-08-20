"""The incremental RESP2 and RESP3 parser.

This module is sans-io. It never touches a socket, never blocks, and is driven
entirely by byte chunks handed to it. It imports nothing from ``socket``,
``select``, ``asyncio``, ``ssl``, or ``subprocess``.

The parser accepts every type byte from both protocol versions regardless of
what was negotiated. A RESP2 connection simply never receives a ``,`` or ``%``
frame, and enforcing the mode here would add a failure path that buys nothing.

A parser instance is not thread safe and belongs to exactly one connection.

Design
------

Two properties govern the implementation, and both are structural rather than
incidental.

The result must not depend on where chunk boundaries fell. That is achieved by
never re-reading a byte that has already been interpreted: all progress is
committed to the resumption state before :meth:`RespParser.gets` returns
:data:`NEED_MORE`, and no branch anywhere asks how much data happens to be
buffered beyond "is the token I am waiting for complete yet". There is no fast
path for a whole frame arriving at once, deliberately, because such a path
would behave differently depending on the split.

Cost must stay linear in the number of bytes, not in the number of chunks they
arrived in. Four things would make it quadratic, and each is closed:

    reslicing the buffer on consume    a read cursor, with compaction guarded
                                       to run only when at least half the
                                       buffer is consumed, so it amortizes
    reparsing a frame after NEED_MORE  an explicit frame stack that survives
                                       across calls
    rescanning for CRLF on every feed  a remembered scan position
    accumulating a partial blob        once a blob's length is known the bytes
                                       stay in the buffer untouched until the
                                       whole payload has arrived

Resumption state is three fields. ``_stack`` holds one entry per unclosed
aggregate, ``_pending`` holds the type and declared length of a blob whose
payload has not fully arrived, and ``_root_attrs`` holds attributes awaiting a
top level value. Nesting depth is ``len(_stack)`` rather than a separate
counter, and aggregates are finalized in a loop rather than by recursion, so
depth costs stack space nowhere.
"""

from __future__ import annotations

from typing import Any, Final

from .errors import ProtocolError
from .protocol import Attributed, ErrorReply, PushMessage, VerbatimBytes

__all__ = ["RespParser", "NEED_MORE"]

_CRLF: Final = b"\r\n"

# Compaction moves the unconsumed tail to the front of the buffer. It runs only
# when the consumed prefix is both large in absolute terms and at least half the
# buffer, which bounds total copying to a constant factor of the bytes fed.
_COMPACT_THRESHOLD: Final = 65536

# Wire type bytes, as the integers indexing a bytes object yields.
_PLUS: Final = 0x2B         # + simple string
_MINUS: Final = 0x2D        # - simple error
_COLON: Final = 0x3A        # : integer
_DOLLAR: Final = 0x24       # $ blob string
_ASTERISK: Final = 0x2A     # * array
_UNDERSCORE: Final = 0x5F   # _ null
_COMMA: Final = 0x2C        # , double
_HASH: Final = 0x23         # # boolean
_LPAREN: Final = 0x28       # ( big number
_BANG: Final = 0x21         # ! blob error
_EQUALS: Final = 0x3D       # = verbatim string
_PERCENT: Final = 0x25      # % map
_TILDE: Final = 0x7E        # ~ set
_GT: Final = 0x3E           # > push
_PIPE: Final = 0x7C         # | attribute

# Aggregate frame kinds.
_ARRAY: Final = 0
_MAP: Final = 1
_SET: Final = 2
_PUSH: Final = 3
_ATTR: Final = 4


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


class _Consumed:
    """Internal: a token was consumed but no top level reply resulted."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<consumed>"


_CONSUMED: Final = _Consumed()


class _Frame:
    """One unclosed aggregate.

    ``remaining`` counts values still to read. Maps and attributes count
    ``2N``, with keys and values alternating in one flat ``items`` list, which
    keeps resumption uniform: there is no separate "expecting a key" state that
    could be lost across a chunk boundary.

    ``attrs`` holds attributes that have been read at this frame's depth and
    are waiting for the value they decorate. Keeping them per frame is what
    makes an attribute decorate the next value *at the same depth* by
    construction rather than by bookkeeping.
    """

    __slots__ = ("kind", "remaining", "items", "attrs")

    def __init__(self, kind: int, remaining: int) -> None:
        self.kind = kind
        self.remaining = remaining
        self.items: list[Any] = []
        self.attrs: dict[Any, Any] | None = None


def _to_int(body: bytes) -> int:
    try:
        return int(body)
    except ValueError:
        raise ProtocolError(f"invalid integer in frame header: {body!r}") from None


def _to_float(body: bytes) -> float:
    try:
        return float(body)
    except ValueError:
        raise ProtocolError(f"invalid double: {body!r}") from None


def _error_reply(payload: bytes) -> ErrorReply:
    """Build an :class:`ErrorReply` from error text, for both ``-`` and ``!``."""
    text = payload.decode("utf-8", "surrogateescape")
    parts = text.split(None, 1)
    return ErrorReply(parts[0].upper() if parts else "", text)


def _build_dict(items: list[Any]) -> dict[Any, Any]:
    """Fold a flat key, value, key, value list into a dict.

    Duplicate keys resolve last write wins, which falls out of assignment
    order. An unhashable key can only come from a nested aggregate in key
    position, which Redis does not emit.
    """
    result: dict[Any, Any] = {}
    for i in range(0, len(items), 2):
        try:
            result[items[i]] = items[i + 1]
        except TypeError:
            raise ProtocolError("unhashable value in map key position") from None
    return result


def _finalize(frame: _Frame) -> Any:
    """Turn a completed aggregate frame into its Python value."""
    kind = frame.kind
    items = frame.items
    if kind == _ARRAY:
        return items
    if kind == _MAP:
        return _build_dict(items)
    if kind == _SET:
        try:
            return set(items)
        except TypeError:
            # Documented degradation: an unhashable member makes the whole
            # frame a list preserving wire order, rather than an error.
            return items
    # _PUSH. _ATTR never reaches here; it becomes pending attributes instead.
    head = items[0]
    if not isinstance(head, (bytes, bytearray)):
        raise ProtocolError("push frame kind is not a string")
    return PushMessage(head.decode("utf-8", "surrogateescape"), items[1:])


class RespParser:
    """Turns byte chunks into reply values.

    The usage pattern is feed, then drain::

        parser.feed(chunk)
        while (value := parser.gets()) is not NEED_MORE:
            handle(value)

    For any byte sequence and any partition of it, feeding the chunks in order
    with a drain after each produces the same sequence of values as feeding the
    whole sequence and draining once, metadata included.
    """

    __slots__ = ("_buf", "_pos", "_scan_from", "_stack", "_pending", "_root_attrs")

    def __init__(self) -> None:
        """Create a parser with an empty buffer and no partial frame."""
        self._buf = bytearray()
        self._pos = 0
        # How far the search for a line terminator has already looked. Always
        # at or ahead of _pos, which compaction relies on.
        self._scan_from = 0
        self._stack: list[_Frame] = []
        # (type byte, declared payload length) for a blob whose payload has not
        # fully arrived. The bytes themselves stay in _buf; nothing is copied
        # until the whole payload is present.
        self._pending: tuple[int, int] | None = None
        self._root_attrs: dict[Any, Any] | None = None

    # -- public interface --------------------------------------------------

    def feed(self, data: bytes) -> None:
        """Append bytes to the internal buffer.

        Never blocks, never raises on incomplete input, and accepts a chunk of
        any size including empty. Feeding does not parse; parsing happens in
        :meth:`gets`.
        """
        pos = self._pos
        if pos:
            if pos == len(self._buf):
                # Fully drained. Rebinding releases the allocation outright
                # instead of carrying a dead prefix forward.
                self._buf = bytearray()
                self._pos = 0
                self._scan_from = 0
            elif pos >= _COMPACT_THRESHOLD and pos * 2 >= len(self._buf):
                del self._buf[:pos]
                self._scan_from -= pos
                self._pos = 0
        if data:
            self._buf += data

    def gets(self) -> object:
        """Return the next complete reply, or :data:`NEED_MORE`.

        Callers drain by calling this repeatedly until it returns
        :data:`NEED_MORE`. Returning that sentinel leaves the parser able to
        resume exactly where it stopped; no input is consumed unless the value
        it belongs to has been produced.

        An attribute frame is never returned. It decorates the value that
        follows it at the same depth, and if no such value has arrived yet this
        returns :data:`NEED_MORE`.

        A push frame is returned like any other complete reply. Filtering
        pushes is a caller's concern.

        Raises :exc:`ProtocolError` on malformed wire data. It never raises on
        a server error reply, which is a value.
        """
        while True:
            result = self._step()
            if result is NEED_MORE:
                return NEED_MORE
            if result is not _CONSUMED:
                self._release_if_drained()
                return result

    def _release_if_drained(self) -> None:
        """Drop the buffer once every byte fed has been consumed.

        Reached only when a complete reply has just been produced, so no
        partial frame can be buffered: `_stack` is empty and `_pending` is None
        whenever this runs. Without it a consumed payload stays resident until
        the next :meth:`feed` happens to rebind the buffer, which for a caller
        that stops reading after a large reply is indefinitely.
        """
        if self._pos and self._pos == len(self._buf):
            self._buf = bytearray()
            self._pos = 0
            self._scan_from = 0

    @property
    def has_buffered_input(self) -> bool:
        """Whether bytes have been fed that have not yet produced a value.

        True means a frame is part-way in: more bytes were fed than the values
        drained account for. A caller draining out-of-band frames needs this to
        tell "nothing has arrived" from "half of something has arrived", because
        those call for opposite responses.
        """
        return self._pos < len(self._buf)

    def reset(self) -> None:
        """Discard all buffered state and return to the initial condition.

        Used when a connection is discarded mid-reply. The buffer is rebound
        rather than cleared in place, so the memory backing it is released.
        """
        self._buf = bytearray()
        self._pos = 0
        self._scan_from = 0
        self._stack.clear()
        self._pending = None
        self._root_attrs = None

    # -- one token at a time -----------------------------------------------

    def _step(self) -> Any:
        """Consume one token. Returns a reply, :data:`NEED_MORE`, or _CONSUMED."""
        if self._pending is not None:
            return self._finish_blob()

        line = self._read_line()
        if line is None:
            return NEED_MORE
        if not line:
            raise ProtocolError("empty frame line")

        marker = line[0]
        body = line[1:]

        if marker == _DOLLAR:
            n = _to_int(body)
            if n < 0:
                return self._emit(None)  # RESP2 null bulk
            self._pending = (_DOLLAR, n)
            return _CONSUMED
        if marker == _ASTERISK:
            n = _to_int(body)
            if n < 0:
                return self._emit(None)  # RESP2 null array
            if n == 0:
                return self._emit([])
            self._stack.append(_Frame(_ARRAY, n))
            return _CONSUMED
        if marker == _PLUS:
            return self._emit(body)
        if marker == _COLON or marker == _LPAREN:
            return self._emit(_to_int(body))
        if marker == _MINUS:
            return self._emit(_error_reply(body))
        if marker == _UNDERSCORE:
            if body:
                raise ProtocolError(f"null frame carries a payload: {body!r}")
            return self._emit(None)
        if marker == _COMMA:
            return self._emit(_to_float(body))
        if marker == _HASH:
            if body == b"t":
                return self._emit(True)
            if body == b"f":
                return self._emit(False)
            raise ProtocolError(f"invalid boolean: {body!r}")
        if marker == _PERCENT:
            n = _to_int(body)
            if n < 0:
                raise ProtocolError(f"negative map length: {n}")
            if n == 0:
                return self._emit({})
            self._stack.append(_Frame(_MAP, 2 * n))
            return _CONSUMED
        if marker == _TILDE:
            n = _to_int(body)
            if n < 0:
                raise ProtocolError(f"negative set length: {n}")
            if n == 0:
                return self._emit(set())
            self._stack.append(_Frame(_SET, n))
            return _CONSUMED
        if marker == _EQUALS:
            n = _to_int(body)
            if n < 4:
                raise ProtocolError(
                    f"verbatim string too short to carry a format prefix: {n}"
                )
            self._pending = (_EQUALS, n)
            return _CONSUMED
        if marker == _BANG:
            n = _to_int(body)
            if n < 0:
                raise ProtocolError(f"negative blob error length: {n}")
            self._pending = (_BANG, n)
            return _CONSUMED
        if marker == _GT:
            n = _to_int(body)
            if n <= 0:
                # A push frame always carries at least its kind.
                raise ProtocolError(f"push frame with no elements: {n}")
            self._stack.append(_Frame(_PUSH, n))
            return _CONSUMED
        if marker == _PIPE:
            n = _to_int(body)
            if n < 0:
                raise ProtocolError(f"negative attribute length: {n}")
            if n == 0:
                self._merge_attrs({})
                return _CONSUMED
            self._stack.append(_Frame(_ATTR, 2 * n))
            return _CONSUMED

        raise ProtocolError(f"unknown type byte: {bytes(line[:1])!r}")

    def _read_line(self) -> bytes | None:
        """Read up to the next CRLF, or return None if it has not arrived.

        The search resumes from where the last unsuccessful one stopped, so
        feeding a long line one byte at a time costs a constant per byte rather
        than rescanning from the start each time.
        """
        start = self._pos
        search = self._scan_from if self._scan_from > start else start
        idx = self._buf.find(_CRLF, search)
        if idx < 0:
            # A trailing CR may still become a CRLF, so back up one byte.
            tail = len(self._buf) - 1
            self._scan_from = tail if tail > start else start
            return None
        line = bytes(self._buf[start:idx])
        self._pos = idx + 2
        self._scan_from = self._pos
        return line

    def _finish_blob(self) -> Any:
        """Complete a length prefixed frame once its payload has arrived."""
        marker, n = self._pending  # type: ignore[misc]
        start = self._pos
        end = start + n
        if len(self._buf) < end + 2:
            return NEED_MORE
        if self._buf[end : end + 2] != _CRLF:
            raise ProtocolError("blob payload not terminated by CRLF")

        # A memoryview slice copies the payload exactly once. Slicing the
        # bytearray directly would build an intermediate bytearray and then
        # copy again, which at 64 MB is the difference between a peak of two
        # payloads and a peak of three.
        if marker == _DOLLAR:
            value: Any = bytes(memoryview(self._buf)[start:end])
        elif marker == _EQUALS:
            if self._buf[start + 3] != 0x3A:  # ':'
                raise ProtocolError("verbatim string format prefix has no colon")
            fmt = bytes(self._buf[start : start + 3]).decode("ascii", "surrogateescape")
            value = VerbatimBytes(
                bytes(memoryview(self._buf)[start + 4 : end]), format=fmt
            )
        else:  # _BANG
            value = _error_reply(bytes(memoryview(self._buf)[start:end]))

        self._pos = end + 2
        self._scan_from = self._pos
        self._pending = None
        return self._emit(value)

    # -- assembling values -------------------------------------------------

    def _merge_attrs(self, attrs: dict[Any, Any]) -> None:
        """Park an attribute dictionary against the depth it decorates.

        Consecutive attribute frames decorating the same value merge, later
        keys winning.
        """
        if self._stack:
            top = self._stack[-1]
            if top.attrs is None:
                top.attrs = attrs
            else:
                top.attrs.update(attrs)
        elif self._root_attrs is None:
            self._root_attrs = attrs
        else:
            self._root_attrs.update(attrs)

    def _emit(self, value: Any) -> Any:
        """Place a completed value, closing any aggregates it finishes.

        Returns the value itself when it completes a top level reply, or
        _CONSUMED when it was absorbed into an enclosing aggregate.

        This is a loop rather than recursion, so nesting depth costs no
        interpreter stack and a deeply nested reply cannot raise
        ``RecursionError``.
        """
        stack = self._stack
        while True:
            if not stack:
                if self._root_attrs is not None:
                    value = Attributed(value, self._root_attrs)
                    self._root_attrs = None
                return value

            top = stack[-1]
            if top.attrs is not None:
                value = Attributed(value, top.attrs)
                top.attrs = None

            top.items.append(value)
            top.remaining -= 1
            if top.remaining:
                return _CONSUMED

            stack.pop()
            if top.kind == _ATTR:
                # An attribute frame is not a reply. It becomes metadata for
                # the next value at the depth it was read at, which is now the
                # depth of whatever frame the pop exposed.
                self._merge_attrs(_build_dict(top.items))
                return _CONSUMED
            value = _finalize(top)
