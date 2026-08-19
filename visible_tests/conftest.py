"""Fixtures for the visible tests."""

from __future__ import annotations

import os
import random
import socket

import pytest

# Published so a failure here is reproducible. The checks your work is graded
# against draw from a different seed.
SEED = 20260818


@pytest.fixture(scope="session")
def seed() -> int:
    print(f"\nvisible tests seed: {SEED}")
    return SEED


@pytest.fixture(scope="session")
def rng(seed: int) -> random.Random:
    return random.Random(seed)


@pytest.fixture(scope="session")
def port() -> int:
    value = int(os.environ.get("RESP3_TEST_PORT", "6379"))
    try:
        with socket.create_connection(("127.0.0.1", value), timeout=2.0):
            pass
    except OSError:
        pytest.fail(
            f"no Redis server answering on 127.0.0.1:{value}. Start one with:\n"
            f"    redis-server --port {value} --save '' --appendonly no "
            f"--enable-debug-command yes --daemonize yes\n"
            f"or set RESP3_TEST_PORT to where yours is listening.",
            pytrace=False,
        )
    return value


@pytest.fixture
def conn(port: int):
    from resp3_wire import Connection

    connection = Connection(port=port, protocol=3, timeout=5.0)
    connection.connect()
    yield connection
    connection.close()


@pytest.fixture
def keyspace(conn, rng: random.Random):
    """A key prefix for one test, cleaned up afterwards."""
    prefix = f"visible:{rng.getrandbits(32):08x}"
    made: list[str] = []

    def key(name: str = "k") -> str:
        full = f"{prefix}:{name}"
        made.append(full)
        return full

    yield key
    if made:
        conn.execute("UNLINK", *made)
