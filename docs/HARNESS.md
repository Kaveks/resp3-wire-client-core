# Harness contract

Status: ratified 2026-08-18. Frozen.
Owner: maintainers.

Defines how an implementation of `resp3_wire` is graded: what each channel
measures, how values are compared, how randomness is controlled, and what
counts as a failure. `docs/PROTOCOL.md` and `docs/API.md` define correct
behavior; this document defines how correctness is observed.

## 1. Structure

    harness/
      run.py                 orchestrator, emits the score
      channels/
        oracle.py            50 cases  differential comparison against redis-py
        chunking.py          20 cases  parser invariance under fragmentation
        pool.py              20 cases  pool integrity under concurrency
        resource.py          10 cases  parser memory behavior
      support/
        redis_boot.py        server lifecycle
        compare.py           the comparator
        probe.py             redis-py behavior measurement (see 2.8)

Every case is a separate pytest test function. Each is independently
meaningful and independently failable. No case asserts more than one property,
and no filler cases exist to reach a count.

Weights are realized through case counts rather than a weight table, so the
50/20/20/10 split holds under a harness that scores as fraction of tests
passed and remains sensible under one that scores pass/fail.

## 2. The comparator

### 2.1 Why not `==`

Python equality is the wrong primitive for this comparison in four ways.

`bool` subclasses `int`, so `True == 1` and `False == 0`. RESP3 distinguishes
`#t` from `:1`, and an implementation that returns `1` for a boolean must fail.
Equality cannot see the difference.

Set equality uses hashing, not elementwise comparison. Whether
`{Attributed(b"x", {})} == {b"x"}` holds depends entirely on `Attributed`
delegating `__hash__` as well as `__eq__`. Making the comparator depend on that
property means a broken delegation shows up as an unrelated oracle failure
rather than as the specific defect it is.

`float("nan") != float("nan")`, so any nan anywhere makes equality useless.

Equality produces a bare `True` or `False`. A failing case needs to say which
position in a nested structure diverged, or the failure is undiagnosable.

The comparator is therefore a recursive structural comparison, not `==`.

### 2.2 Signature

    compare(actual, expected, path=()) -> None

Raises `Divergence` with a rendered path on mismatch, returns `None` on match.
`actual` is the value from the agent client, `expected` is the value from
redis-py. The comparison is asymmetric: attribute unwrapping is applied to
`actual` only, because redis-py does not surface attributes.

Path elements are integers for list indices, `Key(k)` for dict keys,
`SetElem(k)` for canonicalized set members, and `Attr` for descent into an
attribute dictionary. A rendered path looks like `root[2]['field'][0]`.

### 2.3 Type classes

Before comparing values, the comparator assigns each side a type class and
requires the classes to match exactly.

    NONE      value is None
    BOOL      type(v) is bool                    checked before INT
    INT       type(v) is int
    FLOAT     type(v) is float
    BYTES     isinstance(v, bytes)               includes VerbatimBytes
    LIST      type(v) is list
    DICT      type(v) is dict
    SET       type(v) is set or type(v) is frozenset
    ERROR     isinstance(v, ErrorReply)
    EXC       isinstance(v, RedisError)
    PUSH      isinstance(v, PushMessage)

`BOOL` is tested before `INT` and uses `type(v) is bool` rather than
`isinstance`, which is the whole point of the ordering.

`BYTES` uses `isinstance` so that `VerbatimBytes` compares as bytes. Its
`format` attribute is not compared here, because redis-py has no counterpart.
Format fidelity is asserted by sealed chunking cases instead.

`LIST` and `DICT` use `type(v) is` rather than `isinstance`, so a subclass of
`list` returned in place of a list is a divergence. Nothing in the contract
permits one.

### 2.4 Scalar rules

`NONE` matches `NONE`. Nothing else matches `NONE`.

`BOOL`, `INT`, `BYTES` compare by `==` once the class matches.

`FLOAT` compares by exact `==`, with no tolerance. Both sides parse the same
ASCII bytes through CPython's `float()`, so any difference is a real defect
and a tolerance would mask it. Infinities compare by `==` and match. Nan is
excluded from the oracle command matrix entirely; the comparator raises
`Divergence` on encountering a nan on either side, on the grounds that its
presence means the matrix is wrong.

