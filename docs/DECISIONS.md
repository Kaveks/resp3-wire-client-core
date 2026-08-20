# Decision register

Ratified decisions with rationale. Superseding a decision requires a new entry,
not an edit.

## D1. String-ish values are bytes

Ratified 2026-08-18.
No decoding anywhere. Matches redis-py with `decode_responses=False`, which is
the oracle's configuration.

## D2. Errors nest as values, raise at top level

Ratified 2026-08-18.
`ErrorReply` occupies value positions inside aggregates. Required because
`EXEC` returns an array in which individual commands may have failed.

## D3. Attributes use a delegating wrapper

Ratified 2026-08-18.
`Attributed.__eq__` and `__hash__` delegate to the wrapped value. Known cost:
`isinstance(x, bytes)` is false for a wrapped bytes value.

The delegation was chosen so the oracle could compare attributed values against
redis-py transparently. D11 later established that redis-py raises on attribute
frames rather than discarding them, so no oracle case carries one and the
delegation buys nothing there. It is retained because it remains correct for
callers, and because the sealed delegation case in HARNESS.md 4.4 asserts it
directly.

## D4. Verbatim strings are a bytes subclass

Ratified 2026-08-18.
`VerbatimBytes(bytes)` with a `format` attribute, prefix stripped.

## D5. Remaining scalars map to builtins

Ratified 2026-08-18.
Big numbers to `int`, doubles to `float`, sets to `set` with a documented list
fallback. Nan excluded from the oracle matrix.

## D6. The parser is sans-io

Ratified 2026-08-18.
No socket dependency. Moves the task away from the well-worn client shape,
makes the chunking channel exercise a public interface, and is structurally
verifiable.

## D7. Push frames parse, pubsub does not exist

Ratified 2026-08-18. Resolves O1 of PROTOCOL.md.
A client that cannot parse `>` desynchronises permanently once client tracking
is enabled, and the failure is silent until it corrupts a later reply.

## D8. Blob errors are ordinary errors

Ratified 2026-08-18. Resolves O2 of PROTOCOL.md.
`!` parses into `ErrorReply` by the same rules as `-`.

## D9. Relative timing ratios permitted for complexity verification

Ratified 2026-08-18. Resolves O1 of HARNESS.md.
SUPERSEDED by D14 on 2026-08-18. The permission stands; the specific metric
below does not. Do not implement the bound stated here.
Absolute wall clock assertions remain prohibited. The resource channel, which
was channel 4 until D35 renumbered it to 5, may assert
T(8MB)/T(1MB) < 16.0, minimum of 5 trials, against an ideal linear 8.0 and a
quadratic 64.0. This is the only sanctioned use; any new one requires
ratification.

## D10. Server debug flag required

Ratified 2026-08-18. Resolves O3 of HARNESS.md.
Every server invocation passes `--enable-debug-command yes`, enabling
`DEBUG PROTOCOL` and `DEBUG SLEEP`.

## D11. The oracle compares values; sealed cases compare types

Ratified 2026-08-18. Resolves A1, A2, A3.
Measurement against redis-py 8.1.0 established three facts that invalidate an
exact cross-library comparison: redis-py raises `InvalidResponse` on RESP3
attribute frames rather than discarding them; it returns `list` for every RESP3
set by deliberate design, because a set may contain unhashable members; and its
exceptions are `redis.exceptions` classes with no `code` attribute, while a
nested `EXEC` error arrives as an exception instance where this contract
requires `ErrorReply`.

The comparator therefore becomes permissive about cross-library shape in three
specific ways, and three dedicated cases assert the agent-side type directly
with no redis-py involvement. Attributes are excluded from the oracle entirely
and verified in the chunking channel.

Consequence: the attribute requirement carries none of the oracle's weight. The
chunking channel is where it is enforced, and its attribute coverage is
increased accordingly.

## D12. Redis pinned at 7.4

