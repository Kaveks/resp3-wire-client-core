#!/usr/bin/env python3
"""Materialise each attack over a copy of the reference and score it.

Step 8 of the build order. Every attack in `catalogue.py` is built by copying
`reference/resp3_wire` and replacing the modules the attack overrides, then run
through the full sealed harness exactly as a submitted implementation would be.

An attack is defended when it scores below what an honest implementation
scores *and* the thing that stopped it is the structural control rather than a
static check. Each attack writes a diagnostic record naming which of its routes
it got to and what stopped it, which is the part worth reading: a score alone
does not distinguish a defence from an accident.

    attacks/run_attacks.py                run every attack
    attacks/run_attacks.py --only NAME
    attacks/run_attacks.py --list

Needs the same environment as the harness: RESP3_ORACLE_PYTHON naming an
interpreter that has redis-py, and this interpreter not having it.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "attacks"))
sys.path.insert(0, str(ROOT / "harness"))

from catalogue import ATTACKS  # noqa: E402

import importlib.util as _importlib_util  # noqa: E402

_spec = _importlib_util.spec_from_file_location(
    "_harness_run", ROOT / "harness" / "run.py"
)
_harness_run = _importlib_util.module_from_spec(_spec)
_spec.loader.exec_module(_harness_run)

ALLOCATION = dict(_harness_run.CHANNELS)
CHANNELS = tuple(ALLOCATION)
TOTAL = sum(ALLOCATION.values())

VISIBLE_SEED = 20260818
TIMEOUT = 600.0


def materialise(attack, workdir: Path) -> Path:
    """Build the attack's client directory from the reference plus overrides."""
    target = workdir / attack.name
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True)
    shutil.copytree(ROOT / "reference" / "resp3_wire", target / "resp3_wire")
    overrides = ROOT / "attacks" / attack.name / "overrides"
    for module in attack.overrides:
        source = overrides / module
        if not source.exists():
            raise FileNotFoundError(f"{attack.name}: {source} is missing")
        shutil.copyfile(source, target / "resp3_wire" / module)
    return target


def score(attack, client_dir: Path, workdir: Path, env: dict) -> dict:
    """Run the harness against one attack and collect its score and diagnostics."""
    report = workdir / f"{attack.name}.json"
    log = workdir / f"{attack.name}.attacklog"
    if log.exists():
        log.unlink()
    run_env = dict(env)
    # The attack writes here to say which of its routes it reached. Reading it
    # is how the report distinguishes "stopped by interpreter separation" from
    # "stopped by something else that happened to fire first".
    run_env["RESP3_ATTACK_LOG"] = str(log)
    started = time.time()
    try:
        completed = subprocess.run(
            [sys.executable, str(ROOT / "harness" / "run.py"),
             "--client", str(client_dir), "--report", str(report)],
            env=run_env, capture_output=True, text=True, timeout=TIMEOUT,
        )
        timed_out = False
        stderr = completed.stderr[-3000:]
        returncode = completed.returncode
    except subprocess.TimeoutExpired:
        timed_out, stderr, returncode = True, "", None

    payload: dict = {}
    if report.exists():
        try:
            payload = json.loads(report.read_text())
        except json.JSONDecodeError:
            payload = {}
    counts = {
        c: int(((payload.get("channels") or {}).get(c) or {}).get("passed", 0))
        for c in CHANNELS
    }
    notes: list[str] = []
    if log.exists():
        for line in log.read_text().splitlines():
            if line.strip() and line.strip() not in notes:
                notes.append(line.strip())
    return {
        "name": attack.name,
        "requirement": attack.requirement,
        "counts": counts,
        "passed": sum(counts.values()),
        "total": TOTAL,
        "collected": payload.get("collected"),
        "sansio_violations": payload.get("sansio_violations"),
        "aborted": bool(payload.get("aborted")),
        "timed_out": timed_out,
        "exit_code": returncode,
        "attack_notes": notes,
        "stderr_tail": stderr[-600:] if stderr else "",
        "wall_seconds": round(time.time() - started, 1),
    }


def stdlib_check(client_dir: Path) -> list[str]:
    """The secondary static layer, reported separately from the score.

    Run after the harness, never instead of it. If an attack scores well and
    only this catches it, the structural control did not hold and the finding is
    a weakness, not a defence.
    """
    completed = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "check_stdlib_only.py"),
         str(client_dir / "resp3_wire")],
        capture_output=True, text=True,
    )
    return [
        line for line in completed.stderr.splitlines()
        if line and not line.startswith("check_stdlib_only:")
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--only", nargs="+", default=None)
    parser.add_argument("--workdir", default=None)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--json", default=None)
    args = parser.parse_args()

    if args.list:
        for attack in ATTACKS:
            print(f"{attack.name}\n    targets : {attack.requirement}\n"
                  f"    payoff  : {attack.payoff}")
        return 0

    selected = ATTACKS
    if args.only:
        wanted = set(args.only)
        selected = [a for a in ATTACKS if a.name in wanted]
        unknown = wanted - {a.name for a in selected}
        if unknown:
            print(f"unknown attacks: {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2

    workdir = Path(args.workdir).resolve() if args.workdir else ROOT / "build" / "attacks"
    workdir.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    if not env.get("RESP3_ORACLE_PYTHON"):
        print("RESP3_ORACLE_PYTHON is not set", file=sys.stderr)
        return 2
    seed = args.seed
    if seed is None:
        seed = int.from_bytes(os.urandom(4), "big")
    if seed == VISIBLE_SEED:
        print(f"seed {seed} is the published visible seed", file=sys.stderr)
        return 2
    env["RESP3_SEED"] = str(seed)

    print(f"seed {seed}, {len(selected)} attacks, workdir {workdir}\n", flush=True)

    rows = []
    for index, attack in enumerate(selected, 1):
        client_dir = materialise(attack, workdir)
        row = score(attack, client_dir, workdir, env)
        row["stdlib_findings"] = stdlib_check(client_dir)
        rows.append(row)
        counts = row["counts"]
        print(
            f"[{index}/{len(selected)}] {attack.name:<32} "
            + "  ".join(f"{c[:4]}{counts[c]:>3}" for c in CHANNELS)
            + f"  = {row['passed']:>3}/{TOTAL}  ({row['wall_seconds']}s)",
            flush=True,
        )
        for note in row["attack_notes"]:
            print(f"        route: {note}")
        if row["stdlib_findings"]:
            print(f"        static check would also have flagged this "
                  f"({len(row['stdlib_findings'])} finding(s))")
        print(flush=True)

    if args.json:
        Path(args.json).write_text(
            json.dumps({"seed": seed, "allocation": ALLOCATION, "rows": rows},
                       indent=2) + "\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
