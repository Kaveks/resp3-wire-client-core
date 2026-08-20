"""Channel 4: caching and invalidation. 20 cases.

One requirement, from `docs/API.md` section 7A.5: a cached read must never
return a value the server has already invalidated. Everything else in the
caching surface exists so that can be observed.

Expectations come from the server, never from redis-py. D35 records why: redis-py
has its own caching semantics, and comparing against them would grade whether
the agent reimplemented redis-py rather than whether the protocol works.

Determinism. No case sleeps to let an invalidation arrive. The ordering every
case relies on is a property of the server, not of timing: Redis is single
threaded, so when a write issued on the second connection has returned its
reply, the server has already processed that write and has already written the
invalidation to the socket of every connection tracking the key. Whether the
client under test has *parsed* it yet is exactly what is being graded. A write
that has returned is therefore a happens-before edge, and no case needs anything
weaker.

Every freshness case also asserts that hits occurred. Section 6.3 requires it and
D24 is the general rule: an assertion whose success is reachable without the
property holding is not an assertion, and a cache that never caches satisfies
every freshness claim here trivially.
"""

from __future__ import annotations

import threading

import pytest

from resp3_wire import Connection, ConnectionPool

pytestmark = pytest.mark.channel("caching")

CACHE_SIZE = 128
BARRIER_TIMEOUT = 10.0
WORKERS = 4
# Bounded, and used only where an ordering cannot be established by a round trip.
RACE_ATTEMPTS = 15


@pytest.fixture
def make_cache_pool(server):
    """Caching pools created by a case, all closed during teardown."""
    created: list[ConnectionPool] = []

    def build(**kwargs) -> ConnectionPool:
        kwargs.setdefault("port", server.port)
        kwargs.setdefault("cache_size", CACHE_SIZE)
        kwargs.setdefault("timeout", 10.0)
        pool = ConnectionPool(**kwargs)
        created.append(pool)
        return pool

    yield build
    for pool in created:
        try:
            pool.close()
        except Exception:  # noqa: BLE001
            pass


@pytest.fixture
def writer(server):
    """A second connection, outside the pool and without caching.

    Section 6.3: invalidation is induced through a route the code under test
    does not control. An implementation that invalidates on its own writes and
    nothing else fails every case in this channel.
    """
    conn = Connection(port=server.port, protocol=3, timeout=10.0)
    conn.connect()
    yield conn
    conn.close()


def read(pool: ConnectionPool, *args):
    with pool.connection() as conn:
        return conn.execute(*args)


def assert_hits_occurred(pool: ConnectionPool, note: str = "") -> None:
    """Section 6.3. A cache that never caches passes freshness trivially."""
    hits = pool.cache_stats["hits"]
    assert hits > 0, (
        f"no cache hit occurred anywhere in this case{note}, so its freshness "
        f"assertion says nothing: a client that caches nothing satisfies it"
    )


# ---------------------------------------------------------------------------
# The cache works at all. 3 cases.
# ---------------------------------------------------------------------------


def test_a_repeated_read_is_served_from_cache(make_cache_pool, writer, prefix) -> None:
    pool = make_cache_pool()
    key = f"{prefix}:k"
    writer.execute("SET", key, "value")
    assert read(pool, "GET", key) == b"value"
    before = pool.cache_stats["hits"]
    assert read(pool, "GET", key) == b"value"
    assert pool.cache_stats["hits"] == before + 1, (
        f"a repeated read of an unchanged key did not hit: {pool.cache_stats}"
    )


def test_cache_stats_counts_misses_and_entries(make_cache_pool, writer, prefix) -> None:
    pool = make_cache_pool()
    key = f"{prefix}:k"
    writer.execute("SET", key, "value")
    before = pool.cache_stats
    read(pool, "GET", key)
    after = pool.cache_stats
    assert after["misses"] == before["misses"] + 1, (
        f"the first read of a key must be a miss: {before} -> {after}"
    )
    assert after["entries"] == before["entries"] + 1, (
        f"a cacheable reply must produce an entry: {before} -> {after}"
    )


