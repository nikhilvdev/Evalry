"""Content-addressed grading cache (plan §15): same code + tests + grader + image + seed = same score, so skip the sandbox."""

import hashlib
import json
import os
import tempfile
import threading
from pathlib import Path

from pydantic import ValidationError

from evalry.graders import DEFAULT_ISOLATION, PYTHON_TESTS_VERSION, Isolation
from evalry.log import get_logger
from evalry.runner import PYTHON_IMAGE, SEED
from evalry.schemas import GraderScore, Task

log = get_logger("cache")


class GradeCache:
    """On-disk, one JSON file per key. Entries hold grader evidence (stderr tail), so treat the directory like run records."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.hits = 0
        self.misses = 0
        self._count = threading.Lock()  # workers share one cache

    @staticmethod
    def key(task: Task, code: str, isolation: Isolation = DEFAULT_ISOLATION) -> str:
        parts = [PYTHON_TESTS_VERSION, PYTHON_IMAGE, SEED, isolation, task.tests, code]
        return hashlib.sha256(json.dumps(parts).encode()).hexdigest()

    def get(self, key: str) -> tuple[GraderScore, bool] | None:
        try:
            raw = json.loads((self.directory / f"{key}.json").read_text())
            entry = GraderScore.model_validate(raw["score"]), bool(raw["hard_fail"])
        except FileNotFoundError:
            self._bump(miss=True)
            return None
        except (OSError, ValueError, KeyError, ValidationError):
            log.warn("corrupt cache entry ignored", key=key)  # noqa: G010 (LogQuill API)
            self._bump(miss=True)
            return None
        self._bump(miss=False)
        return entry

    def _bump(self, *, miss: bool) -> None:
        with self._count:
            if miss:
                self.misses += 1
            else:
                self.hits += 1

    def put(self, key: str, score: GraderScore, hard_fail: bool) -> None:
        """Atomic write so a crash or a parallel run never leaves a half-written entry."""
        payload = json.dumps({"score": score.model_dump(), "hard_fail": hard_fail})
        fd, tmp = tempfile.mkstemp(dir=self.directory)
        with os.fdopen(fd, "w") as f:
            f.write(payload)
        os.replace(tmp, self.directory / f"{key}.json")
