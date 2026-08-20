# Public API contract

Status: ratified 2026-08-18. Frozen.
Owner: maintainers.

Defines the public surface of the `resp3_wire` package. Types and values are
governed by `docs/PROTOCOL.md`; this document governs interfaces, lifecycle,
concurrency, and failure behavior.

Anything not named here is private. The sealed harness exercises only the
surface defined in this document, with one exception noted in section 2.

## 1. Package layout

    resp3_wire/
      __init__.py     re-exports the public surface
      protocol.py     Attributed, VerbatimBytes, ErrorReply, PushMessage
      parser.py       RespParser, NEED_MORE
      errors.py       the exception hierarchy
      connection.py   Connection
      pool.py         ConnectionPool
      pipeline.py     Pipeline

`parser.py` and `protocol.py` import nothing from `socket`, `select`,
`asyncio`, `ssl`, or `subprocess`. This is verified structurally by the sealed
harness against the modules' ASTs, not by convention.

The constraint is transitive. Whatever those two modules import must satisfy it
too, or the separation is cosmetic. In practice this means `errors.py`, the only
other module `parser.py` needs, must itself stay clear of the five. An
implementation that reaches I/O through an intermediate module has not built a
sans-io parser, and the check follows the import graph rather than stopping at
the two named files.

`__init__.py` re-exports every name in section 2. An implementer may add
private modules but may not move or rename public ones, because the sans-io
separation is checked per module path.

## 2. Public names

    from resp3_wire import (
        RespParser, NEED_MORE,
        Attributed, VerbatimBytes, ErrorReply, PushMessage,
        unwrap,
        Connection, ConnectionPool, Pipeline,
        RedisError, ProtocolError, ConnectionError, TimeoutError, ServerError,
        WrongTypeError, MovedError, NoScriptError, BusyGroupError,
    )

`resp3_wire.parser` and `resp3_wire.protocol` are additionally importable by
module path, since the chunking channel drives the parser directly and the
sans-io check inspects those two modules specifically.

Note that `ConnectionError` and `TimeoutError` shadow builtins within this
package. This is deliberate and matches redis-py's naming. They do not subclass
the builtins.

## 3. Exceptions

    RedisError
      ProtocolError          malformed wire data, unrecoverable
      ConnectionError        socket level failure, refused, reset, closed
        TimeoutError         a socket operation exceeded its deadline
      ServerError            the server returned an error reply
        WrongTypeError       WRONGTYPE
        MovedError           MOVED
        NoScriptError        NOSCRIPT
        BusyGroupError       BUSYGROUP

Every exception in the package subclasses `RedisError`.

`ServerError` carries `code: str` and `message: str`, copied from the
`ErrorReply` it was built from. `MovedError` additionally carries `slot: int`
and `address: str`.

An unrecognised server error code raises `ServerError` itself, not a subclass.
`ServerError` is public but is not required to be constructible by callers.

This document is authority for the generic mapping. `docs/PROTOCOL.md` section 6
lists `RedisError` as the fallback; that is superseded here. Raising the base
class would erase the distinction between a server that answered with an error
and a connection that failed, which is the distinction the whole hierarchy
exists to draw.

`ProtocolError` and `ConnectionError` both mean the connection is unusable.
`ServerError` and its subclasses do not: the connection remains healthy and
usable, because the server completed its reply normally.

`TimeoutError` is the ambiguous case. It means a reply was not received within
the deadline, which leaves the socket in an unknown state with an unknown
number of bytes still in flight. It is treated as unrecoverable. See section
6.3.

## 4. Connection

    Connection(
        host: str = "127.0.0.1",
        port: int = 6379,
        *,
        protocol: int = 3,
        timeout: float | None = 5.0,
        connect_timeout: float | None = None,
        db: int = 0,
        client_name: str | None = None,
    )

Construction performs no I/O. A `Connection` is inert until `connect` is
called.

