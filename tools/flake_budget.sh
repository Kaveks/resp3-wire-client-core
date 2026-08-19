#!/usr/bin/env bash
# The acceptance bar: consecutive clean full suite runs against an
# implementation, with a fresh seed each run. A single failure means the
# responsible channel is redesigned, not retried.
#
#   tools/flake_budget.sh [runs] [client-dir]
#
# Requires RESP3_ORACLE_PYTHON to name an interpreter with redis-py, separate
# from the one running this, and RESP3_REDIS_SERVER if redis-server is not on
# PATH.
set -uo pipefail

RUNS="${1:-20}"
CLIENT="${2:-${RESP3_CLIENT_PATH:-/app}}"
PYTHON="${RESP3_HARNESS_PYTHON:-python3}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

failed=0
for i in $(seq 1 "$RUNS"); do
  report="$WORK/run-$i.json"
  unset RESP3_SEED
  "$PYTHON" "$HERE/harness/run.py" --client "$CLIENT" --report "$report" \
    >"$WORK/run-$i.log" 2>&1
  status=$?
  seed=$(grep -o '"seed": "[0-9]*"' "$report" 2>/dev/null | head -1 | grep -o '[0-9]*')
  score=$(grep -o '"score": [0-9.]*' "$report" 2>/dev/null | head -1 | awk '{print $2}')
  if [ "$status" -eq 0 ]; then
    echo "run $i/$RUNS  clean    score=$score seed=$seed"
  else
    failed=$((failed + 1))
    echo "run $i/$RUNS  FAILED   score=$score seed=$seed  (exit $status)"
    echo "  reproduce with: RESP3_SEED=$seed $PYTHON harness/run.py --client $CLIENT"
    grep -E "^(FAILED|ERROR)" "$WORK/run-$i.log" | head -10 | sed 's/^/  /'
  fi
done

echo
if [ "$failed" -eq 0 ]; then
  echo "$RUNS consecutive clean runs."
  exit 0
fi
echo "$failed of $RUNS runs failed. The responsible channel is redesigned, not retried."
exit 1
