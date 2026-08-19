"""Channel 1: differential comparison against redis-py. 50 cases.

For each case the harness generates a random key prefix, runs a command
sequence through the client and the same sequence through redis-py against the
same server, and compares the results with the comparator.

Expected values are produced by redis-py at run time. No fixture file contains
an expected value anywhere in the harness, and keys are randomised per case per
run, so a lookup table keyed on command arguments cannot be built in advance.

The two clients work on disjoint keys under the same prefix. Running the
identical sequence against one shared key would make the second run see the
first run's mutations, so `RPUSH` would report a different length and every
sequence with a side effect would diverge for reasons that are not defects.

redis-py runs in a separate interpreter. The interpreter executing these cases
imports the client package and must never have redis-py on its path.
"""

from __future__ import annotations

import pytest

from resp3_wire import (
    BusyGroupError,
    Connection,
    ErrorReply,
    MovedError,
    NoScriptError,
    NEED_MORE,
    RedisError,
    RespParser,
    ServerError,
    VerbatimBytes,
    WrongTypeError,
)
from support.compare import compare

pytestmark = pytest.mark.channel("oracle")


@pytest.fixture(scope="session")
def agent3(server):
    conn = Connection(port=server.port, protocol=3, timeout=10.0)
    conn.connect()
    yield conn
    conn.close()


@pytest.fixture(scope="session")
def agent2(server):
    conn = Connection(port=server.port, protocol=2, timeout=10.0)
    conn.connect()
    yield conn
    conn.close()


@pytest.fixture
def run_both(agent3, agent2, oracle, prefix):
    """Run one sequence on both clients over disjoint keys and return results.

    `build` receives a key namer and returns a list of command tuples. The
    sequence is prefixed with an `UNLINK` of every key it names, so cases stay
    independent without serialising on a global flush.
    """

    def run(build, protocol: int = 3, keys: tuple[str, ...] = ("k",)):
        agent = agent3 if protocol == 3 else agent2

        def namer(side: str):
            return lambda name: f"{prefix}:{side}:{name}"

        agent_keys = [namer("a")(k) for k in keys]
        oracle_keys = [namer("b")(k) for k in keys]

        agent_cmds = [("UNLINK", *agent_keys)] + list(build(namer("a")))
        oracle_cmds = [("UNLINK", *oracle_keys)] + list(build(namer("b")))

        agent_results = []
        for cmd in agent_cmds:
            try:
                agent_results.append(agent.execute(*cmd))
            except RedisError as exc:
                agent_results.append(exc)
        oracle_results = oracle(oracle_cmds, protocol=protocol)
        assert len(agent_results) == len(oracle_results)
        # Drop the UNLINK, whose count depends on what a previous run left.
        return agent_results[1:], oracle_results[1:]

    return run


def check_last(pair) -> tuple:
    agent_results, oracle_results = pair
    compare(agent_results[-1], oracle_results[-1])
    return agent_results[-1], oracle_results[-1]


# ===========================================================================
# Strings, 8. RESP2 designated: SET with options, GET on a missing key, APPEND.
# ===========================================================================


def test_set_with_options(run_both) -> None:
    """RESP2 designated."""
    check_last(run_both(
        lambda k: [("SET", k("k"), "first"), ("SET", k("k"), "second", "XX", "GET")],
        protocol=2,
    ))


def test_get_on_a_missing_key(run_both) -> None:
    """RESP2 designated: a null bulk under RESP2, a null under RESP3."""
    check_last(run_both(lambda k: [("GET", k("k"))], protocol=2))


def test_append(run_both) -> None:
    """RESP2 designated."""
    check_last(run_both(
        lambda k: [("APPEND", k("k"), "abc"), ("APPEND", k("k"), "def"),
                   ("GET", k("k"))],
        protocol=2,
    ))


def test_getrange(run_both) -> None:
    check_last(run_both(
        lambda k: [("SET", k("k"), "Hello wire protocol"), ("GETRANGE", k("k"), 6, 9)]
    ))


def test_incr(run_both) -> None:
    check_last(run_both(
        lambda k: [("SET", k("k"), 10), ("INCR", k("k")), ("INCR", k("k"))]
    ))


def test_incrbyfloat(run_both) -> None:
    """INCRBYFLOAT returns a bulk string, not a double, under both protocols."""
    check_last(run_both(
        lambda k: [("SET", k("k"), "10.5"), ("INCRBYFLOAT", k("k"), "0.25")]
    ))