`protocol` is the preferred protocol version, 2 or 3. Any other value raises
`ValueError` at construction.

`timeout` applies to each individual socket operation, not to a whole command.
`None` means block indefinitely. `connect_timeout` defaults to `timeout` when
not given.

### 4.1 Lifecycle

    connect() -> None
    close() -> None
    is_connected -> bool
    is_poisoned -> bool
    protocol_version -> int
    server_info -> dict
    pushes_discarded -> int

`connect` opens the socket, disables Nagle, and performs negotiation as
described in section 5. Calling it on an already connected instance is a no-op.
On failure it raises `ConnectionError` and leaves the instance disconnected.

`close` shuts down the socket, resets the parser, and is idempotent. It never
raises. A closed connection may be reconnected by calling `connect` again.

`protocol_version` is the negotiated version, 2 or 3. Reading it before
`connect` raises `RuntimeError`.

`server_info` is the parsed HELLO response as a dict, or an empty dict when
negotiation fell back to RESP2 without a usable HELLO reply. Keys are `bytes`
per the protocol contract.

Reading it before `connect` returns an empty dict rather than raising, unlike
`protocol_version`. The asymmetry is deliberate: a version of 2 or 3 has no
truthful value before negotiation, so returning one would be a lie, whereas an
empty `server_info` is exactly what an unnegotiated connection knows about its
server.

`Connection` supports the context manager protocol, closing on exit.

### 4.2 Command execution

    execute(*args: bytes | str | int | float) -> object

Encodes the arguments as a RESP array of bulk strings, writes it, and reads one
reply.

Argument encoding: `bytes` pass through, `str` encodes as UTF-8, `int` and
`float` encode via `repr`. Any other type raises `TypeError`. A `bool` raises
`TypeError` rather than encoding as an integer, because silently sending `True`
as `1` hides bugs.

`execute()` with no arguments raises `ValueError` and writes nothing. Redis 7.4
sends no reply at all to an empty command array, so writing one blocks until the
socket timeout and then poisons the connection. Failing at the call site turns a
hang into an immediate, diagnosable error.

The return value is the parsed reply per `docs/PROTOCOL.md`, with one
transformation: a top level `ErrorReply` is converted to the corresponding
exception and raised. Nested `ErrorReply` values are returned intact.

`execute` on a disconnected connection raises `ConnectionError`. It does not
reconnect implicitly; reconnection is the pool's responsibility.

### 4.3 Push frames during command execution

A `PushMessage` may arrive while `execute` is waiting for a command reply.
`execute` discards it and continues reading until a non-push reply arrives.

Discarding rather than surfacing is deliberate. The package has no pubsub
surface, so there is no caller to deliver a push frame to, and treating one as
a command reply would desynchronise every subsequent command on the connection.

The count of discarded frames is exposed as `pushes_discarded -> int` for
diagnostics. It is monotonically increasing and resets on `connect`.

The counter reflects frames discarded so far, not frames attributable to a
given `execute` call. This distinction is real rather than pedantic: Redis 7.4
answers `DEBUG PROTOCOL push` with the command's own reply first and the `>`
frame second, so the push is still unread when `execute` returns and is
discarded by the following call. No test asserts the counter immediately after
a specific `execute`, and an implementation must not drain trailing pushes
before returning, which would block waiting for bytes that may never arrive.

### 4.4 Attribute exposure

`execute` returns `Attributed` values intact. It does not unwrap them.

This was decided against unwrapping for three reasons. Unwrapping makes
attributes unreachable from the client surface, which is the surface a caller
actually uses. Delegating equality means pass-through costs the differential
oracle nothing, since `Attributed(b"x", ...) == b"x"` holds in both directions
and inside containers. And an unwrapping client has no specified behavior on a
connection with client tracking enabled, which is precisely where attributes
appear in practice.

The cost is that `isinstance(result, bytes)` is false for an attributed bulk
string. Callers that need the bare value use `result.value`. A module level
helper is provided:

    unwrap(value: object) -> object

