"""Channel 2: parser correctness and invariance. 26 cases.

Two instruments, and D20 records why both are needed.

The invariant: for a byte sequence B and any partition of it into chunks,
feeding the chunks in order with a drain after each produces the same sequence
of values as feeding B whole and draining once. Equality here is stricter than
the comparator's, additionally comparing `VerbatimBytes.format` and
`Attributed.attributes`, so a parser that loses metadata only when a split
lands inside it does not pass.

The limit: that invariant is self-referential. It compares a parser against
itself and cannot detect any defect that is consistent across split schedules.
The mutation suite confirmed seven such defects passing every invariance case
while the corpus contained the exact frames. The absolute-expectation cases
below therefore assert what a frame parses to, written from
`docs/PROTOCOL.md` sections 3 and 4, with no parser on the other side.

Per D11 this channel is the sole enforcement of attribute handling, since
redis-py raises on `|` frames and no oracle case can carry one. Four cases carry
it directly, comparing `Attributed.value` and `.attributes` rather than a repr.
"""

from __future__ import annotations

import random

import pytest

from resp3_wire import (
    NEED_MORE,
    Attributed,
    ErrorReply,
    PushMessage,
    RespParser,
    VerbatimBytes,
)
from support import corpus
from support.compare import strict_describe

pytestmark = pytest.mark.channel("chunking")


def drain(parser: RespParser) -> list:
    out = []
    while (value := parser.gets()) is not NEED_MORE:
        out.append(value)
    return out


def parse_whole(data: bytes) -> list:
    parser = RespParser()
    parser.feed(data)
    return drain(parser)


def parse_one(data: bytes):
    """Parse a stream expected to hold exactly one complete reply."""
    values = parse_whole(data)
    assert len(values) == 1, (
        f"expected exactly one reply from {data!r}, got {len(values)}: {values!r}"
    )
    return values[0]


def parse_partitioned(data: bytes, cuts) -> list:
    """Feed the chunks named by `cuts`, draining after each."""
    parser = RespParser()
    out: list = []
    previous = 0
    for cut in list(cuts) + [len(data)]:
        parser.feed(data[previous:cut])
        out.extend(drain(parser))
        previous = cut
    return out


def assert_invariant(data: bytes, cuts, note: str = "") -> None:
    reference = [strict_describe(v) for v in parse_whole(data)]
    # D24. The comparison below is self-referential, so it cannot detect the
    # absence of output: an empty observed sequence matches an empty reference
    # exactly. A parser returning nothing satisfied thirteen of these cases
    # until this line existed. Every frame fed here is a complete reply by
    # construction, so an empty reference means the parser produced nothing,
    # not that there was nothing to produce.
    if not reference:
        raise AssertionError(
            f"the whole-buffer feed produced no values at all{note}; "
            f"{len(data)} bytes of complete frames went in and nothing came "
            f"out, so there is no reference to compare a partitioned feed "
            f"against"
        )
    observed = [strict_describe(v) for v in parse_partitioned(data, cuts)]
    if observed != reference:
        raise AssertionError(
            f"partitioned feed diverged from whole-buffer feed{note}\n"
            f"  cuts     : {list(cuts)[:12]}{'...' if len(list(cuts)) > 12 else ''}\n"
            f"  expected : {reference}\n"
            f"  observed : {observed}"
        )


def one_byte_cuts(data: bytes) -> range:
    return range(1, len(data))


# ---------------------------------------------------------------------------
# Absolute value expectations. 8 cases.
#
# docs/HARNESS.md section 4.1 and D20. These assert what a frame parses to,
# against docs/PROTOCOL.md sections 3 and 4, with no parser on the other side of
# the comparison. Every one of them corresponds to a defect that satisfied the
# invariant perfectly.
# ---------------------------------------------------------------------------


def test_resp3_null_is_none() -> None:
    """`_\\r\\n`, the RESP3 null. docs/PROTOCOL.md section 4.6."""
    value = parse_one(b"_\r\n")
    assert value is None, f"`_` must produce None, got {value!r}"