`EXC` and `ERROR` are mutually comparable and normalize to a common form
before comparison, per D11. Each side yields a code: for an `ErrorReply` it is
`.code`; for a `resp3_wire` exception it is `.code`; for a redis-py exception it
is the first whitespace delimited token of `str(exc)`, uppercased. Only codes
compare. Message text and exception class do not, because redis-py's classes
live in another package and its nested `EXEC` errors are exception instances
where this contract requires `ErrorReply`.

Exception identity is therefore not established by the oracle. Three dedicated
cases assert it directly, with no redis-py involvement: a top level `WRONGTYPE`
raises `WrongTypeError`; a nested `EXEC` error is an `ErrorReply` and not an
exception; and a `MOVED` error string parsed through the parser yields
`MovedError` with correct `slot` and `address`.

`PUSH` compares `kind` and then recurses into `data`. It does not arise in the
oracle, since redis-py has no counterpart, and exists here for the sealed
parser cases that reuse the comparator.

### 2.5 Aggregate rules

`LIST` compares length first, then element by element with the index appended
to the path. Length mismatch reports both lengths.

`DICT` compares key sets first, then values for each key. Keys are compared as
raw values, not canonicalized, because dict keys under this contract are always
`bytes` or `int`. A key present on one side only is reported as a missing or
unexpected key rather than as a value divergence.

`SET` is canonicalized rather than compared by set operations. Each side is
converted to a sorted list of canonical keys and the lists are compared
elementwise.

    canonical_key(v) -> tuple

Returns `(type_class_name, sortable_repr)`. For `BYTES` the second element is
the bytes value, for `INT` and `FLOAT` it is the number, for `BOOL` it is the
bool, for `NONE` it is `b""`. Sorting is by the tuple, which is total because
the first element disambiguates across classes.

Canonicalization means the comparator does not depend on `Attributed.__hash__`
delegating correctly. That delegation is load-bearing for callers and is
asserted directly by a dedicated sealed case (section 4.4), so a broken
delegation produces one specific failure rather than a scatter of confusing
oracle failures.

redis-py returns `list` for every RESP3 set, by deliberate design, because a
set may contain unhashable members. Per D11 the comparator therefore permits
one asymmetry: agent `SET` against redis-py `LIST` is not a class mismatch.
Both sides canonicalize to a sorted list of canonical keys and compare
elementwise.

This asymmetry would let a `list` pass where the contract requires a `set`, so
one oracle case asserts `type(result) is set` on a RESP3 `SMEMBERS` reply
directly, outside the comparator. That case is the enforcement; the comparator
is only the comparison.

### 2.6 Attribute handling

Before classifying `actual`, the comparator unwraps `Attributed` and records
the attribute dictionary against the current path in a side channel. Unwrapping
is recursive in the sense that it is applied at every level of descent, not
just at the root.

The recorded attributes are not compared against anything. No oracle case may produce an attributed reply, since redis-py raises on `|`
frames per D11, so the side channel is unused by the oracle in practice. It
exists because the sealed chunking cases reuse the comparator, and because a
case can assert that no attribute leaked into a value position.

The one thing the comparator enforces about attributes is that an attribute
dictionary never appears as a standalone value. An `actual` that is a bare
`dict` where `expected` is a scalar produces a class mismatch, which is exactly
the failure signature of an implementation that emits attributes as replies.

### 2.7 The redis-py configuration

The oracle constructs redis-py as:

    Redis(host=..., port=..., protocol=3, decode_responses=False)

and then clears `response_callbacks`.

Clearing the callbacks is essential and is the single most important line in
the oracle. redis-py applies per command post-processing: `HGETALL` becomes a
dict even under RESP2 where the wire carries a flat array, `SMEMBERS` becomes
a set, `EXISTS` becomes a bool, and so on. Comparing against post-processed
values would test whether the agent reimplemented redis-py's callback table
rather than whether it parsed the protocol.

With callbacks cleared, redis-py returns the parsed RESP tree, which is what
`docs/PROTOCOL.md` specifies the agent must return.

A parallel RESP2 client is constructed with `protocol=2` for the RESP2 half of
the matrix, also with callbacks cleared.

Both clients use a dedicated connection, not a pool, so that connection state
is unambiguous.

### 2.8 The divergence probe

redis-py's exact treatment of two RESP3 types, verbatim strings and attributes,
is not something this document asserts from memory. It is measured.

