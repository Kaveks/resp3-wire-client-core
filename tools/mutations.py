"""The mutation catalogue.

Step 7 of the build order: every property a channel claims to test gets a
mutation that breaks exactly that property. A mutation is applied to a copy of
the reference implementation, never to the reference itself, and the harness is
then run against the copy.

Two outcomes are defects in the harness rather than in the mutation:

  a mutation that fails nothing      the channel does not test what it claims
  a mutation that fails everything   the cases are not independent

Neither is fixed by weakening the mutation. A property that turns out to be
untested is reported as untested.

Each entry names the property in one line, the channels the contracts say
enforce it, and the exact source edits that break it. `aims` is what the
contracts claim, not what was observed; `tools/mutate.py` reports the
difference between the two, which is the whole point of the exercise.

An edit is (relative path under the package, exact old text, new text). Every
old text must match exactly once, which `tools/mutate.py --check` verifies
without running anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["Mutation", "MUTATIONS", "by_name"]

# The chunking channel's invariance cases compare a partitioned feed against a
# whole-buffer feed of the same parser, so on their own they cannot observe a
# defect that is consistent across split schedules. D20 added eight
# absolute-expectation cases for exactly that reason, so a value mapping the
# absolute cases assert is aimed at this channel as well as at the oracle. A
# mapping no absolute case covers, such as a double or a set, is aimed at the
# oracle alone.

ORACLE = "oracle"
CACHING = "caching"
CHUNKING = "chunking"
POOL = "pool"
RESOURCE = "resource"


@dataclass(frozen=True)
class Mutation:
    """One deliberate defect, aimed at one property."""

    name: str
    prop: str
    aims: tuple[str, ...]
    edits: tuple[tuple[str, str, str], ...]
    note: str = ""
    #: Mutations whose cost is dominated by the defect itself rather than by
    #: the harness. Quadratic parsing at 64 MB is minutes, not seconds.
    slow: bool = False


def _m(name: str, prop: str, aims, edits, note: str = "", slow: bool = False) -> Mutation:
    return Mutation(name, prop, tuple(aims), tuple(edits), note, slow)


MUTATIONS: list[Mutation] = []


def add(*args, **kwargs) -> None:
    MUTATIONS.append(_m(*args, **kwargs))


# ===========================================================================
# Parser: resumption state
# ===========================================================================

add(
    "parser-resumption-stack-dropped",
    "an unclosed aggregate survives a chunk boundary",
    [CHUNKING],
    [(
        "parser.py",
        """            result = self._step()
            if result is NEED_MORE:
                return NEED_MORE""",
        """            result = self._step()
            if result is NEED_MORE:
                self._stack.clear()
                return NEED_MORE""",
    )],
    note="docs/PROTOCOL.md section 8. The frame stack is the resumption state.",
)

# ===========================================================================
# Parser: attributes
# ===========================================================================

add(
    "attributes-dropped",
    "a value decorated by an attribute frame comes back as Attributed",
    [CHUNKING],
    [
        (
            "parser.py",
            """            top = stack[-1]
            if top.attrs is not None:
                value = Attributed(value, top.attrs)
                top.attrs = None""",
            """            top = stack[-1]
            if top.attrs is not None:
                top.attrs = None""",
        ),
        (
            "parser.py",
            """                if self._root_attrs is not None:
                    value = Attributed(value, self._root_attrs)
                    self._root_attrs = None""",
            """                if self._root_attrs is not None:
                    self._root_attrs = None""",
        ),
    ],
    note="docs/PROTOCOL.md section 5. D11 makes chunking the sole enforcement.",
)

add(
    "attributes-parked-at-the-root",
    "an attribute decorates the value that follows it at the same depth",
    [CHUNKING],
    [(
        "parser.py",
        """        if self._stack:
            top = self._stack[-1]
            if top.attrs is None:""",
        """        if False:
            top = self._stack[-1]
            if top.attrs is None:""",
    )],
    note="Attributes still appear, on the wrong value. Placement, not presence.",
)

add(
    "attributes-do-not-merge",
    "consecutive attribute frames merge into one dictionary, later keys winning",
    [CHUNKING],
    [
        (
            "parser.py",
            """            if top.attrs is None:
                top.attrs = attrs
            else:
                top.attrs.update(attrs)""",
            """            if top.attrs is None:
                top.attrs = attrs""",
        ),
        (
            "parser.py",
            """        elif self._root_attrs is None:
            self._root_attrs = attrs
        else:
            self._root_attrs.update(attrs)""",
            """        elif self._root_attrs is None:
            self._root_attrs = attrs""",
        ),
    ],
    note="docs/PROTOCOL.md section 5, the merge rule.",
)

add(
    "attribute-dict-emitted-as-a-value",
    "an attribute dictionary is never emitted as a standalone reply",
    [CHUNKING],
    [(
        "parser.py",
        """            stack.pop()
            if top.kind == _ATTR:
                # An attribute frame is not a reply. It becomes metadata for
                # the next value at the depth it was read at, which is now the
                # depth of whatever frame the pop exposed.
                self._merge_attrs(_build_dict(top.items))
                return _CONSUMED""",
        """            stack.pop()
            if top.kind == _ATTR:
                value = _build_dict(top.items)
                continue""",
    )],
    note="docs/PROTOCOL.md section 5 calls this the most commonly failed requirement.",
)

# ===========================================================================
# Parser: verbatim strings
# ===========================================================================

add(
    "verbatim-format-discarded",
    "a `=` frame produces VerbatimBytes carrying its three character format",
    [ORACLE, CHUNKING],
    [(
        "parser.py",
        """            value = VerbatimBytes(
                bytes(memoryview(self._buf)[start + 4 : end]), format=fmt
            )""",
        """            value = bytes(memoryview(self._buf)[start + 4 : end])""",
    )],
    note="docs/PROTOCOL.md section 4.3.",
)

add(
    "verbatim-prefix-not-stripped",
    "the format prefix and its colon are stripped from a verbatim payload",
    [ORACLE, CHUNKING],
    [(
        "parser.py",
        """            value = VerbatimBytes(
                bytes(memoryview(self._buf)[start + 4 : end]), format=fmt
            )""",
        """            value = VerbatimBytes(
                bytes(memoryview(self._buf)[start:end]), format=fmt
            )""",
    )],
)

# ===========================================================================
# Parser: the three null forms
# ===========================================================================

add(
    "null-resp3-underscore",
    "`_\\r\\n` produces None",
    [ORACLE, CHUNKING],
    [(
        "parser.py",
        """            if body:
                raise ProtocolError(f"null frame carries a payload: {body!r}")
            return self._emit(None)""",
        """            if body:
                raise ProtocolError(f"null frame carries a payload: {body!r}")
            return self._emit(b"")""",
    )],
    note="docs/PROTOCOL.md section 4.6, the RESP3 form.",
)

add(
    "null-bulk-string",
    "`$-1\\r\\n` produces None",
    [ORACLE, CHUNKING],
    [(
        "parser.py",
        """            if n < 0:
                return self._emit(None)  # RESP2 null bulk""",
        """            if n < 0:
                return self._emit(b"")  # RESP2 null bulk""",
    )],
    note="docs/PROTOCOL.md section 4.6, the RESP2 bulk form.",
)

