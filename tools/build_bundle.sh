#!/usr/bin/env bash
# Assemble the submission bundle into build/bundle.
#
#   tools/build_bundle.sh [output-dir]
#
# The bundle is assembled from the repository rather than edited by hand, so
# that what ships is a function of what is committed. Nothing here is authored:
# instruction.md is copied and its hash checked, task.toml is derived from
# docs/SUBMISSION.md, and the rest is copied verbatim.
#
# Layout:
#
#   task.toml                 derived from docs/SUBMISSION.md
#   instruction.md            spec/instruction.md, hash verified
#   environment/Dockerfile    the image
#   environment/starter/      what the implementer starts from
#   environment/visible_tests/
#   starter/                  the same, duplicated. See D30.
#   visible_tests/
#   tests/test.sh             verifier entrypoint
#   tests/harness/            the sealed harness
#   tests/tools/              the static check the verifier runs
#   solution/solve.sh         oracle entrypoint
#   solution/resp3_wire/      the reference implementation
#
# D30, the duplication. The platform is not specific about which directory is
# the Docker build context, and COPY has neither a conditional nor a fallback:
# a path that does not resolve is a build failure, which is a deterministic
# rejection rather than a low score. Carrying the image's inputs at both
# `starter/` and `environment/starter/` makes the one COPY path resolve whether
# the context is the bundle root or the Dockerfile's own directory. The copies
# are written from one source here and compared by tools/check_paths.py, so
# they cannot drift. The cost is about 60 KB against a 428 KB bundle.
#
# Copying the whole context and choosing at run time was rejected: under a
# bundle-root context that would pull tests/ and solution/ into a layer, and a
# later deletion does not remove a layer.
#
# Two things must never reach the image an implementer works in: the sealed
# harness and the reference. They live under tests/ and solution/, which the
# Dockerfile does not copy from, and which a .dockerignore excludes from the
# context outright so that no future COPY can reach them either.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$HERE/build/bundle}"

copy_tree() {
  # Copy a directory without the artefacts of having run it.
  rm -rf "$2"
  mkdir -p "$(dirname "$2")"
  cp -r "$1" "$2"
  find "$2" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
  find "$2" -name '.pytest_cache' -type d -prune -exec rm -rf {} + 2>/dev/null || true
  find "$2" -name '*.pyc' -delete 2>/dev/null || true
}

echo "assembling into $OUT"
rm -rf "$OUT"
mkdir -p "$OUT/environment" "$OUT/tests/tools" "$OUT/solution"

# --- instruction.md, hash verified ----------------------------------------
# spec/instruction.md is frozen and hashed. A bundle carrying a different
# instruction than the ratified one is a bundle grading a different task.
expected="$(cut -d' ' -f1 < "$HERE/spec/instruction.md.sha256")"
actual="$(sha256sum "$HERE/spec/instruction.md" | cut -d' ' -f1)"
if [ "$expected" != "$actual" ]; then
  echo "spec/instruction.md does not match its recorded hash" >&2
  echo "  recorded $expected" >&2
  echo "  actual   $actual" >&2
  exit 1
fi
cp "$HERE/spec/instruction.md" "$OUT/instruction.md"
echo "  instruction.md      hash $actual"

# --- the image and its inputs ---------------------------------------------
cp "$HERE/environment/Dockerfile" "$OUT/environment/Dockerfile"
copy_tree "$HERE/starter/resp3_wire" "$OUT/environment/starter/resp3_wire"
copy_tree "$HERE/visible_tests" "$OUT/environment/visible_tests"
# D30: the same inputs at the bundle root, so the Dockerfile's COPY paths
# resolve under either build context. Written from the same source, never
# edited apart, and compared by tools/check_paths.py.
copy_tree "$HERE/starter/resp3_wire" "$OUT/starter/resp3_wire"
copy_tree "$HERE/visible_tests" "$OUT/visible_tests"

# Keep the sealed harness and the reference out of the build context entirely,
# whichever directory the platform chooses as context.
for context in "$OUT" "$OUT/environment"; do
  cat > "$context/.dockerignore" <<'IGNORE'
