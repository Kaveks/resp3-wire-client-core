#!/usr/bin/env python3
"""Validate an assembled bundle against the platform's packaging constraints.

    tools/check_paths.py [bundle-dir]
    tools/check_paths.py --self-test [bundle-dir]

Violating any of these is a deterministic rejection, so the checks are worth
more than the assembly script's own confidence in itself. This deliberately
re-derives the resource figures from `docs/SUBMISSION.md` rather than trusting
what `tools/build_bundle.sh` wrote, because a checker that reads the generator's
output through the generator's own logic cannot catch the generator being wrong.

`--self-test` breaks each rule in a throwaway copy and requires this checker to
reject it. Per D26 a check that has not been seen to fail is not a check, and
per D28 the control tests the claim rather than the apparatus: it is not enough
that the rule is written down, the rejection has to happen.
"""

from __future__ import annotations

import argparse
import filecmp
import os
import re
import shutil
import sys
import tempfile
import tomllib
from pathlib import Path

REQUIRED = (
    "task.toml",
    "instruction.md",
    "environment/Dockerfile",
    "tests/test.sh",
    "solution/solve.sh",
)
EXECUTABLE = ("tests/test.sh", "solution/solve.sh")
REQUIRED_TABLES = ("metadata", "verifier", "agent", "environment")

# docs/SUBMISSION.md key -> (table, task.toml key, units per SUBMISSION unit).
#
# This is a mapping, not a rename, and asserting the mapping is the point:
# SUBMISSION.md states `cpuMillis` in millicores and the platform reads `cpus`
# as a count, so comparing the two numbers directly would pass 4000 CPUs as
# though it were within a declared 4000 millicores. `scale` is what a task.toml
# value must be multiplied by to be comparable with the declared figure.
RESOURCE_MAPPING = {
    "cpuMillis": ("environment", "cpus", 1000),
    "memoryMb": ("environment", "memory_mb", 1),
    "storageMb": ("environment", "storage_mb", 1),
    "gpuCount": ("environment", "gpus", 1),
    "agentTimeoutSec": ("agent", "timeout_sec", 1),
    "verifierTimeoutSec": ("verifier", "timeout_sec", 1),
}

# D30: the image's inputs are carried twice so the Dockerfile's COPY paths
# resolve under either build context. Both copies are written from one source;
# a difference between them means the bundle would build two different images
# depending on how it was invoked.
DUPLICATED_INPUTS = (
    ("starter/resp3_wire", "environment/starter/resp3_wire"),
    ("visible_tests", "environment/visible_tests"),
)

# CLAUDE.md, packaging constraints: the rollout and grading phases have no
# network. The build phase may fetch.
OFFLINE_PHASES = ("agent", "verifier")

# CLAUDE.md sandbox ceilings, in the units task.toml carries.
CEILINGS = {"cpus": 8, "memory_mb": 65536, "storage_mb": 40960}


def submission_figures(submission: Path) -> dict[str, int]:
    """Re-derive the declared figures from the maintainers' own document."""
    text = submission.read_text(encoding="utf-8")
    out: dict[str, int] = {}
    for key in RESOURCE_MAPPING:
        match = re.search(rf"^\s{{4}}{re.escape(key)}\s+(\S+)", text, re.M)
        if not match:
            raise SystemExit(f"docs/SUBMISSION.md has no {key!r}")
        out[key] = int(match.group(1))
    return out


def _dircmp_differences(diff: filecmp.dircmp, where: str) -> list[str]:
    """Every way two directory trees disagree, flattened."""
    out = []
    for name in diff.left_only:
        out.append(f"{where}/{name} only on one side")
    for name in diff.right_only:
        out.append(f"{name} missing from {where}")
    for name in diff.diff_files:
        out.append(f"{where}/{name} differs")
    for name, sub in diff.subdirs.items():
        out.extend(_dircmp_differences(sub, f"{where}/{name}"))
    return out