Ratified 2026-08-18. Resolves A5.
Debian bookworm ships Redis 7.0, and `HEXPIRE` requires 7.4. The image takes
`redis-server` and `redis-cli` from `redis:7.4-bookworm` by multi-stage copy
into `python:3.12-slim-bookworm`. The development helper uses the same
bookworm-based image rather than alpine, for libc parity.

## D13. Credentials are out of scope

Ratified 2026-08-18. Resolves A10.
`Connection` takes no username or password. The AUTH clause in API.md section 5
is struck. No dead code path is implemented for an unreachable feature.

## D14. D9's two-point ratio is superseded by per-byte cost growth

Ratified 2026-08-18. Supersedes D9's formulation, not its intent.

D9 authorized `T(8MB)/T(1MB) < 16.0` as the complexity discriminator.
Measurement against the reference parser showed the metric is not stable enough
to carry a threshold. Two defensible readings of "minimum of 5 trials" produce
9.6 and 17.9 against the same linear parser, with the 16.0 bound between them.

The mechanism: interleaved paired trials couple the measurements through
allocator state. A 1 MB run executed immediately after an 8 MB run inherits a
freshly freed large arena and completes roughly twice as fast, and the ratio
inherits that noise directly. The metric is reading the allocator, not the
algorithm.

The replacement measures per-byte cost across a wider size range:

    for size in (1 MB, 8 MB, 64 MB):
        t[size] = min over 5 trials of the chunked feed-and-drain time
        per_byte[size] = t[size] / size

    assert per_byte[64MB] / per_byte[1MB] < 8.0

A linear parser holds per-byte cost roughly constant. The reference measures
1.8x across the 64x range, the residual growth coming from allocator and cache
effects rather than from the algorithm. A quadratic parser's per-byte cost grows
in proportion to input size, so across a 64x range it grows 64x.

The bound of 8.0 sits between them with room on both sides: 4.4x above what the
reference measures, 8x below what a quadratic implementation would produce. That
is narrower headroom than the two-point ratio appeared to offer, but unlike that
ratio it is headroom against a real signal rather than against measurement noise.
If the flake budget shows the reference approaching 8.0 on a loaded sandbox, the
correct response is to widen the size range rather than to raise the bound.

This is a stronger discriminator than the two-point ratio for two reasons: three
points establish a trend where two establish only a difference, and the wider
range separates the classes by a factor that no allocator artifact can bridge.
It remains a relative comparison, so D9's constraint holds unchanged: absolute
wall clock assertions are still prohibited.

## D15. MovedError degrades rather than raises on malformed text

Ratified 2026-08-18. Resolves the step 3 note on unparseable MOVED text.
Error text not shaped like `MOVED <slot> <address>` yields `slot == -1` and an
empty address rather than raising. Raising from an exception constructor would
replace a diagnosable server error with an unrelated failure. Only a broken
server produces this and no specified case exercises it.

## D16. Release semantics and the pipeline seam

Ratified 2026-08-18. Resolves the step 5 judgement calls.

Releasing a connection the pool is not currently lending raises `ValueError`,
on the same footing as releasing a foreign one. Accepting it silently would
place one connection in the idle set twice and lend it to two borrowers, which
is the failure the pool channel exists to detect.

A release arriving after `close` is a discard. A borrower unwinding its `with`
block after another thread closed the pool has done nothing wrong, and raising
there masks the exception already in flight. This was found by repeated runs
rather than by reasoning, and is the reason the flake budget exists.

`Pipeline` may use internal `Connection` methods. Writing every command before
reading any reply cannot be expressed through `execute`, which couples one write
to one read. The seam is internal and unspecified; only the observable behavior
in `docs/API.md` section 7 is contractual.

## D17. Error codes recover from redis-py's exception class, not its message

Ratified 2026-08-18. Corrects HARNESS.md 2.4.

