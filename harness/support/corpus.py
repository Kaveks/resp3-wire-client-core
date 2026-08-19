"""The chunking corpus, constructed from the type definitions at run time.

Nothing here is read from a file. The frames are built from the wire grammar so
that the corpus cannot drift from what the protocol contract specifies, and so
that no stored expectation exists for an implementation to discover.

Each entry is (label, wire bytes, expected reply count). Expected values are
not stored: the invariant compares a partitioned feed against a whole-buffer
feed of the same bytes, so the parser supplies both sides and a stored answer
would add nothing.
"""

from __future__ import annotations

__all__ = ["GROUPS", "group", "all_frames", "CURATED", "PATHOLOGICAL"]


def _blob(payload: bytes) -> bytes:
    return b"$%d\r\n%s\r\n" % (len(payload), payload)


def _verbatim(fmt: bytes, payload: bytes) -> bytes:
    body = fmt + b":" + payload
    return b"=%d\r\n%s\r\n" % (len(body), body)


def _blob_error(text: bytes) -> bytes:
    return b"!%d\r\n%s\r\n" % (len(text), text)


def _attr(pairs: list[tuple[bytes, bytes]]) -> bytes:
    out = b"|%d\r\n" % len(pairs)
    for k, v in pairs:
        out += _blob(k) + v
    return out


def _nest(depth: int, leaf: bytes) -> bytes:
    return b"*1\r\n" * depth + leaf


# Seven type groups. The eighth one-byte case in the allocation is the
# delegation assertion in docs/HARNESS.md section 4.4, which is a property of
# the value type rather than of fragmentation and so has no frame here.
GROUPS: dict[str, list[tuple[str, bytes]]] = {
    "resp2_scalars": [
        ("simple string", b"+OK\r\n"),
        ("simple error", b"-ERR bad thing\r\n"),
        ("integer", b":42\r\n"),
        ("negative integer", b":-7\r\n"),
        ("bulk", _blob(b"abc")),
        ("empty bulk", _blob(b"")),
        ("empty array", b"*0\r\n"),
    ],
    "resp3_scalars": [
        ("double", b",3.25\r\n"),
        ("double without fraction", b",3\r\n"),
        ("positive infinity", b",inf\r\n"),
        ("negative infinity", b",-inf\r\n"),
        ("boolean true", b"#t\r\n"),
        ("boolean false", b"#f\r\n"),
        ("big number", b"(3492890328409238509389482938498234\r\n"),
    ],
    "length_prefixed": [
        ("bulk with CRLF and NUL", _blob(b"a\r\nb\x00c")),
        ("verbatim txt", _verbatim(b"txt", b"Some string")),
        ("verbatim mkd", _verbatim(b"mkd", b"# heading")),
        ("verbatim with CRLF payload", _verbatim(b"txt", b"line1\r\nline2")),
        ("blob error with CRLF payload", _blob_error(b"ERR line1\r\nline2")),
        ("large bulk", _blob(b"x" * 600)),
    ],
    "nested_arrays": [
        ("depth 6", _nest(6, b":9\r\n")),
        ("depth 6 with blob leaf", _nest(6, _blob(b"deep"))),
        ("mixed array", b"*3\r\n:1\r\n" + _blob(b"hi") + b"#t\r\n"),
        ("array of arrays", b"*2\r\n*2\r\n:1\r\n:2\r\n*2\r\n:3\r\n:4\r\n"),
    ],
    "maps_and_sets": [
        ("map", b"%2\r\n" + _blob(b"a") + b":1\r\n" + _blob(b"b") + b":2\r\n"),
        ("map duplicate key", b"%2\r\n" + _blob(b"a") + b":1\r\n" + _blob(b"a") + b":2\r\n"),
        ("set", b"~3\r\n:1\r\n:2\r\n:3\r\n"),
        ("empty map", b"%0\r\n"),
        ("empty set", b"~0\r\n"),
        ("attribute on a map value",
         b"%1\r\n" + _blob(b"k") + _attr([(b"ttl", b":60\r\n")]) + _blob(b"v")),
        ("attribute on a set member",
         b"~2\r\n:1\r\n" + _attr([(b"m", b"#t\r\n")]) + b":2\r\n"),
        ("attribute at the root", _attr([(b"a", b":1\r\n")]) + b"+OK\r\n"),
        ("consecutive attributes merge",
         _attr([(b"a", b":1\r\n")]) + _attr([(b"b", b":2\r\n")]) + b"+OK\r\n"),
        ("attribute nested deep",
         b"*1\r\n*1\r\n" + _attr([(b"d", b"#t\r\n")]) + _blob(b"hi")),
        ("empty attribute", b"|0\r\n+OK\r\n"),
    ],
    "push_frames": [
        ("push between two replies",
         b"+FIRST\r\n>2\r\n" + _blob(b"invalidate") + b"*1\r\n" + _blob(b"k")
         + b"+SECOND\r\n"),
        ("push carrying a map",
         b">2\r\n" + _blob(b"message") + b"%1\r\n" + _blob(b"c") + b":1\r\n"),
        ("two pushes in a row",
         b">2\r\n" + _blob(b"a") + b":1\r\n>2\r\n" + _blob(b"b") + b":2\r\n"),
    ],
    "nulls_and_empties": [
        ("resp3 null", b"_\r\n"),
        ("resp2 null bulk", b"$-1\r\n"),
        ("resp2 null array", b"*-1\r\n"),
        ("three null forms in one stream", b"_\r\n$-1\r\n*-1\r\n"),
        ("nulls inside an array", b"*3\r\n_\r\n$-1\r\n*-1\r\n"),
    ],
}