# The sealed harness and the reference must never enter an image layer. The
# Dockerfile does not copy them; this makes them unreachable even if it did.
tests/
solution/
task.toml
instruction.md
**/__pycache__/
**/*.pyc
IGNORE
done
echo "  environment/        Dockerfile, starter, visible tests"
echo "  starter/            duplicated for a bundle-root build context (D30)"
echo "  visible_tests/"

# --- the verifier ----------------------------------------------------------
cp "$HERE/tools/bundle/test.sh" "$OUT/tests/test.sh"
chmod +x "$OUT/tests/test.sh"
copy_tree "$HERE/harness" "$OUT/tests/harness"
cp "$HERE/tools/check_stdlib_only.py" "$OUT/tests/tools/check_stdlib_only.py"
echo "  tests/              test.sh, sealed harness, static check"

# --- the reference solution ------------------------------------------------
cp "$HERE/tools/bundle/solve.sh" "$OUT/solution/solve.sh"
chmod +x "$OUT/solution/solve.sh"
copy_tree "$HERE/reference/resp3_wire" "$OUT/solution/resp3_wire"
echo "  solution/           solve.sh, reference implementation"

# --- task.toml, derived ----------------------------------------------------
python3 - "$HERE/docs/SUBMISSION.md" "$OUT/task.toml" <<'PY_TOML'
"""Derive task.toml from docs/SUBMISSION.md.

`CLAUDE.md`: SUBMISSION.md is the single source of truth and task.toml is
derived from it, never edited independently. Deriving it mechanically is what
makes that true rather than aspirational.
"""
import re
import sys

source, target = sys.argv[1], sys.argv[2]
text = open(source, encoding="utf-8").read()


def scalar(key: str) -> str:
    """The rest of the line after an indented key."""
    match = re.search(rf"^\s{{4}}{re.escape(key)}\s+(.+?)\s*$", text, re.M)
    if not match:
        raise SystemExit(f"docs/SUBMISSION.md has no {key!r}")
    return match.group(1).strip()


def figure(key: str) -> int:
    """The first token after an indented key, as an integer.

    Trailing parentheticals such as "(4h; floor is 7200)" are commentary and are
    not part of the value.
    """
    raw = scalar(key).split()[0]
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"{key} is not an integer in docs/SUBMISSION.md: {raw!r}")


title = scalar("title")
slug = scalar("workingSlug")

# D30's companion: the platform documents snake_case keys for [environment], and
# docs/SUBMISSION.md uses draft-form field names. This is a mapping, not a
# rename. `cpuMillis` is millicores and `cpus` is a count, so the derivation
# converts rather than copying the number across.
cpu_millis = figure("cpuMillis")
if cpu_millis % 1000:
    raise SystemExit(
        f"cpuMillis is {cpu_millis}, which is not a whole number of CPUs; "
        f"the [environment] key the platform reads is a count"
    )

lines = [
    "# Generated by tools/build_bundle.sh from docs/SUBMISSION.md.",
    "# Do not edit. Change docs/SUBMISSION.md and reassemble.",
    "#",
    "# [environment] keys are the platform's snake_case forms. cpus is derived",
    "# from cpuMillis by dividing by 1000; SUBMISSION.md states millicores and",
    "# the platform reads a count.",
    "",
    "[metadata]",
    f'name = "{slug}"',
    f'title = "{title}"',
    f'collection_family = "{scalar("collectionFamily")}"',
    f'task_family = "{scalar("taskFamily")}"',
    f'verifier_family = "{scalar("verifierFamily")}"',
    "",
    "# The image build fetches packages; nothing else may reach the network.",
    "[environment]",
    'network_mode = "bridge"',
    f"cpus = {cpu_millis // 1000}",
    f"memory_mb = {figure('memoryMb')}",
    f"storage_mb = {figure('storageMb')}",
    f"gpus = {figure('gpuCount')}",
    "",
    "[agent]",
    'network_mode = "none"',
    f"timeout_sec = {figure('agentTimeoutSec')}",
    "",
    "[verifier]",
    'network_mode = "none"',
    f"timeout_sec = {figure('verifierTimeoutSec')}",
    "",
]
open(target, "w", encoding="utf-8").write("\n".join(lines))
print(f"  task.toml           name={slug!r}, derived from docs/SUBMISSION.md")
PY_TOML

echo
echo "bundle assembled at $OUT"