Measurement established that redis-py's `parse_error` strips the code prefix
from the message for every code in its `EXCEPTION_CLASSES` table and leaves it
intact for codes it does not recognise. The previously documented rule, taking
the first whitespace delimited token of the message, therefore yields `WRONG`
for a generic `ERR` and `NO` for `NOSCRIPT`, failing three error cases against
a correct client.

The code is recovered from the exception class where redis-py has one, and from
the message token only for a plain `ResponseError`.

This is the same class of finding as D11: a factually wrong claim about an
external library, corrected by measurement. Applying it immediately rather than
proposing it is correct, because `CLAUDE.md`'s proposal requirement governs
loosening a channel, and this loosened nothing. Three cases that were failing a
correct reference now pass.

## D18. The oracle runs redis-py in a separate process

Ratified 2026-08-18.

`CLAUDE.md` requires redis-py to be absent from the interpreter that imports the
client package. The development virtualenv holds both pytest and redis-py, so a
harness running in-process would place redis-py directly on that path, which is
the exploit the contract names as needing a structural defence rather than a
static check.

The oracle therefore executes redis-py under a separate interpreter named by
`RESP3_ORACLE_PYTHON`, returning expected values as tagged JSON that is rebuilt
into real Python values before comparison. The comparator sees actual values,
not a parallel representation. Session start asserts the separation and aborts
if the client interpreter can import redis-py.

## D19. The suite scales to 130 cases

Ratified 2026-08-19. Supersedes the fixed 100-case allocation.

The mutation suite established that sixteen of fifty-two mutations changed
observable behavior and failed no case, and that fourteen of those were genuine
coverage gaps rather than acknowledged limits. Attribute handling, which D11
made this harness's sole enforcement of the task's primary novelty claim, rested
on one case out of a hundred checking a `repr()` substring.

The 100-case ceiling was a means to the 50/20/20/10 weighting, not an end. The
suite scales to 130 (65, 26, 26, 13), which is the same ratios multiplied by
1.3. Every existing case survives; thirty slots open for the gaps.

The cost is real and accepted: the twenty-clean-run acceptance and the 100/0
score anchors are both invalidated and must be re-established at 130/0.

The alternative, holding at 100 by cutting enumerated cases, was rejected. A
verifier that misses fourteen properties its own contracts require is the
failure a reviewer is most likely to find, and it is the "weak, one-shot
verifier" the platform names as the commonest reason a working task is not
keepable.

## D20. The chunking invariant is necessary but not sufficient

Ratified 2026-08-19.

The invariant compares a parser against itself and cannot detect a defect
consistent across split schedules. Seven mutations confirmed this: rendering
`*-1` as `[]`, truncating a blob error at an embedded CRLF, returning `1` for
`#t`, parsing a push frame as a list, parking attributes at the wrong depth,
failing to merge consecutive attributes, and returning big numbers as bytes all
satisfied every invariance case while the corpus contained the exact frames.

The channel gains eight absolute-expectation cases asserting what frames parse
to, written from `docs/PROTOCOL.md` with no parser on the other side. The
invariance cases keep their purpose, which is fragmentation robustness; they
were never the right instrument for value correctness and are no longer asked to
be.

## D21. Coverage gaps found by mutation are closed, not accepted

Ratified 2026-08-19.

Four cases passed for reasons unrelated to what they assert, and are corrected
rather than documented: the `reset()` case could not fail because its baseline
was always zero; the post-poison case observed a socket timeout rather than a
client refusal because the server was still stalled; the depth case sat below
CPython's recursion limit; and the health-check case passed when the check
discarded every connection, because the replacement worked.

Two gaps are accepted as genuine limits and remain uncovered. `ProtocolError`
poisoning has no public induction path, already recorded in section 5.4.
Pipeline batching produces byte-identical results to a write-read loop, and
`docs/API.md` section 7.1 states that the syscall count is unobservable and
unverified.

## D22. Blob errors are unreachable from the pinned server