def test_binary_safe_value(run_both) -> None:
    payload = b"before\r\nafter\x00tail\xff\xfe"
    check_last(run_both(
        lambda k: [("SET", k("k"), payload), ("GET", k("k")), ("STRLEN", k("k"))]
    ))


def test_empty_value(run_both) -> None:
    check_last(run_both(
        lambda k: [("SET", k("k"), b""), ("GET", k("k")), ("STRLEN", k("k"))]
    ))


# ===========================================================================
# Lists, 6. RESP2 designated: RPUSH, LRANGE on a missing key.
# ===========================================================================


def test_rpush(run_both) -> None:
    """RESP2 designated."""
    check_last(run_both(
        lambda k: [("RPUSH", k("k"), "a", "b", "c")], protocol=2
    ))


def test_lrange_on_a_missing_key(run_both) -> None:
    """RESP2 designated: an empty array under both, never a null."""
    check_last(run_both(lambda k: [("LRANGE", k("k"), 0, -1)], protocol=2))


def test_lrange_over_a_large_range(run_both) -> None:
    check_last(run_both(
        lambda k: [("RPUSH", k("k"), *[f"item-{i}" for i in range(500)]),
                   ("LRANGE", k("k"), 0, -1)]
    ))


def test_lpop_with_count(run_both) -> None:
    check_last(run_both(
        lambda k: [("RPUSH", k("k"), "a", "b", "c", "d"), ("LPOP", k("k"), 3)]
    ))


def test_linsert(run_both) -> None:
    check_last(run_both(
        lambda k: [("RPUSH", k("k"), "a", "c"),
                   ("LINSERT", k("k"), "BEFORE", "c", "b"),
                   ("LRANGE", k("k"), 0, -1)]
    ))


def test_lpos(run_both) -> None:
    check_last(run_both(
        lambda k: [("RPUSH", k("k"), "a", "b", "c", "b"), ("LPOS", k("k"), "b", "COUNT", 0)]
    ))


# ===========================================================================
# Hashes, 6. RESP2 designated: HSET, HGETALL, a binary field name.
# ===========================================================================


def test_hset(run_both) -> None:
    """RESP2 designated."""
    check_last(run_both(
        lambda k: [("HSET", k("k"), "f1", "v1", "f2", "v2")], protocol=2
    ))


def test_hgetall_flat_array_under_resp2(run_both) -> None:
    """RESP2 designated. The wire carries a flat array; a map here is wrong."""
    agent, _ = check_last(run_both(
        lambda k: [("HSET", k("k"), "f1", "v1", "f2", "v2"), ("HGETALL", k("k"))],
        protocol=2,
    ))
    assert type(agent) is list, (
        f"HGETALL under RESP2 must be a flat list, got {type(agent).__name__}"
    )


def test_hash_field_with_a_binary_name(run_both) -> None:
    """RESP2 designated."""
    field = b"field\r\nwith\x00bytes"
    check_last(run_both(
        lambda k: [("HSET", k("k"), field, "v"), ("HGET", k("k"), field)],
        protocol=2,
    ))


def test_hstrlen(run_both) -> None:
    check_last(run_both(
        lambda k: [("HSET", k("k"), "f", "abcdef"), ("HSTRLEN", k("k"), "f")]
    ))


def test_hdel(run_both) -> None:
    check_last(run_both(
        lambda k: [("HSET", k("k"), "f1", "v1", "f2", "v2"),
                   ("HDEL", k("k"), "f1", "nope"), ("HGETALL", k("k"))]
    ))


def test_hexpire(run_both) -> None:
    """Requires Redis 7.4, which D12 pins."""
    check_last(run_both(
        lambda k: [("HSET", k("k"), "f", "v"),
                   ("HEXPIRE", k("k"), 100, "FIELDS", 1, "f")]
    ))


# ===========================================================================
# Sets, 5. RESP2 designated: SADD, SUNION.
# ===========================================================================


def test_sadd(run_both) -> None:
    """RESP2 designated."""
    check_last(run_both(lambda k: [("SADD", k("k"), "a", "b", "c")], protocol=2))