Returns `value.value` for an `Attributed`, and `value` unchanged otherwise.
It does not recurse into containers.

## 5. Protocol negotiation

On `connect`, when `protocol` is 3, the connection sends:

    HELLO 3

followed by `SETNAME` arguments if a client name was configured. It then reads
one reply.

Credentials are out of scope per D13. `Connection` takes no username or
password and no AUTH path is implemented.

Three outcomes:

A successful reply sets `protocol_version` to 3 and populates `server_info`.

Under RESP3 the reply is a `%` map and becomes `server_info` directly. Under a
server that answers HELLO with a flat array, the client pairs consecutive
elements into a dict. `server_info` is always a `dict`, whichever wire shape
carried it, and its keys are `bytes` per the protocol contract.

A `ServerError` reply means the server does not support HELLO or does not
support protocol 3. This includes servers predating Redis 6, which reply with
an unknown command error. The connection falls back: it sets
`protocol_version` to 2, populates `server_info` with an empty dict, and
continues. This is not an error and raises nothing.

Any `ProtocolError` or `ConnectionError` propagates. A malformed HELLO reply is
a broken server or a broken parser, not a negotiation failure, and must not be
silently downgraded.

When `protocol` is 2, no HELLO is sent at all. `protocol_version` is 2 and
`server_info` is empty.

After negotiation, `SELECT` is issued when `db` is nonzero. Under RESP3,
`SETNAME` rides along with HELLO rather than being issued separately.

Fallback is per connection, not per pool. A pool does not remember that a
previous connection fell back.

## 6. Connection pool

    ConnectionPool(
        host: str = "127.0.0.1",
        port: int = 6379,
        *,
        max_connections: int = 16,
        protocol: int = 3,
        timeout: float | None = 5.0,
        health_check_interval: float = 0.0,
        **connection_kwargs,
    )

### 6.1 Borrow and return

    acquire() -> Connection
    release(conn: Connection) -> None
    connection() -> context manager yielding a Connection
    close() -> None

`acquire` returns a connected `Connection`. It reuses an idle one when
available and otherwise creates one, up to `max_connections`. When the pool is
at capacity and none are idle, it blocks until one is released or `timeout`
elapses, at which point it raises `TimeoutError`.

A `timeout` of `None` blocks indefinitely on socket operations but not on
acquisition. `acquire` always applies a bound; where `timeout` is `None` it uses
30 seconds. A pool that can block forever at capacity is a deadlock, not a
configuration, and the pool channel's exhaustion cases depend on `acquire`
returning control.

An idle connection is health checked before being handed out when
`health_check_interval` is nonzero and that many seconds have elapsed since its
last use. The check is a `PING`. A connection failing it is discarded and
another is tried.

`release` returns a connection to the idle set, or discards it per section 6.3.
Releasing a connection the pool did not issue raises `ValueError`, as does
releasing one the pool is not currently lending. A second release of an
already-returned connection must not be silently accepted: it would place one
connection in the idle set twice and hand it to two borrowers concurrently,
which is the cross-talk this pool exists to prevent.

A release arriving after `close` is a discard, not an error. A borrower unwinding
a `with` block after another thread closed the pool has done nothing wrong, and
raising there would mask whatever exception the block was already propagating.

`connection()` is the preferred interface. It releases on both normal exit and
exception, including `KeyboardInterrupt`.

`close` closes every connection, idle and in use, and marks the pool closed.
Subsequent `acquire` raises `ConnectionError`. It is idempotent.

`ConnectionPool` supports the context manager protocol.

### 6.2 Introspection

    size -> int          total connections, idle plus in use
    idle -> int          currently idle
    in_use -> int        currently borrowed

These exist because the pool integrity channel asserts on genuine concurrent
utilisation, and it needs a way to observe it that does not depend on timing.

### 6.3 Poisoning

