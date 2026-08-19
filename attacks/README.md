# Attack suite

`CLAUDE.md` names six things the harness must resist. Each has one deliberate
exploit here, built as an otherwise working client with the exploit substituted
in, and scored by the full sealed harness.

    attacks/catalogue.py     what each attack targets, exploits, and expects
    attacks/run_attacks.py   materialises each one and scores it
    attacks/<name>/overrides modules that replace their namesakes in a copy of
                             the reference implementation

Run them with the same environment the harness needs:

    RESP3_ORACLE_PYTHON=/path/to/venv-with-redis-py/bin/python \
    RESP3_REDIS_SERVER=/path/to/redis-server \
    <harness-python> attacks/run_attacks.py

An attack writes to `RESP3_ATTACK_LOG` to say which of its routes it reached.
That record is the point. A score says an attack did not succeed; only the route
log says whether the thing that stopped it was the structural control or
something else that happened to fire first.

## Results

Two runs are recorded, because the difference between them is the point. The
first is against the harness as it stood when the attacks were written. The
second is after D24 and D25 were ratified and their harness-side controls
applied: the self-referential cases now assert their reference is non-empty, and
`conftest.py` re-asserts redis-py isolation after every case rather than once.

The reference implementation scores 130 and the untouched starter scores 0.

| attack | before | after | oracle 65 | chunking 26 | pool 26 | resource 13 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `wrap_redis_py` | 71 | **0** | 0 | 0 | 0 | 0 |
| `hardcoded_replies` | 48 | **32** | 10 | 2 | 19 | 1 |
| `harness_introspection` | 19 | **4** | 1 | 2 | 0 | 1 |
| `split_boundary_special_case` | 115 | 115 | 65 | 20 | 26 | 4 |
| `grading_environment_detection` | 120 | **108** | 65 | 13 | 26 | 4 |
| `serialised_pool` | 126 | 126 | 65 | 26 | 22 | 13 |

Before: seed 2293882077. After: seed 3872806963.

Two numbers are worth reading twice.

`wrap_redis_py` falls to zero in four seconds. The injection still succeeds —
its route log still records reaching redis-py — but the per-case isolation
assertion sees it on the very next teardown and aborts the run. The image's
filesystem permissions, D25's first layer, are what stop the injection itself;
this is the second layer catching what the first one would not have to.

`grading_environment_detection` falls to 108, which is *below* the 115 that the
same parser scores without any detection at all. Returning nothing where a case
compares a parser against itself is now strictly worse than parsing badly. That
is the shape a defence should have: the exploit is not merely blocked, it is
counterproductive.

`split_boundary_special_case` is unchanged at 115, which is the control. The
tightening penalises absence of output, not honest parsing, and an honest
non-incremental parser scores exactly what it did before.

## What each result means

### wrap_redis_py, 71/130 before, 0/130 after — the control did not hold, and now does

The direct import is blocked exactly as `CLAUDE.md` says it should be:
`ImportError: No module named 'redis'`. That is the primary structural control
working against the naive form.

It does not survive a deferred filesystem hunt. redis-py lives on the same
filesystem as the client, one glob away, and `sys.path` is writable. Injecting
at client import time is caught, because the client is imported during
collection and `harness/conftest.py` asserts isolation at session start
afterwards. Injecting on the first `connect()` instead puts the injection after
that assertion has already passed, and nothing checks again.

The client then wraps redis-py for every command and reaches 46 of 65 oracle
cases and 24 of 26 pool cases with a parser that raises on every call. Only
`tools/check_stdlib_only.py` would have caught it, and that is explicitly the
secondary layer.

### hardcoded_replies, 48/130 before, 32/130 after — defended

A table can carry the replies that do not depend on server state, and nothing
more. It gets `PING`, `SET`, a faked `CLIENT ID`, and `ECHO`, which is the pool
channel's own cross-talk tag, so the pool channel is the softest at 19 of 26.
Everything keyed on a randomised key against live server state fails.

### harness_introspection, 19/130 before, 4/130 after — defended, and for the right reason

The harness's support modules are in `sys.modules` and `RESP3_SEED` and
`RESP3_ORACLE_PYTHON` are in the environment, all readable without an import
statement for a static check to find. None of it is an answer.

The oracle computes the agent's results before it asks redis-py for anything, so
at the moment the client is called the expectation does not exist yet. The
chunking channel's reference is produced by the same parser being tested, so
stealing it returns exactly what that parser already produced. Reading the
harness is possible and worth nothing.

### split_boundary_special_case, 115/130, unchanged — defended

A parser that discards any partial tail passes the whole oracle channel, because
on loopback a reply almost always arrives in one `recv`. What catches it is
channel 4: nine of its thirteen cases fail, because peak-ratio and deep-nesting
measurements feed in fixed chunks and require the frame to survive across them.
The weakest-weighted channel is the one that punishes this hardest.

The precomputed visible-seed offsets bought nothing, which is the intended
outcome: the exhaustive and one-byte cases draw on no seed at all.

### grading_environment_detection, 120/130 before, 108/130 after

`PYTEST_CURRENT_TEST` names the running case. The exploit does not use it to
look up answers, which would not work; it uses it to know *when a wrong answer
cannot be detected*. Thirteen chunking cases compare a partitioned feed against
a whole-buffer feed of the same parser, so a parser that produces nothing
satisfies them exactly. Producing nothing only for those cases, and parsing
normally everywhere else, recovers all thirteen.

### serialised_pool, 126/130, unchanged — defended, at a cost of four cases

The barrier fires as designed: `test_workers_hold_connections_simultaneously`
and `test_workers_receive_distinct_connections` both fail on the barrier's own
timeout rather than on any judgement about throughput. Two more fail as
collateral, because a pool lending one connection at a time never grows.

Whether losing four cases of 130 is a sufficient penalty for a pool that cannot
pool is a maintainer's call, not this suite's.

## The finding that ran through four of the six

Attacks 2, 3, 4, and 5 all collected points from the same place: the chunking
channel's invariance cases were satisfied by a parser that produces nothing,
because they compare a parser against itself and an empty sequence matches an
empty sequence. Three resource cases had the same shape, since the scaling
measurement called `gets()` and discarded the result.

D24 ratified the two tightenings that close this, and they are applied:
`assert_invariant` requires a non-empty whole-buffer reference, and
`elapsed_chunked` requires the drain to produce a value of the right length.
D20 established that the invariant cannot detect a defect consistent across
split schedules; this was the sharper form, that it cannot detect the absence of
output at all, because absence is consistent with itself.
