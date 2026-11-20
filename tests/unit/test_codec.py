import math
from collections import OrderedDict
from typing import Any

import pytest

from evalry.runner._codec import MAX_DEPTH, ProtocolError, Unsupported, decode, encode


@pytest.mark.parametrize(
    "value",
    [None, True, 0, -5, 10**30, "", "héllo", 1.5, [1, [2, 3]], (1, 2), {1, 2}, frozenset({3}), b"\x00\xff", {"a": 1},
     {1: "x", (1, 2): "y"}, {"$": "not a tag"}, [(1, {2}), {"k": b"v"}]],
)  # fmt: skip
def test_roundtrip(value: Any) -> None:
    out = decode(encode(value))
    assert out == value and type(out) is type(value)


def test_non_finite_floats_roundtrip() -> None:
    assert math.isnan(decode(encode(float("nan")))) and decode(encode(float("-inf"))) == float("-inf")


@pytest.mark.parametrize(
    "value", [object(), OrderedDict(), lambda: 1, ValueError("x"), 1 + 2j, bool, type("A", (), {})()]
)
def test_unsupported_types_fail_closed(value: Any) -> None:
    with pytest.raises(Unsupported):
        encode(value)


def test_subclasses_are_not_silently_coerced() -> None:
    class MyInt(int):
        pass

    with pytest.raises(Unsupported):
        encode(MyInt(3))


def test_depth_and_size_caps() -> None:
    deep: Any = []
    for _ in range(MAX_DEPTH + 5):
        deep = [deep]
    with pytest.raises(Unsupported):
        encode(deep)
    with pytest.raises(ProtocolError):
        decode(deep)
    with pytest.raises(Unsupported):
        encode(list(range(300_000)))


@pytest.mark.parametrize(
    "bad",
    [{"$": "tuple"}, {"$": "tuple", "v": 3}, {"$": "nope", "v": []}, {"$": "bytes", "v": "!!!"}, {"$": "float", "v": "1"},
     {"$": "set", "v": [[1]]}, {"$": "dict", "v": [[1]]}, {"$": "dict", "v": [[[1], 2]]}, {"$": "tuple", "v": [], "x": 1}, object()],
)  # fmt: skip
def test_malformed_input_is_a_protocol_error(bad: Any) -> None:
    with pytest.raises(ProtocolError):
        decode(bad)
