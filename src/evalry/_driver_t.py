"""Tests side of the test driver: runs the hidden tests against a proxy `solution` module. It never sees solution code.

Runs inside container T only (see evalry.runner). Exit code is the verdict: 0 all assertions passed, 1 a test failed or
raised, 4 a value or construct could not cross the boundary (fail closed), 5 the solution side died or broke protocol.
"""

import builtins
import json
import os
import sys
import types
from typing import Any

from evalry._codec import ProtocolError, Unsupported, decode, encode

MAX_LINE = 1 << 20


class IsolationUnsupported(RuntimeError):
    """Raised inside tests when they use something that cannot be proxied."""


class SolutionGone(RuntimeError):
    """The solution side ended or broke the protocol."""


def main() -> None:
    out = os.fdopen(os.dup(1), "w", buffering=1)
    sys.stdout = sys.stderr
    problems: list[int] = []

    def send(obj: Any) -> None:
        try:
            out.write(json.dumps(obj, allow_nan=False, separators=(",", ":")) + "\n")
            out.flush()
        except (BrokenPipeError, ValueError, OSError) as e:
            problems.append(5)
            raise SolutionGone("the solution process is gone") from e

    def recv() -> dict[str, Any]:
        line = sys.stdin.readline(MAX_LINE + 1)
        if not line or len(line) > MAX_LINE:
            problems.append(5)
            raise SolutionGone("the solution process ended or sent an oversized message")
        try:
            msg = json.loads(line)
        except (ValueError, RecursionError) as e:
            problems.append(5)
            raise SolutionGone("the solution process sent a malformed message") from e
        if type(msg) is not dict:
            problems.append(5)
            raise SolutionGone("the solution process sent a malformed message")
        return msg

    init = json.loads(sys.stdin.readline())
    tests = init["tests"]
    send({"op": "names"})
    first = recv()
    if "fatal" in first:
        print(f"solution failed to import: {first['fatal']}", file=sys.stderr)
        sys.exit(1)
    exported = first["names"]

    def remap(err: dict[str, Any]) -> Exception:
        typ, msg = str(err.get("type")), str(err.get("msg", ""))
        if typ in ("Unsupported", "ProtocolError"):
            problems.append(4)
            return IsolationUnsupported(f"{typ}: {msg}")
        cls = getattr(builtins, typ, None)
        if isinstance(cls, type) and issubclass(cls, Exception):
            try:
                return cls(msg)
            except TypeError:
                pass
        return RuntimeError(f"{typ}: {msg}")

    def make_proxy(name: str) -> Any:
        def proxy(*args: Any, **kwargs: Any) -> Any:
            try:
                payload = {"op": "call", "name": name, "args": encode(list(args)), "kwargs": encode(kwargs)}
            except Unsupported as e:
                problems.append(4)
                raise IsolationUnsupported(f"arguments to {name}() cannot cross the boundary: {e}") from e
            send(payload)
            reply = recv()
            if "ok" in reply:
                try:
                    return decode(reply["ok"])
                except ProtocolError as e:
                    problems.append(5)
                    raise SolutionGone(f"malformed result from {name}(): {e}") from e
            if "err" in reply and type(reply["err"]) is dict:
                raise remap(reply["err"])
            problems.append(5)
            raise SolutionGone("the solution process sent a malformed reply")

        proxy.__name__ = name
        return proxy

    def make_unsupported(name: str, why: str) -> Any:
        def stub(*args: Any, **kwargs: Any) -> Any:
            problems.append(4)
            raise IsolationUnsupported(f"{name}: {why}")

        stub.__name__ = name
        return stub

    mod = types.ModuleType("solution")
    for name, info in exported.items():
        kind = info.get("kind")
        if kind == "callable":
            setattr(mod, name, make_proxy(name))
        elif kind == "value":
            setattr(mod, name, decode(info["v"]))
        else:
            setattr(mod, name, make_unsupported(name, str(info.get("why", "unsupported"))))
    mod.__dict__["__all__"] = list(exported)
    sys.modules["solution"] = mod

    ns: dict[str, Any] = {"__name__": "__tests__"}
    try:
        exec("from solution import *", ns)  # noqa: S102 (inside container T: the task's own tests)
        exec(compile(tests, "tests", "exec"), ns)  # noqa: S102
    except (IsolationUnsupported, SolutionGone) as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(4 if isinstance(e, IsolationUnsupported) else 5)
    # A test that swallowed the exception must still not pass: fail closed.
    if problems:
        print(
            "a construct or message could not cross the isolation boundary (swallowed by the tests)",
            file=sys.stderr,
        )
        sys.exit(max(problems))
