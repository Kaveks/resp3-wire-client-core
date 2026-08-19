#!/usr/bin/env python3
"""Run the mutation catalogue and report the mutation-against-channel matrix.

Step 7 of the build order. Each mutation in `tools/mutations.py` is applied to
a fresh copy of the reference implementation, never to the reference itself,
and the full harness is run against that copy. What the matrix is read for:

  a row that scores 100     the mutation broke a property no channel observes
  a row that scores 0       the cases are not independent of one another

Both are defects in the harness. Neither is fixed by weakening the mutation.

The runner needs the same environment the harness does: RESP3_ORACLE_PYTHON
naming an interpreter that has redis-py, and this interpreter not having it.
Runs are serial by default, because channel 4 measures memory and per-byte
cost and a second harness on the same machine perturbs both.

    tools/mutate.py --check                 verify every anchor still applies
    tools/mutate.py --list
    tools/mutate.py                         run everything
    tools/mutate.py --only NAME [NAME ...]
    tools/mutate.py --skip-slow
    tools/mutate.py --rerender run.json [run2.json ...]

`--rerender` re-scores saved runs against the current catalogue without running
anything. It exists because a row's meaning depends on what the catalogue
claims a channel enforces, and correcting a claim should not require another
half hour of harness time. The counts are whatever the saved run measured.
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
sys.path.insert(0, str(ROOT / "tools"))

from mutations import MUTATIONS, Mutation, by_name  # noqa: E402

# Taken from the orchestrator rather than restated here. The allocation is the
# weighting, so a copy of it in this file is a copy that can silently disagree
# with the harness it is measuring.
sys.path.insert(0, str(ROOT / "harness"))
import importlib.util as _importlib_util  # noqa: E402

_spec = _importlib_util.spec_from_file_location(
    "_harness_run", ROOT / "harness" / "run.py"
)
_harness_run = _importlib_util.module_from_spec(_spec)
_spec.loader.exec_module(_harness_run)

ALLOCATION = dict(_harness_run.CHANNELS)
CHANNELS = tuple(ALLOCATION)
TOTAL = sum(ALLOCATION.values())

# docs/HARNESS.md section 7.1. A run that inherits the published visible seed
# is grading against a schedule the implementer has already seen.
VISIBLE_SEED = 20260818

DEFAULT_TIMEOUT = 420.0
SLOW_TIMEOUT = 2400.0


class AnchorError(RuntimeError):
    """A mutation's anchor text no longer matches the reference exactly once."""


def apply_mutation(package: Path, mutation: Mutation) -> None:
    """Rewrite a copy of the package in place. Every anchor must be unique."""
    for relative, old, new in mutation.edits:
        path = package / relative
        source = path.read_text()
        occurrences = source.count(old)
        if occurrences != 1:
            raise AnchorError(
                f"{mutation.name}: anchor in {relative} matched {occurrences} "
                f"times, expected exactly 1:\n{old}"
            )
        path.write_text(source.replace(old, new))


def check_anchors(reference_package: Path) -> list[str]:
    """Verify every anchor without running anything. Returns the failures."""
    problems: list[str] = []
    for mutation in MUTATIONS:
        for relative, old, _new in mutation.edits:
            path = reference_package / relative
            if not path.exists():
                problems.append(f"{mutation.name}: {relative} does not exist")
                continue
            occurrences = path.read_text().count(old)
            if occurrences != 1:
                problems.append(
                    f"{mutation.name}: anchor in {relative} matched "
                    f"{occurrences} times, expected 1"
                )
    return problems


def run_harness(client_dir: Path, report: Path, env: dict, timeout: float) -> dict:
    """Run the harness against one client directory and return its report."""
    started = time.time()
    try:
        completed = subprocess.run(
            [sys.executable, str(ROOT / "harness" / "run.py"),
             "--client", str(client_dir), "--report", str(report)],
            env=env, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            "score": None, "passed": 0, "total": TOTAL,
            "channels": {c: {"passed": 0, "collected": 0} for c in CHANNELS},
            "timed_out": True, "elapsed_seconds": round(time.time() - started, 1),
        }
    payload: dict = {}
    if report.exists():
        try:
            payload = json.loads(report.read_text())
        except json.JSONDecodeError:
            payload = {}
    if not payload:
        payload = {
            "score": None, "passed": 0, "total": TOTAL,
            "channels": {c: {"passed": 0, "collected": 0} for c in CHANNELS},
            "no_report": True,
            "stderr": completed.stderr[-2000:],
        }
    payload["exit_code"] = completed.returncode
    payload["wall_seconds"] = round(time.time() - started, 1)
    return payload


def channel_counts(payload: dict) -> dict[str, int]:
    channels = payload.get("channels") or {}
    return {c: int((channels.get(c) or {}).get("passed", 0)) for c in CHANNELS}