def test_resp2_null_bulk_is_none() -> None:
    """`$-1\\r\\n`, accepted under both protocols and never an empty bytes."""
    value = parse_one(b"$-1\r\n")
    assert value is None, (
        f"`$-1` must produce None, got {value!r} of type {type(value).__name__}"
    )


def test_resp2_null_array_is_none() -> None:
    """`*-1\\r\\n`, accepted under both protocols and never an empty list."""
    value = parse_one(b"*-1\r\n")
    assert value is None, (
        f"`*-1` must produce None, got {value!r} of type {type(value).__name__}"
    )


def test_booleans_are_bool_and_not_int() -> None:
    """`#t` and `#f`. `bool` subclasses `int`, so the type is the assertion."""
    true_value = parse_one(b"#t\r\n")
    false_value = parse_one(b"#f\r\n")
    assert type(true_value) is bool and true_value is True, (
        f"`#t` must produce True, got {true_value!r} of type "
        f"{type(true_value).__name__}"
    )
    assert type(false_value) is bool and false_value is False, (
        f"`#f` must produce False, got {false_value!r} of type "
        f"{type(false_value).__name__}"
    )


def test_big_number_is_an_int() -> None:
    """`(`. Python integers are arbitrary precision, so nothing is lost."""
    digits = b"3492890328409238509389482938498234"
    value = parse_one(b"(" + digits + b"\r\n")
    assert type(value) is int, (
        f"`(` must produce int, got {type(value).__name__}"
    )
    assert value == int(digits), f"big number {value!r} != {int(digits)}"


def test_blob_error_keeps_a_crlf_in_its_payload() -> None:
    """`!` is length prefixed, so CR and LF are legal inside it.

    docs/PROTOCOL.md section 4.7. A parser that stops at the first CRLF, as it
    may for `-`, silently truncates the message.
    """
    text = b"ERR first line\r\nsecond line"
    value = parse_one(corpus.blob_error(text))
    assert isinstance(value, ErrorReply), (
        f"`!` must produce ErrorReply, got {type(value).__name__}"
    )
    assert value.code == "ERR", f"code {value.code!r}"
    assert value.message == text.decode(), (
        f"message {value.message!r} was truncated; the declared length covers "
        f"the whole payload including its CRLF"
    )


def test_push_frame_is_a_push_message() -> None:
    """`>` produces PushMessage, not an ordinary array. D7."""
    frame = b">2\r\n" + corpus.blob(b"invalidate") + b"*1\r\n" + corpus.blob(b"k")
    value = parse_one(frame)
    assert isinstance(value, PushMessage), (
        f"`>` must produce PushMessage, got {type(value).__name__}"
    )
    assert value.kind == "invalidate", f"kind {value.kind!r}"
    assert value.data == [[b"k"]], f"data {value.data!r}"


def test_verbatim_strips_its_prefix_and_keeps_its_format() -> None:
    """`=`. The declared length covers the format, the colon, and the payload."""
    value = parse_one(corpus.verbatim(b"txt", b"Some string"))
    assert isinstance(value, VerbatimBytes), (
        f"`=` must produce VerbatimBytes, got {type(value).__name__}"
    )
    assert bytes(value) == b"Some string", (
        f"payload {bytes(value)!r}; the format prefix and its colon are stripped"
    )
    assert value.format == "txt", f"format {value.format!r}"


# ---------------------------------------------------------------------------
# Attribute semantics. 4 cases.
#
# docs/HARNESS.md section 4.4. D11 makes this channel the sole enforcement of
# attribute handling. Each case asserts one property against `Attributed.value`
# and `.attributes` directly.
# ---------------------------------------------------------------------------


def test_attribute_decorates_the_value_that_follows_it() -> None:
    """A decorated value arrives wrapped, with its dictionary intact."""
    frame = corpus.attr([(b"ttl", b":60\r\n")]) + b"+OK\r\n"
    value = parse_one(frame)
    assert isinstance(value, Attributed), (
        f"a decorated value must arrive as Attributed, got "
        f"{type(value).__name__}; the attribute was dropped"
    )
    assert value.value == b"OK", f"wrapped value {value.value!r}"
    assert value.attributes == {b"ttl": 60}, (
        f"attributes {value.attributes!r}, expected {{b'ttl': 60}}"
    )


