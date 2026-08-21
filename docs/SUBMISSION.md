# Submission metadata

Single source of truth for the draft form. `task.toml` is derived from this
file by `tools/build_bundle.sh`. Never edit either independently.

Status: complete. All figures measured, all prose fields written.

    title                    RESP3 wire client core
    workingSlug              resp3-wire-client-core
    collectionFamily         Library clone
    taskFamily               feature_development
    verifierFamily           programmatic
    networkRequirements      none

The `title` line above is what the derivation reads. The longer form used on the
draft form is under "Prose fields" below.

## resourceEstimate

    cpuMillis            4000
    memoryMb             8192
    storageMb            4096
    gpuCount             0
    agentTimeoutSec      14400
    verifierTimeoutSec   900

`agentTimeoutSec` is four hours, against a two hour floor.

Memory stays at 8 GB because the resource channel feeds a 64 MB payload under
`tracemalloc` across three trials, and the deep-nesting case builds a frame past
CPython's recursion limit. Storage is 4 GB: the built image measures 203 MB, so
4 GB carries the image, both virtualenvs, and the agent's own working files with
a wide margin.

The verifier timeout is 900 s against a measured offline run of 26 s, a 34x
margin that covers a loaded sandbox without reserving time nothing will use.

Measured 2026-08-21, from the assembled bundle, both build contexts:

    cold build, root context           118.2 s
    cold build, environment context     98.4 s
    solve plus verify, offline          25.8 s
    image size                          203 MB
    bundle                     66 files, 600 KB

Build plus a four hour rollout plus verification sits far inside the 50,400 s
per-trial ceiling. All figures are under the 8 CPU / 65536 MB / 40960 MB sandbox
limits, and `task.toml` must request these or less, never more.

## Unconfirmed

Score emission. The Harbor format's mechanism for a continuous score from
`tests/test.sh` is not verified. `run.py` emits both a pytest exit code and a
JSON score file, so either mechanism reads correctly. Channel weights are
realised through case counts, 55/22/22/20/11 of 130 per D35, which degrades
sensibly under a pass/fail harness too.

Two `task.toml` key names. The `[environment]` resource keys follow the
documented snake_case forms. `timeout_sec` under `[agent]` and `[verifier]`
follows the same convention but is documented nowhere.

---

# Prose fields

Verbatim text for the draft form. Character counts are against the stated
bounds.

## title (3-200)

RESP3 wire client core: incremental parser, pool, and tracking cache

## objective (40-20,000)

Implement `resp3_wire`, a Redis client library that speaks the Redis
Serialization Protocol directly over TCP using only the Python standard library.
The agent starts from stubs presenting the full public surface, every method
raising `NotImplementedError`, alongside 70 visible tests and a Redis 7.4 server
in the container.

Six parts, all of them required:

An incremental parser for RESP2 and RESP3, driven by byte chunks and performing
no I/O of its own. It must handle every wire type in both protocol versions,
including RESP3 doubles, booleans, big numbers, verbatim strings, maps, sets,
blob errors, out-of-band push frames, and attribute frames. The parser is a
sans-io component: `parser.py` and `protocol.py` may not import `socket`,
`select`, `asyncio`, `ssl`, or `subprocess`, and the constraint is transitive
across the import graph.

A typed error hierarchy mapping `WRONGTYPE`, `MOVED`, `NOSCRIPT`, and
`BUSYGROUP` to distinct exception classes, with everything else falling to a
generic server error. `MovedError` additionally parses the slot and address out
of the error text. Errors nest as values inside aggregates and raise only at top
level, because `EXEC` returns an array in which individual commands may have
failed.

A connection performing HELLO-based protocol negotiation with graceful fallback
to RESP2, exposing the negotiated version and the parsed server info.

A thread-safe connection pool with borrow, return, health checking, and
eviction. A connection whose most recent use raised a protocol, connection, or
timeout error must be discarded rather than returned, and pool locking must not
serialise command execution.

Command pipelining, writing every queued command before reading any reply, with
per-slot errors surfacing as exception instances while errors nested inside an
`EXEC` array remain values.

Opt-in client-side caching using `CLIENT TRACKING`, with a pool-wide cache and
server-driven invalidation. The requirement is one sentence: a cached read must
never return a value the server has already invalidated.