add(
    "null-array",
    "`*-1\\r\\n` produces None",
    [ORACLE, CHUNKING],
    [(
        "parser.py",
        """            if n < 0:
                return self._emit(None)  # RESP2 null array""",
        """            if n < 0:
                return self._emit([])  # RESP2 null array""",
    )],
    note="docs/PROTOCOL.md section 4.6, the RESP2 array form.",
)

# ===========================================================================
# Parser: remaining wire types
# ===========================================================================

add(
    "blob-error-truncated-at-crlf",
    "a `!` payload may contain CR or LF, since its length is declared",
    [CHUNKING],
    [(
        "parser.py",
        """        else:  # _BANG
            value = _error_reply(bytes(memoryview(self._buf)[start:end]))""",
        """        else:  # _BANG
            payload = bytes(memoryview(self._buf)[start:end])
            value = _error_reply(payload.split(b"\\r\\n")[0])""",
    )],
    note="docs/PROTOCOL.md section 4.7. The frame is still consumed whole, so "
         "the stream stays aligned; only the message text is truncated.",
)

add(
    "push-frame-parsed-as-a-list",
    "a `>` frame produces PushMessage, not an ordinary array",
    [CHUNKING],
    [(
        "parser.py",
        """    head = items[0]
    if not isinstance(head, (bytes, bytearray)):
        raise ProtocolError("push frame kind is not a string")
    return PushMessage(head.decode("utf-8", "surrogateescape"), items[1:])""",
        """    return items""",
    )],
    note="docs/PROTOCOL.md section 4.8, D7.",
)

