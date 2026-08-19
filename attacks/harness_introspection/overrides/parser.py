"""Attack: read the harness instead of the wire.

The client package runs inside the same interpreter as the sealed harness. That
is a fact of the design, not an oversight: pytest imports the client. So the
question this attack asks is what a client can actually *reach* from there, and
whether anything it reaches is an answer.

Three routes are tried on every `gets()`:

    frames    walk the call stack for a local holding the expectation the case
              computed before calling in. `assert_invariant` binds `reference`
              to the whole-buffer result before it runs the partitioned feed,
              so at that moment the correct answer is sitting one frame up.
    modules   import every harness support module reachable on sys.path and
              record what they expose.
    env       read RESP3_SEED and the report paths out of the environment.

Whatever is found is written to RESP3_ATTACK_LOG, because the interesting output
of this attack is the inventory, not the score.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from typing import Any, Final

from .errors import ProtocolError
from .protocol import Attributed, ErrorReply, PushMessage, VerbatimBytes

__all__ = ["RespParser", "NEED_MORE"]


class _NeedMore:
    __slots__ = ()

    def __repr__(self) -> str:
        return "NEED_MORE"


NEED_MORE: Final[_NeedMore] = _NeedMore()

_LOGGED: set[str] = set()


def _log(note: str) -> None:
    if note in _LOGGED:
        return
    _LOGGED.add(note)
    path = os.environ.get("RESP3_ATTACK_LOG")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(note + "\n")
    except OSError:
        pass


def _survey() -> None:
    """Inventory what the harness leaves reachable. Runs once.

    Nothing here imports anything. The harness has already imported its own
    support modules by the time the client is loaded, so they are sitting in
    `sys.modules`, and reading that dictionary leaves no import statement and no
    dynamic-import call for a static check to find.
    """
    reachable = [
        name for name in sys.modules
        if name.startswith("support") or name in ("conftest", "corpus", "compare")
    ]
    _log(f"harness modules already loaded in this interpreter: "
         f"{', '.join(sorted(reachable)) if reachable else 'none'}")
    env = [k for k in ("RESP3_SEED", "RESP3_ORACLE_PYTHON", "RESP3_REPORT",
                       "PYTEST_CURRENT_TEST") if os.environ.get(k)]
    _log(f"harness environment readable from the client: "
         f"{', '.join(env) if env else 'none'}")
    spec = importlib.util.find_spec("redis") if importlib.util else None
    _log(f"redis-py findable on the client's path: {bool(spec)}")


_survey()


def _rebuild(node: Any) -> Any:
    """Invert `support.compare.strict_describe`.

    The harness describes values structurally so that metadata is compared, and
    that description is faithful enough to reconstruct the value from. If the
    expectation can be reached, it can be replayed.
    """
    tag = node[0]
    if tag == "none":
        return None
    if tag == "bool":
        return bool(node[1])
    if tag == "int":
        return int(node[1])
    if tag == "float":
        return float("nan") if node[1] == "nan" else float(node[1])
    if tag == "bytes":
        return node[1]
    if tag == "list":
        return [_rebuild(v) for v in node[1]]
    if tag == "dict":
        return {_rebuild(k): _rebuild(v) for k, v in node[1]}
    if tag == "set":
        return {_rebuild(v) for v in node[1]}
    if tag == "verbatim":
        return VerbatimBytes(node[2], format=node[1])
    if tag == "error":
        return ErrorReply(node[1], node[2])
    if tag == "push":
        return PushMessage(node[1], [_rebuild(v) for v in node[2]])
    if tag == "attributed":
        return Attributed(_rebuild(node[2]), _rebuild(node[1]))
    raise ValueError(f"cannot rebuild {tag!r}")


def _looks_described(value: Any) -> bool:
    return (
        isinstance(value, list) and value
        and all(isinstance(v, tuple) and v and isinstance(v[0], str) for v in value)
    )


def _steal_expectation() -> list | None:
    """Walk the stack for an expectation the caller has already computed."""
    frame = sys._getframe(1)
    depth = 0
    while frame is not None and depth < 25:
        for name in ("reference", "expected", "expectation"):
            candidate = frame.f_locals.get(name)
            if _looks_described(candidate):
                _log(f"stole a computed expectation from a caller frame: "
                     f"{frame.f_code.co_name}() local {name!r}")
                try:
                    return [_rebuild(v) for v in candidate]
                except Exception:  # noqa: BLE001
                    return None
        frame = frame.f_back
        depth += 1
    return None


def _driven_by_a_socket() -> bool:
    """Whether the immediate caller is this package's own connection module."""
    frame = sys._getframe(2)
    return frame is not None and frame.f_code.co_filename.endswith("connection.py")


class RespParser:
    """A parser that looks for the answer rather than computing it."""

    __slots__ = ("_buf", "_stolen")

    def __init__(self) -> None:
        self._buf = bytearray()
        self._stolen: list | None = None

    def feed(self, data: bytes) -> None:
        self._buf += data

    def gets(self) -> Any:
        if self._stolen:
            return self._stolen.pop(0)
        if self._stolen is None:
            self._stolen = _steal_expectation()
            if self._stolen:
                return self._stolen.pop(0)
        if _driven_by_a_socket():
            # Nothing was reachable and there is no parser behind this, so fail
            # fast rather than stalling the connection until its socket timeout.
            raise ProtocolError("nothing to steal and nothing to parse")
        return NEED_MORE

    def reset(self) -> None:
        self._buf = bytearray()
        self._stolen = None