def check_bundle(root: Path, submission: Path) -> list[str]:
    """Return every problem found. An empty list means the bundle is shippable."""
    problems: list[str] = []

    if not root.is_dir():
        return [f"{root} is not a directory"]

    # --- the five required paths -------------------------------------------
    for required in REQUIRED:
        path = root / required
        if not path.is_file():
            problems.append(f"required path is missing: {required}")
    for required in EXECUTABLE:
        path = root / required
        if path.is_file() and not os.access(path, os.X_OK):
            problems.append(f"entrypoint is not executable: {required}")

    # --- path safety and uniqueness ----------------------------------------
    seen: dict[str, str] = {}
    root_resolved = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        for name in list(dirnames) + filenames:
            absolute = Path(dirpath) / name
            relative = absolute.relative_to(root).as_posix()
            if absolute.is_symlink():
                problems.append(f"symlink in bundle: {relative}")
                if name in dirnames:
                    dirnames.remove(name)
                continue
            if relative.startswith("/") or ".." in relative.split("/"):
                problems.append(f"unsafe path: {relative}")
            try:
                if not absolute.resolve().is_relative_to(root_resolved):
                    problems.append(f"path escapes the bundle: {relative}")
            except OSError as exc:
                problems.append(f"path cannot be resolved: {relative} ({exc})")
            # Case-insensitive collisions are duplicates on filesystems the
            # bundle may be unpacked on, even where they are distinct here.
            folded = relative.lower()
            if folded in seen and seen[folded] != relative:
                problems.append(
                    f"duplicate path under case folding: {relative} and {seen[folded]}"
                )
            seen[folded] = relative

    # --- task.toml ----------------------------------------------------------
    toml_path = root / "task.toml"
    if not toml_path.is_file():
        return problems
    try:
        config = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        problems.append(f"task.toml is not valid TOML: {exc}")
        return problems

    for table in REQUIRED_TABLES:
        if not isinstance(config.get(table), dict):
            problems.append(f"task.toml has no [{table}] table")

    name = (config.get("metadata") or {}).get("name")
    if not isinstance(name, str) or not name.strip():
        problems.append("task.toml [metadata] has no non-empty name")

    for phase in OFFLINE_PHASES:
        mode = (config.get(phase) or {}).get("network_mode")
        if mode != "none":
            problems.append(
                f"task.toml [{phase}] network_mode is {mode!r}, must be 'none'"
            )
    if "network_mode" not in (config.get("environment") or {}):
        problems.append("task.toml [environment] declares no network_mode")

    # --- resource figures ---------------------------------------------------
    declared = submission_figures(submission)
    for key, (table, toml_key, scale) in RESOURCE_MAPPING.items():
        value = (config.get(table) or {}).get(toml_key)
        if value is None:
            problems.append(f"task.toml [{table}] has no {toml_key}")
            continue
        if not isinstance(value, int):
            problems.append(f"task.toml [{table}].{toml_key} is not an integer")
            continue
        comparable = value * scale
        if comparable > declared[key]:
            unit = f" ({value} x {scale})" if scale != 1 else ""
            problems.append(
                f"task.toml [{table}].{toml_key} is {value}{unit}, above the "
                f"{declared[key]} {key} declared in docs/SUBMISSION.md; it may "
                f"ask for less, never more"
            )
        ceiling = CEILINGS.get(toml_key)
        if ceiling is not None and value > ceiling:
            problems.append(
                f"task.toml [{table}].{toml_key} is {value}, above the sandbox "
                f"ceiling of {ceiling}"
            )

    # --- D30: the duplicated build inputs must be identical -----------------
    for left, right in DUPLICATED_INPUTS:
        a, b = root / left, root / right
        if not a.is_dir() or not b.is_dir():
            problems.append(
                f"D30 duplication is incomplete: {left} and {right} must both "
                f"exist so the Dockerfile resolves under either build context"
            )
            continue
        diff = filecmp.dircmp(a, b)
        drift = _dircmp_differences(diff, left)
        if drift:
            problems.append(
                f"the duplicated build inputs have drifted: {left} and {right} "
                f"differ ({'; '.join(drift[:3])})"
            )
    return problems


# ---------------------------------------------------------------------------
# The self-test. D26: each rule is broken and the rejection observed.
# ---------------------------------------------------------------------------


def _break_missing_path(root: Path) -> str:
    (root / "tests" / "test.sh").unlink()
    return "required path is missing"


def _break_symlink(root: Path) -> str:
    (root / "solution" / "escape").symlink_to("/etc/passwd")
    return "symlink in bundle"


def _break_toml_syntax(root: Path) -> str:
    (root / "task.toml").write_text("[metadata\nname = broken", encoding="utf-8")
    return "task.toml is not valid TOML"


def _break_missing_table(root: Path) -> str:
    text = (root / "task.toml").read_text(encoding="utf-8")
    (root / "task.toml").write_text(text.replace("[verifier]", "[verifier_typo]"),
                                    encoding="utf-8")
    return "no [verifier] table"


def _break_empty_name(root: Path) -> str:
    text = (root / "task.toml").read_text(encoding="utf-8")
    (root / "task.toml").write_text(
        re.sub(r'^name = ".*"$', 'name = ""', text, count=1, flags=re.M),
        encoding="utf-8",
    )
    return "no non-empty name"