def test_smembers_yields_a_set_under_resp3(run_both) -> None:
    """The RESP3 `~` frame must produce a Python set.

    The comparator permits agent SET against redis-py LIST, because redis-py
    returns sets as lists always. That permission would let a list pass here,
    so this case asserts the type directly. It is never designated RESP2: a
    RESP2 connection receives `*` and cannot exercise the property.
    """
    agent, _ = check_last(run_both(
        lambda k: [("SADD", k("k"), "a", "b", "c"), ("SMEMBERS", k("k"))]
    ))
    assert type(agent) is set, (
        f"a RESP3 set frame must produce a Python set, got {type(agent).__name__}"
    )


def test_sunion_is_a_list_under_resp2(run_both) -> None:
    """RESP2 designated, and the mirror of the SMEMBERS assertion.

    SUNION returns `~` under RESP3 and `*` under RESP2. An implementation that
    post-processes by command name rather than by wire type returns a set in
    both and fails here.
    """
    agent, _ = check_last(run_both(
        lambda k: [("SADD", k("s1"), "a", "b"), ("SADD", k("s2"), "b", "c"),
                   ("SADD", k("s3"), "d"),
                   ("SUNION", k("s1"), k("s2"), k("s3"))],
        protocol=2, keys=("s1", "s2", "s3"),
    ))
    assert type(agent) is list, (
        f"SUNION under RESP2 must be a list, got {type(agent).__name__}; the "
        f"wire type determines the Python type, not the command name"
    )


def test_sintercard(run_both) -> None:
    check_last(run_both(
        lambda k: [("SADD", k("s1"), "a", "b", "c"), ("SADD", k("s2"), "b", "c", "d"),
                   ("SINTERCARD", 2, k("s1"), k("s2"))],
        keys=("s1", "s2"),
    ))


def test_smismember(run_both) -> None:
    check_last(run_both(
        lambda k: [("SADD", k("k"), "a", "b"), ("SMISMEMBER", k("k"), "a", "z", "b")]
    ))


# ===========================================================================
# Sorted sets, 6. RESP2 designated: ZADD, ZRANGE WITHSCORES, ZSCORE, infinity.
# ===========================================================================


def test_zadd(run_both) -> None:
    """RESP2 designated."""
    check_last(run_both(
        lambda k: [("ZADD", k("k"), 1, "a", 2, "b")], protocol=2
    ))


def test_zrange_withscores_under_resp2(run_both) -> None:
    """RESP2 designated: a flat array of member and score strings."""
    check_last(run_both(
        lambda k: [("ZADD", k("k"), "1.5", "a", "2.5", "b"),
                   ("ZRANGE", k("k"), 0, -1, "WITHSCORES")],
        protocol=2,
    ))


def test_zscore_bulk_string_under_resp2(run_both) -> None:
    """RESP2 designated: a bulk string, where RESP3 would give a double.

    Section 3.2 enumerates ZSCORE once and section 3.3 designates it RESP2, so
    there is no second ZSCORE case for the RESP3 double. The `ZADD GT` case
    below ends in a RESP3 ZSCORE, so a `,` frame is still compared.
    """
    agent, _ = check_last(run_both(
        lambda k: [("ZADD", k("k"), "1.5", "m"), ("ZSCORE", k("k"), "m")],
        protocol=2,
    ))
    assert type(agent) is bytes, (
        f"ZSCORE under RESP2 must be a bulk string, got {type(agent).__name__}"
    )


def test_infinity_scores_under_resp2(run_both) -> None:
    """RESP2 designated: infinity arrives as the text `inf`."""
    check_last(run_both(
        lambda k: [("ZADD", k("k"), "+inf", "hi", "-inf", "lo"),
                   ("ZRANGE", k("k"), 0, -1, "WITHSCORES")],
        protocol=2,
    ))


def test_zrangebylex(run_both) -> None:
    check_last(run_both(
        lambda k: [("ZADD", k("k"), 0, "a", 0, "b", 0, "c", 0, "d"),
                   ("ZRANGEBYLEX", k("k"), "[b", "(d")]
    ))


def test_zadd_gt(run_both) -> None:
    check_last(run_both(
        lambda k: [("ZADD", k("k"), 5, "m"), ("ZADD", k("k"), "GT", "CH", 3, "m"),
                   ("ZSCORE", k("k"), "m")]
    ))


# ===========================================================================
# Keyspace and generic, 5. No RESP2 designation: these shapes do not differ.
# ===========================================================================


