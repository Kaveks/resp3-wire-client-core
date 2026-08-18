# Protocol representation contract

Status: ratified 2026-08-18. Frozen.
Owner: maintainers. Implementers and harness authors read this; nobody edits it
without ratification.

This document defines exactly how RESP2 and RESP3 wire types map into Python
values, and the interface through which bytes become those values. It is the
authority for three consumers that must agree: `spec/instruction.md`, the
comparator in `harness/support/compare.py`, and `reference/resp3_wire/`.

## 1. Design constraints

Three constraints shape every decision below.

The differential oracle compares values produced by the client against values
produced by redis-py 8.1.0 running against the same server. Any representation
that does not compare equal to redis-py's output creates false failures, so
equality with redis-py is a hard requirement wherever redis-py produces a
value at all.

redis-py discards RESP3 attributes and the verbatim string format prefix. The
oracle therefore cannot see either. Both are verified by sealed tests instead,
which means the representation must carry that information without disturbing
oracle equality.

The parser is sans-io. It never touches a socket, never blocks, and is driven
entirely by byte chunks handed to it. This makes the chunking channel a test of
a specified public interface rather than of implementation internals.

## 2. Value types

Three types are defined by this contract. Everything else is a Python builtin.

### 2.1 Attributed

    class Attributed:
        value: object
        attributes: dict

Wraps a value that arrived decorated by a RESP3 attribute frame.

Equality delegates to the wrapped value:

    Attributed(b"x", {b"k": b"v"}) == b"x"        -> True
    b"x" == Attributed(b"x", {b"k": b"v"})        -> True

`__hash__` delegates to the wrapped value. Where the wrapped value is
unhashable, `hash()` raises `TypeError`, which is observationally identical to
`__hash__ = None`; the latter cannot be made conditional at class definition
time. `__repr__` shows both the value and the
attributes.

Attributes never compare. Two `Attributed` instances wrapping equal values are
equal regardless of their attribute dictionaries. This is deliberate: it is
what allows the oracle to compare against redis-py, which has no attribute
information at all.

Attribute keys and values are parsed by the normal rules in section 4, so an
attribute dictionary is an ordinary `dict` with `bytes` keys.

### 2.2 VerbatimBytes

    class VerbatimBytes(bytes):
        format: str

A `bytes` subclass carrying the three character format prefix from a RESP3
verbatim string. The prefix and its separating colon are stripped from the
payload, so the instance compares equal to the plain bytes redis-py returns.

    VerbatimBytes(b"Some string", format="txt") == b"Some string"   -> True

`format` is a `str`, not `bytes`, because it is always three ASCII characters
and is metadata rather than payload.

### 2.3 ErrorReply

    class ErrorReply:
        code: str
        message: str

Represents a server error occupying a value position inside an aggregate. It is
a value, not an exception, and is never raised by the parser.

`code` is the first whitespace delimited token of the error string, uppercased,
for example `WRONGTYPE`. `message` is the full error string including the code.
Both are `str`, decoded as UTF-8 with surrogate escaping, because error text is
protocol level diagnostic rather than user payload.

This type exists because `EXEC` returns an array in which individual commands
may have failed. Raising on the first such element would make a partially
failed transaction unrepresentable. Section 6 defines when an `ErrorReply`
becomes a raised exception.

### 2.4 PushMessage

    class PushMessage:
        kind: str
        data: list

Represents a RESP3 out of band push frame. `kind` is the first element of the
frame decoded as UTF-8, for example `invalidate` or `message`. `data` is the
remaining elements, parsed by the normal rules.

A push frame is a complete reply in its own right. It is never merged into the
reply of a pending command, and it is never wrapped by an attribute.

Equality compares both fields. Unlike `Attributed`, this type has no delegating
behavior, because redis-py has no equivalent value and no oracle comparison
involves it.

## 3. RESP2 type mapping

    Wire            Example                 Python
    --------------- ----------------------- ---------------------------
    + simple string +OK\r\n                 b"OK"
    - error         -ERR bad\r\n            ErrorReply
    : integer       :42\r\n                 42            (int)
    $ bulk string   $3\r\nabc\r\n           b"abc"
    $ null bulk     $-1\r\n                 None
    $ empty bulk    $0\r\n\r\n              b""
    * array         *2\r\n...               list
    * null array    *-1\r\n                 None
    * empty array   *0\r\n                  []

All string-ish values are `bytes`. Nothing is decoded. This matches redis-py
with `decode_responses=False`, which is the configuration the oracle uses.

