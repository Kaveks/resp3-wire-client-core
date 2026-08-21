# Reviewer rationale

Status: draft 2026-08-21. Maintainer owned. Does not ship in the bundle.

This document exists so that the task can be judged without reading the
reference implementation or the sealed harness. It is also the source the prose
fields in `docs/SUBMISSION.md` are compressed from, so it is written honestly
rather than persuasively: the weaknesses are here alongside the evidence.

## 1. What the agent is asked to do

Implement a Redis client library in pure Python, speaking the wire protocol
directly over TCP. Six parts: an incremental RESP2 and RESP3 parser, a typed
error hierarchy, a connection with HELLO negotiation and fallback, a connection
pool, command pipelining, and opt-in client-side caching with server driven
invalidation.

The agent starts from stubs presenting the full public surface, every method
raising `NotImplementedError`, plus 70 visible tests and a Redis 7.4 server in
the container. It has four hours.

## 2. Why this is realistic work

Every protocol client in production faces the same four problems this task is
built around, and every mature client has solved them: redis-py, lettuce,
go-redis, and node-redis all carry an incremental parser, a pool with poisoning
rules, pipelining, and tracking-based caching.

The task is not a puzzle wearing engineering clothes. It is the load-bearing
core of a library category, reduced to the parts where correctness is hard and
observable.

## 3. Where the difficulty is

Four places, and they share a property: each produces code that looks correct
and passes casual testing.

**Fragmentation.** A parser that assumes complete buffers works perfectly until
the network splits a reply. The invariant is that any partition of the same
bytes must produce the same values, including splits inside a length prefix,
between the CR and LF of a terminator, and inside a verbatim string's format
prefix. Naive implementations rescan from the start on resumption, which is
correct but quadratic, or discard partial progress, which is neither.

**Attribute frames.** RESP3 `|` decorates the value that follows it and is not a
reply of its own. The common failure is emitting the attribute dictionary as a
standalone reply, which desynchronises every subsequent read by one.

**Pool poisoning.** A timeout after the command is written but before the reply
is fully read leaves an unknown number of bytes in the socket. Returning that
connection to the pool means the next borrower reads the tail of someone else's
reply. The failure is silent: no exception, just a wrong value.

**Cache invalidation.** The server delivers invalidations asynchronously on
whichever connection was tracking the key. The invalidation for a value may
already be in the socket buffer before the reply that would cache it has been
parsed, and the cache is pool-wide so the connection receiving it is usually not
the one that cached the value. Draining what happens to be readable is not
enough: Redis writes the invalidation and the writer's `+OK` in the same event
loop tick, so a reader can observe the reply before the notice.

That last point is measured, not asserted. See section 7.

## 4. How it is graded

Five independent channels, 130 cases, weighted 42/17/17/15/9 percent by case
count rather than by a weight table, so the weighting holds under any harness
that scores as a fraction of tests passed.

**Differential oracle, 55 cases.** The agent's client and redis-py execute the
same commands against the same live server and the results are compared
structurally. Expected values are computed at run time; no fixture file in the
harness contains an expected value. Keys are randomized per case per run.
Thirteen cases run under RESP2 to test degradation fidelity, since a hash reply
is a flat array there and a score is a bulk string.

**Parser correctness, 22 cases.** Seven assert absolute values against
expectations written from the protocol contract, with no parser on the other
side. Four assert attribute semantics directly. The remainder assert the
fragmentation invariant under one-byte feeds, exhaustive splits at every
position, seeded random partitions, and pathological boundaries.

**Pool integrity, 22 cases.** Concurrent workers borrow, issue tagged `ECHO`
commands, and assert every reply carries their own tag. Timeouts are induced
with `DEBUG SLEEP`, disconnects with `CLIENT KILL` from a second connection.
Concurrency is asserted structurally with a barrier rather than by timing.

**Cache correctness, 20 cases.** Invalidation is induced through a second,
non-caching connection, so the write never passes through the code under test.
Expectations come from the server, never from redis-py, which has its own
caching semantics.

**Resource behavior, 11 cases.** Peak retained bytes under `tracemalloc` must
stay within three times the payload. Cost per byte must not grow more than
eightfold across a sixty-fourfold size range, measured at three sizes
independently rather than as a two-point ratio.

## 5. Visible and hidden

Visible: 70 tests in `/app/tests`, demonstrating the API surface and basic
behavior under a published seed. They deliberately avoid adversarial
fragmentation, pool corruption, the full command matrix, and the caching races.

Sealed: the 130-case harness, which ships in `tests/` and never enters the image
an implementer works in. That separation is verified rather than assumed: a leak
probe scans the built image, with negative controls that fail when the harness or
the reference is planted in it.

The specification names the five graded areas and their weights. It does not
describe case construction, seeds, or the induction methods.

## 6. Anti-gaming

Six exploits were built as working clients and scored by the full harness.

**Wrapping redis-py** reaches 0/130. The oracle interpreter is owned by a
separate user at mode 0700 and reachable only through a `sudo` wrapper, so a
client that globs the filesystem for a redis package finds nothing it can open.
The AST check is a secondary layer, not the control. An earlier version of this
defence was a one-shot assertion at session start; the attack suite defeated it
by deferring injection to the first `connect`, and isolation is now re-asserted
after every case.

