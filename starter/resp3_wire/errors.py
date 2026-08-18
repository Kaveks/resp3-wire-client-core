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

    Raised by the parser on a malformed frame: an unknown type byte, a length
    that is not an integer, a verbatim string with no format prefix, an empty
    push frame, or an unhashable value in map key position.

    The connection that produced the data is unusable afterwards, because the
    parser cannot know where the next frame begins.
    """


class ConnectionError(RedisError):
    """A socket level failure.

    Raised when a connection is refused, reset, or closed by the peer, and
    when a command is attempted on a connection that is not connected or has
    been poisoned.
    """


class TimeoutError(ConnectionError):
    """A socket operation did not complete within its deadline.

    Subclasses :class:`ConnectionError` because the outcome is the same: an
    unknown number of bytes remain in flight, so the stream position is no
    longer known and the connection cannot be reused.
    """


class ServerError(RedisError):
    """The server returned an error reply.

    The connection remains healthy. The server sent a complete, well formed
    reply that happens to describe a failure, so the stream is still aligned
    and the connection may continue to be used.

    ``code`` is the first whitespace delimited token of the error text,
    uppercased, for example ``WRONGTYPE``. ``message`` is the full error text
    including that token.
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
    names, so this class parses both out of the error text and exposes them::

        MOVED 3999 127.0.0.1:6381    ->  slot == 3999
                                         address == "127.0.0.1:6381"

    This package does not follow the redirect. Exposing the two fields is the
    full extent of its cluster awareness.

    Implement ``__init__`` to extract ``slot`` and ``address`` from
    ``message`` and to delegate the rest to :class:`ServerError`.
    """

    slot: int
    address: str

    def __init__(self, code: str, message: str) -> None:
        raise NotImplementedError(
            "MovedError.__init__ must parse slot and address from the message"
        )


class NoScriptError(ServerError):
    """Raised for the ``NOSCRIPT`` error code."""


class BusyGroupError(ServerError):
    """Raised for the ``BUSYGROUP`` error code."""


def exception_for(code: str, message: str) -> ServerError:
    """Build the :class:`ServerError` subclass matching an error code.

    The mapping is::

        WRONGTYPE   -> WrongTypeError
        MOVED       -> MovedError
        NOSCRIPT    -> NoScriptError
        BUSYGROUP   -> BusyGroupError
        anything else -> ServerError

    An unrecognised code produces :class:`ServerError` itself, not a subclass.

    This takes two strings rather than an ``ErrorReply`` so that this module
    stays independent of :mod:`resp3_wire.protocol`.
    """
    raise NotImplementedError("exception_for")