def _break_network_mode(root: Path) -> str:
    text = (root / "task.toml").read_text(encoding="utf-8")
    text = text.replace('[agent]\nnetwork_mode = "none"', '[agent]\nnetwork_mode = "bridge"')
    (root / "task.toml").write_text(text, encoding="utf-8")
    return "[agent] network_mode"


def _break_resource_ceiling(root: Path) -> str:
    text = (root / "task.toml").read_text(encoding="utf-8")
    (root / "task.toml").write_text(
        re.sub(r"^memory_mb = \d+$", "memory_mb = 65535", text, count=1, flags=re.M),
        encoding="utf-8",
    )
    return "above the 8192 memoryMb declared"


def _break_unit_mapping(root: Path) -> str:
    """cpus read as though it were millicores.

    The mapping is what makes this catchable: 4000 is within the declared 4000
    cpuMillis if the units are ignored, and is 4000 CPUs if they are not.
    """
    text = (root / "task.toml").read_text(encoding="utf-8")
    (root / "task.toml").write_text(
        re.sub(r"^cpus = \d+$", "cpus = 4000", text, count=1, flags=re.M),
        encoding="utf-8",
    )
    return "above the sandbox ceiling of 8"


def _break_duplicate_drift(root: Path) -> str:
    victim = root / "environment" / "starter" / "resp3_wire" / "parser.py"
    victim.write_text(victim.read_text(encoding="utf-8") + "\n# drifted\n",
                      encoding="utf-8")
    return "duplicated build inputs have drifted"


def _break_duplicate_missing(root: Path) -> str:
    shutil.rmtree(root / "starter")
    return "D30 duplication is incomplete"


def _break_executable_bit(root: Path) -> str:
    (root / "solution" / "solve.sh").chmod(0o644)
    return "not executable"


BREAKAGES = (
    ("a required path removed", _break_missing_path),
    ("a symlink pointing outside", _break_symlink),
    ("task.toml made unparseable", _break_toml_syntax),
    ("a required table renamed", _break_missing_table),
    ("the task name emptied", _break_empty_name),
    ("the agent given network", _break_network_mode),
    ("a resource figure raised above SUBMISSION.md", _break_resource_ceiling),
    ("cpus mistaken for millicores", _break_unit_mapping),
    ("the duplicated build inputs edited apart", _break_duplicate_drift),
    ("a duplicated build input removed", _break_duplicate_missing),
    ("an entrypoint made non-executable", _break_executable_bit),
)


def self_test(root: Path, submission: Path) -> int:
    """Break each rule in a copy and require this checker to reject it."""
    intact = check_bundle(root, submission)
    if intact:
        print("the bundle does not pass as-is, so the self-test cannot mean "
              "anything yet:", file=sys.stderr)
        for problem in intact:
            print(f"  {problem}", file=sys.stderr)
        return 1
    print(f"{root} passes as assembled; now breaking it on purpose\n")

    failures = 0
    for label, breakage in BREAKAGES:
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "bundle"
            shutil.copytree(root, copy, symlinks=True)
            expected = breakage(copy)
            problems = check_bundle(copy, submission)
            caught = [p for p in problems if expected in p]
            if caught:
                print(f"  ok   rejected: {label}")
                print(f"         -> {caught[0]}")
            else:
                failures += 1
                print(f"  FAIL accepted: {label}")
                print(f"         expected a problem containing {expected!r}, got "
                      f"{problems or 'nothing'}")
    print()
    if failures:
        print(f"{failures} of {len(BREAKAGES)} breakages were not caught; those "
              f"rules are not enforced")
        return 1
    print(f"all {len(BREAKAGES)} breakages rejected; the checks can fail")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("bundle", nargs="?", default=None)
    parser.add_argument("--submission", default=None)
    parser.add_argument("--self-test", action="store_true",
                        help="break each rule in a copy and require rejection")
    args = parser.parse_args()

    repo = Path(__file__).resolve().parent.parent
    root = Path(args.bundle).resolve() if args.bundle else repo / "build" / "bundle"
    submission = (Path(args.submission).resolve() if args.submission
                  else repo / "docs" / "SUBMISSION.md")

    if args.self_test:
        return self_test(root, submission)

    problems = check_bundle(root, submission)
    if problems:
        print(f"{len(problems)} problem(s) in {root}:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    files = sum(len(f) for _, _, f in os.walk(root))
    print(f"{root}: {len(REQUIRED)} required paths present, {files} files, "
          f"paths safe and unique, task.toml valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
