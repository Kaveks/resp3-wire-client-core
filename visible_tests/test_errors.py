"""The exception hierarchy, and the distinction it draws."""

from __future__ import annotations

import builtins

from resp3_wire import (
    BusyGroupError, ConnectionError, MovedError, NoScriptError, ProtocolError,
    RedisError, ServerError, TimeoutError, WrongTypeError,
)


def test_everything_subclasses_rediserror() -> None:
    for cls in (ProtocolError, ConnectionError, TimeoutError, ServerError,
                WrongTypeError, MovedError, NoScriptError, BusyGroupError):
        assert issubclass(cls, RedisError), cls.__name__


def test_recoverable_and_unrecoverable_are_separated() -> None:
    # These three mean the connection is unusable.
    assert issubclass(TimeoutError, ConnectionError)
    # A server error does not: the server completed its reply normally.
    assert not issubclass(ServerError, ConnectionError)
    for cls in (WrongTypeError, MovedError, NoScriptError, BusyGroupError):
        assert issubclass(cls, ServerError), cls.__name__


def test_shadowed_names_do_not_subclass_the_builtins() -> None:
    """The shadowing is deliberate and matches redis-py's naming."""
    assert not issubclass(ConnectionError, builtins.ConnectionError)
    assert not issubclass(TimeoutError, builtins.TimeoutError)


def test_server_error_carries_code_and_message() -> None:
    err = ServerError("WRONGTYPE", "WRONGTYPE Operation against a key")
    assert err.code == "WRONGTYPE"
    assert err.message == "WRONGTYPE Operation against a key"


def test_moved_error_parses_slot_and_address() -> None:
    err = MovedError("MOVED", "MOVED 3999 127.0.0.1:6381")
    assert err.slot == 3999
    assert err.address == "127.0.0.1:6381"
