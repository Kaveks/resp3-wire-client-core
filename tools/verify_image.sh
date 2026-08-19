#!/usr/bin/env bash
# Build the shipped image and verify it offline, measuring both.
#
#   tools/verify_image.sh [tag]
#   tools/verify_image.sh --cold [tag]      build with --no-cache and time it
#   tools/verify_image.sh --no-build [tag]
#
# The build has network, because that is the phase where dependencies are
# fetched. Everything after it runs with --network none, because the rollout and
# verification phases have none and an image that only works with network is an
# image that fails at grading.
#
# The sealed harness is not baked into the image. It arrives with the bundle's
# tests/ directory at verification time, so here it is mounted read-only, which
# is the same shape.
#
# Every probe is a file under tools/image_probes, mounted and executed by path.
# An earlier version fed them to python on stdin, which docker run discards
# unless -i is given, so four checks passed without running at all. That is why
# the negative controls below exist and why they run first: a check nobody has
# watched fail is a check nobody has verified.
#
# Build and verifier wall times are measured, not estimated: docs/SUBMISSION.md
# needs real figures and CLAUDE.md forbids guessing them.
set -uo pipefail

BUILD=1
CACHE_FLAG=""
case "${1:-}" in
  --no-build) BUILD=0; shift ;;
  # The platform builds from scratch, so a cached rebuild is not the figure
  # docs/SUBMISSION.md needs.
  --cold) CACHE_FLAG="--no-cache"; shift ;;
esac
TAG="${1:-resp3-wire-client-core:dev}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
chmod 0755 "$WORK"
trap 'rm -rf "$WORK"' EXIT

fail=0
step() { printf '\n=== %s\n' "$1"; }
note() { printf '    %s\n' "$1"; }
pass() { printf '    ok   %s\n' "$1"; }
bad()  { printf '    FAIL %s\n' "$1"; fail=$((fail + 1)); }

PROBES="$HERE/tools/image_probes"

# A redis package that is not redis-py, used only to break the properties the
# probes assert so that the probes can be watched failing.
mkdir -p "$WORK/decoy/site-packages/redis"
printf '__version__ = "0.0.0-decoy"\n' > "$WORK/decoy/site-packages/redis/__init__.py"
chmod -R a+rX "$WORK/decoy"

if [ "$BUILD" -eq 1 ]; then
  step "build (network allowed)"
  build_start=$(date +%s.%N)
  docker build $CACHE_FLAG -f "$HERE/environment/Dockerfile" -t "$TAG" "$HERE" \
    >"$WORK/build.log" 2>&1
  build_status=$?
  build_end=$(date +%s.%N)
  BUILD_SECONDS=$(echo "$build_end - $build_start" | bc)
  if [ "$build_status" -ne 0 ]; then
    echo "build failed:"; tail -40 "$WORK/build.log"; exit 1
  fi
  printf '    build wall time: %.1f s%s\n' "$BUILD_SECONDS" \
    "$([ -n "$CACHE_FLAG" ] && echo ' (cold, --no-cache)' || echo ' (layer cache warm)')"
  grep -E "harness interpreter:|oracle interpreter:|D25 layer one holds|Redis server v=" \
    "$WORK/build.log" | sed 's/^/    /'
else
  BUILD_SECONDS=0
  note "build skipped"
fi
IMAGE_MB=$(docker image inspect "$TAG" --format '{{.Size}}' | awk '{printf "%.0f", $1/1048576}')
note "image size: ${IMAGE_MB} MB"

OFFLINE=(docker run --rm --network none -v "$PROBES:/opt/probes:ro")
HARNESS_PY=/opt/venv-harness/bin/python

# --------------------------------------------------------------------------
# Negative controls. Each deliberately breaks the property its probe asserts
# and requires the probe to say so. A probe that passes here is not measuring
# anything and the check built on it is worthless.
# --------------------------------------------------------------------------
step "negative controls: every probe must be able to fail"

docker run --rm -v "$PROBES:/opt/probes:ro" "$TAG" \
  "$HARNESS_PY" /opt/probes/no_network.py >"$WORK/nc_net.log" 2>&1