Done means the public surface in the specification exists at the stated module
paths, behaves as specified, and uses no third-party package.

## motivation (20-10,000)

Every protocol client in production faces the same problems this task is built
around, and every mature Redis client has solved them: redis-py, lettuce,
go-redis, and node-redis all carry an incremental parser, a pool with poisoning
rules, pipelining, and tracking-based caching. This is the load-bearing core of
a library category, reduced to the parts where correctness is hard and
observable.

It stands in for a class of work that is common and badly served by testing
habits. Protocol clients fail silently. A parser that assumes complete buffers
works perfectly until the network fragments a reply, and then returns a wrong
value rather than an error. A pool that returns a timed-out socket hands the
next borrower the tail of someone else's response. A cache that processes an
invalidation a moment too late serves stale data indistinguishable from fresh.
None of these produce a stack trace, and all of them pass the tests an engineer
writes when the happy path works.

That silence is why the task is worth grading. The correctness question has an
objective answer, because a wire protocol has a specification and a live server
is the arbiter, but reaching that answer requires the kind of adversarial
testing engineers routinely skip. An agent that can build this correctly is
demonstrating something specific: not that it can write a client, but that it
can reason about state that survives across calls, about failures that leave a
stream in an unknown position, and about events that arrive when nothing is
waiting for them.

## difficultyExplanation (40-20,000)

The difficulty is not in the Redis commands, which are trivial, nor in the API
surface, which is fully specified. It is in four places where correct-looking
code is wrong, and where the wrongness only appears under conditions an
implementer has to construct deliberately.

Fragmentation. The parser must produce identical output for any partition of the
same bytes. TCP will split a reply anywhere: inside the digits of a length
prefix, between the CR and the LF of a terminator, inside a verbatim string's
three-character format prefix, at arbitrary depths of nesting. The naive
approaches each fail in a different way. Buffering until a frame looks complete
requires knowing where it ends, which requires parsing it. Rescanning from the
start on each resumption is correct but quadratic, and the resource channel
measures per-byte cost across a sixty-fourfold size range specifically to catch
it. Preserving partial progress across calls means carrying an explicit
resumption state through nested aggregates, which is where implementations
diverge. A generator-based recursive descent parser is a natural design and
exceeds CPython's recursion limit on deeply nested input; one trial
implementation did exactly that.

Attribute frames. RESP3 `|` decorates the value that follows it at the same
nesting depth and is not a reply of its own. Emitting the attribute dictionary
as a standalone reply desynchronises every subsequent read by one, and the
symptom appears far from the cause. Attributes may decorate an element inside an
array, a map value, or a set member, and consecutive attribute frames merge.
redis-py raises rather than handling them, so an implementer cannot copy the
answer from the obvious reference.

Pool poisoning. A timeout occurring after a command is written but before its
reply is fully read leaves an unknown number of bytes in the socket. Returning
that connection to the pool means the next borrower reads the tail of another
worker's reply. There is no exception, no corruption detectable at the point of
failure, just a wrong value delivered to the wrong caller. Detecting this
requires recognising that a timeout is categorically different from a server
error, which completes normally and leaves the stream intact.

Cache invalidation. This is the part whose difficulty survives full
specification. The requirement states in one sentence that a cached read must
never return an invalidated value. Meeting it is not a matter of implementing
what the sentence says. Invalidations arrive asynchronously on whichever
connection was tracking the key, at a moment the client does not choose. The
notice for a value may already sit in the socket buffer before the reply that
would cache it has been parsed. Redis writes the invalidation and the writing
client's `+OK` in the same event loop tick, so draining whatever is readable
before a lookup is not sufficient: a reader can observe the reply before the
notice reaches it. And because the cache is pool-wide, the connection receiving
an invalidation is usually not the connection that cached the value, so eviction
must reach shared state across connections under concurrency without holding a
lock across socket I/O.

The evidence for that last point is measured rather than argued. A trial
implementation built an adversarial probe, observed one stale read in four
hundred, correctly diagnosed it as same-tick delivery, and closed it with an
ordering proof on the tracking channel. It then found a subtler bug in its own
fix, where concurrent readers shared a freshness proof keyed on when the proof
completed rather than when it was requested. That is the shape of the problem:
not hidden, but not soluble by implementing what the specification says either.