def verdict(counts: dict[str, int], mutation: Mutation,
            row: dict | None = None) -> tuple[str, list[str]]:
    """A one word verdict plus any findings the row raises."""
    passed = sum(counts.values())
    broke = [c for c in CHANNELS if counts[c] < ALLOCATION[c]]
    findings: list[str] = []
    row = row or {}
    if row.get("sansio_violations"):
        # run.py scores a sans-io violation as zero across all channels without
        # running pytest at all. That is the specified behaviour, so it is not
        # evidence that the cases are coupled.
        return "sans-io gate", []
    if row.get("timed_out"):
        return "timed out", ["the run did not finish; its counts mean nothing"]
    if passed == TOTAL:
        return "no effect", [
            f"no channel observes: {mutation.prop}"
        ]
    if passed == 0:
        return "total loss", [
            "every case failed; the cases may not be independent"
        ]
    missed = [c for c in mutation.aims if counts[c] == ALLOCATION[c]]
    if missed:
        findings.append(
            f"aimed at {', '.join(mutation.aims)} but {', '.join(missed)} "
            f"scored full"
        )
    return "+".join(broke), findings


def render_matrix(rows: list[dict]) -> str:
    """The matrix, as a markdown table."""
    head = (
        "| mutation | "
        + " | ".join(f"{name} {n}" for name, n in ALLOCATION.items())
        + " | total | channels broken |\n"
        + "| --- | " + " | ".join("---:" for _ in ALLOCATION)
        + " | ---: | --- |\n"
    )
    body = []
    for row in rows:
        counts = row["counts"]
        cells = " | ".join(
            f"{counts[c]}" if counts[c] == ALLOCATION[c] else f"**{counts[c]}**"
            for c in CHANNELS
        )
        body.append(
            f"| `{row['name']}` | {cells} | {sum(counts.values())} | "
            f"{row['verdict']} |"
        )
    return head + "\n".join(body) + "\n"


def summarise(rows: list[dict]) -> None:
    """Print the two readings the matrix exists to produce."""
    no_effect = [r for r in rows if r["verdict"] == "no effect"]
    total_loss = [r for r in rows if r["verdict"] == "total loss"]
    gated = [r for r in rows if r["verdict"] == "sans-io gate"]
    if no_effect:
        print(f"\n{len(no_effect)} mutations failed nothing:")
        for row in no_effect:
            print(f"  {row['name']}: {row['prop']}")
    if total_loss:
        print(f"\n{len(total_loss)} mutations failed everything:")
        for row in total_loss:
            print(f"  {row['name']}: {row['prop']}")
    if gated:
        print(f"\n{len(gated)} mutations scored zero through the sans-io gate, "
              f"which is what docs/HARNESS.md section 7.2 specifies:")
        for row in gated:
            print(f"  {row['name']}: {row['prop']}")