def test_cache_clear_empties_the_cache(make_cache_pool, writer, prefix) -> None:
    pool = make_cache_pool()
    key = f"{prefix}:k"
    writer.execute("SET", key, "value")
    read(pool, "GET", key)
    read(pool, "GET", key)
    cleared = pool.cache_stats
    pool.cache_clear()
    assert pool.cache_stats["entries"] == 0, "cache_clear must drop every entry"
    assert pool.cache_stats["hits"] == cleared["hits"], (
        "cache_clear must not touch the counters"
    )
    misses = pool.cache_stats["misses"]
    assert read(pool, "GET", key) == b"value"
    assert pool.cache_stats["misses"] == misses + 1, (
        "a read after cache_clear must miss"
    )


# ---------------------------------------------------------------------------
# Invalidation by another client. 4 cases.
#
# The write returns before the assertion, so the server has already sent the
# invalidation. Nothing here waits for it.
# ---------------------------------------------------------------------------


def test_a_written_key_is_not_served_stale(make_cache_pool, writer, prefix) -> None:
    pool = make_cache_pool()
    key = f"{prefix}:k"
    writer.execute("SET", key, "first")
    assert read(pool, "GET", key) == b"first"
    assert read(pool, "GET", key) == b"first"
    writer.execute("SET", key, "second")
    assert read(pool, "GET", key) == b"second", (
        "a key written by another client was served from cache"
    )
    assert_hits_occurred(pool)


def test_a_deleted_key_is_not_served_stale(make_cache_pool, writer, prefix) -> None:
    pool = make_cache_pool()
    key = f"{prefix}:k"
    writer.execute("SET", key, "present")
    assert read(pool, "GET", key) == b"present"
    assert read(pool, "GET", key) == b"present"
    writer.execute("UNLINK", key)
    assert read(pool, "GET", key) is None, (
        "a key deleted by another client was served from cache"
    )
    assert_hits_occurred(pool)


def test_an_expired_key_is_not_served_stale(make_cache_pool, writer, prefix) -> None:
    """Expiry, confirmed through the second connection rather than waited for.

    The bounded loop below polls the *server* for a fact, through a connection
    that is not the code under test. It is not waiting for an invalidation to be
    delivered, which section 6.5 forbids; it establishes that the key is gone
    before anything is asserted about the cache.
    """
    pool = make_cache_pool()
    key = f"{prefix}:k"
    writer.execute("SET", key, "transient")
    assert read(pool, "GET", key) == b"transient"
    assert read(pool, "GET", key) == b"transient"
    writer.execute("PEXPIRE", key, 1)
    for _ in range(200):
        if writer.execute("GET", key) is None:
            break
        writer.execute("PING")
    else:
        pytest.fail("the key never expired on the server; the case proves nothing")
    assert read(pool, "GET", key) is None, (
        "a key that expired on the server was served from cache"
    )
    assert_hits_occurred(pool)


def test_invalidations_counter_increments(make_cache_pool, writer, prefix) -> None:
    pool = make_cache_pool()
    key = f"{prefix}:k"
    writer.execute("SET", key, "first")
    read(pool, "GET", key)
    read(pool, "GET", key)
    before = pool.cache_stats["invalidations"]
    writer.execute("SET", key, "second")
    assert read(pool, "GET", key) == b"second"
    assert pool.cache_stats["invalidations"] > before, (
        f"an invalidation arrived but was not counted: {pool.cache_stats}"
    )
    assert_hits_occurred(pool)


# ---------------------------------------------------------------------------
# Invalidation racing the read. 4 cases. This group is the channel.
#
# An implementation that caches a value and processes the pending invalidation
# afterwards passes the group above and fails here.
# ---------------------------------------------------------------------------