def test_type(run_both) -> None:
    check_last(run_both(lambda k: [("RPUSH", k("k"), "a"), ("TYPE", k("k"))]))


def test_ttl_on_a_persistent_key(run_both) -> None:
    check_last(run_both(lambda k: [("SET", k("k"), "v"), ("TTL", k("k"))]))


def test_ttl_on_a_missing_key(run_both) -> None:
    check_last(run_both(lambda k: [("TTL", k("k"))]))


def test_expiretime_on_a_volatile_key(run_both) -> None:
    """An absolute unix time, so it is stable across the two clients' reads.

    TTL on a volatile key is not, because the two reads can straddle a second
    boundary.
    """
    check_last(run_both(
        lambda k: [("SET", k("k"), "v"), ("EXPIREAT", k("k"), 4102444800),
                   ("EXPIRETIME", k("k"))]
    ))


def test_object_encoding(run_both) -> None:
    check_last(run_both(
        lambda k: [("RPUSH", k("k"), *[str(i) for i in range(200)]),
                   ("OBJECT", "ENCODING", k("k"))]
    ))


# ===========================================================================
# Transactions, 4. RESP2 designated: EXEC containing a per command error.
# ===========================================================================


def test_multi_exec_through_a_pipeline(agent3, oracle, prefix) -> None:
    key = f"{prefix}:a:k"
    pipe = agent3.pipeline()
    results = (pipe.push("MULTI").push("SET", key, "v").push("INCRBY", key, 0)
               .push("EXEC").execute())
    expected = oracle([("MULTI",), ("SET", f"{prefix}:b:k", "v"),
                       ("INCRBY", f"{prefix}:b:k", 0), ("EXEC",)])
    compare(results[3], expected[3])


def test_exec_containing_a_per_command_error(run_both, agent2, prefix) -> None:
    """RESP2 designated, and carries the D11 nested-error identity assertion.

    A pipeline slot carries an exception; an error nested inside EXEC's array
    stays an ErrorReply value. The asymmetry is deliberate and is the property
    under test here.
    """
    key = f"{prefix}:a:k"
    agent2.execute("UNLINK", key)
    pipe = agent2.pipeline()
    results = (pipe.push("MULTI").push("SET", key, "v")
               .push("LPUSH", key, "x").push("EXEC").execute())
    inner = results[3]
    assert type(inner) is list and len(inner) == 2, f"EXEC array: {inner!r}"
    assert isinstance(inner[1], ErrorReply), (
        f"a per command error nested inside EXEC must stay an ErrorReply "
        f"value, got {type(inner[1]).__name__}"
    )
    assert not isinstance(inner[1], BaseException), (
        "a nested error must not be an exception instance"
    )


def test_discard(agent3, oracle, prefix) -> None:
    key_a, key_b = f"{prefix}:a:k", f"{prefix}:b:k"
    pipe = agent3.pipeline()
    results = pipe.push("MULTI").push("SET", key_a, "v").push("DISCARD").execute()
    expected = oracle([("MULTI",), ("SET", key_b, "v"), ("DISCARD",)])
    compare(results[2], expected[2])
    compare(agent3.execute("GET", key_a), oracle([("GET", key_b)])[0])


def test_watch_that_aborts(agent3, server, oracle, prefix) -> None:
    """A watched key changed by another connection makes EXEC return null."""
    key = f"{prefix}:a:k"
    agent3.execute("SET", key, "original")
    agent3.execute("WATCH", key)
    intruder = Connection(port=server.port, protocol=3, timeout=10.0)
    intruder.connect()
    try:
        intruder.execute("SET", key, "changed")
    finally:
        intruder.close()
    pipe = agent3.pipeline()
    results = pipe.push("MULTI").push("SET", key, "mine").push("EXEC").execute()
    assert results[2] is None, (
        f"EXEC after a broken WATCH must be null, got {results[2]!r}"
    )


# ===========================================================================
# Protocol and RESP3, 5. RESP2 designated: the protocol=2 negotiation case.
# ===========================================================================


def test_negotiated_version_under_protocol_3(agent3) -> None:
    assert agent3.protocol_version == 3


def test_negotiated_version_under_protocol_2(agent2) -> None:
    """RESP2 designated."""
    assert agent2.protocol_version == 2
    assert agent2.server_info == {}, (
        "protocol=2 sends no HELLO, so server_info stays empty"
    )