`support/probe.py` runs before any oracle case. It issues `DEBUG PROTOCOL` for
each RESP3 type, records what redis-py returns, and asserts that the recorded
behavior matches what this document assumes:

    verbatim   redis-py returns the payload with the format prefix stripped
    attribute  redis-py RAISES InvalidResponse; it does not discard the frame
    double     float
    bignum     int
    true       bool
    null       None
    map        dict
    set        list, not set (redis-py returns sets as lists always)
    push       not delivered as a command reply

The attribute and set entries were measured against redis-py 8.1.0 and Redis
7.4.10 and contradict what this document originally assumed. See D11.

If a probe assertion fails, the harness aborts with a configuration error
rather than scoring. A mismatch means redis-py changed behavior between the
pinned version and the running one, which invalidates every oracle case. It is
not the implementation's fault and must not be scored as such.

The probe is 9 assertions and does not count toward any channel's case
allocation.

`DEBUG PROTOCOL` is gated in Redis 7 by the `enable-debug-command`
configuration, which every server invocation passes per D10.

## 3. Channel 1: differential oracle, 50 cases

### 3.1 Method

A live Redis server runs in the container. For each case, the harness generates
a random key prefix, executes a command sequence through the agent client and
the same sequence through redis-py against the same server with fresh keys,
and compares the results with the comparator.

Expected values are produced by redis-py at run time. No fixture file contains
an expected value anywhere in the harness.

Keys are randomized per case per run, so a lookup table keyed on command
arguments cannot be built in advance.

Both clients flush their own keyspace before each case using a prefix scoped
`UNLINK`, not `FLUSHALL`, so cases remain independent without serializing on a
global flush.

### 3.2 Allocation

    strings                 8
    lists                   6
    hashes                  6
    sets                    5
    sorted sets             6
    keyspace and generic    5
    transactions            4
    protocol and RESP3      5
    error mapping           5
                           --
                           50

The four type-identity cases introduced by D11 are drawn from this 50, not
added to it. `MOVED` parsing and the three identity assertions sit inside error
mapping and transactions; the `type(result) is set` assertion is the `SMEMBERS`
case. The suite totals exactly 100 across all channels and the 50/20/20/10
weighting holds.

Each enumeration below lists exactly as many items as its allocation. A case
that runs under both protocols counts once here and is designated in section
3.3, which draws its sixteen from these fifty rather than adding to them.

Strings, 8: `SET` with options; `GET` on a missing key; `GETRANGE`; `APPEND`;
`INCR`; `INCRBYFLOAT`; a binary safe value containing CRLF and NUL; an empty
value.

Lists, 6: `RPUSH`; `LRANGE` over a large range; `LPOP` with count; `LINSERT`;
`LPOS`; `LRANGE` on a missing key.

Hashes, 6: `HSET`; `HGETALL`; `HSTRLEN`; `HDEL`; `HEXPIRE`; a field with a
binary name.

`HGETALL` is one of the three RESP2 designations in section 3.3, where it
returns a flat array rather than a map. It is one case, run on a RESP2
connection.

`HRANDFIELD` was removed as nondeterministic. `HEXPIRE` is unconditional
because the server is pinned at 7.4 per D12; a conditional case would break the
fixed 100 case denominator.

Sets, 5: `SADD`; `SMEMBERS`, which carries the direct `type(result) is set`
assertion per D11; `SINTERCARD`; `SMISMEMBER`; set algebra across three keys.

`SPOP` with count was removed as nondeterministic; `SMISMEMBER` replaces it.

Sorted sets, 6: `ZADD`; `ZRANGE WITHSCORES`, where RESP3 gives doubles and
RESP2 gives bulk strings; `ZSCORE`; `ZRANGEBYLEX`; `ZADD GT`; infinity scores.

Keyspace, 5: `TYPE`; `TTL` on a persistent key; `TTL` on a missing key;
`EXPIRETIME` on a volatile key; `OBJECT ENCODING`.

`LCS` was listed in error and is removed; the five above are the allocation.

`RANDOMKEY`, `SCAN MATCH`, and `TTL` on a volatile key were removed as
nondeterministic across two clients sharing a keyspace: `RANDOMKEY` is not
prefix scopable, `SCAN` order is cursor dependent, and two `TTL` reads straddle
a second boundary. `EXPIRETIME` returns an absolute unix time and is stable
across both reads.

Transactions, 4: a successful `MULTI`/`EXEC` through a pipeline; an `EXEC`
containing a per command error, which carries the D11 assertion that the nested
error is an `ErrorReply` and not an exception; a `DISCARD`; a `WATCH` that
aborts.

