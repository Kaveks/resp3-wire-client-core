"""ConnectionPool and Pipeline: the ordinary paths.

The pool's behaviour when connections fail underneath it is not exercised
here. It is exercised where your work is graded, so read the poisoning rules in
`instruction.md` rather than inferring the requirement from this file.
"""

from __future__ import annotations

import pytest

from resp3_wire import ConnectionPool, ErrorReply, Pipeline, WrongTypeError


@pytest.fixture
def pool(port: int):
    p = ConnectionPool(port=port, max_connections=4, timeout=5.0)
    yield p
    p.close()


def test_acquire_and_release(pool) -> None:
    assert pool.size == 0
    conn = pool.acquire()
    assert conn.execute("PING") == b"PONG"
    assert pool.size == 1 and pool.in_use == 1 and pool.idle == 0
    pool.release(conn)
    assert pool.idle == 1 and pool.in_use == 0


def test_an_idle_connection_is_reused(pool) -> None:
    first = pool.acquire()
    pool.release(first)
    assert pool.acquire() is first


def test_the_pool_grows_to_its_limit(pool) -> None:
    held = [pool.acquire() for _ in range(4)]
    assert pool.size == 4
    assert len({id(c) for c in held}) == 4
    for conn in held:
        pool.release(conn)


def test_exhaustion_raises_timeouterror(port: int) -> None:
    from resp3_wire import TimeoutError

    small = ConnectionPool(port=port, max_connections=1, timeout=0.5)
    try:
        held = small.acquire()
        with pytest.raises(TimeoutError):
            small.acquire()
        small.release(held)
    finally:
        small.close()


def test_releasing_a_foreign_connection_raises(pool, port: int) -> None:
    from resp3_wire import Connection

    outsider = Connection(port=port)
    outsider.connect()
    try:
        with pytest.raises(ValueError):
            pool.release(outsider)
    finally:
        outsider.close()


def test_connection_context_manager_releases(pool) -> None:
    with pool.connection() as conn:
        assert conn.execute("PING") == b"PONG"
    assert pool.in_use == 0 and pool.idle == 1


def test_close_refuses_further_acquire(port: int) -> None:
    from resp3_wire import ConnectionError

    p = ConnectionPool(port=port, max_connections=2)
    p.acquire()
    p.close()
    assert p.size == 0
    with pytest.raises(ConnectionError):
        p.acquire()


def test_pipeline_returns_replies_in_order(conn, keyspace) -> None:
    key = keyspace("k")
    pipe = conn.pipeline()
    assert isinstance(pipe, Pipeline)
    results = pipe.push("SET", key, "v").push("GET", key).push("STRLEN", key).execute()
    assert results == [b"OK", b"v", 1]


def test_pipeline_is_reusable_and_counts_its_queue(conn) -> None:
    pipe = conn.pipeline()
    assert len(pipe) == 0
    assert pipe.execute() == [], "an empty pipeline does no I/O"
    pipe.push("PING").push("PING")
    assert len(pipe) == 2
    assert pipe.execute() == [b"PONG", b"PONG"]
    assert len(pipe) == 0


def test_pipeline_reset(conn) -> None:
    pipe = conn.pipeline()
    pipe.push("PING")
    pipe.reset()
    assert len(pipe) == 0
    assert pipe.execute() == []


def test_a_failed_command_occupies_its_own_slot(conn, keyspace) -> None:
    """A server error does not raise from execute; it lands in the result list."""
    key = keyspace("str")
    results = (conn.pipeline()
               .push("SET", key, "v")
               .push("LPUSH", key, "x")
               .push("GET", key)
               .execute())
    assert results[0] == b"OK"
    assert isinstance(results[1], WrongTypeError)
    assert results[2] == b"v", "the slots after a failure stay aligned"


def test_the_asymmetry_between_a_slot_and_a_nested_error(conn, keyspace) -> None:
    """Pipeline slots carry exceptions; nested aggregate positions carry values."""
    key = keyspace("str")
    conn.execute("SET", key, "v")
    results = (conn.pipeline()
               .push("MULTI")
               .push("SET", key, "1")
               .push("LPUSH", key, "x")
               .push("EXEC")
               .execute())
    inner = results[3]
    assert isinstance(inner, list)
    assert isinstance(inner[1], ErrorReply)
    assert not isinstance(inner[1], BaseException)