Simple strings and bulk strings both produce `bytes` and are indistinguishable
in the result. This is correct and matches redis-py. The distinction is not
recoverable and is not tested.

## 4. RESP3 type mapping

    Wire              Example                        Python
    ----------------- ------------------------------ -------------------------
    + simple string   +OK\r\n                        b"OK"
    - simple error    -ERR bad\r\n                   ErrorReply
    : number          :42\r\n                        42            (int)
    , double          ,3.25\r\n                      3.25          (float)
    , infinity        ,inf\r\n  ,-inf\r\n            float("inf"), float("-inf")
    # boolean         #t\r\n  #f\r\n                 True, False
    _ null            _\r\n                          None
    ( big number      (3492890328409238509\r\n       int           (arbitrary precision)
    $ blob string     $3\r\nabc\r\n                  b"abc"
    = verbatim string =15\r\ntxt:Some string\r\n     VerbatimBytes
    * array           *2\r\n...                      list
    % map             %2\r\n...                      dict
    ~ set             ~3\r\n...                      set
    | attribute       |1\r\n...                      decorates the next value
    ! blob error      !9\r\nERR broke\r\n            ErrorReply
    > push            >2\r\n...                      PushMessage

### 4.1 Doubles

`,inf` and `,-inf` map to `float("inf")` and `float("-inf")`.

`,nan` parses to `float("nan")`. Because `nan != nan`, no command in the oracle
matrix may produce a nan, and the comparator does not define nan equality. Nan
is exercised only by sealed parser level tests that assert `math.isnan` on the
result rather than comparing values.

A double whose wire form has no fractional part, for example `,3\r\n`, still
produces a `float`. The wire type determines the Python type, not the value.

### 4.2 Big numbers

`(` produces a plain `int`. Python integers are arbitrary precision, so no
information is lost, and redis-py also returns `int`. The distinction between
`:` and `(` is not preserved in the result and is not tested.

### 4.3 Verbatim strings

The declared length counts the format prefix, the colon, and the payload. For
`=15\r\ntxt:Some string\r\n` the length 15 covers `txt:` plus the eleven byte
payload.

The parser strips `txt:` and produces `VerbatimBytes(b"Some string",
format="txt")`.

A verbatim string whose declared length is under four bytes, or whose fourth
byte is not a colon, is a protocol error.

### 4.4 Maps

`%` produces a `dict` built from consecutive key and value pairs. Keys are
parsed by the normal rules, so a map with blob string keys produces a dict with
`bytes` keys.

Duplicate keys resolve last write wins.

A key that is unhashable, which can only arise from a nested aggregate in key
position, is a protocol error. Redis does not emit such a frame.

### 4.5 Sets

`~` produces a `set`.

If any element is unhashable, which can arise from a nested array inside a set,
the entire frame produces a `list` instead, preserving wire order. This is
documented degradation rather than an error. Redis does not currently emit such
a frame, so the fallback exists for well-definedness rather than for practical
use.

### 4.6 Nulls

RESP3 defines `_\r\n` as the sole null. Some servers and some command paths
still emit the RESP2 forms `$-1\r\n` and `*-1\r\n` even under RESP3. The parser
accepts all three and produces `None` for each. Being permissive here costs
nothing and avoids a class of spurious failure.

### 4.7 Blob errors

`!` is a length prefixed error. It parses into `ErrorReply` by exactly the same
rules as `-`, including code extraction and UTF-8 decoding with surrogate
escaping. The distinction between the two wire forms carries no actionable
information and is not preserved in the result.

Unlike `-`, a blob error may legally contain CR or LF inside its payload, since
its length is declared. The code is still the first whitespace delimited token.

### 4.8 Push messages

`>` introduces an out of band frame. Redis emits these for client side caching
invalidation, pubsub delivery, and monitor output. They can arrive at any point
in the stream, including between a command being written and its reply
arriving.

The parser produces a `PushMessage` and returns it from `gets` like any other
complete reply. It does not filter, buffer, or reorder push frames, and it has
no knowledge of which frames a caller considers solicited.

A push frame is never empty; a `>0\r\n` frame is a protocol error.

Handling push frames is a client concern, specified in `docs/API.md`. The
client package does not expose pubsub abstractions. The requirement here is
narrower and is about robustness: a client that cannot parse `>` desynchronises
permanently the first time client tracking is enabled on its connection.

## 5. Attribute semantics

An attribute frame `|N\r\n` is followed by `2N` values forming its dictionary,
and then by the value it decorates. The attribute frame is not itself a reply.

The parser must never emit an attribute dictionary as a standalone value. This
is the single most commonly failed requirement in this contract and the sealed
attribute tests target it directly.

Rules:

An attribute decorates the value that immediately follows it, at the same
nesting depth.

Attributes may appear at any depth, including on individual elements inside an
array, a map value, or a set member. An attribute on a set member decorates
that member; because `Attributed` hashes as its wrapped value, set membership
behaves as though the attribute were absent.

Consecutive attribute frames decorating the same value merge into one
dictionary, later keys winning. This case does not arise in practice and is
specified for determinism.

An attribute at the very end of a stream, with no following value, is
incomplete input, not a complete reply. The parser waits for more data.

A decorated value is returned as `Attributed(value, attributes)`. An
undecorated value is returned bare. The parser does not wrap everything.

## 6. Errors: value or exception

The parser always produces `ErrorReply` as a value, from both `-` and `!`
frames. It never raises on a server error.

The client raises. When a command's top level reply is an `ErrorReply`, the
client converts it to an exception and raises it. When an `ErrorReply` appears
nested inside an aggregate, it is returned as a value.

Exception mapping, keyed on `ErrorReply.code`:

    WRONGTYPE   -> WrongTypeError
    MOVED       -> MovedError
    NOSCRIPT    -> NoScriptError
    BUSYGROUP   -> BusyGroupError
    everything else -> RedisError

All four specific types subclass `RedisError`. The full hierarchy, including
connection and timeout errors that do not originate from a server reply, is
specified in `docs/API.md`.

`MovedError` additionally exposes `slot: int` and `address: str` parsed from
the error text, since a MOVED reply is only actionable with those fields.

## 7. Parser interface

The parser is a pure component. It imports nothing from `socket`, `select`,
`asyncio`, or any other I/O module. This is verified structurally by the sealed
harness, not merely asserted here.

    NEED_MORE = <module level sentinel>

    class RespParser:
        def __init__(self) -> None: ...
        def feed(self, data: bytes) -> None: ...
        def gets(self) -> object: ...
        def reset(self) -> None: ...

`feed` appends bytes to the parser's internal buffer. It never blocks, never
raises on incomplete input, and accepts a chunk of any size including empty.

`gets` returns the next complete reply, or the `NEED_MORE` sentinel if the
buffer does not yet contain one. It is called repeatedly until it returns
`NEED_MORE`. A malformed frame raises `ProtocolError`, defined in
`docs/API.md`.

`NEED_MORE` is a unique sentinel object, distinguishable from every legitimate
reply value. It is not `None`, because `None` is a legitimate reply.

`reset` discards all buffered state and returns the parser to its initial
condition. It is used when a connection is discarded mid-reply.

The parser is protocol agnostic. It accepts every type byte from both protocol
versions regardless of what was negotiated. A RESP2 connection will simply
never receive a `,` or `%` frame. Enforcing mode in the parser would add a
failure path with no corresponding benefit.

The parser is not thread safe. A parser instance belongs to exactly one
connection.

## 8. The chunking invariant

For any byte sequence `B` and any partition of `B` into chunks
`c1, c2, ... cn`:

    feeding B whole, then draining
        produces the same sequence of values as
    feeding c1, draining, feeding c2, draining, ... feeding cn, draining

Equality is Python `==` applied elementwise, with `VerbatimBytes.format`
compared additionally, and `Attributed.attributes` compared additionally. Note
that this is stricter than the equality defined in section 2.1: the sealed
chunking channel deliberately reaches past delegating equality to confirm that
metadata survives fragmentation.

This must hold for every partition, including one byte chunks, splits inside a
length prefix, splits inside a CRLF pair, splits inside a verbatim format
prefix, and splits at arbitrary depths of nesting.

## 9. Memory behavior

Holding a parsed value is not a defect. A bulk string of N bytes necessarily
occupies N bytes once parsed.

The requirement is bounded overhead. Peak retained bytes attributable to the
parser, while a single large reply is being consumed, must not exceed a fixed
multiple of the reply payload size. The multiple and the measurement method are
specified in `docs/HARNESS.md`; the measurement uses `tracemalloc`, not process
RSS.

The failure mode this targets is an implementation that retains the raw input
buffer, the accumulated frame, and the finished value simultaneously, or that
repeatedly copies a growing buffer with slicing.

## 10. Open items

These are unresolved and block the sections they touch. Each needs a
maintainer decision before `spec/instruction.md` is written.

None. O1 and O2 were resolved on 2026-08-18 and are folded into sections 2.4,
4.7, and 4.8. O3 is resolved in `docs/API.md` section 4.4.