add(
    "boolean-as-integer",
    "`#t` and `#f` produce bool, not 1 and 0",
    [ORACLE, CHUNKING],
    [(
        "parser.py",
        """            if body == b"t":
                return self._emit(True)
            if body == b"f":
                return self._emit(False)""",
        """            if body == b"t":
                return self._emit(1)
            if body == b"f":
                return self._emit(0)""",
    )],
    note="docs/HARNESS.md section 2.1 names this as the reason the comparator "
         "classifies BOOL before INT.",
)

add(
    "double-as-bytes",
    "`,` produces float, and the wire type decides the Python type",
    [ORACLE],
    [(
        "parser.py",
        """        if marker == _COMMA:
            return self._emit(_to_float(body))""",
        """        if marker == _COMMA:
            return self._emit(bytes(body))""",
    )],
)

add(
    "big-number-as-bytes",
    "`(` produces an arbitrary precision int",
    [ORACLE, CHUNKING],
    [(
        "parser.py",
        """        if marker == _COLON or marker == _LPAREN:
            return self._emit(_to_int(body))""",
        """        if marker == _COLON:
            return self._emit(_to_int(body))
        if marker == _LPAREN:
            return self._emit(bytes(body))""",
    )],
    note="docs/PROTOCOL.md section 4.2.",
)

add(
    "set-as-list",
    "`~` produces a Python set",
    [ORACLE],
    [(
        "parser.py",
        """    if kind == _SET:
        try:
            return set(items)
        except TypeError:
            # Documented degradation: an unhashable member makes the whole
            # frame a list preserving wire order, rather than an error.
            return items""",
        """    if kind == _SET:
        return items""",
    )],
    note="docs/PROTOCOL.md section 4.5, and the D11 direct type assertion.",
)

add(
    "map-as-flat-list",
    "`%` produces a dict built from consecutive pairs",
    [ORACLE],
    [(
        "parser.py",
        """    if kind == _MAP:
        return _build_dict(items)""",
        """    if kind == _MAP:
        return items""",
    )],
)

add(
    "bulk-strings-decoded-to-str",
    "no value is decoded; string-ish values are bytes",
    [ORACLE],
    [(
        "parser.py",
        """            value: Any = bytes(memoryview(self._buf)[start:end])""",
        """            value: Any = bytes(
                memoryview(self._buf)[start:end]
            ).decode("utf-8", "surrogateescape")""",
    )],
    note="D1. Included to test independence: a defect this broad should still "
         "leave the channels that do not read bulk strings alone.",
)

# ===========================================================================
# Connection: push discarding and negotiation
# ===========================================================================

add(
    "pushes-not-discarded",
    "execute discards a push frame and keeps reading for its own reply",
    [ORACLE],
    [(
        "connection.py",
        """            if isinstance(value, PushMessage):
                self._handle_push(value)
                continue
            return value""",
        """            if isinstance(value, PushMessage):
                self._handle_push(value)
            return value""",
    )],
    note="docs/API.md section 4.3.",
)

add(
    "negotiation-does-not-fall-back",
    "a ServerError reply to HELLO falls back to RESP2 rather than raising",
    [ORACLE],
    [(
        "connection.py",
        """            if isinstance(unwrap(reply), ErrorReply):
                # The server does not speak HELLO, or refuses version 3.
                self._protocol_version = 2
                self._server_info = {}""",
        """            if isinstance(unwrap(reply), ErrorReply):
                failure = unwrap(reply)
                raise exception_for(failure.code, failure.message)""",
    )],
    note="docs/API.md section 5, the second of the three outcomes.",
)

add(
    "hello-sent-under-protocol-2",
    "no HELLO is sent at all when protocol is 2",
    [ORACLE],
    [
        ("connection.py", """        if self._protocol == 3:
            args: list[object] = ["HELLO", "3"]""",
         """        if True:
            args: list[object] = ["HELLO", str(self._protocol)]"""),
        ("connection.py", """                self._protocol_version = 3
                self._server_info = _as_info_dict(reply)""",
         """                self._protocol_version = self._protocol
                self._server_info = _as_info_dict(reply)"""),
    ],
    note="The other half of negotiation: docs/API.md section 5's final clause.",
)

# ===========================================================================
# Connection: poisoning, one mutation per trigger
# ===========================================================================

