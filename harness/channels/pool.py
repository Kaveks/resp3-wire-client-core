"""Channel 3: pool integrity under concurrency. 22 cases.

Concurrent workers borrow from a shared pool, issue commands tagged with a per
worker unique token, and assert that every reply carries their own token. A
failure is cross-talk: a reply belonging to another worker.

Tagging uses `ECHO` with a token containing the worker id and a monotonic
sequence number, so a stale reply is identifiable as to both its origin and its
age.

No case asserts a throughput number or infers concurrency from elapsed time.
Concurrent utilization is asserted structurally: workers block on a
`threading.Barrier` while holding their connections, so an implementation that
serialises acquisition never reaches the barrier and fails on the barrier's own
timeout, which is a correctness failure rather than a performance judgement.
"""

from __future__ import annotations

import threading
import time

import pytest

from resp3_wire import (
    Connection,
    ConnectionError,
    ConnectionPool,
    TimeoutError,
    WrongTypeError,
)
from support.redis_boot import raw_command

pytestmark = pytest.mark.channel("pool")

BARRIER_TIMEOUT = 10.0
WORKERS = 8

# Bounded poll used to confirm the server is answering again after a case that
# stalled it with DEBUG SLEEP.
_QUIESCE_ATTEMPTS = 50
_QUIESCE_INTERVAL = 0.1

# Comfortably longer than a command round trip, so a case that must observe the
# health check running can establish that precondition without inferring
# anything from timing.
HEALTH_INTERVAL = 0.05


@pytest.fixture
def make_pool(server):
    """Pools created by a case, all closed during teardown."""
    created: list[ConnectionPool] = []

    def build(**kwargs) -> ConnectionPool:
        kwargs.setdefault("port", server.port)
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
def side_channel(server):
    """A connection outside the pool, for CLIENT KILL and inspection."""
    conn = Connection(port=server.port, protocol=3, timeout=10.0)
    conn.connect()
    yield conn
    conn.close()


def quiesce(port: int) -> None:
    """Poll a stalled server over a raw socket until it answers again.

    A bounded readiness check, not a sleep used as synchronisation. Deliberately
    does not go through the client package, which is the thing under test.
    """
    for _ in range(_QUIESCE_ATTEMPTS):
        try:
            if b"PONG" in raw_command(port, "PING", timeout=0.5):
                return
        except OSError:
            pass
        time.sleep(_QUIESCE_INTERVAL)
    raise AssertionError(
        f"server did not become responsive within "
        f"{_QUIESCE_ATTEMPTS * _QUIESCE_INTERVAL:.1f}s after a DEBUG SLEEP case"
    )


@pytest.fixture
def stalling(server):
    """For cases that use DEBUG SLEEP, which stalls the entire server.

    Redis is single threaded, so a sleeping server answers nobody: a second
    connection cannot even complete its HELLO while one is in progress. Without
    this, the stall outlives the case that caused it and the next case fails
    during negotiation for a reason that has nothing to do with what it tests.
    """
    yield
    quiesce(server.port)


def await_interval(since: float, interval: float) -> None:
    """Wait until `interval` has demonstrably elapsed since `since`.

    The health check is defined in terms of wall time elapsed since a
    connection was last used, so a case asserting that the check ran must first
    establish that precondition. This waits for a documented duration to pass;
    it is not synchronising with another thread, and it does not infer anything
    from how long an operation took.
    """
    deadline = since + interval
    while time.monotonic() < deadline:
        time.sleep(0.002)


