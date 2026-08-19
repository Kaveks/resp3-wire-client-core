"""Shared fixtures and seed discipline for the sealed harness.

The interpreter running these tests imports the client package. It must not
have redis-py on its path; expected values come from a separate interpreter
through `support/probe.call_backend`.

Per D25 that separation is asserted after every case, not once. The attack
suite established that a one-shot assertion is one an attacker waits out:
redis-py sits on the same filesystem, `sys.path` is writable from the client,
and an injection deferred until the first `connect()` lands after a
session-start check has already passed. Re-checking per case turns a wall an
attacker can walk around into one they would have to hold down.

The permission control that makes the injection fail in the first place belongs
to the image, not here. This is the second of D25's three layers.
"""

from __future__ import annotations

import importlib.util
import os
import random
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from support.probe import call_backend, run_probe  # noqa: E402
from support.redis_boot import RedisServer  # noqa: E402
from support.wire_codec import decode_value, encode_args  # noqa: E402

# docs/HARNESS.md section 7.1. The visible tests use this seed and say so, so a
# sealed run that inherits it is grading against a schedule the implementer has
# already seen.
VISIBLE_SEED = 20260818

# Run on every run regardless of the drawn seed, so a green run is never purely
# luck and a regression a random seed happens to miss is still caught.
REGRESSION_SEEDS = (1, 2, 31337, 2 ** 32 - 1)


def redis_py_reachable() -> str | None:
    """Whether redis-py can be reached from this interpreter, and how.

    Checks both halves of reachability. A client that has already imported it
    leaves it in `sys.modules`; a client that has only prepared the ground
    leaves a findable spec on `sys.path`. Neither is acceptable.
    """
    module = sys.modules.get("redis")
    if module is not None:
        return f"redis is loaded in sys.modules from {getattr(module, '__file__', '?')}"
    try:
        spec = importlib.util.find_spec("redis")
    except (ImportError, ValueError):
        return None
    if spec is not None:
        return f"redis is importable from {spec.origin}"
    return None


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "channel(name): which scoring channel a case belongs to")


def pytest_runtest_teardown(item: pytest.Item) -> None:
    """D25, layer two. Re-assert isolation after every case.

    A client that reaches redis-py at any point during the run has wrapped it,
    whether it did so at import time or on its first command. Aborting rather
    than scoring is deliberate: the cases that already ran were not measuring
    what they appear to have measured.
    """
    reached = redis_py_reachable()
    if reached is None:
        return
    pytest.exit(
        f"redis-py became reachable from the interpreter under test during "
        f"{item.nodeid}: {reached}. The client package must not be able to "
        f"reach it at any point in the run, not merely at session start.",
        returncode=3,
    )


def assert_the_graded_package_is_the_one_requested() -> None:
    """The package that gets imported must be the one `--client` named.

    `python -m pytest` prepends the working directory to `sys.path`, ahead of
    everything `run.py` puts in PYTHONPATH, so a `resp3_wire` in the working
    directory silently wins. That is how a verification run inside the image
    graded the starter stubs while reporting that it was grading the reference:
    every case failed identically and the score looked like an implementation
    that did nothing, because the implementation it ran did nothing.

    `run.py` sets PYTHONSAFEPATH to stop the prepend. This asserts the outcome
    rather than trusting it, because the failure mode is silent and the
    consequence is grading the wrong code.
    """
    expected = os.environ.get("RESP3_CLIENT_PATH")
    if not expected:
        return
    try:
        import resp3_wire
    except Exception as exc:  # noqa: BLE001 - any import failure is fatal here
        pytest.exit(
            f"the client package under {expected} could not be imported: "
            f"{type(exc).__name__}: {exc}",
            returncode=5,
        )
    origin = os.path.realpath(getattr(resp3_wire, "__file__", "") or "")
    root = os.path.realpath(expected)
    if not origin.startswith(root + os.sep):
        pytest.exit(
            f"the graded package resolved to {origin}, which is not under the "
            f"client directory {root}. Something earlier on sys.path shadowed "
            f"it, so this run would have graded a different implementation "
            f"than the one it was asked for.",
            returncode=5,
        )


