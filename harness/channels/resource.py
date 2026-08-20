"""Channel 5: parser memory behaviour. 11 cases.

Holding a parsed value is not a defect: a bulk string of N bytes occupies N
bytes once parsed. What is measured is overhead beyond that, and whether cost
grows linearly with payload size rather than quadratically.

`tracemalloc` is used rather than process RSS, because RSS is affected by
allocator behaviour, garbage collection timing, and anything else running in
the process, none of which the implementation controls.

The failure mode targeted is an implementation that retains the raw input
buffer, the accumulated frame, and the finished value simultaneously, or that
repeatedly copies a growing buffer with slicing.
"""

from __future__ import annotations

import gc
import sys
import time
import tracemalloc
from typing import Any

import pytest

from resp3_wire import NEED_MORE, RespParser

pytestmark = pytest.mark.channel("resource")

# docs/HARNESS.md section 6.1. A correct implementation buffering a chunk,
# accumulating a frame, and materializing a value reaches 2.0 legitimately, and
# CPython's allocator granularity adds headroom on top. An implementation that
# duplicates the payload once more than necessary reaches 4.0 or above.
PEAK_RATIO_BOUND = 3.0

CHUNK = 4096
# docs/HARNESS.md section 6.2 allocates a third scaling case. The first two fix
# the chunk size at 4 KB, so a parser whose cost is linear in bytes but
# quadratic in the number of chunks is eight times less visible to them.
SMALL_CHUNK = 512
TRIALS = 3

# docs/HARNESS.md section 6.2. Retained bytes are compared against the payload
# size, not against a baseline snapshot: `tracemalloc.get_traced_memory()[0]`
# read immediately after `start()` is always 0, and a ratio taken against it
# cannot fail.
RETAINED_FRACTION = 0.1


def build_frame(payload_size: int) -> bytes:
    return b"$%d\r\n" % payload_size + b"x" * payload_size + b"\r\n"


def peak_ratio(payload_size: int, chunked: bool = False, trials: int = TRIALS) -> float:
    """Peak retained bytes attributable to the parser, over payload size.

    The frame is built before tracing begins. Building it inside the traced
    region would make the input alone consume 1.0 of the budget, silently
    tightening the bound to 2.0 and leaving a correct implementation with no
    headroom.

    The minimum across trials is taken, which removes the effect of an
    interpreter level allocation happening to land inside the window.
    """
    best = float("inf")
    for _ in range(trials):
        frame = build_frame(payload_size)
        gc.collect()
        tracemalloc.start()
        try:
            baseline = tracemalloc.get_traced_memory()[0]
            parser = RespParser()
            if chunked:
                for i in range(0, len(frame), CHUNK):
                    parser.feed(frame[i:i + CHUNK])
            else:
                parser.feed(frame)
            value = parser.gets()
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert value is not NEED_MORE, "the whole frame was fed but no value came back"
        assert len(value) == payload_size, "the parsed value is the wrong length"
        del value, parser, frame
        best = min(best, (peak - baseline) / payload_size)
    return best


def elapsed_chunked(payload_size: int, chunk: int = CHUNK) -> float:
    """Wall time to feed one frame in fixed chunks and drain it.

    D24. The drain must produce a value. Timing a parser that returns nothing
    measures nothing, and the D14 assertions were satisfied by exactly that
    until this function checked.
    """
    frame = build_frame(payload_size)
    parser = RespParser()
    value: Any = NEED_MORE
    start = time.perf_counter()
    for i in range(0, len(frame), chunk):
        parser.feed(frame[i:i + chunk])
        result = parser.gets()
        if result is not NEED_MORE:
            value = result
    elapsed = time.perf_counter() - start
    assert value is not NEED_MORE, (
        f"a complete {payload_size} byte frame was fed in {chunk} byte chunks "
        f"and the parser never produced a value; there is no parsing cost to "
        f"measure"
    )
    assert len(value) == payload_size, (
        f"the parsed value is {len(value)} bytes, expected {payload_size}"
    )
    return elapsed


def per_byte_cost(payload_size: int, trials: int = 5,
                  chunk: int = CHUNK) -> float:
    """Nanoseconds per byte, minimum of `trials`, measured on its own.

    Sizes are measured independently and never interleaved. D14 records why:
    paired interleaved trials couple through allocator state, because a small
    run executed immediately after a large one inherits a freshly freed arena
    and completes roughly twice as fast. The resulting ratio reads the
    allocator rather than the algorithm.
    """
    best = min(elapsed_chunked(payload_size, chunk) for _ in range(trials))
    return best / payload_size * 1e9