add(
    "poisoning-connection-error",
    "a ConnectionError during use poisons the connection",
    [POOL],
    [
        ("connection.py", """        except OSError as exc:
            self._poisoned = True
            raise ConnectionError(f"failed reading a reply: {exc}") from exc
        if not data:""",
         """        except OSError as exc:
            raise ConnectionError(f"failed reading a reply: {exc}") from exc
        if not data:"""),
        # "server closed the connection" appears in both the blocking read and
        # the non-blocking drain, so the anchor carries the line above it.
        ("connection.py", """            raise ConnectionError(f"failed reading a reply: {exc}") from exc
        if not data:
            self._poisoned = True
            raise ConnectionError("server closed the connection")""",
         """            raise ConnectionError(f"failed reading a reply: {exc}") from exc
        if not data:
            raise ConnectionError("server closed the connection")"""),
        ("connection.py", """        except OSError as exc:
            self._poisoned = True
            raise ConnectionError(f"failed writing a command: {exc}") from exc""",
         """        except OSError as exc:
            raise ConnectionError(f"failed writing a command: {exc}") from exc"""),
    ],
    note="docs/API.md section 6.3, first trigger.",
)

add(
    "poisoning-timeout",
    "a TimeoutError during use poisons the connection",
    [POOL],
    [
        ("connection.py", """        except socket.timeout as exc:
            self._poisoned = True
            raise TimeoutError("timed out waiting for a reply") from exc""",
         """        except socket.timeout as exc:
            raise TimeoutError("timed out waiting for a reply") from exc"""),
        ("connection.py", """        except socket.timeout as exc:
            self._poisoned = True
            raise TimeoutError("timed out writing a command") from exc""",
         """        except socket.timeout as exc:
            raise TimeoutError("timed out writing a command") from exc"""),
    ],
    note="docs/API.md section 6.3, second trigger. The case that matters most.",
)

add(
    "poisoning-protocol-error",
    "a ProtocolError during use poisons the connection",
    [POOL],
    [(
        "connection.py",
        """            except ProtocolError:
                self._poisoned = True
                raise
            if value is NEED_MORE:
                self._parser.feed(self._recv())""",
        """            except ProtocolError:
                raise
            if value is NEED_MORE:
                self._parser.feed(self._recv())""",
    )],
    note="docs/API.md section 6.3, third trigger. docs/HARNESS.md section 5.4 "
         "records that this trigger has no public induction path and that its "
         "case was reallocated, so the expected result is that nothing fails.",
)

add(
    "poisoned-connection-still-executes",
    "a poisoned connection raises ConnectionError on any further execute",
    [POOL],
    [
        ("connection.py", """        if self._poisoned:
            raise ConnectionError(
                "connection is poisoned; its stream position is unknown"
            )
        payload = _encode_command(args)""",
         """        payload = _encode_command(args)"""),
        ("pipeline.py", """        if self._conn.is_poisoned:
            raise ConnectionError(
                "connection is poisoned; its stream position is unknown"
            )""",
         """        if False:
            raise ConnectionError("unreachable")"""),
    ],
    note="docs/API.md section 6.3, final clause.",
)

# ===========================================================================
# Errors: one mutation per mapping
# ===========================================================================

for _code, _cls in (
    ("WRONGTYPE", "WrongTypeError"),
    ("MOVED", "MovedError"),
    ("NOSCRIPT", "NoScriptError"),
    ("BUSYGROUP", "BusyGroupError"),
):
    add(
        f"error-mapping-{_code.lower()}",
        f"the {_code} code maps to {_cls}",
        [ORACLE],
        [("errors.py", f'    "{_code}": {_cls},', f'    "{_code}": ServerError,')],
        note="docs/PROTOCOL.md section 6.",
    )

add(
    "moved-slot-and-address-not-parsed",
    "MovedError exposes the slot and address parsed from its error text",
    [ORACLE],
    [(
        "errors.py",
        """        parts = message.split()
        if len(parts) < 3:
            return -1, ""
        try:
            slot = int(parts[1])
        except ValueError:
            return -1, ""
        return slot, parts[2]""",
        """        return -1, \"\"""",
    )],
    note="docs/API.md section 3, and D15 for the degraded form.",
)

add(
    "timeout-error-outside-connection-error",
    "TimeoutError sits under ConnectionError in the hierarchy",
    [POOL],
    [(
        "errors.py",
        """class TimeoutError(ConnectionError):""",
        """class TimeoutError(RedisError):""",
    )],
    note="docs/API.md section 3.",
)

# ===========================================================================
# Pool
# ===========================================================================

add(
    "pool-capacity-unbounded",
    "the pool grows to max_connections and no further",
    [POOL],
    [(
        "pool.py",
        """                elif self._total_locked() + self._reserved < self._max_connections:""",
        """                elif True:""",
    )],
    note="docs/API.md section 6.1.",
)

