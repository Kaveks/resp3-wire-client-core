"""The redis-py side of the differential oracle.

Runs under its own interpreter, the only one with redis-py installed. It reads
a job as JSON on stdin and writes the encoded results as JSON on stdout.

Keeping redis-py in a separate process is what makes the isolation structural.
The interpreter that imports the client package never has redis-py on its path,
so a client cannot reach it to wrap, whatever it tries.

Not imported by the harness. It is executed with the oracle interpreter named
by ``RESP3_ORACLE_PYTHON``.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from support.wire_codec import decode_args, encode_value  # noqa: E402

# The nine behaviours section 2.8 of docs/HARNESS.md assumes of redis-py. They
# are measured, not asserted from memory, and two of them contradict what that
# document originally assumed. See D11.
PROBE_TYPES = [
    "double", "bignum", "true", "null", "map", "set", "verbatim", "attrib", "push",
]


def _client(mod, port: int, protocol: int):
    """A redis-py client with response callbacks cleared.

    Clearing the callbacks is the single most important line here. redis-py
    post-processes per command: HGETALL becomes a dict even under RESP2 where
    the wire carries a flat array, SMEMBERS becomes a set, EXISTS becomes a
    bool. Comparing against post-processed values would test whether the client
    reimplemented redis-py's callback table rather than whether it parsed the
    protocol.
    """
    client = mod.Redis(
        host="127.0.0.1", port=port, protocol=protocol, decode_responses=False
    )
    client.response_callbacks.clear()
    return client


def error_code(mod, exc: BaseException) -> str:
    """Recover the Redis error code from a redis-py exception.

    docs/HARNESS.md section 2.4 says to take the first whitespace delimited
    token of `str(exc)`, uppercased. Measurement shows that recovers the code
    only for codes redis-py does not know about.

    redis-py's `parse_error` strips the code prefix from the message for every
    code in its own `EXCEPTION_CLASSES` table, and leaves it in place for the
    rest. So `WRONGTYPE` and `BUSYGROUP` keep their prefix and the documented
    rule works, while a generic `ERR` yields "WRONG" (from "wrong number of
    arguments...") and `NOSCRIPT` yields "NO" (from "No matching script..."),
    neither of which can ever match the client's code.

    The code is therefore recovered from redis-py's own table first, by
    exception class, and only then from the message. This is a correction to a
    factually wrong rule rather than a loosening of an assertion; see the step
    6 report.
    """
    table = getattr(_base_parser(mod), "EXCEPTION_CLASSES", {})
    for code, target in table.items():
        # The ERR entry maps messages to classes rather than naming one class.
        if isinstance(target, dict):
            continue
        if type(exc) is target:
            return code
    text = str(exc)
    parts = text.split(None, 1)
    first = parts[0] if parts else ""
    # A code redis-py does not know is left in the message, and Redis codes are
    # uppercase. Anything else means the prefix was stripped, which for a plain
    # ResponseError means the code was ERR.
    if first.isupper() and first.isalpha():
        return first
    if type(exc) is mod.exceptions.ResponseError:
        return "ERR"
    return first.upper()


def _base_parser(mod):
    try:
        from redis._parsers.base import BaseParser

        return BaseParser
    except ImportError:  # pragma: no cover - depends on redis-py internals
        return mod


def run_commands(mod, job: dict) -> dict:
    client = _client(mod, job["port"], job["protocol"])
    results = []
    try:
        for encoded in job["commands"]:
            args = decode_args(encoded)
            try:
                results.append(encode_value(
                    client.execute_command(*args),
                    code_of=lambda e: error_code(mod, e),
                ))
            except mod.exceptions.RedisError as exc:
                results.append(["exc", error_code(mod, exc)])
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "results": results}


def run_probe(mod, job: dict) -> dict:
    """Measure what redis-py actually does with each RESP3 type."""
    client = _client(mod, job["port"], 3)
    observed: dict[str, str] = {}
    try:
        for name in PROBE_TYPES:
            try:
                value = client.execute_command("DEBUG", "PROTOCOL", name)
            except mod.exceptions.RedisError as exc:
                observed[name] = f"raises:{type(exc).__name__}"
                continue
            except Exception as exc:  # noqa: BLE001
                observed[name] = f"raises:{type(exc).__name__}"
                continue
            observed[name] = type(value).__name__
            if name == "verbatim":
                observed["verbatim_value"] = value.decode("utf-8", "replace")
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "observed": observed, "version": mod.__version__}


def main() -> int:
    job = json.loads(sys.stdin.read())
    try:
        import redis as mod
    except ImportError:
        json.dump({"ok": False, "error": "redis-py is not installed in the oracle "
                                         "interpreter"}, sys.stdout)
        return 1
    try:
        if job["op"] == "probe":
            payload = run_probe(mod, job)
        else:
            payload = run_commands(mod, job)
    except Exception as exc:  # noqa: BLE001
        payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    json.dump(payload, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
