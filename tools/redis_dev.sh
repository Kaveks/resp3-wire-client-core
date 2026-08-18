#!/usr/bin/env bash
# Local Redis for development iteration. Not part of the shipped image.
set -euo pipefail

NAME="resp3-dev-redis"
PORT="${REDIS_PORT:-6399}"
IMAGE="redis:7.4-alpine"

case "${1:-up}" in
  up)
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker run -d --name "$NAME" -p "127.0.0.1:${PORT}:6379" "$IMAGE" \
      redis-server --save '' --appendonly no --enable-debug-command yes >/dev/null
    for _ in $(seq 1 50); do
      if docker exec "$NAME" redis-cli ping 2>/dev/null | grep -q PONG; then
        echo "redis up on 127.0.0.1:${PORT}"
        exit 0
      fi
      sleep 0.1
    done
    echo "redis failed to become ready" >&2
    exit 1
    ;;
  down)
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    echo "redis down"
    ;;
  cli)
    docker exec -it "$NAME" redis-cli
    ;;
  *)
    echo "usage: $0 {up|down|cli}" >&2
    exit 2
    ;;
esac