Protocol and RESP3, 5: negotiated version under `protocol=3`; negotiated
version under `protocol=2`; `server_info` contents after HELLO; `DEBUG PROTOCOL`
for map and for array, one case covering both; a verbatim reply carrying the
`VerbatimBytes.format` assertion.

A double reply is covered by `ZSCORE` in sorted sets and is not duplicated
here.

`DEBUG PROTOCOL attrib` is excluded from the oracle entirely: redis-py raises on
it. Attributes are verified in the chunking channel, which is now their sole
enforcement per D11.

Error mapping, 5: the four required codes plus `MOVED`. Each is produced by a real
server condition, never by a synthesized error string: `WRONGTYPE` from a list
operation on a string key, `NOSCRIPT` from `EVALSHA` with an unknown digest,
`BUSYGROUP` from a duplicate `XGROUP CREATE`, and a generic error from a
malformed argument count. `MOVED` cannot be produced by a non clustered server;
its case asserts the parsing of a `MOVED` error string through the parser,
including `slot` and `address` extraction, and is the one error case that does
not go through the live server.

### 3.3 What the RESP2 half tests

Sixteen of the fifty cases run against a `protocol=2` connection with a
`protocol=2` redis-py. They are drawn from the fifty, not added to them.

Their purpose is degradation fidelity: under RESP2 a hash reply is a flat array
and a score is a bulk string, and an implementation that returns RESP3 shapes on
a RESP2 connection is wrong even where the values look better.

That purpose constrains which cases are designated. A command whose reply shape
is identical under both protocols tests nothing when designated RESP2, so the
sixteen concentrate where the shape genuinely differs:

    hashes         3   HGETALL flat array vs map; HSET; a binary field name
    sorted sets    4   ZRANGE WITHSCORES flat array vs pairs; ZSCORE bulk
                       string vs double; ZADD; infinity scores as strings
    strings        3   GET on a missing key, null bulk vs null; SET; APPEND
    lists          2   LRANGE on a missing key, empty array; RPUSH
    sets           2   SMEMBERS as array under both, confirming the agent does
                       NOT return a set under RESP2; SADD
    protocol       1   negotiated version under protocol=2
    transactions   1   EXEC with a nested error, ErrorReply under both
                  --
                  16

The sets designation is the subtle one: under RESP2 the wire carries `*`, so a
correct client returns a `list`. An implementation that returns a `set` because
it post-processes by command name rather than by wire type fails here, which is
the mirror of the RESP3 `type(result) is set` assertion.

Keyspace has no RESP2 designation. `TYPE`, `TTL`, `EXPIRETIME`, and
`OBJECT ENCODING` return identical shapes under both protocols, so designating
one would consume a case without testing degradation.

## 4. Channel 2: chunking, 20 cases

### 4.1 The invariant

For a byte sequence `B` and any partition into chunks, feeding the chunks in
order with a drain after each produces the same value sequence as feeding `B`
whole and draining.

Equality here is stricter than the comparator's. It additionally compares
`VerbatimBytes.format` and `Attributed.attributes`, reaching past delegating
equality on purpose. A parser that loses the format prefix only when the split
lands inside it would otherwise pass.

### 4.2 The corpus

A corpus of frames is built at run time covering every wire type from both
protocols, nested to depth 6, including an attribute on a set member, an
attribute on a map value, a verbatim string whose format prefix straddles a
plausible chunk boundary, a push frame interleaved between two command replies,
a blob error containing CRLF in its payload, and a null in each of its three
wire forms.

The corpus is constructed by the harness from the type definitions, not read
from a file.

### 4.3 Allocation

    one byte feeds, per type group          8
    exhaustive split at every position      4
    seeded random partitions                4
    pathological boundaries                 4
                                           --
                                           20

The exhaustive cases take a small curated frame and feed it split at position
`i` for every `i` from 1 to `len(frame) - 1`, asserting the invariant at each.
For a 200 byte frame that is 199 partitions inside one case, which is
appropriate: they test one property.

Pathological boundaries covers a split between CR and LF, a split inside a
length prefix's digits, a split inside a verbatim format prefix, and a feed
sequence containing empty `feed(b"")` calls interleaved.

### 4.4 The delegation case