add(
    "pool-serialised-to-one-borrower",
    "pool locking does not serialise command execution",
    [POOL],
    [(
        "pool.py",
        """                if self._idle:
                    reuse, last_used = self._idle.popleft()""",
        """                if self._in_use:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(
                            "serialised pool: one borrower at a time"
                        )
                    self._cond.wait(remaining)
                    continue
                if self._idle:
                    reuse, last_used = self._idle.popleft()""",
    )],
    note="docs/API.md section 6.4. This is the exploit CLAUDE.md names: "
         "serialising every pool operation behind a single lock.",
)

add(
    "pool-health-check-skipped",
    "an idle connection is health checked before being handed out",
    [POOL],
    [(
        "pool.py",
        """        interval = self._health_check_interval
        if interval <= 0 or time.monotonic() - last_used < interval:
            return False""",
        """        return False
        interval = self._health_check_interval""",
    )],
    note="docs/API.md section 6.1.",
)

add(
    "pool-release-accepts-anything",
    "releasing a foreign or already returned connection raises ValueError",
    [POOL],
    [
        ("pool.py", """            if conn not in self._owned:
                raise ValueError("connection was not issued by this pool")""",
         """            if False:
                raise ValueError("connection was not issued by this pool")"""),
        ("pool.py", """            elif conn not in self._in_use:
                raise ValueError("connection is not currently borrowed from this pool")""",
         """            elif False:
                raise ValueError("connection is not currently borrowed from this pool")"""),
    ],
    note="docs/API.md section 6.1 and D16.",
)

add(
    "pool-close-leaves-connections-open",
    "close closes every connection, idle and in use",
    [POOL],
    [(
        "pool.py",
        """        for conn in doomed:
            conn.close()""",
        """        for conn in doomed:
            pass""",
    )],
)

add(
    "pool-release-does-not-wake-waiters",
    "a blocked acquire is woken by a release, not only by its own timeout",
    [POOL],
    [(
        "pool.py",
        """                    self._idle.append((conn, time.monotonic()))
            self._cond.notify()""",
        """                    self._idle.append((conn, time.monotonic()))""",
    )],
)

# ===========================================================================
# Pipeline
# ===========================================================================

add(
    "pipeline-results-reversed",
    "execute returns replies in the order the commands were queued",
    [ORACLE],
    [(
        "pipeline.py",
        """            else:
                results.append(reply)
        return results""",
        """            else:
                results.append(reply)
        return results[::-1]""",
    )],
    note="docs/API.md section 7.1.",
)

add(
    "pipeline-writes-and-reads-one-at-a-time",
    "no reply is read until every queued command has been written",
    [ORACLE],
    [(
        "pipeline.py",
        """        self._conn._send(b"".join(commands))

        results: list[object] = []
        for _ in range(len(commands)):
            reply = self._conn._read_reply()""",
        """        results: list[object] = []
        for command in commands:
            self._conn._send(command)
            reply = self._conn._read_reply()""",
    )],
    note="docs/API.md section 7.1 states the syscall count is unobservable and "
         "unverified. This mutation measures whether that admission is honest.",
)

add(
    "pipeline-slot-keeps-errorreply",
    "a per command server error occupies its slot as an exception instance",
    [ORACLE],
    [(
        "pipeline.py",
        """            if isinstance(error, ErrorReply):
                results.append(exception_for(error.code, error.message))""",
        """            if isinstance(error, ErrorReply):
                results.append(reply)""",
    )],
    note="docs/API.md section 7.2, the pipeline half of the asymmetry.",
)

add(
    "pipeline-converts-nested-errors",
    "an error nested inside EXEC's array stays an ErrorReply value",
    [ORACLE],
    [(
        "pipeline.py",
        """            error = unwrap(reply)
            if isinstance(error, ErrorReply):
                results.append(exception_for(error.code, error.message))
            else:
                results.append(reply)""",
        """            def _convert(item):
                inner = unwrap(item)
                if isinstance(inner, ErrorReply):
                    return exception_for(inner.code, inner.message)
                if type(inner) is list:
                    return [_convert(element) for element in inner]
                return item

            results.append(_convert(reply))""",
    )],
    note="docs/API.md section 7.2 and D2, the nested half of the asymmetry.",
)

# ===========================================================================
# Resource behaviour
# ===========================================================================

