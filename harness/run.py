"""Harness orchestrator. Runs the four channels and emits the score.

Weights are realized through case counts rather than a weight table, so the
50/20/20/10 split holds under a harness that scores as the fraction of tests
passed and remains sensible under one that scores pass or fail.

Two outcomes are emitted, because the platform's mechanism for reading a
continuous score is not yet confirmed: a JSON report, and a process exit code
that is zero only when every case passed.

Two gates sit outside the scoring:

  - the sans-io structural check, whose failure is the implementation's fault
    and scores zero across all channels;
  - the redis-py probe, whose failure is a configuration error and aborts
    without scoring, because it invalidates every oracle case at once and is
    not the implementation's fault.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

HARNESS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HARNESS_DIR))

from support.sansio import check_sansio  # noqa: E402

# docs/HARNESS.md section 1. The counts are the weights.
CHANNELS = {
    "oracle": 50,
    "chunking": 20,
    "pool": 20,
    "resource": 10,
}
TOTAL_CASES = sum(CHANNELS.values())

# Aborting configuration errors, distinguished from a low score.
EXIT_PROBE_MISMATCH = 4
EXIT_ISOLATION = 3


def parse_junit(path: Path) -> dict[str, dict[str, int]]:
    """Per-channel pass counts, keyed on the module each case came from."""
    tally = {name: {"passed": 0, "failed": 0, "total": 0} for name in CHANNELS}
    if not path.exists():
        return tally
    root = ET.parse(path).getroot()
    for case in root.iter("testcase"):
        module = (case.get("classname") or "").split(".")
        channel = next((c for c in CHANNELS if c in module or c in (case.get("file") or "")), None)
        if channel is None:
            continue
        tally[channel]["total"] += 1
        failed = any(
            child.tag in ("failure", "error") for child in case
        )
        skipped = any(child.tag == "skipped" for child in case)
        if failed or skipped:
            tally[channel]["failed"] += 1
        else:
            tally[channel]["passed"] += 1
    return tally


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--client",
        default=os.environ.get("RESP3_CLIENT_PATH", "/app"),
        help="directory containing the resp3_wire package under test",
    )
    parser.add_argument(
        "--report", default=os.environ.get("RESP3_REPORT", "score.json"),
        help="where to write the JSON score report",
    )
    parser.add_argument("--junit", default=None, help="where to write junit xml")
    parser.add_argument("pytest_args", nargs="*", help="extra arguments for pytest")
    args = parser.parse_args()

    started = time.time()
    client_dir = Path(args.client).resolve()
    package_dir = client_dir / "resp3_wire"
    report_path = Path(args.report)
    junit_path = Path(args.junit) if args.junit else report_path.with_suffix(".xml")

    def emit(payload: dict) -> None:
        payload["elapsed_seconds"] = round(time.time() - started, 3)
        report_path.write_text(json.dumps(payload, indent=2) + "\n")
        print(json.dumps(payload, indent=2))

    if not package_dir.is_dir():
        emit({
            "score": 0.0, "passed": 0, "total": TOTAL_CASES,
            "aborted": True,
            "reason": f"no resp3_wire package at {package_dir}",
        })
        return 2

    # Gate one: the sans-io property. A violation is the implementation's
    # fault, so it scores zero rather than aborting.
    violations = check_sansio(package_dir)
    if violations:
        emit({
            "score": 0.0, "passed": 0, "total": TOTAL_CASES,
            "channels": {name: {"passed": 0, "total": n} for name, n in CHANNELS.items()},
            "sansio_violations": violations,
            "reason": "the parser is not sans-io; see sansio_violations",
        })
        return 1

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(client_dir), str(HARNESS_DIR)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    if "RESP3_SEED" not in env:
        env["RESP3_SEED"] = str(int.from_bytes(os.urandom(4), "big"))
    print(f"RESP3_SEED={env['RESP3_SEED']}", flush=True)

    completed = subprocess.run(
        [sys.executable, "-m", "pytest", str(HARNESS_DIR / "channels"),
         "-q", "--junitxml", str(junit_path), *args.pytest_args],
        env=env,
    )

    # An aborting gate exits with its own code and must not read as a score.
    if completed.returncode in (EXIT_ISOLATION, EXIT_PROBE_MISMATCH):
        emit({
            "score": None, "passed": 0, "total": TOTAL_CASES,
            "aborted": True,
            "reason": (
                "redis-py behaviour does not match the harness contract"
                if completed.returncode == EXIT_PROBE_MISMATCH
                else "redis-py is reachable from the interpreter under test"
            ),
            "seed": env["RESP3_SEED"],
        })
        return completed.returncode

    tally = parse_junit(junit_path)
    passed = sum(c["passed"] for c in tally.values())
    collected = sum(c["total"] for c in tally.values())

    payload = {
        "score": round(passed / TOTAL_CASES, 4),
        "passed": passed,
        "total": TOTAL_CASES,
        "collected": collected,
        "seed": env["RESP3_SEED"],
        "channels": {
            name: {
                "passed": tally[name]["passed"],
                "allocated": allocation,
                "collected": tally[name]["total"],
            }
            for name, allocation in CHANNELS.items()
        },
    }
    if collected != TOTAL_CASES:
        # Collection is part of the contract: a case that cannot even be
        # collected is a case that did not pass, and the denominator stays 100.
        payload["note"] = (
            f"collected {collected} cases, expected {TOTAL_CASES}; "
            f"uncollected cases score as failures"
        )
    emit(payload)
    return 0 if passed == TOTAL_CASES else 1


if __name__ == "__main__":
    raise SystemExit(main())