One of the eight one byte cases is reserved for the `Attributed` delegation
property, asserted directly rather than through the invariant:

    a = Attributed(b"x", {b"k": b"v"})
    assert a == b"x" and b"x" == a
    assert hash(a) == hash(b"x")
    assert {a} == {b"x"}
    assert {b"x": 1}[a] == 1
    assert Attributed(b"x", {}) == Attributed(b"x", {b"other": b"v"})

This is separated out because the property is load-bearing for callers and
because a failure here should read as what it is rather than as an unrelated
set comparison failure elsewhere.

## 5. Channel 3: pool integrity, 20 cases

### 5.1 Method

Concurrent workers borrow from a shared pool, issue commands tagged with a per
worker unique token, and assert that every reply carries their own token.
Failures are cross-talk: a reply belonging to another worker.

Tagging uses `ECHO` with a token containing the worker id and a monotonic
sequence number, so a stale reply is identifiable as to both its origin and its
age.

### 5.2 Concurrency without timing

No case asserts a throughput number or infers concurrency from elapsed time.

Concurrent utilization is asserted structurally. N workers each acquire a
connection, record `pool.in_use`, and block on a `threading.Barrier` before
releasing. An implementation that serialises acquisition never reaches the
barrier and the case fails on the barrier's own timeout, which is a
correctness failure rather than a performance judgement.

`CLIENT ID` is issued by each worker while holding its connection, and the set
of observed ids must have cardinality N, proving the pool issued distinct
connections rather than handing the same one out repeatedly.

### 5.3 Allocation

    borrow, release, reuse, capacity         4
    health check and eviction                3
    poisoning: connection, timeout, post-poison 3
    concurrent utilization and distinct ids  2
    cross-talk under injected timeouts       4
    close and cleanup semantics              2
    capacity exhaustion raises TimeoutError  2
                                            --
                                            20

### 5.4 Poisoning

The three poisoning cases each induce their failure through the public API
rather than by mutating internals.

`ProtocolError` poisoning has no public induction path and its case is
reallocated. `execute` always emits well formed RESP, and a genuine protocol
level rejection makes Redis close the connection, which surfaces as
`ConnectionError` and duplicates the adjacent case. Section 7.2 forbids reaching
into internals, which closes the remaining route. The freed case asserts instead
that a poisoned connection raises `ConnectionError` on any further `execute`,
per `docs/API.md` section 6.3.

Connection death is induced with `CLIENT KILL` issued from a separate
connection against the target's `CLIENT ID`.

Timeout poisoning is induced with `DEBUG SLEEP` on a connection whose timeout
is shorter than the sleep. This is the case that matters most. After the
timeout, the connection is released, and the next borrower must not receive the
delayed reply. The assertion is on the borrower's reply tag, not on any
internal flag.

Each case additionally asserts `pool.size` decreased, confirming the connection
was discarded rather than quietly reused.

## 6. Channel 4: resource behavior, 10 cases

This is the weakest of the four channels and is weighted accordingly.

### 6.1 What is measured

Holding a parsed value is not a defect. A bulk string of N bytes occupies N
bytes once parsed. The requirement is bounded overhead.

Primary metric, peak retained overhead:

    payload_frame = build_frame(payload_size)   # BEFORE tracing starts
    gc.collect()
    tracemalloc.start()
    baseline = tracemalloc.get_traced_memory()[0]
    parser.feed(payload_frame)
    value = parser.gets()
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    ratio = (peak - baseline) / payload_size

The frame is built before tracing begins. If it were built inside the traced
region the input alone would consume 1.0 of the 3.0 budget, silently tightening
the bound to 2.0 and leaving a correct implementation with no headroom.

`ratio` must not exceed 3.0.

The bound is set at 3.0 because a correct implementation buffering a chunk,
accumulating a frame, and materializing a value can reach 2.0 legitimately, and
CPython's allocator granularity adds headroom on top. An implementation that
duplicates the payload once more than necessary reaches 4.0 or above.

`tracemalloc` is used rather than process RSS because RSS is affected by
allocator behavior, garbage collection timing, and anything else running in the
process, none of which the implementation controls.

`gc.collect()` runs before the baseline snapshot. The measurement is repeated 3
times and the minimum ratio is taken, which removes the effect of an
interpreter level allocation happening to land inside the window.