What makes all four hard to self-check is that the failing conditions do not
arise naturally. A developer testing against localhost sees replies arrive
whole, sees no timeouts, sees no concurrent invalidation. Every one of these
defects requires the implementer to construct the adverse condition before they
can observe it, and the specification deliberately does not describe how to
construct any of them.

## expertTimeEstimateHours

32

A qualified backend engineer familiar with wire protocols, working from the
specification: roughly eight hours for the parser including fragmentation
testing, three for connection and negotiation, five for the pool, two for
pipelining, ten for caching including the invalidation races, and four for
verification against a live server. Descriptive rather than a gate.

## environmentSummary (40-20,000)

Base image `python:3.12-slim-bookworm`. Redis server 7.4.10 binaries are copied
in by multi-stage build from `redis:7.4-bookworm`, because Debian bookworm ships
Redis 7.0 and the command matrix requires 7.4 features. The server is present
but not running; the specification gives the command to start it, including the
`--enable-debug-command yes` flag that `DEBUG PROTOCOL` and `DEBUG SLEEP`
require.

Two Python virtual environments, and the separation between them is a deliberate
control. One holds pytest 9.1.1 and pytest-timeout 2.4.0 and no redis-py. The
other holds redis-py 8.1.0, is owned by a separate unprivileged user at mode
0700, and is reachable by the agent's user only through a sudo wrapper that can
execute it without reading it. A client that searches the filesystem for a redis
package to place on `sys.path` finds nothing it can open. The build asserts this
arrangement holds, so a broken image fails at build time rather than during
grading.

The agent finds `/app` owned by its own user, containing `resp3_wire/` with
seven stub modules presenting the complete public surface, `tests/` with 70
visible tests, and `instruction.md`. The sealed harness is deliberately absent
from the image: it arrives with the bundle's `tests/` directory at verification
time, so an implementer working in this container cannot read the cases that
grade them. A leak probe verifies this against the built image on every bundle
verification, with negative controls that fail when the harness or the reference
is planted.

Everything is baked in at build time. The rollout and verification phases both
run with no network, verified by an offline run of the full suite. Measured:
cold build 118 seconds, image 203 MB, full verification run 26 seconds.

## oracleStrategy (20-20,000)

`solution/solve.sh` replaces `/app/resp3_wire` with the reference implementation
carried alongside it in `solution/`, then exits. The verifier runs afterwards
and scores what it finds.

The reference is a complete implementation, not a shim. Seven modules: value
types with the delegating equality that lets attributed values compare
transparently, the exception hierarchy, an incremental parser, a connection with
negotiation and poison tracking, a thread-safe pool, a pipeline, and a pool-wide
cache.

The parser is an explicit state machine rather than recursion: a stack of open
aggregates, a pending-blob record for a length-known payload whose bytes have
not all arrived, and a scan offset so a CRLF search never rescans. Nesting costs
no interpreter stack, and resumption never re-reads a byte already interpreted,
which is what makes the fragmentation invariant a property of the structure
rather than something tests confirm.

The cache carries the requirement with two mechanisms: a per-key generation
recorded before the command is sent and checked when the reply is offered back,
which refuses a value that went stale while it was in flight; and a sweep of
every connection the pool owns before any hit is served, because the
invalidation arrives on whichever connection read the key. A sweep that cannot
reach a peer forces a miss rather than serving a possibly-stale hit.

Measured: 130 of 130 through the bundle's own entrypoints, from a cold build,
offline, from both plausible Docker build contexts, with `/app` byte-identical
either way. Twenty consecutive clean full-suite runs with a fresh seed each.

## verificationStrategy (40-20,000)

`tests/test.sh` starts a Redis server on a private port, runs a sealed 130-case
suite across five independent channels, and emits a continuous score. Weighting
is realised through case counts rather than a weight table, so the intended
42/17/17/15/9 split holds under any harness that scores as a fraction of tests
passed.

