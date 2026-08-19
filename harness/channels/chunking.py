"""Channel 2: parser invariance under fragmentation. 20 cases.

For a byte sequence B and any partition of it into chunks, feeding the chunks
in order with a drain after each produces the same sequence of values as
feeding B whole and draining once.

Equality here is stricter than the comparator's: it additionally compares
`VerbatimBytes.format` and `Attributed.attributes`, reaching past delegating
equality on purpose. A parser that loses the format prefix only when a split
lands inside it would otherwise pass.

Per D11 this channel is the sole enforcement of attribute handling, since
redis-py raises on `|` frames and no oracle case can carry one.
"""

from __future__ import annotations

import random

import pytest

from resp3_wire import NEED_MORE, Attributed, RespParser
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
# One byte feeds, per type group. 8 cases, one of which is the delegation
# assertion in section 4.4 rather than a fragmentation case.
# ---------------------------------------------------------------------------


def test_one_byte_resp2_scalars() -> None:
    data = corpus.group("resp2_scalars")
    assert_invariant(data, one_byte_cuts(data), " for RESP2 scalars")


def test_one_byte_resp3_scalars() -> None:
    data = corpus.group("resp3_scalars")
    assert_invariant(data, one_byte_cuts(data), " for RESP3 scalars")


def test_one_byte_length_prefixed() -> None:
    data = corpus.group("length_prefixed")
    assert_invariant(data, one_byte_cuts(data), " for length prefixed frames")


def test_one_byte_nested_arrays() -> None:
    data = corpus.group("nested_arrays")
    assert_invariant(data, one_byte_cuts(data), " for nested arrays")


def test_one_byte_maps_and_sets() -> None:
    data = corpus.group("maps_and_sets")
    assert_invariant(data, one_byte_cuts(data), " for maps, sets, and attributes")


def test_one_byte_push_frames() -> None:
    data = corpus.group("push_frames")
    assert_invariant(data, one_byte_cuts(data), " for push frames")


def test_one_byte_nulls_and_empties() -> None:
    data = corpus.group("nulls_and_empties")
    assert_invariant(data, one_byte_cuts(data), " for nulls and empty aggregates")


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
# Exhaustive split at every interior position. 4 cases.
#
# A frame of N bytes has exactly N-1 interior cut points, and each case runs
# all of them. That is many partitions inside one case, which is appropriate:
# they test one property.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("label,frame", corpus.CURATED, ids=[c[0] for c in corpus.CURATED])
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
# Pathological boundaries. 4 cases.
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


def test_metadata_survives_every_split_of_its_own_frame() -> None:
    """Format prefixes and attribute dictionaries survive fragmentation.

    This is the case that delegating equality would let through: a parser that
    dropped a format or an attribute dictionary when a split landed inside it
    still satisfies `==` against the bare value.
    """
    frames = [
        corpus.GROUPS["length_prefixed"][1][1],   # verbatim txt
        corpus.GROUPS["length_prefixed"][2][1],   # verbatim mkd
        corpus.GROUPS["maps_and_sets"][5][1],     # attribute on a map value
        corpus.GROUPS["maps_and_sets"][6][1],     # attribute on a set member
        corpus.GROUPS["maps_and_sets"][8][1],     # consecutive attributes merge
    ]
    for frame in frames:
        whole = parse_whole(frame)
        described = strict_describe(whole)
        assert "verbatim" in repr(described) or "attributed" in repr(described), (
            "the frame under test carries no metadata to lose"
        )
        for position in range(1, len(frame)):
            assert_invariant(frame, [position])
