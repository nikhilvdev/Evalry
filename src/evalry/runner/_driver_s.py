"""Solution side of the test driver: imports the untrusted solution and answers calls. It never sees the tests.

Runs inside container S only (see evalry.runner). Protocol: JSON lines on stdin/stdout; everything the solution prints goes
to stderr so it cannot corrupt the channel (it can still write its own fd 1, which only lets it lie about its own results).
"""

import inspect
import json
import os
import sys
from typing import Any

from evalry.runner._codec import ProtocolError, Unsupported, decode, encode


def main() -> None:
    out = os.fdopen(os.dup(1), "w", buffering=1)
    sys.stdout = sys.stderr

    def send(obj: Any) -> None:
        out.write(json.dumps(obj, allow_nan=False, separators=(",", ":")) + "\n")
        out.flush()

    init = json.loads(sys.stdin.readline())
    with open("/work/solution.py", "w") as f:
        f.write(init["code"])
    sys.path.insert(0, "/work")
    try:
        import solution as mod  # type: ignore[import-not-found]
    except BaseException as e:  # noqa: BLE001 (any failure to import is a failed solution, reported to the tests side)
        send({"fatal": f"{type(e).__name__}: {e}"[:500]})
        sys.exit(3)

    exported: dict[str, Any] = {}
    names = getattr(mod, "__all__", None) or [n for n in dir(mod) if not n.startswith("_")]
    table: dict[str, Any] = {}
    for n in names:
        v = getattr(mod, n, None)
        if inspect.ismodule(v):
            continue
        defined_here = getattr(v, "__module__", None) == mod.__name__
        if inspect.isclass(v) or inspect.isgeneratorfunction(v) or inspect.iscoroutinefunction(v):
            if defined_here:
                exported[n] = {
                    "kind": "unsupported",
                    "why": "classes, generators and coroutines cannot cross the boundary",
                }
        elif inspect.isfunction(v) or inspect.isbuiltin(v):
            if defined_here:
                exported[n] = {"kind": "callable"}
                table[n] = v
        else:
            try:
                exported[n] = {"kind": "value", "v": encode(v)}
            except Unsupported as e:
                exported[n] = {"kind": "unsupported", "why": str(e)}

    for line in sys.stdin:
        try:
            req = json.loads(line)
            if req.get("op") == "names":
                send({"names": exported})
                continue
            fn = table.get(req.get("name"))
            if req.get("op") != "call" or fn is None:
                raise ProtocolError("unknown operation or name")
            args, kwargs = decode(req["args"]), decode(req["kwargs"])
            if type(args) is not list or type(kwargs) is not dict:
                raise ProtocolError("args must be a list and kwargs a dict")
        except (ValueError, RecursionError, ProtocolError, KeyError) as e:
            send({"err": {"type": "ProtocolError", "msg": str(e)[:300]}})
            continue
        try:
            result = fn(*args, **kwargs)
        except BaseException as e:  # noqa: BLE001 (report any exception from the solution, including SystemExit)
            send({"err": {"type": type(e).__name__, "msg": str(e)[:500]}})
            continue
        try:
            send({"ok": encode(result)})
        except Unsupported as e:
            send({"err": {"type": "Unsupported", "msg": str(e)}})