**Hardcoded replies** reaches 32/130. Expectations are computed at run time from
a server whose keys are randomized per case per run, so there is nothing to
tabulate.

**Reading harness internals** reaches 4/130, and the reason matters: the attack
succeeds at reading everything it wants. Module objects and environment
variables are all reachable. None of it is an answer, because the oracle
computes the agent's result before asking redis-py for anything, so the expected
value does not exist at call time.

**Special-casing split boundaries** reaches 115/130, caught by the resource
channel rather than the chunking channel. Precomputed offsets against the
published visible seed bought nothing, because the exhaustive and one-byte cases
draw on no seed.

**Detecting the grading environment** reaches 108/130, below the 115 the same
parser reaches with no detection at all. The exploit produced nothing for cases
it recognised, which the self-referential invariant satisfied; those cases now
assert their reference side is non-empty, and the exploit is now
counterproductive.

**Serialising the pool** reaches 126/130. It is detected structurally by the
barrier rather than by any throughput judgement.

**Caching nothing at all** reaches 114/130, losing 16 of 20 caching cases,
because every freshness case additionally asserts that hits occurred.

## 7. Evidence

**The reference solves it.** 130/130 through the bundle's own entrypoints, cold
build, offline, from both plausible Docker build contexts.

**The floor is zero.** Untouched starter stubs score 0/130 with all 130 cases
collected, so the suite runs and reports rather than erroring out.

**The channels have teeth.** 64 deliberate mutations, each breaking one property
a channel claims to test, applied to copies of the reference. Every mutation
fails something except four accepted exceptions, each recorded with its reason.
The first run of this suite found sixteen mutations failing nothing, of which
fourteen were genuine coverage gaps; closing them is why the suite scaled from
100 cases to 130.

**It does not flake.** Twenty consecutive clean full-suite runs against the
reference with a fresh seed each. This bar has been re-established after every
change that touched the harness or the image, and it has failed twice: once
finding a pool release bug that escaped `__exit__` and masked the caller's
exception, once finding two caching defects in the reference.

**The specification is sufficient.** Three blind trials, each a fresh agent
session given only the container and `instruction.md`, no repository access, no
intervention, no answered questions.

## 8. The difficulty question, stated honestly

The blind trials are the weakest part of this task's case and they are reported
in full rather than summarised favourably.

    trial                            model      elapsed   score
    before caching                   Opus 5     29 min    130/130
    before caching                   Sonnet 5   20 min    129/130
    after caching, tightened spec    Opus 5      ~1 h     129/130

The third trial's single miss was a 4 percent overshoot on an allocator slack
bound, not an implementation defect; the slack has since been widened, so the
honest reading is 130/130.

Two rounds of work were done in response. Client-side caching was added on the
theory that an asynchronous race survives full specification in a way that a
named trap does not. The specification was then cut by sixty lines, removing
every sentence that explained why something was hard or how it would be checked
while keeping every requirement.

Neither moved the score. The third trial found the invalidation race unaided, by
writing an adversarial probe, observing one stale read in four hundred,
diagnosing it as same-tick delivery, and closing it with an ordering proof on
the tracking channel. It then caught a subtler bug in its own fix. That is the
difficulty this task was rebuilt around, discovered without being pointed at.

The conclusion the evidence supports: a current frontier model with four hours
solves this task. Whether that disqualifies it depends on where the threshold
sits, which is not something these trials can establish.

What the trials do establish is that the verifier works. Every trial
implementation differed from the reference architecturally, and the harness
graded all three without a single false failure attributable to a design choice
rather than a defect.

## 9. Known weaknesses

**It may be too easy.** Section 8.

**Four properties are required and unenforced.** `ProtocolError` poisoning has
no induction path through the public API. Pipeline batching is byte-identical to
a write-read loop, so the batching itself is unobservable. A store-then-drain
reordering inside one `execute` leaves a window too narrow to observe without
instrumenting internals. And the cache generation check became unobservable once
the drain was strengthened, because a second mechanism now covers its window.
Each is recorded rather than papered over.

**The build context is a guess.** The platform requires `environment/Dockerfile`
and does not state the context. The image's inputs are carried twice so the
`COPY` paths resolve either way, at a cost of 60 KB, because an unresolved path
is a build failure rather than a low score.

**Two `task.toml` key names are unconfirmed.** The `[environment]` resource keys
follow the documented snake_case forms. `timeout_sec` under `[agent]` and
`[verifier]` follows the same convention but is not documented anywhere.

## 10. What a reviewer might reasonably object to

That the task is a library clone of something a model has seen many times. The
counter is that the graded properties are not the ones redis-py's shape teaches:
fragmentation invariance, attribute decoration, poisoning, and invalidation
races are all places where reading redis-py's source would mislead rather than
help, and one of them, the RESP3 set mapping, is a place where redis-py is
deliberately wrong by this task's contract.

That the specification is long. It is 467 lines, and every line states a
requirement rather than a hint. The earlier version was 527 and the difference
is exactly the material a reviewer would object to as leaky.

That five channels is elaborate. The mutation suite is the answer: at four
channels and 100 cases, fourteen required properties were enforced by nothing.
