#!/usr/bin/env bash
# Verifier entrypoint. Grades the implementation at RESP3_CLIENT_PATH.
#
# This is an entrypoint, not the implementation. The sealed harness sits beside
# it under tests/harness and is what actually grades; this script establishes
# the environment the harness expects and makes the score legible to whatever
# reads it.
#
# The run has no network. Every dependency, including redis-server, was baked
# into the image at build time.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLIENT="${RESP3_CLIENT_PATH:-/app}"
HARNESS_PY="${RESP3_HARNESS_PYTHON:-/opt/venv-harness/bin/python}"
REPORT="${RESP3_REPORT:-/tmp/resp3_score.json}"

export RESP3_ORACLE_PYTHON="${RESP3_ORACLE_PYTHON:-/usr/local/bin/oracle-python}"
export RESP3_REDIS_SERVER="${RESP3_REDIS_SERVER:-/usr/local/bin/redis-server}"

# docs/HARNESS.md section 7.1: drawn from os.urandom at grading time and
# printed, so a failing run can be reproduced exactly.
if [ -z "${RESP3_SEED:-}" ]; then
  RESP3_SEED="$("$HARNESS_PY" -c 'import os; print(int.from_bytes(os.urandom(4), "big"))')"
fi
export RESP3_SEED
echo "RESP3_SEED=${RESP3_SEED}"

emit_zero() {
  cat > "$REPORT" <<JSON
{
  "score": 0.0,
  "passed": 0,
  "total": 130,
  "seed": "${RESP3_SEED}",
  "reason": "$1"
}
JSON
  cat "$REPORT"
}

if [ ! -d "$CLIENT/resp3_wire" ]; then
  emit_zero "no resp3_wire package at ${CLIENT}"
  exit 1
fi

# D25 layer three. The permission control in the image is layer one and the
# per-case assertion in conftest is layer two; this is the static backstop, and
# it is the only one of the three that sees a client vendoring a package in.
if ! "$HARNESS_PY" "$HERE/tools/check_stdlib_only.py" "$CLIENT/resp3_wire"; then
  emit_zero "the client package imports outside the standard library"
  exit 1
fi

# The harness starts its own Redis on a private port with persistence disabled,
# polls it ready, flushes, and tears it down. See harness/support/redis_boot.py.
# It never assumes a server exists and never uses the default port.
"$HARNESS_PY" "$HERE/harness/run.py" --client "$CLIENT" --report "$REPORT"
status=$?

echo "--- score report ---"
cat "$REPORT"
exit "$status"
