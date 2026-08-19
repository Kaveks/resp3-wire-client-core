"""Shared fixtures and seed discipline for the sealed harness.

The interpreter running these tests imports the client package. It must not
have redis-py on its path; expected values come from a separate interpreter
through `support/probe.call_backend`. That separation is asserted at session
start rather than assumed.
"""

from __future__ import annotations

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


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "channel(name): which scoring channel a case belongs to")


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

    This is the primary defence against a client that wraps redis-py rather
    than parsing the protocol. `tools/check_stdlib_only.py` is a secondary,
    static layer; this is the structural one.
    """
    try:
        import redis  # noqa: F401
    except ImportError:
        return
    pytest.exit(
        "redis-py is importable by the interpreter running the harness. The "
        "client package must not be able to reach it. Run the harness from an "
        "interpreter without redis-py and point RESP3_ORACLE_PYTHON at the one "
        "that has it.",
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