Differential oracle, 55 cases. The agent's client and redis-py 8.1.0 execute the
same command sequences against the same live server, and results are compared by
a structural comparator rather than by `==`. Expected values are computed at run
time; no file in the harness contains an expected value. Keys are randomised per
case per run, so no lookup table can be built in advance. Thirteen cases run
under RESP2, where a hash reply is a flat array and a sorted set score is a bulk
string, because returning RESP3 shapes on a RESP2 connection is wrong even where
the values look better.

The comparator exists because Python equality is the wrong instrument here.
`bool` subclasses `int`, so `True == 1` and an implementation returning 1 for a
RESP3 boolean would pass. Set equality uses hashing rather than elementwise
comparison. Measurement established three places where redis-py cannot serve as
ground truth at all: it raises on attribute frames rather than discarding them,
returns a list for every RESP3 set by deliberate design, and strips error code
prefixes from messages for codes in its own table. Each is handled by a
documented asymmetry in the comparator plus a dedicated case asserting the
agent-side type directly, with no redis-py involvement.

Parser correctness, 22 cases. Seven assert absolute values against expectations
written from the protocol contract, with no parser on the other side of the
comparison. Four assert attribute semantics directly, comparing the wrapped
value and its dictionary rather than a repr. The remainder assert the
fragmentation invariant under one-byte feeds, exhaustive splits at every
position of curated frames, seeded random partitions, and pathological
boundaries. The absolute cases exist because the invariance cases compare a
parser against itself and cannot detect a defect consistent across schedules.

Pool integrity, 22 cases. Concurrent workers borrow, issue `ECHO` commands
tagged with a worker id and sequence number, and assert every reply carries their
own tag. Timeouts are induced with `DEBUG SLEEP`, disconnects with `CLIENT KILL`
from a separate connection. Concurrency is asserted structurally: workers acquire
and block on a barrier before releasing, so an implementation that serialises
fails on the barrier's own timeout rather than on a throughput judgement. No case
sleeps as synchronisation and no case asserts elapsed time.

Cache correctness, 20 cases. Invalidation is induced through a second,
non-caching connection, so the write that invalidates never passes through the
code under test; a client that invalidates only on its own writes fails every
case. Expectations come from the server, never from redis-py, which has its own
caching semantics. Four cases race the invalidation against the read
specifically. Every freshness case additionally asserts that cache hits occurred
somewhere in the run, because a freshness assertion passes trivially against an
implementation that caches nothing.

Resource behavior, 11 cases. Peak retained bytes measured with `tracemalloc`
must stay within three times the payload. Per-byte parsing cost must not grow
more than eightfold across a sixty-fourfold size range, measured at three sizes
independently rather than as a two-point ratio, because interleaved paired
measurements couple through allocator state and read the allocator rather than
the algorithm.

Visible and sealed. The 70 visible tests demonstrate the API surface and basic
behavior under a published seed, and deliberately exclude adversarial
fragmentation, pool corruption, the full command matrix, and the caching races.
The sealed suite ships in `tests/` and never enters the image the implementer
works in; a leak probe verifies that against the built image, with negative
controls that fail when the harness or reference is planted in it. The
specification names the five graded areas and their weights, and does not
describe case construction, seeds, or induction methods.

Evidence the channels work. 64 deliberate mutations, each breaking one property
a channel claims to test, were applied to copies of the reference and scored by
the full suite. Every mutation fails something except four documented
exceptions. The first run of this suite found sixteen mutations failing nothing,
fourteen of which were genuine coverage gaps, and closing them is why the suite
scaled from 100 cases to 130. Twenty consecutive clean runs against the
reference are required after any change, a bar that has failed twice and found a
real defect both times.

## binarySuccessCondition (20-10,000)

The task counts as solved when the sealed suite reports a score of 1.0: all 130
cases pass, across all five channels, in a single run at the grading seed.

Concretely that requires the differential oracle to find no divergence from
redis-py across the full command matrix on both protocol versions, the parser to
produce identical output under every tested partition of its input, no
concurrent worker to receive a reply belonging to another worker, no cached read
to return a value the server has already invalidated, and parser memory and cost
scaling to stay within their stated bounds.

The condition is machine-checkable and the score is emitted as JSON alongside a
process exit code.

## partialScoreStrategy (20-10,000)