add(
    "reset-keeps-the-buffer",
    "reset discards buffered state and releases the memory backing it",
    [RESOURCE],
    [(
        "parser.py",
        """        self._buf = bytearray()
        self._pos = 0
        self._scan_from = 0
        self._stack.clear()""",
        """        self._stack.clear()""",
    )],
    note="docs/HARNESS.md section 6.2, the reset case.",
)

add(
    "consumed-input-never-released",
    "input the parser has finished with is released, not carried forward",
    [RESOURCE],
    [
        (
            "parser.py",
            """        pos = self._pos
        if pos:
            if pos == len(self._buf):
                # Fully drained. Rebinding releases the allocation outright
                # instead of carrying a dead prefix forward.
                self._buf = bytearray()
                self._pos = 0
                self._scan_from = 0
            elif pos >= _COMPACT_THRESHOLD and pos * 2 >= len(self._buf):
                del self._buf[:pos]
                self._scan_from -= pos
                self._pos = 0
        if data:""",
            """        if data:""",
        ),
        (
            "parser.py",
            """        if self._pos and self._pos == len(self._buf):
            self._buf = bytearray()
            self._pos = 0
            self._scan_from = 0""",
            """        return""",
        ),
    ],
    note="docs/HARNESS.md section 6.2, the drained-buffer case and the 10,000 "
         "small replies case. Both release paths are removed together because "
         "either one alone keeps the property true, so neutering one would "
         "measure the other rather than the property.",
)

add(
    "blob-payload-copied-three-times",
    "peak overhead while materialising one large reply stays under 3x",
    [RESOURCE],
    [(
        "parser.py",
        """            value: Any = bytes(memoryview(self._buf)[start:end])""",
        """            value: Any = bytes(bytearray(self._buf[start:end]))""",
    )],
    note="docs/HARNESS.md section 6.1. The memoryview in the reference exists "
         "precisely to avoid the intermediate copies this reintroduces.",
)

add(
    "crlf-rescanned-periodically",
    "parsing cost is linear in the bytes fed, not in the chunks they arrived in",
    [RESOURCE],
    [
        (
            "parser.py",
            '''    __slots__ = ("_buf", "_pos", "_scan_from", "_stack", "_pending", "_root_attrs")''',
            '''    __slots__ = ("_buf", "_pos", "_scan_from", "_stack", "_pending",
                 "_root_attrs", "_feeds")''',
        ),
        (
            "parser.py",
            """        self._buf = bytearray()
        self._pos = 0
        # How far the search for a line terminator has already looked. Always
        # at or ahead of _pos, which compaction relies on.""",
            """        self._buf = bytearray()
        self._pos = 0
        self._feeds = 0
        # How far the search for a line terminator has already looked. Always
        # at or ahead of _pos, which compaction relies on.""",
        ),
        (
            "parser.py",
            """        if len(self._buf) < end + 2:
            return NEED_MORE""",
            """        if len(self._buf) < end + 2:
            self._feeds += 1
            if self._feeds % 16 == 0:
                # Re-scan the whole buffered payload for the terminator rather
                # than trusting the declared length.
                self._buf.find(_CRLF, start)
            return NEED_MORE""",
        ),
    ],
    note="D14, and the third of the four quadratic failure modes the reference "
         "parser's docstring enumerates: rescanning for CRLF on every feed. "
         "The rescan is throttled to one feed in sixteen. Unthrottled it is "
         "genuinely quadratic, which costs about seven minutes for a single "
         "64 MB trial and the channel takes ten of them; throttled it still "
         "grows about 47x across the 64x size range, well past the 8.0 bound, "
         "so the channel is being asked to catch a defect far milder than the "
         "one it was designed for.",
    slow=True,
)

add(
    "aggregate-closing-made-recursive",
    "a deeply nested reply completes without RecursionError",
    [RESOURCE],
    [(
        "parser.py",
        """            value = _finalize(top)""",
        """            return self._emit(_finalize(top))""",
    )],
    note="docs/HARNESS.md section 6.2, the depth 100 case.",
)

# ===========================================================================
# Caching and invalidation. D33's channel.
#
# The first of these is the defect docs/API.md section 7A.5 names in so many
# words: caching the value and processing the pending invalidation afterwards.
# It is the reason the channel exists, and a caching mutation that fails nothing
# is a defect in the channel rather than in the mutation.
# ===========================================================================