if [ $? -ne 0 ]; then pass "no_network fails when the network is present"
else bad "no_network passed with the network present; it cannot fail"; fi

docker run --rm --network none \
  -v "$PROBES:/opt/probes:ro" -v "$WORK/decoy/site-packages:/opt/decoy:ro" \
  -e PYTHONPATH=/opt/decoy "$TAG" \
  "$HARNESS_PY" /opt/probes/redis_unimportable.py >"$WORK/nc_imp.log" 2>&1
if [ $? -ne 0 ]; then pass "redis_unimportable fails when redis is on the path"
else bad "redis_unimportable passed with redis on the path; it cannot fail"; fi

docker run --rm --network none \
  -v "$PROBES:/opt/probes:ro" -v "$WORK/decoy:/opt/decoy:ro" "$TAG" \
  "$HARNESS_PY" /opt/probes/no_redis_on_disk.py >"$WORK/nc_disk.log" 2>&1
if [ $? -ne 0 ]; then pass "no_redis_on_disk fails when a redis package is readable"
else bad "no_redis_on_disk passed with a readable redis package; it cannot fail"; fi

docker run --rm --network none -v "$PROBES:/opt/probes:ro" "$TAG" \
  python /opt/probes/timeout_plugin.py >"$WORK/nc_to.log" 2>&1
if [ $? -ne 0 ]; then pass "timeout_plugin fails on an interpreter without it"
else bad "timeout_plugin passed on an interpreter without it; it cannot fail"; fi

# The plugin being installed is not the same claim as the bound being enforced.
# A case that sleeps past 30 s must be killed at 30 s, under the harness's own
# ini, or docs/HARNESS.md section 8 is documentation rather than a control.
mkdir -p "$WORK/slow"
cat > "$WORK/slow/test_slow.py" <<'SLOWEOF'
import time


def test_sleeps_past_the_per_case_bound():
    time.sleep(45)
SLOWEOF
chmod -R a+rX "$WORK/slow"
slow_start=$(date +%s.%N)
docker run --rm --network none \
  -v "$WORK/slow:/opt/slow:ro" -v "$HERE/harness:/opt/harness:ro" "$TAG" \
  "$HARNESS_PY" -m pytest -c /opt/harness/pytest.ini /opt/slow/test_slow.py \
  -q -p no:cacheprovider >"$WORK/nc_slow.log" 2>&1
slow_status=$?
slow_end=$(date +%s.%N)
SLOW_SECONDS=$(echo "$slow_end - $slow_start" | bc)
if [ "$slow_status" -ne 0 ] && [ "$(echo "$SLOW_SECONDS < 44" | bc)" -eq 1 ]; then
  printf '    ok   a 45 s case is killed at the 30 s bound (took %.1f s)\n' "$SLOW_SECONDS"
else
  bad "$(printf 'a 45 s case ran %.1f s with status %s; the bound is not enforced' \
        "$SLOW_SECONDS" "$slow_status")"
fi

# --------------------------------------------------------------------------
# The checks themselves.
# --------------------------------------------------------------------------
step "no network at runtime"
"${OFFLINE[@]}" "$TAG" "$HARNESS_PY" /opt/probes/no_network.py \
  >"$WORK/net.log" 2>&1
if [ $? -eq 0 ]; then pass "$(tail -1 "$WORK/net.log")"; else bad "$(tail -1 "$WORK/net.log")"; fi

step "D25 layer one: redis-py is unreachable, not merely unimported"
"${OFFLINE[@]}" "$TAG" "$HARNESS_PY" /opt/probes/redis_unimportable.py \
  >"$WORK/d25a.log" 2>&1
if [ $? -eq 0 ]; then pass "$(tail -1 "$WORK/d25a.log")"; else bad "$(tail -1 "$WORK/d25a.log")"; fi

"${OFFLINE[@]}" "$TAG" "$HARNESS_PY" /opt/probes/no_redis_on_disk.py \
  >"$WORK/d25b.log" 2>&1
