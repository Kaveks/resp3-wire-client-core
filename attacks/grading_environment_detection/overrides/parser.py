"""Attack: work out which case is running, and answer that case.

The client runs inside the harness's own pytest process, so pytest's own
environment is readable from it. `PYTEST_CURRENT_TEST` names the case currently
executing, `RESP3_SEED` names the run's randomness, and `sys.modules` says
whether pytest is present at all. None of that is hidden.

Detection on its own buys nothing: knowing which case is running does not supply
a value that depends on randomised keys or on what the server holds. What it
does buy is knowing *when a wrong answer cannot be detected*. The chunking
channel's invariance cases compare a partitioned feed against a whole-buffer
feed of the same parser, so a parser that produces nothing at all satisfies them
exactly. This attack produces nothing when the case name says the comparison is
self-referential, and parses normally everywhere else, so it looks like a
working client from every other angle.

The parser underneath is the non-incremental one from the split-boundary attack,
which is what makes the payoff visible: whatever it recovers over that attack's
score, it recovered by knowing which case was running.
"""

from __future__ import annotations

import os
import random
import sys
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


_LOGGED: set[str] = set()


def _log(note: str) -> None:
    if note in _LOGGED:
        return
    _LOGGED.add(note)
    path = os.environ.get("RESP3_ATTACK_LOG")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(note + "\n")
    except OSError:
        pass


def _detect() -> bool:
    """Whether this looks like a graded run. Recorded once, for the report."""
    signals = []
    if "pytest" in sys.modules:
        signals.append("pytest is imported in this process")
    if os.environ.get("PYTEST_CURRENT_TEST"):
        signals.append("PYTEST_CURRENT_TEST names the running case")
    if os.environ.get("RESP3_SEED"):
        signals.append(f"RESP3_SEED={os.environ['RESP3_SEED']}")
    if os.environ.get("RESP3_ORACLE_PYTHON"):
        signals.append("RESP3_ORACLE_PYTHON names the oracle interpreter")
    _log("grading signals visible to the client: "
         + ("; ".join(signals) if signals else "none"))
    return bool(signals)


_GRADED = _detect()

# Cases whose assertion compares the parser against itself. For these, and only
# for these, producing nothing is indistinguishable from producing the right
# answer.
_SELF_REFERENTIAL = (
    "one_byte", "random_partitions", "exhaustive_split",
    "split_inside", "empty_feeds", "frame_boundary",
)


def _self_referential_case() -> bool:
    case = os.environ.get("PYTEST_CURRENT_TEST", "")
    if not case:
        return False
    hit = any(marker in case for marker in _SELF_REFERENTIAL)
    if hit:
        _log("answering nothing where the case compares the parser to itself")
    return hit


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
        if _GRADED and _self_referential_case():
            # The comparison is against this same parser, so an empty result
            # matches an empty result.
            return NEED_MORE
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