def test_a_write_landing_while_the_reply_is_in_flight_is_never_served_stale(
    make_cache_pool, writer, prefix
) -> None:
    """The invalidation is already buffered when the value would be cached.

    Each attempt races a write against a read, so the racing read may return
    either value and nothing is asserted about it. What is asserted is the read
    *after* the write has returned: by then the server has processed the write
    and sent the invalidation, so a client that cached the old value during the
    race and processed the invalidation later serves it here.

    Bounded retries, per section 6.5, and the claim is over all of them: no
    attempt ever returned a stale value.
    """
    pool = make_cache_pool(max_connections=2)
    key = f"{prefix}:k"
    stale: list[str] = []

    for attempt in range(RACE_ATTEMPTS):
        old, new = f"v{attempt}a".encode(), f"v{attempt}b".encode()
        writer.execute("SET", key, old)
        read(pool, "GET", key)

        start = threading.Barrier(2, timeout=BARRIER_TIMEOUT)

        def racing_write() -> None:
            start.wait()
            writer.execute("SET", key, new)

        thread = threading.Thread(target=racing_write)
        thread.start()
        start.wait()
        read(pool, "GET", key)          # may return either value; unconstrained
        thread.join(timeout=BARRIER_TIMEOUT)
        assert not thread.is_alive(), "the racing writer did not finish"

        observed = read(pool, "GET", key)
        if observed != new:
            stale.append(f"attempt {attempt}: read {observed!r}, server holds {new!r}")

    assert not stale, (
        "a value the server had already invalidated was served from cache:\n  "
        + "\n  ".join(stale)
    )
    assert_hits_occurred(pool)


def test_a_write_landing_during_an_unrelated_command_is_not_served_stale(
    make_cache_pool, writer, prefix
) -> None:
    """The invalidation arrives while the connection is busy with something else."""
    pool = make_cache_pool(max_connections=1)
    key = f"{prefix}:k"
    writer.execute("SET", key, "first")
    assert read(pool, "GET", key) == b"first"
    assert read(pool, "GET", key) == b"first"

    writer.execute("SET", key, "second")
    # An unrelated command on the same pool, which is where an implementation
    # that only looks for invalidations between commands would notice.
    assert read(pool, "ECHO", b"unrelated") == b"unrelated"
    assert read(pool, "GET", key) == b"second", (
        "the invalidation arrived during an unrelated command and was ignored"
    )
    assert_hits_occurred(pool)


def test_a_write_landing_while_the_connection_is_idle_is_not_served_stale(
    make_cache_pool, writer, prefix
) -> None:
    """The connection that cached the value is back in the pool, unread.

    docs/API.md section 7A.5: a connection idle in the pool still has a socket
    with unread bytes on it, and invalidations for keys it read do not stop
    arriving because nobody is borrowing it.
    """
    pool = make_cache_pool(max_connections=2)
    key = f"{prefix}:k"
    writer.execute("SET", key, "first")
    with pool.connection() as conn:
        assert conn.execute("GET", key) == b"first"
    assert read(pool, "GET", key) == b"first"
    # Every pool connection is idle from here until the read below.
    writer.execute("SET", key, "second")
    assert read(pool, "GET", key) == b"second", (
        "the invalidation arrived on an idle connection and was never consumed"
    )
    assert_hits_occurred(pool)


def test_a_burst_of_writes_during_a_burst_of_reads_leaves_nothing_stale(
    make_cache_pool, writer, prefix
) -> None:
    """Reads and writes overlapping, then a settled comparison against the server."""
    pool = make_cache_pool(max_connections=WORKERS)
    key = f"{prefix}:k"
    writer.execute("SET", key, "v0")
    read(pool, "GET", key)

    seen: list[bytes] = []
    lock = threading.Lock()
    stop = threading.Event()
    written = {f"v{i}".encode() for i in range(41)} | {b"v0"}

    def reader() -> None:
        while not stop.is_set():
            value = read(pool, "GET", key)
            with lock:
                seen.append(value)

    threads = [threading.Thread(target=reader) for _ in range(WORKERS)]
    for thread in threads:
        thread.start()
    try:
        for i in range(1, 41):
            writer.execute("SET", key, f"v{i}".encode())
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=30.0)
    assert not any(t.is_alive() for t in threads), "a reader did not finish"

    unknown = [v for v in seen if v not in written]
    assert not unknown, f"reads returned values the server never held: {unknown[:5]}"

    final = writer.execute("GET", key)
    assert read(pool, "GET", key) == final, (
        f"after the burst the server holds {final!r} and the cache served "
        f"something else"
    )
    assert_hits_occurred(pool)


# ---------------------------------------------------------------------------
# Invalidation across pooled connections. 4 cases.
# ---------------------------------------------------------------------------


