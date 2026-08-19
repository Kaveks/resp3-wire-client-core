#!/usr/bin/env bash
# Reference solution entrypoint.
#
# Installs the reference implementation over the starter stubs at
# RESP3_CLIENT_PATH. The platform's oracle stage runs this and then the
# verifier, and the result must be full reward: a task its own reference cannot
# solve is rejected.
#
# This is an entrypoint, not the implementation. The reference sits beside it
# under solution/resp3_wire.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${RESP3_CLIENT_PATH:-/app}"

if [ ! -d "$HERE/resp3_wire" ]; then
  echo "solve.sh: no reference package beside this script" >&2
  exit 1
fi

mkdir -p "$TARGET"
rm -rf "${TARGET:?}/resp3_wire"
cp -r "$HERE/resp3_wire" "$TARGET/resp3_wire"
echo "installed the reference implementation into ${TARGET}/resp3_wire"
