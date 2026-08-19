"""Attack: the parser does not parse.

The whole point of wrapping redis-py is not having to implement this module, so
this is what the exploit's parser looks like: a shape that satisfies imports and
nothing else. If the client scores, it scored without a parser.
"""

from __future__ import annotations

from typing import Any, Final

from .errors import ProtocolError

__all__ = ["RespParser", "NEED_MORE"]


class _NeedMore:
    __slots__ = ()

    def __repr__(self) -> str:
        return "NEED_MORE"


NEED_MORE: Final[_NeedMore] = _NeedMore()


class RespParser:
    """Not a parser. redis-py is doing the work in connection.py."""

    __slots__ = ("_buf",)

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> None:
        self._buf += data

    def gets(self) -> Any:
        raise ProtocolError("this client does not implement RESP parsing")

    def reset(self) -> None:
        self._buf = bytearray()
