#!/usr/bin/env bash
# Verify an assembled bundle end to end, from a clean state, under both plausible
# Docker build contexts.
#
#   tools/verify_bundle.sh [bundle-dir]
#
# D30: the platform is not specific about which directory is the build context,
# and a COPY path that does not resolve is a build failure rather than a low
# score. The bundle carries the image's inputs at both `starter/` and
# `environment/starter/` so one Dockerfile resolves either way. This verifies
# that claim by building both and running the bundle's own entrypoints against
# each.
#
# D29: any change to the image, the entrypoints, or the working directory is
# re-checked by running solve.sh then test.sh through the bundle's own paths,
# cold and offline. That is what the two runs below do.
#
# Every check has a negative control that runs first, per D26, and each control
# breaks the property rather than the apparatus, per D28.
#
# One trap this script fell into and now avoids: `local` is a command and resets
# `$?`. Declaring result variables after the command whose status is wanted
# reads the declaration's status instead. Every `local` here precedes the
# command it describes.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUNDLE="${1:-$HERE/build/bundle}"
PROBES="$HERE/tools/image_probes"
WORK="$(mktemp -d)"
chmod 0755 "$WORK"
trap 'rm -rf "$WORK"' EXIT

fail=0
step() { printf '\n=== %s\n' "$1"; }
pass() { printf '    ok   %s\n' "$1"; }
bad()  { printf '    FAIL %s\n' "$1"; fail=$((fail + 1)); }

HARNESS_PY=/opt/venv-harness/bin/python

step "packaging constraints"
if /usr/bin/python3 "$HERE/tools/check_paths.py" "$BUNDLE" >"$WORK/paths.log" 2>&1; then
  pass "$(tail -1 "$WORK/paths.log")"
else
  bad "check_paths.py rejected the bundle"; cat "$WORK/paths.log"
fi

verify_context() {
  local label="$1" context="$2" tag="$3"

  step "build from ${label} context: ${context#$BUNDLE/}"
  local start end seconds status
  start=$(date +%s.%N)
  docker build --no-cache -f "$BUNDLE/environment/Dockerfile" -t "$tag" \
    "$context" >"$WORK/build_$label.log" 2>&1
  status=$?
  end=$(date +%s.%N)
  seconds=$(echo "$end - $start" | bc)
  if [ "$status" -ne 0 ]; then
    bad "the build failed from the ${label} context"
    tail -20 "$WORK/build_$label.log"
    return
  fi
  printf '    build wall time: %.1f s (cold)\n' "$seconds"
  local mb
  mb=$(docker image inspect "$tag" --format '{{.Size}}' | awk '{printf "%.0f", $1/1048576}')
  printf '    image size: %s MB\n' "$mb"

  step "${label}: negative controls"
  docker run --rm --network none \
    -v "$PROBES:/opt/probes:ro" -v "$BUNDLE/tests:/opt/leaked:ro" "$tag" \
    "$HARNESS_PY" /opt/probes/no_sealed_content.py >"$WORK/nc_sealed_$label.log" 2>&1
  if [ $? -ne 0 ]; then pass "leak probe fails when the sealed harness is present"
  else bad "leak probe passed with the sealed harness mounted; it cannot fail"; fi

  docker run --rm --network none \
    -v "$PROBES:/opt/probes:ro" -v "$BUNDLE/solution/resp3_wire:/opt/answer:ro" "$tag" \
    "$HARNESS_PY" /opt/probes/no_sealed_content.py >"$WORK/nc_ref_$label.log" 2>&1
  if [ $? -ne 0 ]; then pass "leak probe fails when the reference is present"
  else bad "leak probe passed with the reference mounted; it cannot fail"; fi

  step "${label}: the implementer's image carries neither the harness nor the answer"
  docker run --rm --network none -v "$PROBES:/opt/probes:ro" "$tag" \
    "$HARNESS_PY" /opt/probes/no_sealed_content.py >"$WORK/sealed_$label.log" 2>&1
  if [ $? -eq 0 ]; then pass "$(tail -1 "$WORK/sealed_$label.log")"
  else bad "sealed content leaked into the image"; head -5 "$WORK/sealed_$label.log"; fi

  step "${label}: tests/test.sh against the untouched starter"
  local floor_status floor_passed floor_collected
  docker run --rm --network none -v "$BUNDLE/tests:/opt/tests:ro" "$tag" \
    /opt/tests/test.sh >"$WORK/floor_$label.log" 2>&1
  floor_status=$?
  floor_passed=$(grep -o '"passed": [0-9]*' "$WORK/floor_$label.log" | head -1 | awk '{print $2}')
  floor_collected=$(grep -o '"collected": [0-9]*' "$WORK/floor_$label.log" | head -1 | awk '{print $2}')
  if [ "$floor_status" -ne 0 ] && [ "$floor_passed" = "0" ] && [ "$floor_collected" = "130" ]; then
    pass "starter scores 0/130 with 130 collected, exit ${floor_status}"
  else
    bad "starter scored ${floor_passed:-?}/130, collected ${floor_collected:-?}, exit ${floor_status}"
    tail -20 "$WORK/floor_$label.log"
  fi

  step "${label}: solution/solve.sh then tests/test.sh"
  local reward_status reward_score reward_passed
  start=$(date +%s.%N)
  docker run --rm --network none \
    -v "$BUNDLE/tests:/opt/tests:ro" -v "$BUNDLE/solution:/opt/solution:ro" "$tag" \
    sh -c '/opt/solution/solve.sh && /opt/tests/test.sh' >"$WORK/reward_$label.log" 2>&1
  reward_status=$?
  end=$(date +%s.%N)
  printf '    solve plus verify wall time: %.1f s\n' "$(echo "$end - $start" | bc)"
  reward_score=$(grep -o '"score": [0-9.]*' "$WORK/reward_$label.log" | head -1 | awk '{print $2}')
  reward_passed=$(grep -o '"passed": [0-9]*' "$WORK/reward_$label.log" | head -1 | awk '{print $2}')
  if [ "$reward_status" -eq 0 ] && [ "$reward_score" = "1.0" ]; then
    pass "reference scores ${reward_passed}/130, exit 0"
  else
    bad "reference scored ${reward_passed:-?}/130, score ${reward_score:-?}, exit ${reward_status}"
    tail -20 "$WORK/reward_$label.log"
  fi
}

verify_context "root" "$BUNDLE" "resp3-bundle:ctx-root"
verify_context "environment" "$BUNDLE/environment" "resp3-bundle:ctx-env"

step "the two contexts produce the same image content"
for tag in resp3-bundle:ctx-root resp3-bundle:ctx-env; do
  docker run --rm --network none "$tag" \
    sh -c 'find /app -type f | sort | xargs sha256sum 2>/dev/null | sha256sum' \
    >>"$WORK/digests.log" 2>&1
done
if [ "$(sort -u "$WORK/digests.log" | wc -l)" -eq 1 ]; then
  pass "/app is byte-identical whichever context built the image"
else
  bad "the two contexts produced different /app trees"; cat "$WORK/digests.log"
fi

step "measurements"
printf '    bundle_files           %s\n' "$(find "$BUNDLE" -type f | wc -l)"
printf '    bundle_kb              %s\n' "$(du -sk "$BUNDLE" | cut -f1)"

printf '\n'
if [ "$fail" -eq 0 ]; then
  echo "bundle verified end to end from both build contexts, 0 failures"
  exit 0
fi
echo "$fail check(s) failed"
exit 1
