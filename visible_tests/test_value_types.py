"""The four value types, and what makes each of them exist."""

from __future__ import annotations

from resp3_wire import (
    Attributed, ErrorReply, PushMessage, VerbatimBytes, unwrap,
)


def test_attributed_compares_as_its_value() -> None:
    """An attribute decorates a value; it must not hide it."""
    decorated = Attributed(b"payload", {b"ttl": 60})
    assert decorated == b"payload"
    assert b"payload" == decorated
    assert decorated.value == b"payload"
    assert decorated.attributes == {b"ttl": 60}


def test_attributed_hashes_as_its_value() -> None:
    """So it works as a dict key and a set member interchangeably."""
    decorated = Attributed(b"x", {b"k": b"v"})
    assert hash(decorated) == hash(b"x")
    assert {decorated} == {b"x"}
    assert {b"x": 1}[decorated] == 1


def test_attributes_do_not_take_part_in_equality() -> None:
    assert Attributed(b"x", {}) == Attributed(b"x", {b"other": b"v"})


def test_unwrap() -> None:
    assert unwrap(Attributed(b"x", {})) == b"x"
    assert unwrap(b"x") == b"x"
    assert unwrap(None) is None
    # It does not recurse: a list comes back with its elements still wrapped.
    inner = Attributed(b"x", {})
    assert unwrap([inner])[0] is inner


def test_verbatim_bytes_keeps_its_format() -> None:
    value = VerbatimBytes(b"Some string", format="txt")
    assert value == b"Some string"
    assert value.format == "txt"
    assert isinstance(value, bytes)


def test_error_reply_is_a_value_not_an_exception() -> None:
    reply = ErrorReply("WRONGTYPE", "WRONGTYPE Operation against a key")
    assert reply.code == "WRONGTYPE"
    assert reply.message.startswith("WRONGTYPE")
    assert not isinstance(reply, BaseException)


def test_push_message_fields() -> None:
    message = PushMessage("invalidate", [[b"key"]])
    assert message.kind == "invalidate"
    assert message.data == [[b"key"]]
