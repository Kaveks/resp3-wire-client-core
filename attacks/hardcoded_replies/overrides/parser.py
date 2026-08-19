"""Attack: a parser that never produces a value.

Worth its own note. The chunking channel's invariance cases compare a
partitioned feed against a whole-buffer feed of the same parser, and a parser
that returns nothing satisfies that comparison perfectly: both sides drain to an
empty list. D20 is the record of why that is not enough on its own, and this
module is the extreme case it describes.

The absolute-expectation and attribute cases are what this cannot survive, and
the score is the measurement of how much of the channel invariance alone
carries.
"""

from __future__ import annotations

from typing import Any, Final

__all__ = ["RespParser", "NEED_MORE"]


class _NeedMore:
    __slots__ = ()

    def __repr__(self) -> str:
        return "NEED_MORE"


NEED_MORE: Final[_NeedMore] = _NeedMore()


class RespParser:
    """Accepts bytes, forgets them, and always says it needs more."""

    __slots__ = ("_buf",)

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> None:
        self._buf += data

    def gets(self) -> Any:
        return NEED_MORE

    def reset(self) -> None:
        self._buf = bytearray()