# ---------------------------------------------------------------------------
# Peak ratio at two payload sizes, fed whole. 2 cases.
# ---------------------------------------------------------------------------


def test_peak_ratio_1mb() -> None:
    ratio = peak_ratio(1 << 20)
    assert ratio <= PEAK_RATIO_BOUND, f"peak ratio {ratio:.2f} exceeds {PEAK_RATIO_BOUND}"



def test_peak_ratio_64mb() -> None:
    ratio = peak_ratio(64 << 20)
    assert ratio <= PEAK_RATIO_BOUND, f"peak ratio {ratio:.2f} exceeds {PEAK_RATIO_BOUND}"


# ---------------------------------------------------------------------------
# Peak ratio under a chunked feed. 1 case.
# ---------------------------------------------------------------------------


def test_peak_ratio_chunked_1mb() -> None:
    ratio = peak_ratio(1 << 20, chunked=True)
    assert ratio <= PEAK_RATIO_BOUND, (
        f"peak ratio {ratio:.2f} exceeds {PEAK_RATIO_BOUND} under a "
        f"{CHUNK} byte chunked feed"
    )



# ---------------------------------------------------------------------------
# Buffer release, steady state, and depth. 5 cases.
# ---------------------------------------------------------------------------


def test_reset_releases_buffered_state() -> None:
    """After a partial large frame, `reset` must release the buffer.

    Retained bytes are compared against the payload size. The earlier
    formulation compared them against a baseline snapshot taken immediately
    after `tracemalloc.start()`, which is always 0, so the guard against
    dividing by it reported success unconditionally and a parser holding 4 MB
    scored green.
    """
    payload_size = 8 << 20
    partial = b"$%d\r\n" % payload_size + b"x" * (payload_size // 2)
    gc.collect()
    tracemalloc.start()
    try:
        parser = RespParser()
        parser.feed(partial)
        assert parser.gets() is NEED_MORE, "a half-delivered frame is not complete"
        parser.reset()
        gc.collect()
        retained = tracemalloc.get_traced_memory()[0]
    finally:
        tracemalloc.stop()
    bound = RETAINED_FRACTION * payload_size
    assert retained < bound, (
        f"{retained} bytes still held after reset(), bound {bound:.0f} "
        f"({RETAINED_FRACTION:.0%} of a {payload_size} byte payload); the "
        f"buffer was not released"
    )


def test_drained_buffer_is_released() -> None:
    """The same assertion without `reset()`.

    A parser that never releases consumed input holds the payload until
    something else happens to displace it, which for a caller that stops reading
    after a large reply is indefinitely.
    """
    payload_size = 8 << 20
    frame = build_frame(payload_size)
    gc.collect()
    tracemalloc.start()
    try:
        parser = RespParser()
        parser.feed(frame)
        value = parser.gets()
        assert value is not NEED_MORE, "the whole frame was fed but no value came back"
        assert len(value) == payload_size, "the parsed value is the wrong length"
        del value
        gc.collect()
        retained = tracemalloc.get_traced_memory()[0]
    finally:
        tracemalloc.stop()
    bound = RETAINED_FRACTION * payload_size
    assert retained < bound, (
        f"{retained} bytes still held after a complete frame was drained and "
        f"its value dropped, bound {bound:.0f} ({RETAINED_FRACTION:.0%} of a "
        f"{payload_size} byte payload); consumed input was never released"
    )


def test_many_small_replies_do_not_accumulate() -> None:
    """Peak across 10,000 small replies is bounded by a constant, not by count."""
    one = b"$8\r\nabcdefgh\r\n"
    count = 10_000
    gc.collect()
    tracemalloc.start()
    try:
        baseline = tracemalloc.get_traced_memory()[0]
        parser = RespParser()
        for _ in range(count):
            parser.feed(one)
            while parser.gets() is not NEED_MORE:
                pass
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    growth = peak - baseline
    bound = PEAK_RATIO_BOUND * len(one) + 65536
    assert growth <= bound, (
        f"peak grew {growth} bytes across {count} replies, bound {bound:.0f}; "
        f"the bound must not scale with the reply count"
    )


def test_depth_100_nesting_parses_to_the_right_value() -> None:
    """No payload, so no meaningful ratio: this asserts the value."""
    frame = b"*1\r\n" * 100 + b":9\r\n"
    parser = RespParser()
    parser.feed(frame)
    value = parser.gets()
    assert value is not NEED_MORE, "the whole frame was fed but no value came back"
    depth = 0
    while isinstance(value, list):
        assert len(value) == 1, f"unexpected arity at depth {depth}"
        depth += 1
        value = value[0]
    assert depth == 100, f"nesting depth {depth}, expected 100"
    assert value == 9, f"leaf {value!r}, expected 9"
    assert parser.gets() is NEED_MORE, "residue left after a complete frame"


def test_nesting_past_the_recursion_limit_does_not_raise() -> None:
    """This is the case that discriminates an iterative parser from a recursive one.

    Depth 100 alone cannot: CPython's default limit is 1000, and the mutation
    suite confirmed a recursive parser passing at 100 and raising at 2000. The
    depth here is derived from the running interpreter's limit rather than
    hard coded, so the case keeps its meaning if the limit is changed.
    """
    depth = sys.getrecursionlimit() + 500
    frame = b"*1\r\n" * depth + b":9\r\n"
    parser = RespParser()
    parser.feed(frame)
    try:
        value = parser.gets()
    except RecursionError as exc:
        raise AssertionError(
            f"parsing {depth} levels of nesting raised RecursionError; the "
            f"parser closes aggregates by recursion, so nesting depth costs "
            f"interpreter stack"
        ) from exc
    assert value is not NEED_MORE, "the whole frame was fed but no value came back"
    observed = 0
    while isinstance(value, list):
        observed += 1
        value = value[0]
    assert observed == depth, f"nesting depth {observed}, expected {depth}"
    assert value == 9, f"leaf {value!r}, expected 9"


# ---------------------------------------------------------------------------
# Scaling, per D14. 3 cases.
# ---------------------------------------------------------------------------


def test_per_byte_cost_does_not_grow_with_size() -> None:
    """Cost per byte across a 64x size range.

    A linear parser holds per-byte cost roughly constant. A quadratic one's
    grows in proportion to input size, so across a 64x range it grows 64x. The
    bound of 8.0 sits between them.
    """
    small = per_byte_cost(1 << 20)
    large = per_byte_cost(64 << 20)
    growth = large / small
    assert growth < 8.0, (
        f"cost per byte grew {growth:.2f}x across a 64x size range "
        f"({small:.2f} -> {large:.2f} ns/byte); linear is about 1x and "
        f"quadratic about 64x"
    )


def test_per_byte_cost_does_not_degrade_at_any_step() -> None:
    """No single step in the sequence degrades.

    Catches a parser that is linear across the small sizes and falls over only
    at scale, which the endpoint ratio alone could average away.
    """
    sizes = [1 << 20, 8 << 20, 64 << 20]
    costs = [per_byte_cost(size) for size in sizes]
    for i in range(len(costs) - 1):
        step = costs[i + 1] / costs[i]
        assert step < 4.0, (
            f"cost per byte grew {step:.2f}x from {sizes[i] >> 20} MB to "
            f"{sizes[i + 1] >> 20} MB ({costs[i]:.2f} -> {costs[i + 1]:.2f} "
            f"ns/byte)"
        )


def test_per_byte_cost_does_not_grow_at_a_smaller_chunk_size() -> None:
    """The same D14 metric with eight times as many feed calls per byte.

    The two cases above fix the chunk size at 4 KB. A parser whose cost is
    linear in the bytes fed but grows with the number of chunks they arrived in
    is far less visible at that granularity, and arriving in small chunks is
    exactly what a socket does under load.
    """
    small = per_byte_cost(1 << 20, chunk=SMALL_CHUNK)
    large = per_byte_cost(64 << 20, chunk=SMALL_CHUNK)
    growth = large / small
    assert growth < 8.0, (
        f"cost per byte grew {growth:.2f}x across a 64x size range at a "
        f"{SMALL_CHUNK} byte chunk size ({small:.2f} -> {large:.2f} ns/byte); "
        f"linear is about 1x and quadratic about 64x"
    )