def test_server_info_after_hello(agent3, oracle) -> None:
    info = agent3.server_info
    expected = oracle([("HELLO", 3)])[0]
    assert isinstance(info, dict) and info, "server_info must be a populated dict"
    # `id` is per connection, so only the stable fields compare.
    for field in (b"server", b"version", b"proto"):
        assert field in info, f"server_info is missing {field!r}"
        compare(info[field], expected[field], (field,))


def test_debug_protocol_map_and_array(run_both) -> None:
    check_last(run_both(
        lambda k: [("DEBUG", "PROTOCOL", "map"), ("DEBUG", "PROTOCOL", "array")]
    ))


def test_verbatim_reply_carries_its_format(run_both) -> None:
    agent, _ = check_last(run_both(lambda k: [("DEBUG", "PROTOCOL", "verbatim")]))
    assert isinstance(agent, VerbatimBytes), (
        f"a verbatim frame must produce VerbatimBytes, got {type(agent).__name__}"
    )
    assert agent.format == "txt", f"format {agent.format!r}, expected 'txt'"


# ===========================================================================
# Error mapping, 5. Each from a real server condition, except MOVED.
# ===========================================================================


def test_wrongtype_raises_wrongtypeerror(agent3, oracle, prefix) -> None:
    """Carries the D11 exception-identity assertion."""
    key_a, key_b = f"{prefix}:a:k", f"{prefix}:b:k"
    agent3.execute("SET", key_a, "string")
    with pytest.raises(WrongTypeError) as caught:
        agent3.execute("LPUSH", key_a, "x")
    expected = oracle([("SET", key_b, "string"), ("LPUSH", key_b, "x")])[1]
    compare(caught.value, expected)
    assert caught.value.code == "WRONGTYPE"


def test_noscript_raises_noscripterror(agent3, oracle) -> None:
    digest = "ffffffffffffffffffffffffffffffffffffffff"
    with pytest.raises(NoScriptError) as caught:
        agent3.execute("EVALSHA", digest, 0)
    compare(caught.value, oracle([("EVALSHA", digest, 0)])[0])


def test_busygroup_raises_busygrouperror(agent3, oracle, prefix) -> None:
    key_a, key_b = f"{prefix}:a:k", f"{prefix}:b:k"
    agent3.execute("XGROUP", "CREATE", key_a, "g", "$", "MKSTREAM")
    with pytest.raises(BusyGroupError) as caught:
        agent3.execute("XGROUP", "CREATE", key_a, "g", "$", "MKSTREAM")
    expected = oracle([
        ("XGROUP", "CREATE", key_b, "g", "$", "MKSTREAM"),
        ("XGROUP", "CREATE", key_b, "g", "$", "MKSTREAM"),
    ])[1]
    compare(caught.value, expected)


def test_unrecognised_code_raises_servererror_itself(agent3, oracle, prefix) -> None:
    key_a, key_b = f"{prefix}:a:k", f"{prefix}:b:k"
    with pytest.raises(ServerError) as caught:
        agent3.execute("GET", key_a, "surplus", "arguments")
    assert type(caught.value) is ServerError, (
        f"an unrecognised code must raise ServerError itself, not "
        f"{type(caught.value).__name__}"
    )
    compare(caught.value, oracle([("GET", key_b, "surplus", "arguments")])[0])


def test_moved_parses_slot_and_address() -> None:
    """The one error case that does not go through the live server.

    A non clustered server cannot produce MOVED, so this drives the parser
    directly and asserts the fields a MOVED reply is only actionable with.
    """
    parser = RespParser()
    parser.feed(b"-MOVED 3999 127.0.0.1:6381\r\n")
    reply = parser.gets()
    assert reply is not NEED_MORE, "a complete error frame was fed"
    assert isinstance(reply, ErrorReply), f"expected ErrorReply, got {reply!r}"
    assert reply.code == "MOVED"
    from resp3_wire.errors import exception_for

    exc = exception_for(reply.code, reply.message)
    assert isinstance(exc, MovedError), (
        f"the MOVED code must map to MovedError, got {type(exc).__name__}"
    )
    assert exc.slot == 3999, f"slot {exc.slot!r}, expected 3999"
    assert exc.address == "127.0.0.1:6381", f"address {exc.address!r}"