### 6.2 Allocation

    peak ratio at 1 MB, 8 MB, 64 MB payloads        3
    peak ratio under 4 KB chunked feed              2
    reset() releases buffered state                 1
    pipeline of 10k small replies does not grow     1
    depth 100 nesting completes structurally        1
    scaling behavior under chunked feed             2
                                                   --
                                                   10

The `reset()` case asserts that after feeding a partial large frame and calling
`reset`, traced memory returns to within 1.1 times baseline. It catches an
implementation that never releases its buffer.

The pipeline case feeds 10,000 small replies, draining and discarding after
each. Peak retained must not exceed 3.0 times a single reply's size plus 64 KB
of slack. The slack absorbs interpreter level allocation; the point is that the
bound does not scale with the reply count.

The depth 100 case has no payload and therefore no meaningful ratio. It asserts
structurally: the value parses correctly and no `RecursionError` occurs, which
is what it was actually testing.

The two scaling cases implement D14. The first measures per-byte cost at 1 MB,
8 MB, and 64 MB under a fixed chunk size, taking the minimum of 5 trials at each
size independently, and asserts that per-byte cost at 64 MB is under 8.0 times
per-byte cost at 1 MB. The second asserts monotonicity is not required but that
no single step in the sequence exceeds 4.0, which catches a parser that is
linear across the small sizes and degrades only at scale.

Sizes are measured independently, never interleaved. D14 records why: paired
interleaved trials couple through allocator state and the resulting ratio reads
the allocator rather than the algorithm.

## 7. Determinism and seeds

### 7.1 Seed discipline

Randomness appears in three places: key generation in the oracle, chunk
partitions in the chunking channel, and interleaving in the pool channel.

All three draw from a single `random.Random` instance seeded from
`RESP3_SEED`, which `tests/test.sh` sets from `os.urandom` at grading time and
prints to stdout for reproduction.

Visible tests use the fixed published seed `20260818`. It is stated in
`visible_tests/README.md` so an implementer can reproduce a visible failure.
The sealed channels assert `RESP3_SEED != 20260818` at startup, so a run that
accidentally inherits the visible seed fails loudly rather than grading against
a schedule the implementer has seen.

In addition to the run seed, every channel executes a fixed set of regression
seeds on every run: `1`, `2`, `31337`, and `2 ** 32 - 1`. These catch a
regression that a random seed happens to miss, and they mean a green run is
never purely luck.

### 7.2 What may not be used

No `time.sleep` as synchronization. Use `threading.Barrier` and `Event`.

No absolute wall clock threshold as a correctness assertion. Relative scaling
ratios are permitted under D14, and only under D14; the two channel 4 scaling
cases are their sole sanctioned use.

No assertion on the ordering of concurrent operations beyond what the contract
guarantees.

No reaching into private attributes of the implementation. Every assertion goes
through the surface in `docs/API.md`, with the sole exception of the sans-io
structural check, which inspects module ASTs rather than runtime state.

The sans-io check is a precondition and holds no case allocation, but unlike the
probe its failure is the implementation's fault: a violation scores zero across
all channels rather than aborting as a configuration error.

### 7.3 Flake budget

The acceptance bar is 20 consecutive clean full suite runs against the
reference implementation, with a fresh seed each run. A single failure in 20
means the responsible channel is redesigned, not retried.

`tools/flake_budget.sh` runs this and reports the seed of any failing run.

## 8. Execution bounds

    server startup readiness poll     bounded, 50 attempts at 100 ms
    per case timeout                  30 s
    pool channel barrier timeout      10 s
    full suite target wall time       under 10 minutes
    verifier timeout requested        1800 s

The full suite target leaves substantial headroom against the requested
verifier timeout, which itself sits well inside the platform's per trial pool.
Actual timings are measured and recorded during packaging rather than
estimated.

Redis is started by `tests/test.sh` on a private port with persistence
disabled, polled for readiness, flushed, and torn down. The harness never
assumes a server already exists and never uses the default port.

## 9. Open items

None. O1, O2, and O3 were resolved and ratified on 2026-08-18.

O1: relative time ratios are permitted as a narrow exception to the timing
rule, for detecting quadratic buffer churn in channel 4. D9 ratified this;
D14 superseded its formulation after measurement showed the two-point ratio
reads allocator state rather than complexity class.

O2: probe assumptions verified against redis-py 8.1.0. Section 2.8 stands.

O3: `--enable-debug-command yes` is mandated for every server invocation, in
`tests/test.sh`, in `tools/redis_dev.sh`, and in the rollout environment. See
D10.