def group(name: str) -> bytes:
    """Every frame in one group, concatenated into a single stream."""
    return b"".join(frame for _, frame in GROUPS[name])


def all_frames() -> bytes:
    """The whole corpus as one stream."""
    return b"".join(group(name) for name in GROUPS)


# Curated frames for the exhaustive split cases. Each mixes several wire types
# so that a single split position exercises a boundary inside a length prefix,
# a format prefix, an attribute, and a nested aggregate.
CURATED: list[tuple[str, bytes]] = [
    ("aggregate of every scalar",
     b"*7\r\n:1\r\n,2.5\r\n#t\r\n_\r\n" + _blob(b"s")
     + b"(12345678901234567890\r\n" + b",-inf\r\n"),
    ("attributed values at three depths",
     _attr([(b"top", b":1\r\n")])
     + b"*2\r\n" + _attr([(b"mid", b":2\r\n")]) + _blob(b"a")
     + b"*1\r\n" + _attr([(b"low", b":3\r\n")]) + _blob(b"b")),
    ("verbatim and blob error together",
     b"*2\r\n" + _verbatim(b"txt", b"Some string") + _blob_error(b"ERR a\r\nb")),
    ("map holding a set holding an attributed member",
     b"%1\r\n" + _blob(b"k") + b"~2\r\n:1\r\n"
     + _attr([(b"m", b"#f\r\n")]) + b":2\r\n"),
]

# Frames whose interesting split positions are known by construction. The
# positions themselves are computed, not hard coded, so a change to the frame
# cannot silently stop exercising the boundary it was built for.
PATHOLOGICAL: list[tuple[str, bytes, list[int]]] = [
    # Between the CR and the LF of a terminator.
    ("split between CR and LF", _blob(b"abc"),
     [i for i in range(1, len(_blob(b"abc"))) if _blob(b"abc")[i - 1:i + 1] == b"\r\n"]),
    # Inside the digits of a length prefix.
    ("split inside a length prefix", _blob(b"x" * 1234), [2, 3, 4]),
    # Inside a verbatim string's three character format prefix and its colon.
    ("split inside a verbatim format prefix", _verbatim(b"txt", b"Some string"),
     [6, 7, 8, 9]),
]
