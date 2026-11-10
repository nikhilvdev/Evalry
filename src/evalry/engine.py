"""Grade and sample: the engine steps shared by the CLI, suite runs and (later) scheduler jobs."""

import hashlib

from evalry.cache import GradeCache
from evalry.generation import Generator, extract_code, generate_logged
from evalry.graders import python_tests
from evalry.runner import PYTHON_IMAGE, SEED
from evalry.schemas import SampleResult, Task
from evalry.scoring import composite


def grade(
    task: Task,
    code: str,
    *,
    timeout_s: float = 10,
    generator_model: str = "local-file",
    sample: int = 0,
    cost_usd: float = 0,
    cache: GradeCache | None = None,
) -> SampleResult:
    """Grade one solution. Only Python tasks have a grader so far."""
    if task.language != "python":
        raise ValueError(
            f"Task {task.id!r} is {task.language}; only python is supported. Add a language profile first."
        )
    key = GradeCache.key(task, code)
    if cache and (hit := cache.get(key)):
        score, hard_fail = hit
    else:
        score, hard_fail = python_tests(task, code, timeout_s=timeout_s)
        # A timeout depends on machine load, not just inputs, so it is never cached.
        if cache and not hard_fail:
            cache.put(key, score, hard_fail)
    return SampleResult(
        task_id=task.id,
        task_hash=hashlib.sha256(task.model_dump_json().encode()).hexdigest(),
        sample=sample,
        generator_model=generator_model,
        code=code,
        scores=[score],
        composite=composite([score], {}, hard_fail=hard_fail),
        cost_usd=cost_usd,
        image=PYTHON_IMAGE,
        seed=SEED,
    )


def run_sample(
    task: Task, generator: Generator, sample: int, *, timeout_s: float = 10, cache: GradeCache | None = None
) -> SampleResult:
    """Generate (prompt only, never the tests), extract the code, grade it."""
    out = generate_logged(generator, task.prompt, sample=sample)
    return grade(
        task,
        extract_code(out.text),
        timeout_s=timeout_s,
        generator_model=out.model,
        sample=sample,
        cost_usd=out.cost_usd,
        cache=cache,
    )
