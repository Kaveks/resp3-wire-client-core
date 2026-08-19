"""Measures redis-py's actual behaviour before any oracle case runs.

This document's assumptions about redis-py are measured rather than recalled.
Two of them were wrong when first written, which is why this exists: redis-py
8.1.0 raises `InvalidResponse` on RESP3 attribute frames rather than discarding
them, and returns `list` for every RESP3 set by deliberate design.

A probe mismatch aborts the run with a configuration error instead of scoring.
A redis-py behaviour change invalidates every oracle case at once and is not
the implementation's fault, so scoring it against the implementation would be
a lie about what failed.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any

__all__ = ["ProbeMismatch", "run_probe", "call_backend", "EXPECTED"]

_BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "oracle_backend.py")

# Measured against redis-py 8.1.0 and Redis 7.4.10. See D11.
EXPECTED: dict[str, str] = {
    "double": "float",
    "bignum": "int",
    "true": "bool",
    "null": "NoneType",
    "map": "dict",
    # Not `set`. redis-py returns sets as lists always, for predictability,
    # because a set may contain unhashable members.
    "set": "list",
    "verbatim": "bytes",
    # Not the decorated value. redis-py has no branch for the `|` type byte and
    # falls through to raising.
    "attrib": "raises:InvalidResponse",
    # A push frame is not delivered as a command reply; redis-py handles it and
    # reads the next reply, so what arrives is the command's own bulk string.
    "push": "bytes",
}


class ProbeMismatch(RuntimeError):
    """redis-py does not behave as the harness contract records."""


class BackendError(RuntimeError):
    """The oracle backend could not run."""


def call_backend(job: dict[str, Any], timeout: float = 60.0) -> dict[str, Any]:
    """Run one job in the oracle interpreter and return its decoded reply.

    The oracle interpreter is named by ``RESP3_ORACLE_PYTHON``. It is a
    separate process on purpose: the interpreter running the harness imports
    the client package and must never have redis-py on its path.
    """
    python = os.environ.get("RESP3_ORACLE_PYTHON")
    if not python:
        raise BackendError(
            "RESP3_ORACLE_PYTHON is not set; it must name an interpreter with "
            "redis-py installed, separate from the one running the harness"
        )
    proc = subprocess.run(
        [python, _BACKEND],
        input=json.dumps(job),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0 and not proc.stdout:
        raise BackendError(
            f"oracle backend exited {proc.returncode}: {proc.stderr[:500]}"
        )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise BackendError(
            f"oracle backend produced no JSON: {proc.stdout[:200]!r} "
            f"stderr={proc.stderr[:300]!r}"
        ) from exc
    if not payload.get("ok"):
        raise BackendError(f"oracle backend failed: {payload.get('error')}")
    return payload


def run_probe(port: int) -> dict[str, str]:
    """Verify redis-py behaves as recorded. Raises :class:`ProbeMismatch`.

    Nine assertions. They do not count toward any channel's case allocation.
    """
    payload = call_backend({"op": "probe", "port": port})
    observed = payload["observed"]
    mismatches = []
    for name, want in EXPECTED.items():
        got = observed.get(name, "<missing>")
        if got != want:
            mismatches.append(f"{name}: observed {got}, recorded {want}")
    if mismatches:
        raise ProbeMismatch(
            "redis-py "
            + str(payload.get("version"))
            + " does not match docs/HARNESS.md section 2.8:\n  "
            + "\n  ".join(mismatches)
            + "\nThis invalidates every oracle case and is not the "
              "implementation's fault."
        )
    return observed
