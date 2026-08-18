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