if [ $? -eq 0 ]; then pass "$(tail -1 "$WORK/d25b.log")"; else bad "$(tail -1 "$WORK/d25b.log")"; fi

"${OFFLINE[@]}" "$TAG" sh -c 'ls /opt/venv-oracle >/dev/null 2>&1 && exit 1; exit 0' \
  >"$WORK/d25c.log" 2>&1
if [ $? -eq 0 ]; then pass "the harness user cannot list the oracle interpreter"
else bad "the harness user can list the oracle interpreter"; fi

"${OFFLINE[@]}" "$TAG" oracle-python -c 'import redis; print(redis.__version__)' \
  >"$WORK/d25d.log" 2>&1
if [ $? -eq 0 ]; then pass "the oracle proxy still runs redis-py ($(tail -1 "$WORK/d25d.log"))"
else bad "the oracle proxy cannot run redis-py"; fi

step "per case bound is enforced, not documentary"
"${OFFLINE[@]}" "$TAG" "$HARNESS_PY" /opt/probes/timeout_plugin.py \
  >"$WORK/to.log" 2>&1
if [ $? -eq 0 ]; then pass "$(tail -1 "$WORK/to.log")"; else bad "$(tail -1 "$WORK/to.log")"; fi

# --------------------------------------------------------------------------
# The verifier runs. Both anchors, offline.
# --------------------------------------------------------------------------
VERIFY=(docker run --rm --network none
  -v "$HERE/harness:/opt/harness:ro"
  -v "$HERE/reference/resp3_wire:/opt/reference/resp3_wire:ro")

step "verifier run: the reference must reach full reward"
verify_start=$(date +%s.%N)
"${VERIFY[@]}" "$TAG" "$HARNESS_PY" /opt/harness/run.py \
  --client /opt/reference --report /tmp/score.json >"$WORK/reference.log" 2>&1
ref_status=$?
verify_end=$(date +%s.%N)
VERIFY_SECONDS=$(echo "$verify_end - $verify_start" | bc)
ref_score=$(grep -o '"score": [0-9.]*' "$WORK/reference.log" | head -1 | awk '{print $2}')
ref_passed=$(grep -o '"passed": [0-9]*' "$WORK/reference.log" | head -1 | awk '{print $2}')
printf '    verifier wall time: %.1f s\n' "$VERIFY_SECONDS"
if [ "$ref_status" -eq 0 ] && [ "$ref_score" = "1.0" ]; then
  pass "reference scores ${ref_passed}/130 offline"
else
  bad "reference scores ${ref_passed:-?}/130 offline"
fi

step "verifier run: the untouched starter must sit at the floor"
docker run --rm --network none -v "$HERE/harness:/opt/harness:ro" "$TAG" \
  "$HARNESS_PY" /opt/harness/run.py --client /app --report /tmp/score.json \
  >"$WORK/starter.log" 2>&1
st_passed=$(grep -o '"passed": [0-9]*' "$WORK/starter.log" | head -1 | awk '{print $2}')
st_collected=$(grep -o '"collected": [0-9]*' "$WORK/starter.log" | head -1 | awk '{print $2}')
if [ "$st_passed" = "0" ] && [ "$st_collected" = "130" ]; then
  pass "starter scores 0/130 with 130 collected"
else
  bad "starter scores ${st_passed:-?}/130 with ${st_collected:-?} collected"
fi

step "measurements"
printf '    build_seconds      %.1f\n' "$BUILD_SECONDS"
printf '    verifier_seconds   %.1f\n' "$VERIFY_SECONDS"
printf '    image_mb           %s\n' "$IMAGE_MB"

printf '\n'
if [ "$fail" -eq 0 ]; then
  echo "image verified offline, 0 failures"
  exit 0
fi
echo "$fail check(s) failed"
for log in "$WORK"/reference.log "$WORK"/starter.log; do
  [ -f "$log" ] || continue
  printf '\n--- %s\n' "$(basename "$log")"; tail -30 "$log"
done
exit 1
