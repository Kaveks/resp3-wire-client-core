"""Server lifecycle for the harness.

The harness never assumes a Redis instance exists and never uses the default
port. It starts one on a private port, waits for readiness with a bounded poll,
flushes before use, and tears it down afterwards.

Readiness and flushing are done over a raw socket rather than through the
client package, because the client package is the thing under test. A broken
client must fail its own cases, not prevent the server from being declared
ready.
"""

from __future__ import annotations

import os
import socket
import subprocess
import tempfile
import time

__all__ = ["RedisServer", "raw_command"]

# docs/HARNESS.md section 8: bounded, 50 attempts at 100 ms.
_READY_ATTEMPTS = 50
_READY_INTERVAL = 0.1


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def raw_command(port: int, *args: bytes | str, timeout: float = 2.0) -> bytes:
    """Send one command over a raw socket and return the unparsed reply.

    Deliberately does not use the client package.
    """
    payload = bytearray(b"*%d\r\n" % len(args))
    for a in args:
        b = a.encode() if isinstance(a, str) else a
        payload += b"$%d\r\n%s\r\n" % (len(b), b)
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(bytes(payload))
        return sock.recv(65536)


class RedisServer:
    """A Redis server owned by the harness for the duration of a run."""

    def __init__(self, binary: str | None = None) -> None:
        self.binary = binary or os.environ.get("RESP3_REDIS_SERVER", "redis-server")
        self.port = _free_port()
        self._proc: subprocess.Popen | None = None
        self._dir: tempfile.TemporaryDirectory | None = None

    def start(self) -> "RedisServer":
        self._dir = tempfile.TemporaryDirectory(prefix="resp3-harness-")
        self._proc = subprocess.Popen(
            [
                self.binary,
                "--port", str(self.port),
                "--bind", "127.0.0.1",
                "--save", "",
                "--appendonly", "no",
                # D10: DEBUG PROTOCOL and DEBUG SLEEP are both required, and
                # Redis 7 gates them behind this flag.
                "--enable-debug-command", "yes",
                "--dir", self._dir.name,
                "--daemonize", "no",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self._await_ready()
        self.flush()
        return self

    def _await_ready(self) -> None:
        for _ in range(_READY_ATTEMPTS):
            if self._proc is not None and self._proc.poll() is not None:
                err = b""
                if self._proc.stderr is not None:
                    err = self._proc.stderr.read() or b""
                raise RuntimeError(
                    f"redis-server exited during startup: "
                    f"{err.decode('utf-8', 'replace')[:400]}"
                )
            try:
                if b"PONG" in raw_command(self.port, "PING", timeout=0.5):
                    return
            except OSError:
                pass
            time.sleep(_READY_INTERVAL)
        self.stop()
        raise RuntimeError(
            f"redis-server on port {self.port} did not become ready within "
            f"{_READY_ATTEMPTS * _READY_INTERVAL:.1f}s"
        )

    def flush(self) -> None:
        raw_command(self.port, "FLUSHALL")

    def stop(self) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=10)
            self._proc = None
        if self._dir is not None:
            self._dir.cleanup()
            self._dir = None

    def __enter__(self) -> "RedisServer":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