def test_a_value_cached_through_one_connection_is_evicted_by_a_frame_on_another(
    make_cache_pool, writer, prefix
) -> None:
    """Two connections both tracking the key; the cache is one, shared.

    `cache_clear` between the two reads is what makes the second read reach the
    server, so both connections register as readers of the key and the server
    sends the invalidation to both. Which of them the pool happens to hand out
    afterwards is not controllable, and does not need to be: the requirement is
    that the eviction reaches the shared cache whichever connection receives it.
    """
    pool = make_cache_pool(max_connections=2)
    key = f"{prefix}:k"
    writer.execute("SET", key, "first")

    first = pool.acquire()
    second = pool.acquire()
    try:
        assert first.execute("GET", key) == b"first"
        pool.cache_clear()
        assert second.execute("GET", key) == b"first"
        assert second.execute("GET", key) == b"first"
    finally:
        pool.release(first)
        pool.release(second)

    writer.execute("SET", key, "second")
    assert read(pool, "GET", key) == b"second", (
        "the invalidation reached a connection but not the pool-wide cache"
    )
    assert_hits_occurred(pool)


def test_concurrent_workers_see_no_stale_value_after_a_write(
    make_cache_pool, writer, prefix
) -> None:
    pool = make_cache_pool(max_connections=WORKERS)
    key = f"{prefix}:k"
    writer.execute("SET", key, "before")
    for _ in range(WORKERS):
        read(pool, "GET", key)

    writer.execute("SET", key, "after")
    ready = threading.Barrier(WORKERS, timeout=BARRIER_TIMEOUT)
    results: list = [None] * WORKERS

    def body(index: int) -> None:
        ready.wait()
        results[index] = read(pool, "GET", key)

    threads = [threading.Thread(target=body, args=(i,)) for i in range(WORKERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30.0)
    assert not any(t.is_alive() for t in threads), "a worker did not finish"
    stale = [r for r in results if r != b"after"]
    assert not stale, (
        f"{len(stale)} of {WORKERS} concurrent readers were served the value the "
        f"server had already invalidated: {stale[:3]}"
    )
    assert_hits_occurred(pool)


def test_a_worker_holding_a_connection_does_not_block_eviction(
    make_cache_pool, writer, prefix
) -> None:
    """One borrower parked at a barrier must not stop another seeing the write."""
    pool = make_cache_pool(max_connections=2)
    key = f"{prefix}:k"
    writer.execute("SET", key, "first")
    read(pool, "GET", key)
    read(pool, "GET", key)

    holding = threading.Barrier(2, timeout=BARRIER_TIMEOUT)
    released = threading.Event()
    failure: list[BaseException] = []

    def parked() -> None:
        try:
            with pool.connection() as conn:
                assert conn.execute("PING") == b"PONG"
                holding.wait()
                released.wait(timeout=BARRIER_TIMEOUT)
        except BaseException as exc:  # noqa: BLE001
            failure.append(exc)

    thread = threading.Thread(target=parked)
    thread.start()
    try:
        holding.wait()
        writer.execute("SET", key, "second")
        assert read(pool, "GET", key) == b"second", (
            "eviction waited for a borrower to give its connection back"
        )
    finally:
        released.set()
        thread.join(timeout=30.0)
    assert not failure, f"the parked worker failed: {failure[0]!r}"
    assert_hits_occurred(pool)


def test_eviction_holds_no_lock_across_socket_io(make_cache_pool, writer, prefix) -> None:
    """The pool channel's barrier construction, applied to a caching pool.

    docs/API.md section 6.4 forbids holding a lock across socket I/O and section
    7A.5 says cache correctness is not an exemption. Workers that each hold a
    connection and block before releasing cannot all arrive if eviction
    serialises them, and the case fails on the barrier's own timeout rather than
    on any judgement about speed.
    """
    pool = make_cache_pool(max_connections=WORKERS)
    key = f"{prefix}:k"
    writer.execute("SET", key, "value")
    read(pool, "GET", key)

    barrier = threading.Barrier(WORKERS, timeout=BARRIER_TIMEOUT)
    observed: list[int] = []
    lock = threading.Lock()
    failure: list[BaseException] = []

    def body(index: int) -> None:
        try:
            conn = pool.acquire()
            try:
                conn.execute("GET", key)
                with lock:
                    observed.append(pool.in_use)
                barrier.wait()
            finally:
                pool.release(conn)
        except BaseException as exc:  # noqa: BLE001
            failure.append(exc)

    threads = [threading.Thread(target=body, args=(i,)) for i in range(WORKERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30.0)
    assert not failure, f"a worker failed: {failure[0]!r}"
    assert observed and max(observed) == WORKERS, (
        f"peak concurrent utilisation was {max(observed) if observed else 0}, "
        f"expected {WORKERS}; a caching pool that serialises its borrowers is "
        f"holding a lock somewhere it may not"
    )
    assert_hits_occurred(pool)


# ---------------------------------------------------------------------------
# Scope and configuration. 3 cases.
# ---------------------------------------------------------------------------


def test_cache_size_zero_caches_nothing(make_cache_pool, writer, prefix) -> None:
    pool = make_cache_pool(cache_size=0)
    key = f"{prefix}:k"
    writer.execute("SET", key, "value")
    for _ in range(4):
        assert read(pool, "GET", key) == b"value"
    stats = pool.cache_stats
    assert stats["hits"] == 0 and stats["entries"] == 0, (
        f"cache_size=0 is off, but the pool reports {stats}"
    )


def test_caching_under_protocol_2_raises_value_error(server) -> None:
    """docs/API.md section 7A.3: RESP2 has no channel for an invalidation."""
    with pytest.raises(ValueError):
        ConnectionPool(port=server.port, protocol=2, cache_size=CACHE_SIZE)


def test_a_pipelined_read_is_neither_served_from_cache_nor_populates_it(
    make_cache_pool, writer, prefix
) -> None:
    pool = make_cache_pool()
    key = f"{prefix}:k"
    writer.execute("SET", key, "first")

    with pool.connection() as conn:
        before = pool.cache_stats
        results = conn.pipeline().push("GET", key).execute()
        assert results == [b"first"]
        after = pool.cache_stats
    assert after["entries"] == before["entries"], (
        f"a pipelined read populated the cache: {before} -> {after}"
    )
    assert after["hits"] == before["hits"], (
        "a pipelined read was served from cache"
    )

    # And a pipelined read still reaches the server once the key is cached.
    assert read(pool, "GET", key) == b"first"
    assert read(pool, "GET", key) == b"first"
    writer.execute("SET", key, "second")
    with pool.connection() as conn:
        assert conn.pipeline().push("GET", key).execute() == [b"second"]
    assert_hits_occurred(pool)


# ---------------------------------------------------------------------------
# Flush and overflow. 2 cases.
# ---------------------------------------------------------------------------


def test_flushall_from_another_client_drops_every_entry(
    make_cache_pool, writer, prefix
) -> None:
    """A null in place of the key array means drop everything."""
    pool = make_cache_pool()
    keys = [f"{prefix}:k{i}" for i in range(5)]
    for index, key in enumerate(keys):
        writer.execute("SET", key, f"v{index}")
        read(pool, "GET", key)
        read(pool, "GET", key)
    assert pool.cache_stats["entries"] >= len(keys)
    assert_hits_occurred(pool)

    writer.execute("FLUSHALL")
    for key in keys:
        assert read(pool, "GET", key) is None, (
            f"{key} survived a FLUSHALL and was served from cache"
        )


def test_a_cache_filled_past_its_bound_never_serves_stale(
    make_cache_pool, writer, prefix
) -> None:
    """Eviction under pressure must not lose an invalidation.

    The bound is small and the keyspace is larger, so entries are evicted
    throughout. The key under test is written after the cache has churned past
    it, and must still not be served stale.
    """
    bound = 4
    pool = make_cache_pool(cache_size=bound)
    tracked = f"{prefix}:tracked"
    writer.execute("SET", tracked, "first")
    assert read(pool, "GET", tracked) == b"first"
    assert read(pool, "GET", tracked) == b"first"
    assert_hits_occurred(pool)

    for i in range(bound * 4):
        filler = f"{prefix}:filler{i}"
        writer.execute("SET", filler, f"f{i}")
        read(pool, "GET", filler)
    assert pool.cache_stats["entries"] <= bound, (
        f"the cache holds {pool.cache_stats['entries']} entries against a bound "
        f"of {bound}"
    )

    writer.execute("SET", tracked, "second")
    assert read(pool, "GET", tracked) == b"second", (
        "a key evicted and re-read under cache pressure was served stale"
    )
