"""Tagged-JSON value codec for the test-driver RPC (docs: out-of-process test driver).

Stdlib only: this file is shipped into both containers. Values cross the boundary as JSON and nothing else, so hostile data
from the solution side can never be unpickled or evaluated in the test container. Exact types only: subclasses (IntEnum,
defaultdict, ...) are unsupported on purpose, because a silent coercion could change what an assertion compares.
"""

import base64
import math
from typing import Any

MAX_DEPTH = 50
MAX_NODES = 200_000


class Unsupported(Exception):
    """The value cannot cross the isolation boundary; the run fails closed with this message."""


class ProtocolError(Exception):
    """A message from the other side is malformed. Treated as a failed run, never repaired."""


def encode(value: Any) -> Any:
    return _enc(value, 0, [MAX_NODES])


def _enc(v: Any, depth: int, budget: list[int]) -> Any:
    budget[0] -= 1
    if budget[0] < 0 or depth > MAX_DEPTH:
        raise Unsupported("value is too large or too deeply nested to cross the isolation boundary")
    t = type(v)
    if v is None or t in (bool, int, str):
        return v
    if t is float:
        if math.isnan(v):
            return {"$": "float", "v": "nan"}
        if math.isinf(v):
            return {"$": "float", "v": "inf" if v > 0 else "-inf"}
        return v
    if t is list:
        return [_enc(x, depth + 1, budget) for x in v]
    if t is tuple:
        return {"$": "tuple", "v": [_enc(x, depth + 1, budget) for x in v]}
    if t is set or t is frozenset:
        return {"$": t.__name__, "v": [_enc(x, depth + 1, budget) for x in v]}
    if t is bytes:
        return {"$": "bytes", "v": base64.b64encode(v).decode("ascii")}
    if t is dict:
        if all(type(k) is str for k in v) and "$" not in v:
            return {k: _enc(x, depth + 1, budget) for k, x in v.items()}
        return {
            "$": "dict",
            "v": [[_enc(k, depth + 1, budget), _enc(x, depth + 1, budget)] for k, x in v.items()],
        }
    raise Unsupported(
        f"values of type {t.__name__} cannot cross the isolation boundary (use plain data types)"
    )


def decode(obj: Any) -> Any:
    return _dec(obj, 0, [MAX_NODES])


def _dec(o: Any, depth: int, budget: list[int]) -> Any:
    budget[0] -= 1
    if budget[0] < 0 or depth > MAX_DEPTH:
        raise ProtocolError("message is too large or too deeply nested")
    t = type(o)
    if o is None or t in (bool, int, str, float):
        return o
    if t is list:
        return [_dec(x, depth + 1, budget) for x in o]
    if t is not dict:
        raise ProtocolError(f"unexpected JSON type {t.__name__}")
    if "$" not in o:
        return {k: _dec(x, depth + 1, budget) for k, x in o.items()}
    if set(o) != {"$", "v"}:
        raise ProtocolError("tagged value has unexpected keys")
    tag, v = o["$"], o["v"]
    if tag == "float" and v in ("nan", "inf", "-inf"):
        return float(v)
    if tag == "bytes" and type(v) is str:
        try:
            return base64.b64decode(v, validate=True)
        except ValueError as e:
            raise ProtocolError("invalid base64 in bytes value") from e
    if tag in ("tuple", "set", "frozenset", "dict") and type(v) is list:
        items = [_dec(x, depth + 1, budget) for x in v]
        try:
            if tag == "tuple":
                return tuple(items)
            if tag == "set":
                return set(items)
            if tag == "frozenset":
                return frozenset(items)
            if any(type(p) is not list or len(p) != 2 for p in items):
                raise ProtocolError("dict entries must be [key, value] pairs")
            return {k: x for k, x in items}
        except TypeError as e:
            raise ProtocolError("unhashable set element or dict key") from e
    raise ProtocolError(f"unknown tag {tag!r}")
