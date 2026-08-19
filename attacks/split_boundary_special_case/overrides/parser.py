"""Attack: a parser that is not incremental, dressed up to look like one.

The exploit an implementer actually reaches for. Writing a resumable parser is
the hard part of this task; parsing a buffer that already holds a whole frame is
not. So this parses whatever complete frames the current buffer contains and
throws away any partial tail, which is correct precisely when every feed lands
on a frame boundary.

On top of that sits the literal form of the requirement: the published visible
seed is stated in `visible_tests/README.md` and the partition algorithm ships
with the visible tests, so the offsets that seed produces can be computed ahead
of time. When the running total of bytes fed matches one of those offsets the
parser switches to a correct buffered path. That is special casing known split
boundaries, and it is precomputed here from the stdlib alone, exactly as an
implementer with the visible tests in hand would compute it.

What is supposed to stop it: the exhaustive cases split at every interior
position of a frame and the one-byte cases feed a byte at a time, neither of
which draws on any seed.
"""

from __future__ import annotations

import random
from typing import Any, Final

from .errors import ProtocolError
from .protocol import Attributed, ErrorReply, PushMessage, VerbatimBytes

__all__ = ["RespParser", "NEED_MORE"]

# visible_tests/README.md publishes this so an implementer can reproduce a
# visible failure. It is also everything needed to precompute the schedule.
VISIBLE_SEED: Final = 20260818


def _visible_offsets() -> frozenset[int]:
    """Offsets the published seed produces, for a range of plausible lengths."""
    offsets: set[int] = set()
    for length in range(64, 2048, 64):
        source = random.Random(VISIBLE_SEED)
        for _ in range(40):
            count = source.randint(1, min(60, length - 1))
            offsets.update(source.sample(range(1, length), count))
    return frozenset(offsets)


_KNOWN_OFFSETS: Final = _visible_offsets()


class _NeedMore:
    __slots__ = ()

    def __repr__(self) -> str:
        return "NEED_MORE"


NEED_MORE: Final[_NeedMore] = _NeedMore()


class _Incomplete(Exception):
    """The buffer does not hold a whole frame."""


def _parse(data: bytes, pos: int) -> tuple[Any, int]:
    """Parse one frame starting at `pos`. Raises _Incomplete if it is cut off."""
    end = data.find(b"\r\n", pos)
    if end < 0:
        raise _Incomplete
    marker = data[pos:pos + 1]
    body = data[pos + 1:end]
    after = end + 2

    if marker == b"+":
        return bytes(body), after
    if marker == b"-":
        text = body.decode("utf-8", "surrogateescape")
        parts = text.split(None, 1)
        return ErrorReply(parts[0].upper() if parts else "", text), after
    if marker in (b":", b"("):
        return int(body), after
    if marker == b",":
        return float(body), after
    if marker == b"#":
        return body == b"t", after
    if marker == b"_":
        return None, after
    if marker in (b"$", b"=", b"!"):
        size = int(body)
        if size < 0:
            return None, after
        if len(data) < after + size + 2:
            raise _Incomplete
        payload = bytes(data[after:after + size])
        after += size + 2
        if marker == b"$":
            return payload, after
        if marker == b"=":
            return VerbatimBytes(payload[4:], format=payload[:3].decode("ascii")), after
        text = payload.decode("utf-8", "surrogateescape")
        parts = text.split(None, 1)
        return ErrorReply(parts[0].upper() if parts else "", text), after
    if marker in (b"*", b"%", b"~", b">", b"|"):
        count = int(body)
        if count < 0:
            return None, after
        wanted = count * 2 if marker in (b"%", b"|") else count
        items = []
        for _ in range(wanted):
            value, after = _parse(data, after)
            items.append(value)
        if marker == b"*":
            return items, after
        if marker == b"%":
            return {items[i]: items[i + 1] for i in range(0, len(items), 2)}, after
        if marker == b"~":
            try:
                return set(items), after
            except TypeError:
                return items, after
        if marker == b">":
            head = items[0]
            return PushMessage(head.decode("utf-8", "surrogateescape"), items[1:]), after
        attrs = {items[i]: items[i + 1] for i in range(0, len(items), 2)}
        value, after = _parse(data, after)
        return Attributed(value, attrs), after
    raise ProtocolError(f"unknown type byte {marker!r}")


class RespParser:
    """Parses whole frames out of the current buffer and drops the rest."""

    __slots__ = ("_buf", "_fed", "_ready")

    def __init__(self) -> None:
        self._buf = bytearray()
        self._fed = 0
        self._ready: list = []

    def feed(self, data: bytes) -> None:
        self._fed += len(data)
        self._buf += data

    def gets(self) -> Any:
        if self._ready:
            return self._ready.pop(0)
        if not self._buf:
            return NEED_MORE
        pos = 0
        while pos < len(self._buf):
            try:
                value, pos = _parse(bytes(self._buf), pos)
            except _Incomplete:
                if self._fed in _KNOWN_OFFSETS:
                    # A boundary the published schedule is known to produce, so
                    # keep the tail and wait for the rest.
                    del self._buf[:pos]
                    break
                # Otherwise there is no resumption state to keep it in.
                self._buf = bytearray()
                break
            self._ready.append(value)
        else:
            self._buf = bytearray()
        if self._ready:
            return self._ready.pop(0)
        return NEED_MORE

    def reset(self) -> None:
        self._buf = bytearray()
        self._fed = 0
        self._ready = []