add(
    "cache-stores-before-checking-for-invalidation",
    "a reply is cached only if no invalidation for its key arrived while it was read",
    [CACHING],
    [(
        "connection.py",
        """            self.drain_invalidations()
            cache.offer(encoded, key, reply, generation)""",
        """            cache.offer(encoded, key, reply, generation)
            self.drain_invalidations()""",
    )],
    note="docs/API.md section 7A.5. MEASURED: this fails nothing, and the reason "
         "is worth keeping. The reordering still drains before `execute` "
         "returns, so the stale entry exists only between the store and the "
         "drain inside one call. That window is real and another thread can in "
         "principle read through it, but it is microseconds wide and observing "
         "it reliably would need instrumentation of internals, which "
         "docs/HARNESS.md section 8.2 forbids. The observable form of the same "
         "property is cache-serves-hits-without-draining-first below, which "
         "fails. This entry is kept rather than deleted because a mutation that "
         "cannot be caught is worth recording as such.",
)

add(
    "cache-ignores-the-generation-it-recorded",
    "an invalidation arriving during the read refuses the offer",
    [CACHING],
    [(
        "cache.py",
        """            if (self._epoch, self._generation.get(key, 0)) != generation:
                return False""",
        """            if False:
                return False""",
    )],
    note="The generation check is the whole of the pre-cache race defence.",
)

add(
    "cache-sweeps-only-idle-connections",
    "invalidations are consumed from every connection, not only unborrowed ones",
    [CACHING],
    [(
        "pool.py",
        """            peers = [c for c in self._owned if c is not borrower]""",
        """            idle = {c for c, _ in self._idle}
            peers = [c for c in self._owned if c is not borrower and c in idle]""",
    )],
    note="D34. The connection holding an invalidation is often one a worker is "
         "holding between commands. This is the defect the channel's "
         "worker-holding-a-connection case was written for, and the reference "
         "had it until that case failed.",
)

add(
    "cache-never-sweeps-peers",
    "a hit is served only after pending invalidations are consumed pool-wide",
    [CACHING],
    [(
        "connection.py",
        """            if self._predrain is not None:
                self._predrain(self)
            self.drain_invalidations()""",
        """            self.drain_invalidations()""",
    )],
    note="Leaves the per-connection defence intact and removes the pool-wide one, "
         "so only the cases that cross connections should notice.",
)

add(
    "cache-serves-hits-without-draining-first",
    "pending invalidations are consumed before a cached value is served",
    [CACHING],
    [(
        "connection.py",
        """            if self._predrain is not None:
                self._predrain(self)
            self.drain_invalidations()
            hit = cache.get(encoded)""",
        """            hit = cache.get(encoded)""",
    )],
    note="The observable form of the property above. An invalidation that has "
         "arrived but not been parsed is exactly as stale as one that has not "
         "arrived, and serving a hit without accounting for it is what "
         "docs/API.md section 7A.5 forbids.",
)

add(
    "invalidation-frame-ignored",
    "an `invalidate` push frame evicts the keys it names",
    [CACHING],
    [(
        "connection.py",
        """        if self._cache is not None and message.kind == "invalidate":""",
        """        if False:""",
    )],
    note="docs/API.md section 7A.4. The frame is discarded like any other push, "
         "which also means pushes_discarded counts it.",
)

add(
    "flush-invalidation-drops-nothing",
    "a null key array means drop everything",
    [CACHING],
    [(
        "cache.py",
        """            dropped = len(self._entries)
            self._entries.clear()
            self._by_key.clear()
            self._epoch += 1
            return dropped""",
        """            return 0""",
    )],
    note="docs/API.md section 7A.4, the FLUSHALL and table-overflow form.",
)

add(
    "cache-tracking-never-enabled",
    "each connection issues CLIENT TRACKING ON after negotiation",
    [CACHING],
    [(
        "connection.py",
        """            self._roundtrip(("CLIENT", "TRACKING", "ON"))""",
        """            pass""",
    )],
    note="docs/API.md section 7A.3. Without tracking the server sends no "
         "invalidation at all, so every cached value is stale the moment it is "
         "written by anyone.",
)

add(
    "cache-ignores-its-bound",
    "the cache is bounded at cache_size entries",
    [CACHING],
    [(
        "cache.py",
        """            if command not in self._entries and len(self._entries) >= self._max:
                oldest = next(iter(self._entries))
                self._forget_locked(oldest)""",
        """            pass""",
    )],
)

add(
    "cache-accepts-protocol-2",
    "cache_size > 0 with protocol=2 raises ValueError",
    [CACHING],
    [(
        "pool.py",
        """        if cache_size and protocol != 3:""",
        """        if False:""",
    )],
    note="docs/API.md section 7A.3. Under RESP2 there is no channel for an "
         "invalidation, so such a cache can only ever be wrong.",
)

