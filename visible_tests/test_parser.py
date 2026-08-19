"""The parser: the type mapping, and resuming across a chunk boundary.

The splits here are a handful of obvious ones. Your parser is checked against
arbitrary partitions under seeds you have not seen, so treat these as a shape
check rather than as coverage.
"""

from __future__ import annotations

import random

import pytest

from resp3_wire import (
    NEED_MORE, Attributed, ErrorReply, PushMessage, ProtocolError, RespParser,
    VerbatimBytes,
)


def parse_one(data: bytes):
    parser = RespParser()
    parser.feed(data)
    value = parser.gets()
    assert value is not NEED_MORE, "a complete frame should have produced a value"
    assert parser.gets() is NEED_MORE, "exactly one frame was fed"
    return value


RESP2 = [
    (b"+OK\r\n", b"OK"),
    (b"-ERR bad\r\n", ErrorReply("ERR", "ERR bad")),
    (b":42\r\n", 42),
    (b"$3\r\nabc\r\n", b"abc"),
    (b"$0\r\n\r\n", b""),
    (b"$-1\r\n", None),
    (b"*-1\r\n", None),
    (b"*0\r\n", []),
    (b"*2\r\n:1\r\n$2\r\nhi\r\n", [1, b"hi"]),
]

RESP3 = [
    (b",3.25\r\n", 3.25),
    (b",3\r\n", 3.0),
    (b",inf\r\n", float("inf")),
    (b"#t\r\n", True),
    (b"#f\r\n", False),
    (b"_\r\n", None),
    (b"(12345678901234567890\r\n", 12345678901234567890),
    (b"%2\r\n$1\r\na\r\n:1\r\n$1\r\nb\r\n:2\r\n", {b"a": 1, b"b": 2}),
    (b"~3\r\n:1\r\n:2\r\n:3\r\n", {1, 2, 3}),
]


@pytest.mark.parametrize("frame,expected", RESP2, ids=[f[:12] for f, _ in RESP2])
def test_resp2_types(frame: bytes, expected) -> None:
    assert parse_one(frame) == expected


@pytest.mark.parametrize("frame,expected", RESP3, ids=[f[:12] for f, _ in RESP3])
def test_resp3_types(frame: bytes, expected) -> None:
    assert parse_one(frame) == expected


def test_a_double_without_a_fraction_is_still_a_float() -> None:
    """The wire type determines the Python type, not the value."""
    assert type(parse_one(b",3\r\n")) is float
    assert type(parse_one(b":3\r\n")) is int


def test_a_boolean_is_not_an_integer() -> None:
    assert type(parse_one(b"#t\r\n")) is bool


def test_verbatim_strips_its_prefix_and_keeps_the_format() -> None:
    value = parse_one(b"=15\r\ntxt:Some string\r\n")
    assert value == b"Some string"
    assert isinstance(value, VerbatimBytes)
    assert value.format == "txt"


def test_blob_error_may_contain_crlf() -> None:
    value = parse_one(b"!16\r\nERR line1\r\nline2\r\n")
    assert isinstance(value, ErrorReply)
    assert value.code == "ERR"
    assert "\r\n" in value.message


def test_push_frame_is_its_own_reply() -> None:
    value = parse_one(b">2\r\n$10\r\ninvalidate\r\n*1\r\n$1\r\nk\r\n")
    assert isinstance(value, PushMessage)
    assert value.kind == "invalidate"
    assert value.data == [[b"k"]]


def test_an_attribute_decorates_the_next_value() -> None:
    """It is never a reply of its own."""
    value = parse_one(b"|1\r\n$3\r\nttl\r\n:60\r\n+OK\r\n")
    assert isinstance(value, Attributed), (
        "an attribute frame must decorate the value that follows it, and must "
        "never be returned as a value itself"
    )
    assert value == b"OK"
    assert value.attributes == {b"ttl": 60}


def test_an_attribute_with_no_value_yet_is_incomplete() -> None:
    parser = RespParser()
    parser.feed(b"|1\r\n$3\r\nttl\r\n:60\r\n")
    assert parser.gets() is NEED_MORE
    parser.feed(b"+OK\r\n")
    assert parser.gets() == b"OK"


def test_a_frame_split_in_half_parses_the_same() -> None:
    frame = b"*2\r\n$5\r\nhello\r\n$5\r\nworld\r\n"
    for cut in (4, 9, 14, len(frame) - 1):
        parser = RespParser()
        parser.feed(frame[:cut])
        assert parser.gets() is NEED_MORE, f"incomplete at cut {cut}"
        parser.feed(frame[cut:])
        assert parser.gets() == [b"hello", b"world"], f"cut {cut}"


def test_one_byte_at_a_time(rng: random.Random) -> None:
    frame = b"%1\r\n$1\r\nk\r\n*2\r\n:1\r\n,2.5\r\n"
    parser = RespParser()
    seen = []
    for byte in frame:
        parser.feed(bytes([byte]))
        while (value := parser.gets()) is not NEED_MORE:
            seen.append(value)
    assert seen == [{b"k": [1, 2.5]}]


def test_empty_feeds_are_accepted() -> None:
    parser = RespParser()
    parser.feed(b"")
    assert parser.gets() is NEED_MORE
    parser.feed(b"+OK")
    parser.feed(b"")
    parser.feed(b"\r\n")
    assert parser.gets() == b"OK"


def test_reset_discards_buffered_state() -> None:
    parser = RespParser()
    parser.feed(b"$100\r\npartial")
    assert parser.gets() is NEED_MORE
    parser.reset()
    parser.feed(b"+OK\r\n")
    assert parser.gets() == b"OK"


def test_malformed_input_raises_protocolerror() -> None:
    parser = RespParser()
    parser.feed(b"^nonsense\r\n")
    with pytest.raises(ProtocolError):
        parser.gets()


def test_need_more_is_not_none() -> None:
    """None is a legitimate reply, so the sentinel must be distinguishable."""
    assert NEED_MORE is not None
    assert parse_one(b"_\r\n") is None