def test_attribute_dictionary_is_never_a_reply_of_its_own() -> None:
    """The single most commonly failed requirement in docs/PROTOCOL.md section 5.

    An attribute frame is not a reply. A stream carrying one attribute and one
    value yields exactly one value.
    """
    frame = corpus.attr([(b"ttl", b":60\r\n")]) + b"+OK\r\n"
    values = parse_whole(frame)
    assert len(values) == 1, (
        f"one attribute decorating one value is one reply, got {len(values)}: "
        f"{values!r}; the attribute dictionary was emitted as a value"
    )


def test_attribute_decorates_at_its_own_nesting_depth() -> None:
    """Three attributes at three depths each decorate their own neighbour.

    A parser that parks every attribute at the root satisfies the invariant and
    still attaches all three to the outermost value.
    """
    frame = (corpus.attr([(b"top", b":1\r\n")])
             + b"*2\r\n" + corpus.attr([(b"mid", b":2\r\n")]) + corpus.blob(b"a")
             + b"*1\r\n" + corpus.attr([(b"low", b":3\r\n")]) + corpus.blob(b"b"))
    root = parse_one(frame)
    assert isinstance(root, Attributed) and root.attributes == {b"top": 1}, (
        f"the root carries {getattr(root, 'attributes', None)!r}, expected "
        f"{{b'top': 1}}"
    )
    outer = root.value
    assert type(outer) is list and len(outer) == 2, f"root value {outer!r}"
    first = outer[0]
    assert isinstance(first, Attributed) and first.attributes == {b"mid": 2}, (
        f"element 0 carries {getattr(first, 'attributes', None)!r}, expected "
        f"{{b'mid': 2}}"
    )
    assert first.value == b"a", f"element 0 value {first.value!r}"
    inner = outer[1]
    assert type(inner) is list and len(inner) == 1, f"element 1 {inner!r}"
    deep = inner[0]
    assert isinstance(deep, Attributed) and deep.attributes == {b"low": 3}, (
        f"the depth 2 element carries {getattr(deep, 'attributes', None)!r}, "
        f"expected {{b'low': 3}}"
    )
    assert deep.value == b"b", f"depth 2 value {deep.value!r}"


def test_consecutive_attributes_merge_with_later_keys_winning() -> None:
    """docs/PROTOCOL.md section 5, the merge rule."""
    frame = (corpus.attr([(b"a", b":1\r\n"), (b"shared", b":10\r\n")])
             + corpus.attr([(b"b", b":2\r\n"), (b"shared", b":20\r\n")])
             + b"+OK\r\n")
    value = parse_one(frame)
    assert isinstance(value, Attributed), (
        f"expected Attributed, got {type(value).__name__}"
    )
    assert value.attributes == {b"a": 1, b"b": 2, b"shared": 20}, (
        f"attributes {value.attributes!r}; consecutive frames merge and later "
        f"keys win"
    )


# ---------------------------------------------------------------------------
# One byte feeds. 4 cases: three sweeps over the corpus, and the delegation
# property of docs/HARNESS.md section 4.4.
# ---------------------------------------------------------------------------


def test_one_byte_scalars() -> None:
    data = corpus.group("resp2_scalars") + corpus.group("resp3_scalars")
    assert_invariant(data, one_byte_cuts(data), " for RESP2 and RESP3 scalars")


def test_one_byte_length_prefixed_and_nested() -> None:
    data = corpus.group("length_prefixed") + corpus.group("nested_arrays")
    assert_invariant(data, one_byte_cuts(data),
                     " for length prefixed frames and nested arrays")


def test_one_byte_aggregates_pushes_and_nulls() -> None:
    data = (corpus.group("maps_and_sets") + corpus.group("push_frames")
            + corpus.group("nulls_and_empties"))
    assert_invariant(data, one_byte_cuts(data),
                     " for maps, sets, attributes, pushes, and nulls")