Ratified 2026-08-19.

`DEBUG PROTOCOL err` does not exist in Redis 7.4.10. The command rejects it and
lists the valid names, none of which emit a `!` frame. No live-server oracle
case can therefore carry a blob error, and the RESP3 scalar coverage group
substitutes `double`.

Blob error handling remains enforced, by the chunking absolute-expectation case
asserting that a payload containing CRLF survives intact. That is the property
that distinguishes `!` from `-` in the first place, so nothing meaningful moved.

## D23. The third scaling case varies chunk size, not payload size

Ratified 2026-08-19.

D14's two cases fix the chunk size at 4 KB and vary payload size. A parser whose
cost grows with the number of feed calls rather than with total bytes is eight
times less visible to them than to a 512-byte measurement. The third case
repeats the D14 metric at 512 bytes, same form and same 8.0 bound, so it
introduces no new use of the D9/D14 timing exception. The reference measures
1.20x.

## D24. Self-referential cases must assert their reference is non-empty

Ratified 2026-08-19.

The attack suite established that a parser producing nothing satisfies thirteen
chunking invariance cases exactly, because those cases compare a partitioned
feed against a whole-buffer feed of the same parser. Three resource cases have
the same shape: the scaling measurement calls `gets()` and discards the result,
so the D14 assertions pass without a value ever coming back.

An exploit does not need to know the expected answer to use this. It needs only
to know which case is running, which `PYTEST_CURRENT_TEST` supplies, and to
return nothing there while parsing normally elsewhere. That reached 120 of 130.

Two assertions are tightened, not added, so the 65/26/26/13 allocation is
unchanged:

    chunking.py::assert_invariant   the whole-buffer reference must be non-empty
                                    before it is compared against
    resource.py::elapsed_chunked    the drain must produce a value

D20 established that the invariant cannot detect a defect consistent across
split schedules. This is the sharper form of the same limit: the invariant
cannot detect the absence of output at all, because absence is consistent with
itself. Every self-referential comparison in this harness must assert that its
reference side is meaningful before comparing.

## D25. redis-py must be unreachable, not merely unimported

Ratified 2026-08-19.

`CLAUDE.md` names interpreter separation as the primary structural control
against wrapping redis-py, with the AST check as a secondary layer. The attack
suite established that the separation as built is neither structural nor a wall.
redis-py sits on the same filesystem one glob away, `sys.path` is writable, and
the isolation assertion fires once at session start. An injection deferred until
the first `connect()` runs after the assertion has passed and is never
rechecked. That reached 71 of 130 with a parser raising on every call, caught
only by the static check the contract says must not be the control.

Three controls, all required, none sufficient alone:

The image runs the harness as an unprivileged user who cannot read the oracle
interpreter's `site-packages`. The glob then finds nothing and the import fails
wherever it is attempted. This is the structural control and it belongs to the
Dockerfile.

`harness/conftest.py` re-asserts isolation after every case rather than once at
session start. A one-shot check is a check an attacker waits out.

The AST check remains, unchanged, as the third layer.

The oracle subprocess is unaffected: it runs as a different user with its own
interpreter, which is the arrangement D18 already describes.

## D26. A check that has not been seen to fail is not a check

Ratified 2026-08-19.

Three times in this project a check passed because its success path was
reachable without the property holding. The `reset()` case divided by a baseline
that was always zero and reported 1.0 unconditionally. Thirteen chunking cases
compared a parser against itself, which a parser producing nothing satisfies
exactly. Four image checks fed Python on stdin to `docker run` without `-i`, so
the interpreter read nothing and exited 0.

Each was found by something other than the check: the mutation suite, the attack
suite, and manual review of the script written to verify the controls.

Every assertion added from here is validated by breaking the property it tests
and observing the failure. This applies to harness cases, image checks, and
tooling equally. The cost is one deliberate breakage per assertion; the
alternative is a verifier whose green is uninformative.