add(
    "cache-serves-pipelined-reads",
    "a pipelined read is neither served from cache nor populates it",
    [CACHING],
    [(
        "pipeline.py",
        """        results: list[object] = []
        for _ in range(len(commands)):""",
        """        cache = getattr(self._conn, "_cache", None)
        if cache is not None:
            served = []
            for command in commands:
                cached = cache.get(command)
                if cached is not None and type(cached).__name__ != "_Miss":
                    served.append(cached)
            if len(served) == len(commands):
                self._commands = []
                return served
        results: list[object] = []
        for _ in range(len(commands)):""",
    )],
    note="docs/API.md section 7A.2. Reading through the cache from a pipeline "
         "counts the hit, which is what the scope case observes.",
)

add(
    "cache-clear-resets-the-counters",
    "cache_clear drops entries and leaves the counters alone",
    [CACHING],
    [(
        "cache.py",
        """        with self._lock:
            self._entries.clear()
            self._by_key.clear()

    def stats(self)""",
        """        with self._lock:
            self._entries.clear()
            self._by_key.clear()
            self._hits = 0
            self._misses = 0
            self._invalidations = 0

    def stats(self)""",
    )],
    note="docs/API.md section 7A.1: counters are monotonic and reset only on close.",
)


# ===========================================================================
# The sans-io gate, which is a precondition rather than a channel
# ===========================================================================

add(
    "parser-imports-socket",
    "the parser imports nothing from socket, select, asyncio, ssl, subprocess",
    [ORACLE, CHUNKING, POOL, RESOURCE],
    [(
        "parser.py",
        """from typing import Any, Final""",
        """import socket  # noqa: F401

from typing import Any, Final""",
    )],
    note="docs/HARNESS.md section 7.2. Failing every channel is the specified "
         "behaviour here, not a sign that the cases are coupled: run.py scores "
         "a sans-io violation as zero across the board without running pytest.",
)


# ===========================================================================
# Properties a channel names as a case of its own
# ===========================================================================

add(
    "attributed-hash-not-delegating",
    "Attributed hashes as its wrapped value, so it is interchangeable as a "
    "set member and a dict key",
    [CHUNKING],
    [(
        "protocol.py",
        """        # Deliberately not guarded. An unhashable wrapped value must make the
        # wrapper unhashable, with the TypeError the bare value would raise.
        return hash(self.value)""",
        """        return object.__hash__(self)""",
    )],
    note="docs/HARNESS.md section 4.4 reserves a case for this directly.",
)

add(
    "server-info-not-populated",
    "server_info is the parsed HELLO reply as a dict",
    [ORACLE],
    [(
        "connection.py",
        """                self._protocol_version = 3
                self._server_info = _as_info_dict(reply)""",
        """                self._protocol_version = 3
                self._server_info = {}""",
    )],
    note="docs/API.md section 4.1.",
)

add(
    "pool-never-reuses-an-idle-connection",
    "acquire reuses an idle connection when one is available",
    [POOL],
    [(
        "pool.py",
        """                if self._idle:
                    reuse, last_used = self._idle.popleft()""",
        """                if False:
                    reuse, last_used = self._idle.popleft()""",
    )],
    note="docs/API.md section 6.1.",
)

add(
    "pool-health-check-discards-every-connection",
    "a connection that passes its health check is handed back, not replaced",
    [POOL],
    [(
        "pool.py",
        """        try:
            return conn.execute("PING") != b"PONG"
        except RedisError:
            return True""",
        """        return True""",
    )],
    note="docs/API.md section 6.1. The check is meant to discard connections "
         "that fail it, not connections that pass it.",
)

add(
    "replies-post-processed-by-command-name",
    "the wire type decides the Python type, not the command name",
    [ORACLE],
    [(
        "connection.py",
        """        error = unwrap(reply)
        if isinstance(error, ErrorReply):
            raise exception_for(error.code, error.message)""",
        """        if name in (b"SUNION", b"SMEMBERS") and type(reply) is list:
            reply = set(reply)
        error = unwrap(reply)
        if isinstance(error, ErrorReply):
            raise exception_for(error.code, error.message)""",
    )],
    note="docs/HARNESS.md section 3.3. This is the defect the RESP2 SUNION "
         "designation exists to catch: a client that reimplements redis-py's "
         "per command callback table instead of parsing the protocol.",
)


def by_name(name: str) -> Mutation:
    for mutation in MUTATIONS:
        if mutation.name == name:
            return mutation
    raise KeyError(name)