def test_attributed_delegation() -> None:
    """docs/HARNESS.md section 4.4, asserted directly rather than through the
    invariant.

    Separated out because the property is load-bearing for callers, and a
    failure here should read as what it is rather than as an unrelated set
    comparison failing somewhere else.
    """
    a = Attributed(b"x", {b"k": b"v"})
    assert a == b"x", "Attributed must compare equal to its wrapped value"
    assert b"x" == a, "and in the reflected direction"
    assert hash(a) == hash(b"x"), "hashing must delegate to the wrapped value"
    assert {a} == {b"x"}, "so an Attributed is interchangeable as a set member"
    assert {b"x": 1}[a] == 1, "and as a dict key"
    assert Attributed(b"x", {}) == Attributed(b"x", {b"other": b"v"}), (
        "attributes must never take part in the comparison"
    )


# ---------------------------------------------------------------------------
# Exhaustive split at every interior position. 3 cases.
#
# A frame of N bytes has exactly N-1 interior cut points, and each case runs
# all of them. That is many partitions inside one case, which is appropriate:
# they test one property.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("label,frame", corpus.CURATED, ids=[c[0][:40] for c in corpus.CURATED])
def test_exhaustive_split(label: str, frame: bytes) -> None:
    for position in range(1, len(frame)):
        assert_invariant(frame, [position], f" for {label} at position {position}")


# ---------------------------------------------------------------------------
# Seeded random partitions. 4 cases.
#
# The run seed plus the fixed regression seeds, so a green run is never purely
# luck and a regression a random seed happens to miss is still caught.
# ---------------------------------------------------------------------------


def _random_partition_sweep(data: bytes, source: random.Random, rounds: int = 60) -> None:
    for _ in range(rounds):
        count = source.randint(1, min(60, len(data) - 1))
        cuts = sorted(source.sample(range(1, len(data)), count))
        assert_invariant(data, cuts)


def test_random_partitions_run_seed(rng: random.Random) -> None:
    _random_partition_sweep(corpus.all_frames(), random.Random(rng.getrandbits(64)))


def test_random_partitions_regression_seeds() -> None:
    from conftest import REGRESSION_SEEDS

    data = corpus.all_frames()
    for seed in REGRESSION_SEEDS:
        _random_partition_sweep(data, random.Random(seed), rounds=25)


def test_random_partitions_curated_frames(rng: random.Random) -> None:
    source = random.Random(rng.getrandbits(64))
    for _, frame in corpus.CURATED:
        _random_partition_sweep(frame, source, rounds=40)


def test_random_partitions_single_frames(rng: random.Random) -> None:
    source = random.Random(rng.getrandbits(64))
    for name in corpus.GROUPS:
        for _, frame in corpus.GROUPS[name]:
            if len(frame) < 3:
                continue
            for _ in range(6):
                count = source.randint(1, len(frame) - 1)
                cuts = sorted(source.sample(range(1, len(frame)), count))
                assert_invariant(frame, cuts, f" for {name}")


# ---------------------------------------------------------------------------
# Pathological boundaries. 3 cases.
# ---------------------------------------------------------------------------


def test_split_inside_crlf_and_prefixes() -> None:
    """A split between CR and LF, inside length digits, and inside a format prefix."""
    for label, frame, positions in corpus.PATHOLOGICAL:
        assert positions, f"{label} computed no split positions to exercise"
        for position in positions:
            assert_invariant(frame, [position], f" for {label} at {position}")


def test_empty_feeds_interleaved() -> None:
    """`feed(b"")` before and after every byte must change nothing."""
    data = corpus.all_frames()
    reference = [strict_describe(v) for v in parse_whole(data)]
    parser = RespParser()
    observed = []
    for byte in data:
        parser.feed(b"")
        parser.feed(bytes([byte]))
        parser.feed(b"")
        observed.extend(drain(parser))
    assert [strict_describe(v) for v in observed] == reference


def test_split_at_every_frame_boundary() -> None:
    """Cutting exactly where one frame ends and the next begins."""
    boundaries = []
    offset = 0
    for name in corpus.GROUPS:
        for _, frame in corpus.GROUPS[name]:
            offset += len(frame)
            boundaries.append(offset)
    data = corpus.all_frames()
    assert_invariant(data, boundaries[:-1], " at frame boundaries")