def pytest_sessionstart(session: pytest.Session) -> None:
    """Checked at startup, not when the first seeded case happens to run.

    A run that inherits the published seed is grading against a schedule the
    implementer has already seen, and the cases that draw no randomness would
    otherwise sail past and report a partial score that looks legitimate.
    """
    raw = os.environ.get("RESP3_SEED")
    if raw is None:
        raise pytest.UsageError(
            "RESP3_SEED is not set. tests/test.sh draws it from os.urandom and "
            "prints it so a failing run can be reproduced."
        )
    try:
        value = int(raw)
    except ValueError:
        raise pytest.UsageError(f"RESP3_SEED is not an integer: {raw!r}") from None
    if value == VISIBLE_SEED:
        raise pytest.UsageError(
            f"RESP3_SEED is the published visible seed {VISIBLE_SEED}. The "
            f"sealed channels must not grade against a schedule the "
            f"implementer has already seen."
        )
    assert_the_graded_package_is_the_one_requested()


@pytest.fixture(scope="session")
def seed() -> int:
    return int(os.environ["RESP3_SEED"])


@pytest.fixture(scope="session")
def rng(seed: int) -> random.Random:
    """The single seeded source for every randomised decision in the run."""
    return random.Random(seed)


@pytest.fixture(scope="session")
def isolation_checked() -> None:
    """redis-py must be unreachable from the interpreter under test.

    The first of three checks D25 requires, and on its own the weakest: it fires
    once, and a client that defers its injection until after it has passed is
    not seen here at all. `pytest_runtest_teardown` above is what closes that,
    and the image's filesystem permissions are what make the injection fail in
    the first place. `tools/check_stdlib_only.py` remains the third layer.
    """
    reached = redis_py_reachable()
    if reached is None:
        return
    pytest.exit(
        f"redis-py is reachable by the interpreter running the harness "
        f"({reached}). The client package must not be able to reach it. Run "
        f"the harness from an interpreter without redis-py and point "
        f"RESP3_ORACLE_PYTHON at the one that has it.",
        returncode=3,
    )


@pytest.fixture(scope="session")
def server(isolation_checked: None) -> RedisServer:
    srv = RedisServer()
    srv.start()
    yield srv
    srv.stop()


@pytest.fixture(scope="session")
def probed(server: RedisServer) -> dict[str, str]:
    """Runs before any oracle case; aborts the run rather than scoring.

    A redis-py behaviour change invalidates every oracle case at once, so it is
    a configuration error, not an implementation failure.
    """
    from support.probe import ProbeMismatch

    try:
        return run_probe(server.port)
    except ProbeMismatch as exc:
        pytest.exit(f"probe mismatch: {exc}", returncode=4)


@pytest.fixture(scope="session")
def oracle(server: RedisServer, probed: dict):
    """Runs a command sequence through redis-py and returns decoded replies."""

    def run(commands: list[tuple], protocol: int = 3) -> list:
        payload = call_backend(
            {
                "op": "run",
                "port": server.port,
                "protocol": protocol,
                "commands": [encode_args(tuple(c)) for c in commands],
            }
        )
        return [decode_value(node) for node in payload["results"]]

    return run


@pytest.fixture(scope="session")
def client_module(isolation_checked: None):
    import resp3_wire

    return resp3_wire


@pytest.fixture
def prefix(rng: random.Random, request: pytest.FixtureRequest) -> str:
    """A key prefix unique to this case and this run.

    Keys are randomised per case per run so a lookup table keyed on command
    arguments cannot be built in advance.
    """
    return f"h{rng.getrandbits(48):012x}:{request.node.name[:24]}"
