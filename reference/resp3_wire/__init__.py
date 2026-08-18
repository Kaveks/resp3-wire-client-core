"""A Redis client written directly against the wire protocol.

Standard library only. The package is arranged so that parsing is separable
from I/O::

    protocol.py     value types
    parser.py       RespParser, NEED_MORE
    errors.py       exception hierarchy
    connection.py   Connection
    pool.py         ConnectionPool
    pipeline.py     Pipeline

These module paths are part of the contract. Private modules may be added, but
these may not be moved or renamed, because ``parser`` and ``protocol`` are
checked individually for their independence from I/O. That check is transitive:
whatever those two import must satisfy it too, which is why ``errors`` imports
nothing beyond ``__future__``.

``ConnectionError`` and ``TimeoutError`` shadow the builtins of the same name
within this package. That is deliberate, and they do not subclass them.
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
from .pipeline import Pipeline
from .pool import ConnectionPool
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
    "ConnectionPool",
    "Pipeline",
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
