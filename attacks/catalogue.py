"""The attack catalogue.

`CLAUDE.md` names six things the harness must resist. Each has one deliberate
exploit here, built as an otherwise working client with the exploit substituted
in, and scored by the full sealed harness.

The score is not the finding. What matters is *why* an attack fails, and
whether the thing that stopped it is the structural control or the static one.
`CLAUDE.md` is explicit that static checks are a secondary layer and never the
primary control: an attack that survives every structural defence and dies only
on `tools/check_stdlib_only.py` has found a real weakness, and is reported as a
weakness rather than counted as a defence.

Each attack names the requirement it targets, the defence that is supposed to
stop it, and what a successful exploit would have looked like. `run_attacks.py`
materialises each one over a copy of the reference implementation and reports
the score it reaches.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["Attack", "ATTACKS", "by_name"]


@dataclass(frozen=True)
class Attack:
    """One deliberate exploit attempt."""

    #: Directory name under attacks/, and the name in the report.
    name: str
    #: The resistance requirement from CLAUDE.md that this targets.
    requirement: str
    #: What the exploit does.
    exploit: str
    #: What is supposed to stop it, and whether that control is structural.
    defence: str
    #: What the attacker would have gained had it worked.
    payoff: str
    #: Modules replaced in a copy of the reference implementation.
    overrides: tuple[str, ...]


ATTACKS: list[Attack] = [
    Attack(
        name="wrap_redis_py",
        requirement="wrapping redis-py",
        exploit=(
            "The parser is a stub that never parses. The connection reaches for "
            "redis-py by three escalating routes: a direct import; a filesystem "
            "hunt for a redis package belonging to another interpreter, injected "
            "onto sys.path; and a subprocess running the oracle interpreter named "
            "by RESP3_ORACLE_PYTHON, proxying every command through redis-py and "
            "returning its answers."
        ),
        defence=(
            "Primary and structural: redis-py is absent from the interpreter that "
            "imports the client package, so the direct import raises ImportError. "
            "tools/check_stdlib_only.py is the secondary static layer and must not "
            "be what stops this."
        ),
        payoff="the whole oracle channel without parsing a byte",
        overrides=("parser.py", "connection.py"),
    ),
    Attack(
        name="hardcoded_replies",
        requirement="hardcoding responses",
        exploit=(
            "The parser never returns a value. The connection opens a real socket "
            "so that the pool's bookkeeping is satisfied, writes commands so the "
            "server sees them, and answers from a table keyed on the command name: "
            "PING to PONG, ECHO to its own argument, SET to OK, CLIENT ID to a "
            "counter, HELLO to a plausible map."
        ),
        defence=(
            "Structural: expected values are computed at run time from a live "
            "server, keys are randomised per case per run, and no fixture file "
            "holds an answer. A table can only carry replies that do not depend on "
            "server state, which is a small and shrinking set."
        ),
        payoff="oracle and pool points with no protocol implementation at all",
        overrides=("parser.py", "connection.py"),
    ),
    Attack(
        name="harness_introspection",
        requirement="reading harness internals",
        exploit=(
            "The parser walks the call stack looking for the expectation the case "
            "computed before calling it, imports every harness support module it "
            "can reach, and reads the run seed and the report paths out of the "
            "environment. Whatever it finds, it answers with."
        ),
        defence=(
            "Structural: the oracle's expected values are produced in a separate "
            "interpreter and, within a case, are not computed until after the "
            "agent's commands have already run, so there is nothing to steal at "
            "the moment the client is called."
        ),
        payoff="every expectation the harness holds, read directly",
        overrides=("parser.py",),
    ),
    Attack(
        name="split_boundary_special_case",
        requirement="special casing known split boundaries",
        exploit=(
            "A parser that is not incremental at all. It parses whatever whole "
            "frames the current chunk happens to contain and discards any partial "
            "tail, which is correct exactly when every feed lands on a frame "
            "boundary and wrong otherwise."
        ),
        defence=(
            "Structural: the exhaustive cases split at every interior position of "
            "a frame and the one-byte cases feed a byte at a time, so every "
            "boundary is exercised without any randomness to predict."
        ),
        payoff="passing the chunking channel without buffering across feeds",
        overrides=("parser.py",),
    ),
    Attack(
        name="grading_environment_detection",
        requirement="detecting the grading environment",
        exploit=(
            "The same non-incremental parser, plus detection: it reads "
            "PYTEST_CURRENT_TEST to learn which case is running and answers the "
            "constant-valued ones from a table keyed on the case name, and reads "
            "RESP3_SEED to try to reproduce the partitions the seeded cases will "
            "use."
        ),
        defence=(
            "Structural: the deterministic cases cover every split position "
            "already, so reproducing a seeded schedule buys nothing; and a case "
            "name supplies no value for any case whose expectation depends on "
            "randomised keys or live server state."
        ),
        payoff="the cases the previous attack lost, recovered by knowing which one is running",
        overrides=("parser.py",),
    ),
    Attack(
        name="cache_that_never_caches",
        requirement="a cache that passes every freshness assertion trivially",
        exploit=(
            "The cache surface is present and its counters move, but nothing is "
            "ever stored: `get` always misses and `offer` always refuses. Every "
            "read therefore reaches the server and every freshness assertion in "
            "the caching channel is satisfied by construction."
        ),
        defence=(
            "docs/HARNESS.md section 6.3: a case asserting freshness passes "
            "trivially against an implementation that caches nothing, so each "
            "such case additionally asserts that hits occurred. That is D24 "
            "applied to this channel."
        ),
        payoff="the whole caching channel without implementing a cache",
        overrides=("cache.py",),
    ),
    Attack(
        name="serialised_pool",
        requirement="serialising all pool operations behind a single lock",
        exploit=(
            "One global lock, taken in acquire and released in release, so exactly "
            "one borrower holds a connection at a time. Every sequential property "
            "the pool channel asserts still holds."
        ),
        defence=(
            "Structural: workers block on a threading.Barrier while holding their "
            "connections, so a pool that hands out one at a time never reaches the "
            "barrier and fails on its timeout, which is a correctness failure "
            "rather than a throughput judgement."
        ),
        payoff="a trivially correct pool that cannot pool",
        overrides=("pool.py",),
    ),
]


def by_name(name: str) -> Attack:
    for attack in ATTACKS:
        if attack.name == name:
            return attack
    raise KeyError(name)
