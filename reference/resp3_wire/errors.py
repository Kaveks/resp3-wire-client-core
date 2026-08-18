"""Exception hierarchy for :mod:`resp3_wire`.

Every exception this package raises subclasses :class:`RedisError`::

    RedisError
      ProtocolError          malformed wire data
      ConnectionError        socket level failure
        TimeoutError         a socket operation exceeded its deadline
      ServerError            the server returned an error reply
        WrongTypeError       WRONGTYPE
        MovedError           MOVED
        NoScriptError        NOSCRIPT
        BusyGroupError       BUSYGROUP

The division that matters is recoverability. :class:`ProtocolError`,
:class:`ConnectionError`, and :class:`TimeoutError` all mean the connection is
unusable: the stream position is unknown, so nothing further may be read from
it. :class:`ServerError` does not, because the server completed its reply
normally and the stream is intact.

:class:`ConnectionError` and :class:`TimeoutError` deliberately shadow the
builtins of the same name within this package. They do not subclass them.

This module imports nothing from :mod:`resp3_wire.protocol`. Keeping the
dependency in one direction is what makes the parser's independence from I/O
transitive rather than merely true of its own module.
"""

from __future__ import annotations

__all__ = [
    "RedisError",
    "ProtocolError",
    "ConnectionError",
    "TimeoutError",
    "ServerError",
    "WrongTypeError",
    "MovedError",
    "NoScriptError",
    "BusyGroupError",
    "exception_for",
]


class RedisError(Exception):
    """Base class for every exception raised by this package."""


class ProtocolError(RedisError):
    """The wire data could not be parsed.

    The connection that produced the data is unusable afterwards, because the
    parser cannot know where the next frame begins.
    """


class ConnectionError(RedisError):
    """A socket level failure: refused, reset, or closed by the peer."""


class TimeoutError(ConnectionError):
    """A socket operation did not complete within its deadline.

    Subclasses :class:`ConnectionError` because the outcome is the same: an
    unknown number of bytes remain in flight, so the stream position is no
    longer known and the connection cannot be reused.
    """


class ServerError(RedisError):
    """The server returned an error reply.

    The connection remains healthy: the server sent a complete, well formed
    reply that happens to describe a failure, so the stream is still aligned.

    ``code`` is the first whitespace delimited token of the error text,
    uppercased. ``message`` is the full error text including that token.
    """

    code: str
    message: str

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class WrongTypeError(ServerError):
    """Raised for the ``WRONGTYPE`` error code."""


class MovedError(ServerError):
    """Raised for the ``MOVED`` error code.

    A ``MOVED`` reply is only actionable with the slot and the address it
    names, so both are parsed out of the error text::

        MOVED 3999 127.0.0.1:6381    ->  slot == 3999
                                         address == "127.0.0.1:6381"

    This package does not follow the redirect. Exposing the two fields is the
    full extent of its cluster awareness.

    Text that does not have that shape yields ``slot == -1`` and
    ``address == ""``. Only a broken server can produce it, and raising from an
    exception constructor would replace a diagnosable server error with an
    unrelated failure.
    """

    slot: int
    address: str

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message)
        self.slot, self.address = self._parse(message)

    @staticmethod
    def _parse(message: str) -> tuple[int, str]:
        parts = message.split()
        if len(parts) < 3:
            return -1, ""
        try:
            slot = int(parts[1])
        except ValueError:
            return -1, ""
        return slot, parts[2]


class NoScriptError(ServerError):
    """Raised for the ``NOSCRIPT`` error code."""


class BusyGroupError(ServerError):
    """Raised for the ``BUSYGROUP`` error code."""


_CODE_MAP: dict[str, type[ServerError]] = {
    "WRONGTYPE": WrongTypeError,
    "MOVED": MovedError,
    "NOSCRIPT": NoScriptError,
    "BUSYGROUP": BusyGroupError,
}


def exception_for(code: str, message: str) -> ServerError:
    """Build the :class:`ServerError` subclass matching an error code.

    An unrecognised code produces :class:`ServerError` itself, not a subclass.

    This takes two strings rather than an ``ErrorReply`` so that this module
    stays independent of :mod:`resp3_wire.protocol`.
    """
    return _CODE_MAP.get(code, ServerError)(code, message)
