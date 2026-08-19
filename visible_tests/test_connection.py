"""Connection: negotiation, encoding, and where errors raise."""

from __future__ import annotations

import pytest

from resp3_wire import Connection, ErrorReply, ServerError, WrongTypeError


def test_construction_performs_no_io() -> None:
    conn = Connection(host="127.0.0.1", port=1)
    assert not conn.is_connected
    with pytest.raises(RuntimeError):
        conn.protocol_version
    assert conn.server_info == {}


def test_protocol_must_be_2_or_3() -> None:
    for bad in (0, 1, 4):
        with pytest.raises(ValueError):
            Connection(protocol=bad)


def test_resp3_negotiation(conn) -> None:
    assert conn.protocol_version == 3
    assert isinstance(conn.server_info, dict)
    assert conn.server_info, "a successful HELLO populates server_info"
    assert all(isinstance(k, bytes) for k in conn.server_info)


def test_resp2_sends_no_hello(port: int) -> None:
    conn = Connection(port=port, protocol=2, timeout=5.0)
    conn.connect()
    try:
        assert conn.protocol_version == 2
        assert conn.server_info == {}
        assert conn.execute("PING") == b"PONG"
    finally:
        conn.close()


def test_the_same_command_has_different_shapes_per_protocol(port: int, keyspace) -> None:
    """An implementation returning RESP3 shapes on a RESP2 connection is wrong."""
    key = keyspace("hash")
    three = Connection(port=port, protocol=3, timeout=5.0)
    two = Connection(port=port, protocol=2, timeout=5.0)
    three.connect()
    two.connect()
    try:
        three.execute("HSET", key, "f", "v")
        assert type(three.execute("HGETALL", key)) is dict
        assert type(two.execute("HGETALL", key)) is list
    finally:
        three.close()
        two.close()


def test_argument_encoding(conn) -> None:
    assert conn.execute("ECHO", b"bytes") == b"bytes"
    assert conn.execute("ECHO", "text") == b"text"
    assert conn.execute("ECHO", 42) == b"42"
    assert conn.execute("ECHO", 2.5) == b"2.5"
    assert conn.execute("ECHO", b"binary\r\n\x00safe") == b"binary\r\n\x00safe"


def test_bool_is_rejected(conn) -> None:
    """Sending True as 1 would hide a bug rather than reveal one."""
    for bad in (True, False):
        with pytest.raises(TypeError):
            conn.execute("ECHO", bad)


def test_other_types_are_rejected(conn) -> None:
    for bad in (None, [1], {"a": 1}):
        with pytest.raises(TypeError):
            conn.execute("ECHO", bad)


def test_empty_command_raises(conn) -> None:
    with pytest.raises(ValueError):
        conn.execute()


def test_a_top_level_error_raises(conn, keyspace) -> None:
    key = keyspace("str")
    conn.execute("SET", key, "value")
    with pytest.raises(WrongTypeError) as caught:
        conn.execute("LPUSH", key, "x")
    assert caught.value.code == "WRONGTYPE"


def test_an_unrecognised_code_raises_servererror_itself(conn, keyspace) -> None:
    with pytest.raises(ServerError) as caught:
        conn.execute("GET", keyspace("k"), "too", "many")
    assert type(caught.value) is ServerError


def test_a_server_error_leaves_the_connection_usable(conn, keyspace) -> None:
    key = keyspace("str")
    conn.execute("SET", key, "value")
    with pytest.raises(WrongTypeError):
        conn.execute("LPUSH", key, "x")
    assert not conn.is_poisoned
    assert conn.execute("PING") == b"PONG"


def test_a_nested_error_stays_a_value(conn, keyspace) -> None:
    """EXEC returns an array in which individual commands may have failed."""
    key = keyspace("str")
    conn.execute("SET", key, "value")
    conn.execute("MULTI")
    conn.execute("SET", key, "1")
    conn.execute("LPUSH", key, "x")
    results = conn.execute("EXEC")
    assert results[0] == b"OK"
    assert isinstance(results[1], ErrorReply), (
        "raising on the first failed command would make a partially failed "
        "transaction unrepresentable"
    )


def test_execute_on_a_disconnected_connection_raises(port: int) -> None:
    from resp3_wire import ConnectionError

    conn = Connection(port=port)
    with pytest.raises(ConnectionError):
        conn.execute("PING")


def test_context_manager_closes(port: int) -> None:
    with Connection(port=port, timeout=5.0) as conn:
        conn.connect()
        assert conn.execute("PING") == b"PONG"
    assert not conn.is_connected