def run_workers(count: int, body, timeout: float = 60.0) -> list:
    """Run `body(i)` in `count` threads and re-raise the first failure here."""
    failures: list[BaseException] = []
    results: list = [None] * count
    lock = threading.Lock()

    def wrapper(index: int) -> None:
        try:
            results[index] = body(index)
        except BaseException as exc:  # noqa: BLE001
            with lock:
                failures.append(exc)

    threads = [threading.Thread(target=wrapper, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=timeout)
    alive = [t for t in threads if t.is_alive()]
    if alive:
        raise AssertionError(
            f"{len(alive)} of {count} workers did not finish within {timeout}s"
        )
    if failures:
        raise failures[0]
    return results


def tagged_round(pool: ConnectionPool, worker: int, rounds: int) -> None:
    """Issue tagged ECHOs and assert every reply carries this worker's token."""
    for seq in range(rounds):
        token = b"w%d:%d" % (worker, seq)
        with pool.connection() as conn:
            reply = conn.execute("ECHO", token)
            assert reply == token, (
                f"cross-talk: worker {worker} sequence {seq} sent {token!r} "
                f"and received {reply!r}"
            )


# ---------------------------------------------------------------------------
# Borrow, release, reuse, capacity. 4 cases.
# ---------------------------------------------------------------------------


def test_acquire_returns_a_live_connection(make_pool) -> None:
    pool = make_pool(max_connections=2)
    conn = pool.acquire()
    assert conn.is_connected
    assert conn.execute("PING") == b"PONG"
    assert pool.size == 1 and pool.in_use == 1 and pool.idle == 0
    pool.release(conn)



def test_pool_grows_to_max_connections_and_no_further(make_pool) -> None:
    pool = make_pool(max_connections=3, timeout=0.5)
    held = [pool.acquire() for _ in range(3)]
    assert pool.size == 3 and pool.in_use == 3
    assert len({id(c) for c in held}) == 3, "the pool reissued the same connection"
    with pytest.raises(TimeoutError):
        pool.acquire()
    assert pool.size == 3, "a refused acquire must not create a connection"


def test_release_validates_provenance(make_pool, server) -> None:
    pool = make_pool(max_connections=2)
    foreign = Connection(port=server.port)
    foreign.connect()
    try:
        with pytest.raises(ValueError):
            pool.release(foreign)
    finally:
        foreign.close()
    conn = pool.acquire()
    pool.release(conn)
    with pytest.raises(ValueError):
        pool.release(conn)
    assert pool.idle == 1, "a rejected double release must not duplicate the connection"


def test_capacity_holds_under_concurrent_acquisition(make_pool) -> None:
    """Racing borrowers cannot push the pool past max_connections.

    A pool that opens connections outside its lock has to reserve the slot
    before it starts connecting, or several threads each see room and each
    create one. The overshoot is invisible to a sequential case.
    """
    limit = 3
    pool = make_pool(max_connections=limit, timeout=5.0)
    start = threading.Barrier(WORKERS, timeout=BARRIER_TIMEOUT)
    observed: list[int] = []
    lock = threading.Lock()

    def body(index: int) -> None:
        start.wait()
        try:
            conn = pool.acquire()
        except TimeoutError:
            return
        try:
            with lock:
                observed.append(pool.size)
        finally:
            pool.release(conn)

    run_workers(WORKERS, body)
    assert observed, "no worker acquired a connection"
    assert max(observed) <= limit, (
        f"the pool held {max(observed)} connections against a limit of {limit}"
    )
    assert pool.size <= limit, f"pool.size settled at {pool.size}, limit {limit}"


# ---------------------------------------------------------------------------
# Health check and eviction. 3 cases.
# ---------------------------------------------------------------------------


def test_health_check_evicts_a_dead_idle_connection(make_pool, side_channel) -> None:
    pool = make_pool(max_connections=2, timeout=2.0,
                     health_check_interval=HEALTH_INTERVAL)
    conn = pool.acquire()
    victim_id = conn.execute("CLIENT", "ID")
    pool.release(conn)
    released_at = time.monotonic()
    side_channel.execute("CLIENT", "KILL", "ID", victim_id)
    await_interval(released_at, HEALTH_INTERVAL * 1.2)
    fresh = pool.acquire()
    assert fresh.execute("CLIENT", "ID") != victim_id, (
        "a connection killed while idle was handed out again"
    )



def test_health_check_disabled_by_default(make_pool) -> None:
    """With the interval at zero no check runs, so the same connection returns."""
    pool = make_pool(max_connections=2)
    conn = pool.acquire()
    conn_id = conn.execute("CLIENT", "ID")
    pool.release(conn)
    again = pool.acquire()
    assert again is conn and again.execute("CLIENT", "ID") == conn_id


def test_a_connection_passing_its_health_check_is_the_one_handed_back(
    make_pool,
) -> None:
    """The check discards connections that fail it, not ones that pass it.

    The mutation suite found the adjacent case green against a pool whose check
    discarded every connection it examined, because the replacement worked too.
    `CLIENT ID` is what distinguishes the two.
    """
    pool = make_pool(max_connections=2, health_check_interval=HEALTH_INTERVAL)
    conn = pool.acquire()
    conn_id = conn.execute("CLIENT", "ID")
    pool.release(conn)
    await_interval(time.monotonic(), HEALTH_INTERVAL * 1.2)
    checked = pool.acquire()
    assert checked.execute("CLIENT", "ID") == conn_id, (
        "a connection that passed its health check was discarded and replaced; "
        "the check must evict only connections that fail it"
    )


# ---------------------------------------------------------------------------
# Poisoning. 3 cases. Each induces its failure through the public API and
# additionally asserts pool.size decreased.
# ---------------------------------------------------------------------------


def test_connection_death_poisons_and_is_discarded(make_pool, side_channel) -> None:
    pool = make_pool(max_connections=2)
    conn = pool.acquire()
    side_channel.execute("CLIENT", "KILL", "ID", conn.execute("CLIENT", "ID"))
    with pytest.raises(ConnectionError):
        conn.execute("PING")
    assert conn.is_poisoned
    before = pool.size
    pool.release(conn)
    assert pool.size == before - 1, "a dead connection was returned to the pool"
    assert pool.idle == 0


def test_timeout_poisons_and_is_discarded(make_pool, stalling) -> None:
    """DEBUG SLEEP outlasting the socket timeout is the case that matters most.

    Redis is single threaded, so this stalls the whole server. The case runs
    with the pool otherwise quiescent for that reason.
    """
    pool = make_pool(max_connections=2, timeout=0.3)
    conn = pool.acquire()
    with pytest.raises(TimeoutError):
        conn.execute("DEBUG", "SLEEP", 1)
    assert conn.is_poisoned
    before = pool.size
    pool.release(conn)
    assert pool.size == before - 1, "a timed-out connection was returned to the pool"


def test_poisoned_connection_refuses_further_commands(make_pool, server, stalling) -> None:
    """docs/API.md section 6.3: any further execute raises ConnectionError.

    The server is polled back to readiness before the refusal is asserted. The
    mutation suite found this case passing with its refusal deleted, because a
    still-stalled server times the socket out anyway and `TimeoutError`
    subclasses `ConnectionError`. With the server answering again, a client that
    does not refuse reads the delayed reply and returns a value, which is the
    failure this case exists to see.
    """
    pool = make_pool(max_connections=2, timeout=0.3)
    conn = pool.acquire()
    with pytest.raises(TimeoutError):
        conn.execute("DEBUG", "SLEEP", 1)
    quiesce(server.port)
    with pytest.raises(ConnectionError):
        conn.execute("PING")
    with pytest.raises(ConnectionError):
        conn.pipeline().push("PING").execute()
    pool.release(conn)


# ---------------------------------------------------------------------------
# Concurrent utilization and distinct ids. 2 cases.
# ---------------------------------------------------------------------------


def test_workers_hold_connections_simultaneously(make_pool) -> None:
    """A serialising pool never reaches the barrier and fails on its timeout."""
    pool = make_pool(max_connections=WORKERS, timeout=20.0)
    barrier = threading.Barrier(WORKERS, timeout=BARRIER_TIMEOUT)
    observed: list[int] = []
    lock = threading.Lock()

    def body(index: int) -> None:
        conn = pool.acquire()
        try:
            with lock:
                observed.append(pool.in_use)
            barrier.wait()
        finally:
            pool.release(conn)

    run_workers(WORKERS, body)
    assert max(observed) == WORKERS, (
        f"peak concurrent utilization was {max(observed)}, expected {WORKERS}"
    )


def test_workers_receive_distinct_connections(make_pool) -> None:
    """CLIENT ID cardinality proves distinct sockets, not one handed out twice."""
    pool = make_pool(max_connections=WORKERS, timeout=20.0)
    barrier = threading.Barrier(WORKERS, timeout=BARRIER_TIMEOUT)

    def body(index: int) -> int:
        conn = pool.acquire()
        try:
            client_id = conn.execute("CLIENT", "ID")
            barrier.wait()
            return client_id
        finally:
            pool.release(conn)

    ids = run_workers(WORKERS, body)
    assert len(set(ids)) == WORKERS, (
        f"{len(set(ids))} distinct server-side connections for {WORKERS} "
        f"simultaneous borrowers: {sorted(ids)}"
    )


# ---------------------------------------------------------------------------
# Cross-talk. 4 cases.
# ---------------------------------------------------------------------------


def test_no_cross_talk_under_concurrent_tagged_traffic(make_pool) -> None:
    pool = make_pool(max_connections=4, timeout=20.0)
    run_workers(WORKERS, lambda i: tagged_round(pool, i, 25))


def test_no_cross_talk_when_connections_are_reused_heavily(make_pool) -> None:
    """More workers than connections, so every connection is reused repeatedly."""
    pool = make_pool(max_connections=2, timeout=20.0)
    run_workers(WORKERS, lambda i: tagged_round(pool, i, 20))
    assert pool.size <= 2


def test_no_cross_talk_after_a_timeout_discarded_a_connection(make_pool, stalling) -> None:
    """The delayed reply must never reach the next borrower.

    The assertion is on the borrower's reply tag, not on any internal flag.
    """
    pool = make_pool(max_connections=1, timeout=0.3)
    victim = pool.acquire()
    with pytest.raises(TimeoutError):
        victim.execute("DEBUG", "SLEEP", 1)
    pool.release(victim)

    # The server is still finishing its sleep; the next borrower's connect
    # waits for it. Its timeout is generous so this is not a race.
    fresh = make_pool(max_connections=1, timeout=20.0)
    with fresh.connection() as conn:
        token = b"after-the-timeout"
        reply = conn.execute("ECHO", token)
        assert reply == token, (
            f"the borrower after a timeout received {reply!r}, which is the "
            f"tail of the discarded connection's delayed reply"
        )


def test_no_cross_talk_after_a_killed_connection_was_released(make_pool, side_channel) -> None:
    pool = make_pool(max_connections=2, timeout=20.0)
    victim = pool.acquire()
    side_channel.execute("CLIENT", "KILL", "ID", victim.execute("CLIENT", "ID"))
    with pytest.raises(ConnectionError):
        victim.execute("PING")
    pool.release(victim)
    run_workers(4, lambda i: tagged_round(pool, i, 15))


# ---------------------------------------------------------------------------
# Close and cleanup. 2 cases.
# ---------------------------------------------------------------------------


def test_close_closes_every_connection_and_refuses_acquire(make_pool) -> None:
    pool = make_pool(max_connections=3)
    borrowed = pool.acquire()
    idle = pool.acquire()
    pool.release(idle)
    pool.close()
    assert pool.size == 0
    assert not borrowed.is_connected, "close must close in-use connections too"
    assert not idle.is_connected, "close must close idle connections too"
    with pytest.raises(ConnectionError):
        pool.acquire()
    pool.close()  # idempotent


def test_connection_context_manager_always_releases(make_pool) -> None:
    pool = make_pool(max_connections=1, timeout=5.0)
    with pytest.raises(RuntimeError):
        with pool.connection() as conn:
            assert conn.execute("PING") == b"PONG"
            raise RuntimeError("propagated out of the block")
    assert pool.in_use == 0, "an exception inside the block must still release"
    assert pool.idle == 1
    # A server error inside the block must not poison or discard.
    with pool.connection() as conn:
        conn.execute("SET", "cleanup:str", "x")
        with pytest.raises(WrongTypeError):
            conn.execute("LPUSH", "cleanup:str", "y")
    assert pool.idle == 1 and pool.size == 1, (
        "a server error must leave the connection healthy and pooled"
    )


# ---------------------------------------------------------------------------
# Capacity exhaustion. 2 cases.
# ---------------------------------------------------------------------------


def test_exhaustion_raises_timeout_error(make_pool) -> None:
    pool = make_pool(max_connections=1, timeout=0.5)
    held = pool.acquire()
    with pytest.raises(TimeoutError):
        pool.acquire()
    assert isinstance(TimeoutError("x"), ConnectionError), (
        "TimeoutError must sit under ConnectionError in the hierarchy"
    )
    pool.release(held)


def test_exhaustion_clears_once_a_connection_is_released(make_pool) -> None:
    """A waiter is woken by a release rather than only by its own timeout."""
    pool = make_pool(max_connections=1, timeout=20.0)
    held = pool.acquire()
    released = threading.Event()
    acquired = threading.Event()

    def waiter() -> None:
        conn = pool.acquire()
        acquired.set()
        pool.release(conn)

    thread = threading.Thread(target=waiter)
    thread.start()
    pool.release(held)
    released.set()
    thread.join(timeout=BARRIER_TIMEOUT)
    assert acquired.is_set(), (
        "a blocked acquire was not woken when a connection was released"
    )



# ---------------------------------------------------------------------------
# Idle reuse is genuine. 2 cases.
#
# docs/HARNESS.md section 5.3. A pool that discards every connection on release,
# or whose health check discards every connection it checks, passes every other
# case in this channel because the replacement works too.
# ---------------------------------------------------------------------------



def test_client_id_is_stable_across_a_borrow_release_borrow_cycle(make_pool) -> None:
    """The server agrees it is the same connection, not just the same object."""
    pool = make_pool(max_connections=2)
    conn = pool.acquire()
    first_id = conn.execute("CLIENT", "ID")
    pool.release(conn)
    again = pool.acquire()
    second_id = again.execute("CLIENT", "ID")
    assert second_id == first_id, (
        f"CLIENT ID moved from {first_id} to {second_id} across a release and "
        f"reacquire; the pool opened a new socket instead of reusing one"
    )


def test_pool_size_stays_at_one_across_repeated_cycles(make_pool) -> None:
    """Ten borrow-release cycles leave one connection, not ten."""
    pool = make_pool(max_connections=4)
    for cycle in range(10):
        conn = pool.acquire()
        assert conn.execute("PING") == b"PONG"
        pool.release(conn)
        assert pool.size == 1, (
            f"after {cycle + 1} borrow-release cycles the pool holds "
            f"{pool.size} connections; a reusing pool holds one"
        )