Scoring is continuous and monotone: the score is the fraction of the 130 cases
that pass, so a more complete implementation never scores lower than a less
complete one.

Channel weights are realised through case counts rather than a weight table. The
differential oracle carries 55 cases, parser correctness 22, pool integrity 22,
cache correctness 20, and resource behavior 11, which realises a 42/17/17/15/9
percent split under any harness that scores as a fraction of tests passed, and
degrades sensibly under one that scores pass/fail.

Every case is a separate test function asserting one property, so credit is
fine-grained. An agent that implements the parser and connection but not the
pool scores meaningfully above zero. Within the oracle, cases are grouped by
command family, so partial command coverage earns proportional credit.

The floor is verified, not assumed: the untouched starter stubs score 0 of 130
with all 130 cases collected, meaning the suite runs against them and reports
zero rather than erroring out. That property is what makes partial credit
meaningful at the low end, and it is re-measured after every change to the
harness.

A sans-io violation is the one all-or-nothing condition: a client whose parser
reaches an I/O module scores zero across all channels, because the constraint is
structural rather than behavioural.

## anticipatedExploits (20-20,000)

Six exploits were built as working client implementations and scored by the full
suite. Each is a real attempt, not a described one, and the score each reaches is
measured.

Wrapping redis-py. Reaches 0 of 130. The primary control is structural rather
than detective: the oracle interpreter is owned by a separate unprivileged user
at mode 0700, reachable only through a sudo wrapper that executes it without
permitting a read, so a client that searches the filesystem for a redis package
finds nothing it can open. An AST check on the import graph is a secondary
layer. An earlier version of this defence was a one-shot assertion at session
start, and the attack defeated it by deferring the injection until after the
assertion had passed; isolation is now re-asserted after every case.

Hardcoding expected outputs. Reaches 32 of 130. Expectations are computed at run
time from a live server, keys are randomised per case per run, and no file in the
harness contains an expected value. A table can only carry what does not depend
on server state.

Reading the harness. Reaches 4 of 130, and the reason is the interesting part:
the attack succeeds completely at reading everything it wants. Harness module
objects are in `sys.modules` and the seed is in the environment, all reachable
without an import statement a static check could see. None of it is an answer,
because the oracle computes the agent's result before asking redis-py for
anything, so at call time the expected value does not exist.

Special-casing known split boundaries. Reaches 115 of 130. A parser that discards
partial tails passes the entire oracle channel, because on loopback a reply
usually arrives in one recv. What catches it is the resource channel, which the
weighting makes the smallest. Precomputed offsets against the published visible
seed bought nothing, because the exhaustive and one-byte cases draw on no seed at
all.

Detecting the grading environment. Reaches 108 of 130, which is below the 115 the
same parser reaches with no detection. The exploit used `PYTEST_CURRENT_TEST` not
to look up answers, which does not work, but to recognise cases whose assertion a
parser producing nothing would satisfy: the fragmentation invariant compares a
parser against itself, and absence is consistent with itself. Those cases now
assert their reference side is non-empty before comparing, which makes the
exploit counterproductive rather than merely blocked.

Serialising the pool behind one lock. Reaches 126 of 130. It is detected
structurally, by workers acquiring and blocking on a barrier before releasing, so
a serialising implementation fails on the barrier's timeout rather than on any
throughput judgement that a loaded sandbox could perturb.

Caching nothing at all. Reaches 114 of 130, losing 16 of the 20 caching cases. A
cache that never caches satisfies every freshness assertion trivially, which is
why every freshness case additionally asserts that hits occurred.

Beyond the exploits, the harness is defended against a subtler failure that is
not adversarial at all: a check that cannot fail. Five were found during
construction, by the mutation suite, by the attack suite, and once by review of
the script written to verify the image. A case dividing by a baseline that was
always zero. Thirteen cases an empty output satisfied. Four image probes fed on a
stdin that `docker run` discards. A shell `local` declaration resetting the exit
status it was reading. And a memory bound widened without re-deriving the
workload it bounded, which made the case unfailable rather than permissive. Every
assertion is now validated by breaking the property it tests and observing the
failure, and that discipline is what caught the fifth one before it reached the
bundle.
