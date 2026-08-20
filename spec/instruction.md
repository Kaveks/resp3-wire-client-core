# Build a Redis wire protocol client

Implement `resp3_wire`, a Redis client library that speaks the Redis
Serialization Protocol directly over TCP, using only the Python standard
library.

## Constraints

The package imports only from the Python standard library. No third party
packages, no vendored code, no `importlib`, `__import__`, or `exec` used to
reach outside it. In particular, redis-py is not available to your code and
must not be used.

`resp3_wire/parser.py` and `resp3_wire/protocol.py` additionally import nothing
from `socket`, `select`, `asyncio`, `ssl`, or `subprocess`. The parser is a
sans-io component: it is driven by byte chunks handed to it and never performs
I/O itself. This is checked structurally against the modules' abstract syntax
trees.

The constraint is transitive. Whatever those two modules import must satisfy it
too, so reaching I/O through an intermediate module does not satisfy it. The
check follows the import graph rather than stopping at the two named files.

Starter stubs are at `/app/resp3_wire/`. Visible tests are at `/app/tests/`.

## Environment

A Redis server is available in the container but is not running. Start it with:

    redis-server --port 6379 --save '' --appendonly no \
                 --enable-debug-command yes --daemonize yes

The `--enable-debug-command yes` flag matters. `DEBUG PROTOCOL` is the only way
to make the server emit every RESP3 type on demand, and `DEBUG SLEEP` is the
only way to induce a timeout mid-reply. Both are used to check your work, and
both are useful while developing it.

Run the visible tests with:

    cd /app && python -m pytest tests/ -v

## Package layout

    resp3_wire/
      __init__.py     re-exports the public surface
      protocol.py     value types
      parser.py       RespParser, NEED_MORE
      errors.py       exception hierarchy
      connection.py   Connection
      pool.py         ConnectionPool
      pipeline.py     Pipeline

Module paths are part of the contract. You may add private modules; you may not
move or rename these, because the sans-io check inspects specific paths.

`__init__.py` re-exports:

    RespParser, NEED_MORE,
    Attributed, VerbatimBytes, ErrorReply, PushMessage, unwrap,
    Connection, ConnectionPool, Pipeline,
    RedisError, ProtocolError, ConnectionError, TimeoutError, ServerError,
    WrongTypeError, MovedError, NoScriptError, BusyGroupError

`ConnectionError` and `TimeoutError` deliberately shadow builtins within this
package and do not subclass them.

## Part 1: the parser

    NEED_MORE = <module level sentinel>

    class RespParser:
        def __init__(self) -> None: ...
        def feed(self, data: bytes) -> None: ...
        def gets(self) -> object: ...
        def reset(self) -> None: ...

`feed` appends bytes to an internal buffer. It never blocks, never raises on
incomplete input, and accepts a chunk of any size including empty.

`gets` returns the next complete reply, or `NEED_MORE` when the buffer does not
yet hold one. Callers drain by calling it repeatedly until it returns
`NEED_MORE`. Malformed wire data raises `ProtocolError`.

`NEED_MORE` must be distinguishable from every legitimate reply. It is not
`None`, because `None` is a legitimate reply.

`reset` discards all buffered state and returns the parser to its initial
condition.

The parser accepts every type byte from both protocol versions regardless of
what was negotiated. It is not thread safe; one parser belongs to one
connection.

### The invariant that matters

For any byte sequence `B` and any partition of it into chunks, feeding the
chunks in order with a drain after each must produce the same sequence of
values as feeding `B` whole and draining once.

This holds for every partition, with no exceptions. Metadata survives too, so a
`VerbatimBytes` keeps its format and an `Attributed` keeps its attributes
whatever the partition.

### RESP2 types

    + simple string    +OK\r\n                  b"OK"
    - error            -ERR bad\r\n             ErrorReply
    : integer          :42\r\n                  42
    $ bulk string      $3\r\nabc\r\n            b"abc"
    $ null bulk        $-1\r\n                  None
    * array            *2\r\n...                list
    * null array       *-1\r\n                  None

All string-ish values are `bytes`. Nothing is decoded. Simple strings and bulk
strings are indistinguishable in the result, which is correct.

