"""Attack: reach redis-py by any route and let it do the protocol work.

`CLAUDE.md` names wrapping redis-py as the first thing the harness must resist,
and names the control: redis-py is absent from the interpreter that imports the
client package. `tools/check_stdlib_only.py` is explicitly the secondary layer.

This attack therefore does not stop at the direct import. It escalates:

    route 1  import redis, at module import time
    route 2  hunt the filesystem for a redis package belonging to some other
             interpreter and inject its directory onto sys.path, deferred until
             the first connect() rather than done at import
    route 3  spawn the interpreter named by RESP3_ORACLE_PYTHON, which the
             harness itself uses precisely because it has redis-py, and proxy
             every command through it

Route 1 is what interpreter separation is supposed to stop.

Route 2 is deliberately deferred. `harness/conftest.py` asserts at session start
that redis-py cannot be imported, and an injection performed at client import
time trips that assertion because the client is imported during collection,
before the session fixture runs. Waiting until the first `connect()` puts the
injection after that assertion has already passed. Whether that matters is the
question this attack exists to answer.

Which route the client reaches is written to RESP3_ATTACK_LOG so the report can
say what actually stopped it rather than guessing.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any

from .errors import ConnectionError, ProtocolError, TimeoutError, exception_for
from .parser import NEED_MORE, RespParser
from .protocol import ErrorReply, PushMessage, unwrap

if TYPE_CHECKING:  # pragma: no cover
    from .pipeline import Pipeline

__all__ = ["Connection"]


def _log(note: str) -> None:
    path = os.environ.get("RESP3_ATTACK_LOG")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(note + "\n")
    except OSError:
        pass


# --- route 1: the direct import ------------------------------------------

_REDIS = None
try:
    import redis as _REDIS  # type: ignore[no-redef]
    _log("route 1 (direct import redis) SUCCEEDED")
except ImportError as _exc:
    _log(f"route 1 (direct import redis) blocked: ImportError: {_exc}")


# --- route 2: find redis-py on disk and inject it onto sys.path ----------
#
# Deferred to the first connect(). See the module docstring.

def _hunt_for_redis() -> str | None:
    """Look for a redis package belonging to some other interpreter.

    The image is expected to carry two virtualenvs, one of which has redis-py,
    so the package is on the same filesystem as the client even when it is not
    on the client's path.
    """
    roots: list[Path] = []
    oracle = os.environ.get("RESP3_ORACLE_PYTHON")
    if oracle:
        roots.append(Path(oracle).resolve().parent.parent)
    roots.append(Path(sys.prefix))
    roots.append(Path.cwd())
    for parent in list(Path.cwd().parents)[:3]:
        roots.append(parent)
    seen: set[Path] = set()
    for root in roots:
        if not root or root in seen or not root.is_dir():
            continue
        seen.add(root)
        try:
            for candidate in root.glob("**/site-packages/redis/__init__.py"):
                return str(candidate.parent.parent)
        except OSError:
            continue
    return None


_ROUTE_2_TRIED = False


def _try_injection() -> None:
    """Run once, on the first connect, after session start has come and gone."""
    global _REDIS, _ROUTE_2_TRIED
    if _ROUTE_2_TRIED or _REDIS is not None:
        return
    _ROUTE_2_TRIED = True
    found = _hunt_for_redis()
    if not found:
        _log("route 2 (filesystem hunt for redis-py) found no candidate")
        return
    sys.path.insert(0, found)
    try:
        import redis as _injected
    except ImportError as exc:
        _log(f"route 2 (deferred sys.path injection from {found}) blocked: {exc}")
        return
    _REDIS = _injected
    _log(f"route 2 (deferred sys.path injection from {found}) SUCCEEDED, "
         f"after the session-start isolation assertion had already passed")


# --- route 3: proxy through the interpreter that does have redis-py ------

_PROXY_SCRIPT = r'''
import base64, json, sys
import redis

def encode(value):
    if value is None:
        return ["none"]
    if value is True or value is False:
        return ["bool", value]
    if isinstance(value, int):
        return ["int", str(value)]
    if isinstance(value, float):
        return ["float", repr(value)]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return ["bytes", base64.b64encode(bytes(value)).decode()]
    if isinstance(value, str):
        return ["bytes", base64.b64encode(value.encode()).decode()]
    if isinstance(value, (set, frozenset)):
        return ["set", [encode(v) for v in value]]
    if isinstance(value, dict):
        return ["dict", [[encode(k), encode(v)] for k, v in value.items()]]
    if isinstance(value, (list, tuple)):
        return ["list", [encode(v) for v in value]]
    if isinstance(value, Exception):
        return ["error", str(value)]
    return ["bytes", base64.b64encode(repr(value).encode()).decode()]

job = json.loads(sys.stdin.readline())
client = redis.Redis(host="127.0.0.1", port=job["port"],
                     protocol=job["protocol"], decode_responses=False)
client.response_callbacks.clear()
sys.stdout.write(json.dumps({"ready": True}) + "\n")
sys.stdout.flush()
for line in sys.stdin:
    line = line.strip()
    if not line:
        break
    args = [base64.b64decode(a) for a in json.loads(line)]
    try:
        out = encode(client.execute_command(*args))
    except Exception as exc:
        out = ["error", str(exc)]
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()
'''


def _decode(node: Any) -> Any:
    tag = node[0]
    if tag == "none":
        return None
    if tag == "bool":
        return bool(node[1])
    if tag == "int":
        return int(node[1])
    if tag == "float":
        return float(node[1])
    if tag == "bytes":
        return base64.b64decode(node[1])
    if tag == "list":
        return [_decode(v) for v in node[1]]
    if tag == "set":
        return {_decode(v) for v in node[1]}
    if tag == "dict":
        return {_decode(k): _decode(v) for k, v in node[1]}
    if tag == "error":
        text = node[1]
        code = text.split(None, 1)[0].upper() if text.split() else ""
        return ErrorReply(code, text)
    raise ProtocolError(f"unknown proxy tag {tag!r}")


def _encode_arg(value: object) -> bytes:
    if value is True or value is False:
        raise TypeError("bool is not a valid command argument")
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    if isinstance(value, (int, float)):
        return repr(value).encode("ascii")
    raise TypeError(f"cannot encode {type(value).__name__}")


def _encode_command(args: tuple[object, ...]) -> bytes:
    out = bytearray(b"*%d\r\n" % len(args))
    for value in args:
        payload = _encode_arg(value)
        out += b"$%d\r\n%s\r\n" % (len(payload), payload)
    return bytes(out)


class Connection:
    """A connection that is really a redis-py client wearing this API."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 6379,
        *,
        protocol: int = 3,
        timeout: float | None = 5.0,
        connect_timeout: float | None = None,
        db: int = 0,
        client_name: str | None = None,
    ) -> None:
        if protocol not in (2, 3):
            raise ValueError(f"protocol must be 2 or 3, got {protocol!r}")
        self._host = host
        self._port = port
        self._protocol = protocol
        self._timeout = timeout
        self._db = db
        self._client_name = client_name
        self._client: Any = None
        self._proxy: subprocess.Popen | None = None
        self._parser = RespParser()
        self._protocol_version: int | None = None
        self._server_info: dict[Any, Any] = {}
        self._poisoned = False
        self._pushes_discarded = 0

    # -- lifecycle ---------------------------------------------------------

    def connect(self) -> None:
        if self._client is not None or self._proxy is not None:
            return
        _try_injection()
        if _REDIS is not None:
            try:
                self._client = _REDIS.Redis(
                    host=self._host, port=self._port, protocol=self._protocol,
                    decode_responses=False, socket_timeout=self._timeout,
                )
                self._client.response_callbacks.clear()
                self._client.ping()
            except Exception as exc:  # noqa: BLE001
                raise ConnectionError(f"redis-py refused to connect: {exc}") from exc
        else:
            self._start_proxy()
        self._protocol_version = self._protocol
        self._server_info = {b"server": b"redis", b"proto": self._protocol}

    def _start_proxy(self) -> None:
        oracle = os.environ.get("RESP3_ORACLE_PYTHON")
        if not oracle:
            _log("route 3 (subprocess proxy) unavailable: "
                 "RESP3_ORACLE_PYTHON is not set")
            raise ConnectionError("no route to redis-py")
        try:
            proc = subprocess.Popen(
                [oracle, "-c", _PROXY_SCRIPT],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, bufsize=1,
            )
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write(json.dumps(
                {"port": self._port, "protocol": self._protocol}) + "\n")
            proc.stdin.flush()
            handshake = proc.stdout.readline()
            if not handshake or not json.loads(handshake).get("ready"):
                raise RuntimeError(f"proxy did not start: {handshake!r}")
        except Exception as exc:  # noqa: BLE001
            _log(f"route 3 (subprocess proxy via RESP3_ORACLE_PYTHON) blocked: {exc}")
            raise ConnectionError(f"no route to redis-py: {exc}") from exc
        _log("route 3 (subprocess proxy via RESP3_ORACLE_PYTHON) SUCCEEDED")
        self._proxy = proc

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None
        if self._proxy is not None:
            try:
                if self._proxy.stdin is not None:
                    self._proxy.stdin.close()
                self._proxy.wait(timeout=5)
            except Exception:  # noqa: BLE001
                self._proxy.kill()
            self._proxy = None
        self._parser.reset()

    @property
    def is_connected(self) -> bool:
        return self._client is not None or self._proxy is not None

    @property
    def is_poisoned(self) -> bool:
        return self._poisoned

    @property
    def protocol_version(self) -> int:
        if self._protocol_version is None:
            raise RuntimeError("protocol_version is not known until connect()")
        return self._protocol_version

    @property
    def server_info(self) -> dict[Any, Any]:
        return self._server_info

    @property
    def pushes_discarded(self) -> int:
        return self._pushes_discarded

    # -- commands ----------------------------------------------------------

    def execute(self, *args: bytes | str | int | float) -> object:
        reply = self._roundtrip(args)
        error = unwrap(reply)
        if isinstance(error, ErrorReply):
            raise exception_for(error.code, error.message)
        return reply

    def _roundtrip(self, args: tuple[object, ...]) -> object:
        if not args:
            raise ValueError("execute() requires at least a command name")
        if not self.is_connected:
            raise ConnectionError("connection is not connected")
        if self._poisoned:
            raise ConnectionError("connection is poisoned")
        encoded = [_encode_arg(a) for a in args]
        if self._client is not None:
            try:
                return self._client.execute_command(*encoded)
            except Exception as exc:  # noqa: BLE001
                name = type(exc).__name__
                if "Timeout" in name:
                    self._poisoned = True
                    raise TimeoutError(str(exc)) from exc
                if "Connection" in name:
                    self._poisoned = True
                    raise ConnectionError(str(exc)) from exc
                return ErrorReply(str(exc).split(None, 1)[0].upper(), str(exc))
        assert self._proxy is not None
        stdin, stdout = self._proxy.stdin, self._proxy.stdout
        assert stdin is not None and stdout is not None
        try:
            stdin.write(json.dumps(
                [base64.b64encode(a).decode() for a in encoded]) + "\n")
            stdin.flush()
            line = stdout.readline()
        except OSError as exc:
            self._poisoned = True
            raise ConnectionError(f"proxy died: {exc}") from exc
        if not line:
            self._poisoned = True
            raise ConnectionError("proxy closed")
        return _decode(json.loads(line))

    def _send(self, payload: bytes) -> None:
        """Pipeline uses this. A wrapped client has no wire to write to."""
        raise ProtocolError("this client has no wire; redis-py owns the socket")

    def _read_reply(self) -> object:
        raise ProtocolError("this client has no wire; redis-py owns the socket")

    def pipeline(self) -> "Pipeline":
        from .pipeline import Pipeline

        return Pipeline(self)

    def __enter__(self) -> "Connection":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
