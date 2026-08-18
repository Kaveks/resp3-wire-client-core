"""A Redis client written directly against the wire protocol.

Standard library only. The package is arranged so that parsing is separable
from I/O::

    protocol.py     value types
    parser.py       RespParser, NEED_MORE
    errors.py       exception hierarchy
    connection.py   Connection
    pool.py         ConnectionPool
    pipeline.py     Pipeline

``ConnectionError`` and ``TimeoutError`` shadow the builtins of the same name
within this package. That is deliberate, and they do not subclass them.

``ConnectionPool`` and ``Pipeline`` are added in the step that follows, and
this module's re-exports grow to the full public surface with them.
"""

from __future__ import annotations

from .connection import Connection
from .errors import (
    BusyGroupError,
    ConnectionError,
    MovedError,
    NoScriptError,
    ProtocolError,
    RedisError,
    ServerError,
    TimeoutError,
    WrongTypeError,
)
from .parser import NEED_MORE, RespParser
from .protocol import Attributed, ErrorReply, PushMessage, VerbatimBytes, unwrap

__all__ = [
    "RespParser",
    "NEED_MORE",
    "Attributed",
    "VerbatimBytes",
    "ErrorReply",
    "PushMessage",
    "unwrap",
    "Connection",
    "RedisError",
    "ProtocolError",
    "ConnectionError",
    "TimeoutError",
    "ServerError",
    "WrongTypeError",
    "MovedError",
    "NoScriptError",
    "BusyGroupError",
]