A connection is discarded rather than returned to the idle set when any of the
following occurred during its most recent use:

    ProtocolError        the stream is desynchronised
    ConnectionError      the socket is dead
    TimeoutError         an unknown number of bytes remain in flight

A `ServerError` does not poison a connection. The server completed its reply
normally and the stream is intact.

This is the requirement the pool integrity channel targets. A timeout that
occurs after a command is written but before its reply is fully read leaves
unread bytes in the socket buffer. Returning that connection to the pool means
the next borrower reads the tail of somebody else's reply, which is silent
cross-talk rather than a visible error.

The connection tracks its own poisoned state. `release` consults it; callers
are not required to.

    Connection.is_poisoned -> bool

A poisoned connection raises `ConnectionError` on any further `execute`.

### 6.4 Concurrency

The pool is thread safe. Multiple threads may call `acquire` and `release`
concurrently.

A `Connection` is not thread safe and must be used by one thread at a time. The
pool guarantees a connection is issued to at most one borrower at a time.

Pool internal locking must not serialise command execution. Holding a lock
across a socket read reduces the pool to a single connection's throughput and
defeats its purpose. The pool integrity channel verifies this structurally: N
workers acquire simultaneously and block on a barrier before releasing, so an
implementation that serialises fails to reach the barrier and times out.

## 7. Pipeline

    Pipeline(conn: Connection)

Or, preferred:

    Connection.pipeline() -> Pipeline

### 7.1 Interface

    push(*args) -> Pipeline       queue a command, returns self for chaining
    execute() -> list             flush, read all replies, return them in order
    reset() -> None               discard queued commands
    __len__() -> int              queued command count

`push` encodes and buffers a command. It performs no I/O and never blocks.
`push()` with no arguments raises `ValueError`, matching `execute()` in section
4.2 and for the same reason: an empty command array draws no reply, so the batch
would come up one short and block.

`Pipeline` may use internal `Connection` methods to write without reading. The
public `execute` couples one write to one read by definition, so pipelining
cannot be built on it. That seam is internal and unspecified; what is specified
is the observable behavior in this section.

`execute` writes every buffered command before reading any reply, then reads
exactly as many replies as there were commands and returns them as a list in
the order the commands were queued.

The requirement is the ordering, not the syscall count. Whether the commands
leave in one `sendall` or several is unobservable and unverified; what matters
is that no reply is read until every command is written, which is what makes a
pipeline a pipeline rather than a loop.

An empty pipeline's `execute` returns `[]` and performs no I/O.

`execute` clears the queue, so a pipeline is reusable.

### 7.2 Errors within a pipeline

A `ServerError` for an individual command does not raise. It appears in the
result list as the corresponding exception instance, not as an `ErrorReply` and
not raised. This lets a caller inspect per command outcomes.

    results = pipe.push("SET", "k", "v").push("LPUSH", "k", "v").execute()
    # results[0] == b"OK"
    # isinstance(results[1], WrongTypeError)

A `ProtocolError`, `ConnectionError`, or `TimeoutError` raises from `execute`
and poisons the connection, because reply alignment is lost.

`PushMessage` frames arriving mid-pipeline are discarded and do not consume a
reply slot, matching section 4.3.

### 7.3 Transactions

`MULTI` and `EXEC` are ordinary commands. The package provides no transaction
abstraction. A caller pipelines `MULTI`, the queued commands, and `EXEC`, and
receives `EXEC`'s array as one element of the result list. Per command errors
inside that array are `ErrorReply` values, per `docs/PROTOCOL.md` section 6,
because they are nested rather than top level.

This asymmetry is deliberate and is specified rather than incidental: pipeline
slots carry exceptions, nested aggregate positions carry `ErrorReply`.

## 7A. Client-side caching

Opt-in, off by default, and graded by its own channel. Every other section of
this document describes behavior with caching disabled.

