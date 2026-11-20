"""Grade and sample: the engine steps shared by the CLI, suite runs and (later) scheduler jobs."""

import hashlib

from evalry.agent.loop import AgentModel
from evalry.generation.base import Generator, extract_code, generate_logged
from evalry.graders.cache import GradeCache
from evalry.graders.python_tests import DEFAULT_ISOLATION, Isolation, python_tests
from evalry.judge.base import Judge, judge_logged
from evalry.repo_tasks import Files
from evalry.runner.sandbox import PYTHON_IMAGE, SEED
from evalry.scheduler.repo_engine import run_agent_sample, run_repo_sample
from evalry.schemas import GraderScore, SampleResult, Task
from evalry.scoring.stats import composite


def grade(
    task: Task,
    code: str,
    *,
    timeout_s: float = 10,
    isolation: Isolation = DEFAULT_ISOLATION,
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
    key = GradeCache.key(task, code, isolation)
    if cache and (hit := cache.get(key)):
        score, hard_fail = hit
    else:
        score, hard_fail = python_tests(task, code, timeout_s=timeout_s, isolation=isolation)
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
        isolation=isolation,
        seed=SEED,
    )


def run_sample(
    task: Task,
    generator: Generator,
    sample: int,
    *,
    timeout_s: float = 10,
    cache: GradeCache | None = None,
    isolation: Isolation = DEFAULT_ISOLATION,
    judge: Judge | None = None,
    judge_weight: float = 0.0,
    snapshot: Files | None = None,
    agent: AgentModel | None = None,
) -> SampleResult:
    """Generate (prompt only, never the tests), extract the code, grade it, and judge it if it passed everything.

    Only solutions that pass every deterministic grader are judged (plan §15), so judge spend follows p_judge. The judge score
    is always recorded raw; it counts toward the composite only with a positive ``judge_weight``, so an uncalibrated judge
    stays advisory by default (plan §6).
    """
    if task.tier >= 3:
        # Repository and agent tasks have their own path. They are advisory only (see runner.repo) and are not judged.
        if snapshot is None:
            raise ValueError(
                f"Task {task.id!r} is tier {task.tier}: it needs its repository snapshot. Run it through a worker with an "
                "object store (evalry worker --object-dir ...), not the local suite command."
            )
        if task.tier == 4:
            if agent is None:
                raise ValueError(
                    f"Task {task.id!r} is an agent task: the worker needs an agent model (--generator-url)."
                )
            return run_agent_sample(task, agent, sample, snapshot, timeout_s=max(timeout_s, 300))
        return run_repo_sample(task, generator, sample, snapshot, timeout_s=max(timeout_s, 300))
    out = generate_logged(generator, task.prompt, sample=sample)
    result = grade(
        task,
        extract_code(out.text),
        timeout_s=timeout_s,
        isolation=isolation,
        generator_model=out.model,
        sample=sample,
        cost_usd=out.cost_usd,
        cache=cache,
    )
    if judge is None or result.composite == 0 or any(sc.score < 1.0 for sc in result.scores):
        return result
    verdict = judge_logged(judge, task.prompt, result.code, sample=sample)
    scores = [
        *result.scores,
        GraderScore(
            grader="judge", grader_version=verdict.judge_version, score=(verdict.score - 1) / 4,
            evidence=verdict.rationale,
        ),
    ]  # fmt: skip
    return result.model_copy(
        update={
            "scores": scores,
            "composite": composite(scores, {"judge": judge_weight}),
            "cost_usd": result.cost_usd + verdict.cost_usd,
            "judge_version": verdict.judge_version,
        }
    )