def rerender(args) -> int:
    """Re-score saved runs against the current catalogue. Runs nothing."""
    merged: dict[str, dict] = {}
    seeds: list = []
    for path in args.rerender:
        payload = json.loads(Path(path).read_text())
        seeds.append(payload.get("seed"))
        for row in payload.get("rows", []):
            merged[row["name"]] = row
    rows: list[dict] = []
    for name, row in merged.items():
        counts = {c: int(row["counts"].get(c, 0)) for c in CHANNELS}
        if name.startswith("("):
            rows.append({**row, "counts": counts, "verdict": "-", "findings": []})
            continue
        try:
            mutation = by_name(name)
        except KeyError:
            rows.append({**row, "counts": counts,
                         "verdict": "not in catalogue", "findings": []})
            continue
        word, findings = verdict(counts, mutation, row)
        rows.append({**row, "counts": counts, "prop": mutation.prop,
                     "aims": list(mutation.aims), "note": mutation.note,
                     "verdict": word, "findings": findings})
    order = {m.name: i for i, m in enumerate(MUTATIONS)}
    rows.sort(key=lambda r: (not r["name"].startswith("("),
                             order.get(r["name"], len(order))))
    for row in rows:
        counts = row["counts"]
        flag = "  <-- " + "; ".join(row["findings"]) if row["findings"] else ""
        print(f"{row['name']:<46} o{counts['oracle']:>3} c{counts['chunking']:>3} "
              f"p{counts['pool']:>3} r{counts['resource']:>3}  "
              f"= {sum(counts.values()):>3}/{TOTAL}{flag}")
    summarise(rows)
    payload = {"seed": seeds, "rerendered": True,
               "allocation": ALLOCATION, "rows": rows}
    if args.json:
        Path(args.json).write_text(json.dumps(payload, indent=2) + "\n")
    if args.matrix:
        Path(args.matrix).write_text(render_matrix(rows))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true",
                        help="verify every anchor applies, then exit")
    parser.add_argument("--list", action="store_true", help="list the catalogue")
    parser.add_argument("--only", nargs="+", default=None, help="run these mutations")
    parser.add_argument("--skip-slow", action="store_true",
                        help="skip mutations whose defect is quadratic by construction")
    parser.add_argument("--reference", default=str(ROOT / "reference"),
                        help="directory holding the resp3_wire package to mutate")
    parser.add_argument("--workdir", default=None,
                        help="where the mutated copies and reports are written")
    parser.add_argument("--seed", type=int, default=None,
                        help="RESP3_SEED for every run; one seed keeps rows comparable")
    parser.add_argument("--json", default=None, help="where to write the matrix as JSON")
    parser.add_argument("--matrix", default=None, help="where to write the matrix as markdown")
    parser.add_argument("--no-baseline", action="store_true",
                        help="skip the unmutated control run")
    parser.add_argument("--rerender", nargs="+", default=None,
                        help="re-score saved runs against the current catalogue")
    args = parser.parse_args()

    if args.rerender:
        return rerender(args)

    reference = Path(args.reference).resolve()
    reference_package = reference / "resp3_wire"
    if not reference_package.is_dir():
        print(f"no resp3_wire package under {reference}", file=sys.stderr)
        return 2

    if args.list:
        for mutation in MUTATIONS:
            marker = " (slow)" if mutation.slow else ""
            print(f"{mutation.name}{marker}\n    {mutation.prop}\n"
                  f"    aims: {', '.join(mutation.aims)}")
        return 0

    problems = check_anchors(reference_package)
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        print(f"\n{len(problems)} anchors do not apply. The catalogue has "
              f"drifted from the reference.", file=sys.stderr)
        return 1
    if args.check:
        print(f"all {sum(len(m.edits) for m in MUTATIONS)} anchors across "
              f"{len(MUTATIONS)} mutations apply exactly once")
        return 0

    selected = MUTATIONS
    if args.only:
        wanted = set(args.only)
        selected = [m for m in MUTATIONS if m.name in wanted]
        unknown = wanted - {m.name for m in selected}
        if unknown:
            print(f"unknown mutations: {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2
    if args.skip_slow:
        selected = [m for m in selected if not m.slow]

    workdir = Path(args.workdir).resolve() if args.workdir else ROOT / "build" / "mutations"
    workdir.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    for required in ("RESP3_ORACLE_PYTHON",):
        if not env.get(required):
            print(f"{required} is not set; see docs/HARNESS.md section 2.7",
                  file=sys.stderr)
            return 2
    seed = args.seed
    if seed is None:
        seed = int.from_bytes(os.urandom(4), "big")
    if seed == VISIBLE_SEED:
        print(f"seed {seed} is the published visible seed", file=sys.stderr)
        return 2
    env["RESP3_SEED"] = str(seed)

    print(f"seed {seed}, {len(selected)} mutations, workdir {workdir}\n", flush=True)

    rows: list[dict] = []
    started = time.time()

    if not args.no_baseline:
        control = workdir / "_control"
        shutil.rmtree(control, ignore_errors=True)
        (control).mkdir(parents=True)
        shutil.copytree(reference_package, control / "resp3_wire")
        payload = run_harness(control, workdir / "_control.json", env, DEFAULT_TIMEOUT)
        counts = channel_counts(payload)
        print(f"control (unmutated copy): {sum(counts.values())}/{TOTAL} "
              f"in {payload.get('wall_seconds')}s", flush=True)
        if sum(counts.values()) != TOTAL:
            print("the control run is not clean; every row below would be "
                  "measured against a moving baseline", file=sys.stderr)
            return 1
        rows.append({
            "name": "(control, unmutated)", "prop": "", "aims": [],
            "counts": counts, "verdict": "-", "findings": [],
            "wall_seconds": payload.get("wall_seconds"),
        })

    for index, mutation in enumerate(selected, 1):
        target = workdir / mutation.name
        shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True)
        shutil.copytree(reference_package, target / "resp3_wire")
        apply_mutation(target / "resp3_wire", mutation)
        timeout = SLOW_TIMEOUT if mutation.slow else DEFAULT_TIMEOUT
        payload = run_harness(target, workdir / f"{mutation.name}.json", env, timeout)
        counts = channel_counts(payload)
        word, findings = verdict(counts, mutation, payload)
        rows.append({
            "name": mutation.name, "prop": mutation.prop,
            "aims": list(mutation.aims), "counts": counts, "verdict": word,
            "findings": findings, "note": mutation.note,
            "wall_seconds": payload.get("wall_seconds"),
            "timed_out": bool(payload.get("timed_out")),
            "aborted": bool(payload.get("aborted")),
            "sansio_violations": payload.get("sansio_violations"),
        })
        flag = "  <-- " + "; ".join(findings) if findings else ""
        print(
            f"[{index:2d}/{len(selected)}] {mutation.name:<44} "
            f"o{counts['oracle']:>3} c{counts['chunking']:>3} "
            f"p{counts['pool']:>3} r{counts['resource']:>3}  "
            f"= {sum(counts.values()):>3}/{TOTAL}  ({payload.get('wall_seconds')}s)"
            f"{flag}",
            flush=True,
        )

    elapsed = round(time.time() - started, 1)
    print(f"\n{len(selected)} mutations in {elapsed}s")

    summarise(rows)

    payload = {
        "seed": seed, "elapsed_seconds": elapsed,
        "allocation": ALLOCATION, "rows": rows,
    }
    if args.json:
        Path(args.json).write_text(json.dumps(payload, indent=2) + "\n")
    if args.matrix:
        Path(args.matrix).write_text(render_matrix(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