## D27. The graded package must be asserted, not assumed

Ratified 2026-08-19.

`python -m pytest` prepends the working directory to `sys.path`, ahead of
everything in `PYTHONPATH`. The image's `WORKDIR` is `/app`, which holds the
starter, so a harness invoked there imported the starter's `resp3_wire`
regardless of what `--client` named. The reference scored 0/130 inside the image
while reporting that it was grading the reference.

This never surfaced on the host because the repository root has no top-level
`resp3_wire`. Every host score was correct by accident of working directory.

The consequence had it shipped: the platform's oracle stage runs the reference
solution and would have scored it zero, rejecting the task as unsolvable by its
own reference with nothing in the output explaining why.

Two controls, prevention and detection:

`harness/run.py` sets `PYTHONSAFEPATH=1` for the pytest subprocess, which stops
the prepend, and passes `RESP3_CLIENT_PATH`.

`harness/conftest.py` asserts that the imported `resp3_wire.__file__` resolves
under that directory and aborts with exit code 5 otherwise. The assertion was
observed failing, with `PYTHONSAFEPATH` deliberately withheld, before being
trusted.

The general form: a harness that can be pointed at an implementation must prove
it graded the one it was pointed at. Naming it is not proving it.

## D28. A negative control tests the claim, not the apparatus

Ratified 2026-08-19.

Four image checks were found passing without executing, because `docker run`
discards stdin without `-i`. The fix was to write the probes as files and
execute them by path, and to run a negative control for each before the check
itself, per D26.

Writing those controls exposed a fifth gap. "pytest-timeout is installed" is not
the claim `docs/HARNESS.md` section 9 makes; the claim is that a case exceeding
30 seconds is killed. The control now runs a 45-second case and observes it
terminated at 30.9 seconds.

The distinction generalises. A control that verifies the mechanism is present is
weaker than one that verifies the mechanism acts. Where both are available, the
second is the control.

## D29. End-to-end verification is not optional

Ratified 2026-08-19.

Two defects reached step 10 that no earlier verification could have found, and
both would have caused the platform's oracle stage to fail with the reference
scoring zero.

`python -m pytest` prepended the working directory to `sys.path`, so the harness
graded whatever `resp3_wire` happened to sit in `/app` rather than the one
`--client` named. Recorded as D27.

`/app` was root-owned, because `WORKDIR` creates it that way and `COPY --chown`
changes only what it copies. `solve.sh` could not remove the starter package to
put the reference in its place.

Neither surfaced earlier because every prior run either only read `/app` or
mounted the implementation elsewhere. The common shape: a control verified in
the arrangement it was developed in, failing in the arrangement it ships in.

Any change to the image, the entrypoints, or the working directory is re-checked
by running `solve.sh` and then `test.sh` through the bundle's own paths, from a
cold build, offline. Nothing short of that is evidence.

## D30. The bundle's Docker build context is not specified

Ratified 2026-08-19. Open risk, recorded rather than resolved.

The platform requires `environment/Dockerfile` and does not state the build
context. If it is the Dockerfile's own directory, the image's inputs must live
under `environment/`. If it is the bundle root, the same `COPY` paths resolve to
nothing and the build fails outright, which is a deterministic rejection rather
than a partial score.

The Dockerfile is written to work under either context where that is achievable,
and where it is not, the inputs are duplicated so both path forms resolve. A few
kilobytes against a 428 KB bundle is the correct price for removing a total
failure mode.

The `task.toml` key names under `[environment]` carry the same character. The
platform documentation names `cpus`, `memory_mb`, `storage_mb`, and `gpus`;
`docs/SUBMISSION.md` uses the draft form's camelCase. The TOML uses the
documented snake_case, and the derivation from SUBMISSION.md maps between them
rather than copying spelling.

## D31. The apparatus is not exempt

Ratified 2026-08-19.