### RESP3 types

    + simple string    +OK\r\n                  b"OK"
    - simple error     -ERR bad\r\n             ErrorReply
    : number           :42\r\n                  42
    , double           ,3.25\r\n                3.25    (float)
    , infinity         ,inf\r\n ,-inf\r\n       float("inf"), float("-inf")
    # boolean          #t\r\n #f\r\n            True, False
    _ null             _\r\n                    None
    ( big number       (349289032840923850\r\n  int
    $ blob string      $3\r\nabc\r\n            b"abc"
    = verbatim         =15\r\ntxt:Some string   VerbatimBytes
    ! blob error       !9\r\nERR broke\r\n      ErrorReply
    * array            *2\r\n...                list
    % map              %2\r\n...                dict
    ~ set              ~3\r\n...                set
    > push             >2\r\n...                PushMessage
    | attribute        |1\r\n...                decorates the next value

Notes that are easy to get wrong:

A double with no fractional part, `,3\r\n`, still produces a `float`. The wire
type determines the Python type.

A verbatim string's declared length counts the format prefix and its colon. For
`=15\r\ntxt:Some string\r\n` the 15 covers `txt:` plus the eleven byte payload.
Strip the prefix from the payload and keep it as metadata.

A blob error may legally contain CR or LF in its payload, since its length is
declared. A simple error may not.

Duplicate map keys resolve last write wins.

If a set contains an unhashable member, produce a `list` preserving wire order
rather than raising. Redis does not emit such a frame; this exists so the
behavior is defined.

RESP3 defines `_\r\n` as the null, but `$-1\r\n` and `*-1\r\n` still appear on
some paths. Accept all three and produce `None`.

### Value types

    class Attributed:
        value: object
        attributes: dict

Wraps a value that arrived decorated by an attribute frame. Equality delegates
to the wrapped value in both directions, so `Attributed(b"x", {...}) == b"x"`
and the reverse both hold. `__hash__` delegates too, where the wrapped value is
hashable, which means an `Attributed` works as a dict key and as a set member
interchangeably with its bare value. Attributes are not compared: two
`Attributed` instances wrapping equal values are equal regardless of their
attribute dictionaries.

This delegation is load-bearing and is checked directly.

    class VerbatimBytes(bytes):
        format: str

A `bytes` subclass with the three character format as a `str`, prefix stripped
from the payload, so it compares equal to the plain bytes.

    class ErrorReply:
        code: str
        message: str

A server error occupying a value position. `code` is the first whitespace
delimited token uppercased, `message` is the full text. Both `str`, decoded
UTF-8 with surrogate escaping. The parser produces these; it never raises on a
server error.

    class PushMessage:
        kind: str
        data: list

An out of band `>` frame. `kind` is the first element decoded as UTF-8, `data`
is the rest. A push frame is a complete reply in its own right and is never
merged into a command's reply. A `>0\r\n` frame is a protocol error.

    def unwrap(value: object) -> object

Returns `value.value` for an `Attributed`, the value unchanged otherwise. Does
not recurse.

### Attribute semantics

An attribute frame `|N\r\n` is followed by `2N` values forming its dictionary,
then by the value it decorates. The attribute frame is not itself a reply.

An attribute dictionary must never be emitted as a standalone value.

Attributes may appear at any depth, including on an individual element inside
an array, on a map value, or on a set member. Consecutive attribute frames
decorating the same value merge into one dictionary, later keys winning. An
attribute at the end of the stream with no following value is incomplete input;
wait for more data.

Decorated values are returned wrapped. Undecorated values are returned bare.

## Part 2: errors

    RedisError
      ProtocolError          malformed wire data
      ConnectionError        socket level failure
        TimeoutError         a socket operation exceeded its deadline
      ServerError            the server returned an error reply
        WrongTypeError       WRONGTYPE
        MovedError           MOVED
        NoScriptError        NOSCRIPT
        BusyGroupError       BUSYGROUP

`ServerError` carries `code` and `message`. `MovedError` additionally parses
`slot: int` and `address: str` out of the error text, since a MOVED reply is
only actionable with those. An unrecognised code raises `ServerError` itself.

`ProtocolError`, `ConnectionError`, and `TimeoutError` all mean the connection
is unusable. `ServerError` does not; the connection stays usable.

## Part 3: connection

    Connection(host="127.0.0.1", port=6379, *, protocol=3, timeout=5.0,
               connect_timeout=None, db=0, client_name=None)

Construction performs no I/O. `protocol` other than 2 or 3 raises `ValueError`.
`timeout` applies per socket operation, not per command.

    connect() -> None
    close() -> None                    idempotent, never raises
    is_connected -> bool
    is_poisoned -> bool
    protocol_version -> int            raises RuntimeError before connect
    server_info -> dict
    pushes_discarded -> int
    execute(*args) -> object
    pipeline() -> Pipeline

Supports the context manager protocol.

`execute` encodes arguments as a RESP array of bulk strings and reads one
reply. `bytes` pass through, `str` encodes UTF-8, `int` and `float` encode via
`repr`, `bool` raises `TypeError` rather than encoding as an integer, anything
else raises `TypeError`.

`execute()` with no arguments raises `ValueError` and writes nothing.

`server_info` before `connect` returns an empty dict rather than raising. Only
`protocol_version` raises `RuntimeError`, because a version has no truthful
value before negotiation while an empty `server_info` is accurate.

A top level `ErrorReply` in the reply is converted to the matching exception and
raised. A nested one is returned as a value.

`execute` returns `Attributed` values intact. It does not unwrap them.

A `PushMessage` arriving while `execute` waits for a reply is discarded, and
`pushes_discarded` increments. Reading continues until a non-push reply
arrives.

`execute` on a disconnected connection raises `ConnectionError` and does not
reconnect implicitly.

### Negotiation

On `connect` with `protocol=3`, send `HELLO 3`, with `SETNAME` arguments
appended when a client name was configured, and read one reply. There is no
authentication: `Connection` takes no credentials and no AUTH path exists.

A successful reply sets `protocol_version` to 3 and populates `server_info`.
Under RESP3 that reply is a `%` map and becomes `server_info` directly. A server
answering HELLO with a flat array instead requires pairing consecutive elements
into a dict. `server_info` is always a `dict` whichever shape carried it, with
`bytes` keys.

A `ServerError` reply means the server does not support HELLO or does not
support protocol 3, including servers predating Redis 6 which answer with an
unknown command error. Fall back: set `protocol_version` to 2, leave
`server_info` empty, continue. This is not an error and raises nothing.

A `ProtocolError` or `ConnectionError` propagates. A malformed HELLO reply is a
broken server or a broken parser, not a negotiation failure, and must not be
silently downgraded.

With `protocol=2`, send no HELLO at all.

Issue `SELECT` afterwards when `db` is nonzero. Under RESP3, `SETNAME` rides
along with HELLO rather than being issued separately.

Fallback is per connection. A pool does not remember that an earlier connection
fell back.

## Part 4: connection pool

    ConnectionPool(host="127.0.0.1", port=6379, *, max_connections=16,
                   protocol=3, timeout=5.0, health_check_interval=0.0,
                   **connection_kwargs)

    acquire() -> Connection
    release(conn) -> None
    connection() -> context manager
    close() -> None                    idempotent
    size -> int
    idle -> int
    in_use -> int

`acquire` reuses an idle connection or creates one up to `max_connections`. At
capacity with none idle it blocks until one is released or `timeout` elapses,
then raises `TimeoutError`.

A `timeout` of `None` means socket operations block indefinitely, but acquisition
does not: `acquire` always applies a bound, using 30 seconds when `timeout` is
`None`.

When `health_check_interval` is nonzero and that many seconds have elapsed
since a connection was last used, check it with `PING` before handing it out.
Discard one that fails and try another.

`release` returns a connection to the idle set or discards it per the poisoning
rules. Releasing a connection the pool did not issue raises `ValueError`, as
does releasing one the pool is not currently lending. A release arriving after
`close` is a discard rather than an error.

`push()` with no arguments raises `ValueError`. `Pipeline` may use internal
`Connection` methods to write without reading.

`close` closes every connection, idle and in use. Subsequent `acquire` raises
`ConnectionError`.

### Poisoning

Discard rather than return a connection whose most recent use raised
`ProtocolError`, `ConnectionError`, or `TimeoutError`. Do not discard on
`ServerError`.

A poisoned connection raises `ConnectionError` on any further `execute`.

### Concurrency

The pool is thread safe. A `Connection` is not, and the pool must issue one to
at most one borrower at a time.

Pool locking must not serialise command execution. No lock may be held across
a socket operation.

## Part 5: pipeline

    Pipeline(conn)                     or conn.pipeline()

    push(*args) -> Pipeline            queues, returns self for chaining
    execute() -> list                  flushes, reads all replies in order
    reset() -> None
    __len__() -> int

`push` buffers and performs no I/O. `execute` writes every buffered command,
reads exactly as many replies as there were commands, and returns them in
queue order. An empty pipeline returns `[]` with no I/O. `execute` clears the
queue, so a pipeline is reusable.

A `ServerError` for an individual command does not raise. It appears in the
result list as the exception instance:

    results = pipe.push("SET", "k", "v").push("LPUSH", "k", "v").execute()
    # results[0] == b"OK"
    # isinstance(results[1], WrongTypeError)

A `ProtocolError`, `ConnectionError`, or `TimeoutError` raises from `execute`
and poisons the connection, because reply alignment is lost.

Push frames arriving mid-pipeline are discarded and do not consume a reply
slot.

`MULTI` and `EXEC` are ordinary commands; there is no transaction abstraction.
Pipeline `MULTI`, the queued commands, and `EXEC`, and `EXEC`'s array arrives
as one element of the result list. Per command errors inside that array are
`ErrorReply` values, not exceptions.

## Part 6: client-side caching

Off by default. Everything above describes behavior with caching disabled, and
that is the configuration most of the checking uses.

    ConnectionPool(..., cache_size=0)

    cache_stats -> dict     {"hits", "misses", "invalidations", "entries"}
    cache_clear() -> None   drops every entry; counters unaffected

`cache_size` of 0 disables caching. A positive value enables it and bounds the
cache at that many entries. Counters are monotonic and reset only on `close`.

Caching requires `protocol=3`: under RESP2 the server has no channel on which to
deliver an invalidation. `cache_size > 0` with `protocol=2` raises `ValueError`.

### What is cached

Replies to read-only commands issued through `execute`, and only where the
command has exactly one key, in position 1. `GET`, `HGET`, `LRANGE`, `SMEMBERS`,
`TYPE`, `STRLEN` and their kind.

Nothing else. No write command, no multi-key command, nothing issued through a
`Pipeline`, nothing inside `MULTI`. The cache key is the full encoded command,
so `GET k` and `GETRANGE k 0 -1` are separate entries.

### Tracking

With caching enabled, each connection issues `CLIENT TRACKING ON` after
negotiation and before any command. The server then sends a push frame naming
keys whose cached values have gone stale:

    >2\r\n$10\r\ninvalidate\r\n*1\r\n$3\r\nfoo\r\n

A null in place of the key array means drop everything. Redis sends that on
`FLUSHALL` and when its tracking table overflows.

An `invalidate` frame is consumed rather than discarded, so it does not
increment `pushes_discarded`. Any other push frame still does.

### The requirement

**A cached read must never return a value the server has already invalidated.**

The cache is pool-wide: one cache shared by every connection the pool holds, not
one cache per connection.

Serving a stale value is a failure. Serving a miss where a hit was possible is
not. Hit rate is not graded, but a cache that never caches is not a cache.

## Out of scope

No pubsub. No cluster support beyond parsing `MOVED` into a typed error, with
no redirect following. No async interface. No TLS. No command-specific methods.
No decoding to `str`. No caching of writes, multi-key commands, pipelined
commands, or anything inside `MULTI`.

## How your work is checked

Five graded areas, weighted roughly 42, 17, 17, 15, and 9 percent:

    command results       against a live server, both protocol versions
    parser correctness    values and the fragmentation invariant
    pool integrity        under concurrency
    cache correctness     invalidation
    parser resources      memory and cost scaling

Scoring is continuous, so partial work earns partial credit.

Two requirements are performance rather than behavior. While a reply is being
parsed, peak bytes retained by the parser must stay under three times the
payload size; holding the parsed value is expected and counts toward that. And
parsing cost must grow linearly with payload size, not faster.

The visible tests in `/app/tests/` demonstrate the API and basic behavior.
Passing them is necessary and not sufficient.
