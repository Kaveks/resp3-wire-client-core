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
`isinstance(x, bytes)` is false for a wrapped bytes value. See D11 for the
consequence discovered later.

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
Absolute wall clock assertions remain prohibited. Channel 4 may assert
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

A linear parser holds per-byte cost roughly constant; the reference measures
1.8x across the 64x range. A quadratic parser's per-byte cost grows with size
directly, giving a ratio near 64. The bound of 8.0 sits an order of magnitude
from both.

This is a stronger discriminator than the two-point ratio for two reasons: three
points establish a trend where two establish only a difference, and the wider
range separates the classes by a factor that no allocator artifact can bridge.
It remains a relative comparison, so D9's constraint holds unchanged: absolute
wall clock assertions are still prohibited.

## D15. MovedError degrades rather than raises on malformed text
Ratified 2026-08-18.
Error text not shaped like `MOVED <slot> <address>` yields `slot == -1` and an
empty address rather than raising. Raising from an exception constructor would
replace a diagnosable server error with an unrelated failure. Only a broken
server produces this and no specified case exercises it.