Four times now a check has passed without testing its property: a division by a
baseline that was always zero, a comparison a parser producing nothing
satisfied, probes fed on a stdin that `docker run` discarded, and a shell
`local` declaration resetting `$?` so an exit status read the declaration rather
than the command.

The last is the instructive one. It sat in the script written to verify the
bundle, after D26 had been ratified specifically to prevent this, and it
produced two failures on one line: a spurious failure that was investigated
because it was loud, and a vacuous pass that was found only because the
investigation happened to pass through it.

D26 applies to tooling, image checks, and shell scripts exactly as it applies to
harness cases. A verification script is a check, and an unverified verification
script is worse than none, because its green is trusted.

## D32. Derived values are asserted by their derivation

Ratified 2026-08-19.

`docs/SUBMISSION.md` declares `cpuMillis: 4000`. The platform's `task.toml` key
is `cpus`, a count, so the correct value is 4. Writing 4000 there requests 4000
CPUs against an 8-CPU ceiling, which is a deterministic rejection, and it looks
correct to any check comparing the two numbers for equality.

`tools/check_paths.py` asserts the conversion including its factor of 1000
rather than asserting the figure. The same applies to every value the bundle
derives from a contract: the check is of the mapping, not of the result, because
a result can agree with its source and still be wrong.

## D33. The task gains client-side caching

Ratified 2026-08-20.

Two blind trials established that the task is saturated. Opus 5 scored 130/130
in 29 minutes and Sonnet 5 scored 129/130 in 20, both against a four-hour
budget. The single miss was a generator-based parser exceeding the recursion
limit, which the resource channel caught correctly.

The verifier is not the problem. The mutation suite proved every channel fails
independently, and the resource channel demonstrated that live. The problem is
that `spec/instruction.md` names every trap explicitly, so avoiding them
requires competent implementation rather than discovery, and both models are
competent implementers.

Client-side caching is added because its difficulty survives full
specification. The requirement is stateable in a paragraph; meeting it is not,
because invalidation arrives asynchronously on a channel the client is not
reading at the time. A `GET` returns a value while the invalidation for that
key may already sit in the socket buffer, may arrive during the next command,
or may arrive while another thread is mid-read. An implementation that checks
for invalidations only between commands serves stale data. So does one that
caches before confirming no invalidation is pending. Under a pool with
concurrent workers the window widens.

That is a race a specification can require the resolution of without describing
it, which is what the previous feature set lacked.

## D34. The cache is pool-wide, not per-connection

Ratified 2026-08-20.

A per-connection cache is simpler and weaker: an invalidation arriving on the
connection that cached the value is straightforward to handle, and the hard case
never arises.

Pool-wide is where the difficulty lives. Redis sends an invalidation to every
tracking client that read the key, but a pooled client reads through whichever
connection it borrowed, so the invalidation for a value cached by worker A can
arrive on the connection worker B is holding. Correct eviction therefore
requires the connection that receives an invalidation to reach shared state it
does not own, under concurrency, without holding a lock across socket I/O, which
`docs/API.md` section 6.4 already forbids.

## D35. Caching gets its own channel; the oracle is untouched

Ratified 2026-08-20.

redis-py implements client-side caching with its own semantics. Comparing a
cached read against it would test whether the agent reimplemented redis-py's
cache rather than whether the protocol works, which is the trap D11 exists to
prevent.

Caching is opt-in through a constructor argument and off by default, so every
existing oracle case runs against a non-caching connection and its behavior is
unchanged. Correctness is graded by a fifth channel with expectations written
from `docs/API.md` and verified against observable server state, never against
redis-py.

The suite stays at 130 with the weight redistributed:

    oracle      55   was 65
    chunking    22   was 26
    pool        22   was 26
    caching     20   new
    resource    11   was 13

Ratios move from 50/20/20/10 to roughly 42/17/17/15/8. The oracle remains the
largest single channel and the reduction is proportional across the other three,
so no existing property loses relative weight against another.