### 7A.1 Surface

    ConnectionPool(..., cache_size: int = 0, **connection_kwargs)

`cache_size` of 0 disables caching entirely, which is the default and the
configuration every other channel exercises. A positive value enables it and
bounds the cache at that many entries.

    ConnectionPool.cache_stats -> dict

Returns `{"hits": int, "misses": int, "invalidations": int, "entries": int}`.
Counters are monotonic and reset only on `close`. They exist because the caching
channel must observe cache behavior without reaching into internals, and because
a cache that never hits is indistinguishable from no cache by result alone.

    ConnectionPool.cache_clear() -> None

Drops every entry. Counters are unaffected.

### 7A.2 What is cached

Only replies to read-only commands issued through `execute`, and only when the
command has exactly one key in position 1. That covers `GET`, `HGET`, `LRANGE`,
`SMEMBERS`, `TYPE`, `STRLEN`, and their kind.

Nothing else is cached: no write command, no multi-key command, no command
issued through a `Pipeline`, no command inside `MULTI`. The narrowing is
deliberate. Deciding which commands are cacheable is a lookup table, and a
lookup table is not what this requirement is about.

The cache key is the full encoded command, so `GET k` and `GETRANGE k 0 -1` are
distinct entries even though both read `k`.

A cached hit returns a value equal to what the server would have returned. The
comparison the channel makes is against the server, not against a previous
reply.

### 7A.3 Tracking

When caching is enabled, each connection issues `CLIENT TRACKING ON` after
negotiation and before any command. Caching requires `protocol=3`: under RESP2
the server has no channel on which to deliver an invalidation, so a
`ConnectionPool` constructed with `cache_size > 0` and `protocol=2` raises
`ValueError`.

### 7A.4 Invalidation

The server sends `>2\r\n$10\r\ninvalidate\r\n*N\r\n...` naming keys whose
cached values are now stale. A null in place of the key array means the client
should drop everything, which Redis sends on `FLUSHALL` and on tracking table
overflow.

An invalidation frame received on any connection evicts the matching entries
from the pool's cache, not from that connection's view of it. This is the
requirement, and D34 records why: the connection that receives an invalidation
is usually not the connection that cached the value.

`Connection.pushes_discarded` counts frames a non-caching connection dropped.
With caching enabled an `invalidate` frame is consumed rather than discarded and
does not increment that counter; any other push frame still does.

### 7A.5 The requirement the channel grades

**A cached read must never return a value the server has already invalidated.**

An invalidation may arrive at any point, including in the socket buffer before
the reply that would populate the cache is fully read, during an unrelated
command on the same connection, or on another connection while this one is
mid-read. Whichever way it arrives, a subsequent read of that key must not be
served from cache.

Consequences worth stating, because they are where implementations diverge:

Reading a reply is not the same as being clear to cache it. If an invalidation
for the key is already buffered when the reply is parsed, caching the value and
then processing the invalidation is correct only if the eviction actually
happens before the next read; caching it and processing the invalidation later
is not.

The pool must not hold a lock across socket I/O to make this work. Section 6.4
already forbids that and the pool channel's barrier case already enforces it.
Cache correctness is not an exemption.

A connection that is idle in the pool still has a socket with unread bytes on
it. Invalidations arriving for keys it read do not stop arriving because nobody
is borrowing it.

Serving a stale value is a failure. Serving a miss where a hit was possible is
not: the channel asserts that hits occur, so a cache that never caches fails,
but it never asserts that a specific read was a hit.

## 8. What is not in scope

No pubsub. No cluster support beyond parsing `MOVED` into a typed error. No
async interface. No TLS. No caching of writes, multi-key commands, pipelined
commands, or anything inside `MULTI`; see section 7A.2. No command-specific methods; `execute` and `push` take
raw command arguments. No response decoding to `str`.

`MovedError` exposing `slot` and `address` is the full extent of cluster
awareness. The client does not follow redirects.

## 9. Open items

None.
