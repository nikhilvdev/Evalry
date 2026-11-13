"""Samples for tier 3 (the model edits files from the repository text) and tier 4 (an agent works in a sandbox)."""

import hashlib
import io
import tarfile

from evalry.agent.loop import AgentModel, run_agent
from evalry.generation.base import Generator, generate_logged
from evalry.graders.repo_tests import repo_tests
from evalry.repo_tasks import (
    MAX_FILES,
    MAX_UNPACKED_BYTES,
    Files,
    RepoError,
    extract_edits,
    render_edits,
    repo_prompt,
    workspace_edits,
)
from evalry.runner.repo import AgentContainer
from evalry.runner.sandbox import SEED
from evalry.safepath import safe_relpath
from evalry.schemas import AgentMetrics, SampleResult, Task
from evalry.scoring.stats import composite


def _result(
    task: Task,
    edits: str,
    model: str,
    sample: int,
    score: object,
    hard_fail: bool,
    cost: float,
    agent: AgentMetrics | None,
) -> SampleResult:
    assert task.repo
    return SampleResult(
        task_id=task.id, task_hash=hashlib.sha256(task.model_dump_json().encode()).hexdigest(), sample=sample,
        generator_model=model, code=edits, scores=[score], composite=composite([score], {}, hard_fail=hard_fail),  # type: ignore[list-item]
        cost_usd=cost, image=task.repo.image, seed=SEED,
        isolation="inprocess",  # edited code and hidden tests share a container (see runner.repo), so this can never hard-gate
        agent=agent,
    )  # fmt: skip


def run_repo_sample(
    task: Task, generator: Generator, sample: int, files: Files, *, timeout_s: float = 300
) -> SampleResult:
    """Tier 3: show the model the repository and the issue (never the hidden tests), apply its edits, run the repo's tests."""
    from evalry.schemas import GraderScore

    out = generate_logged(generator, repo_prompt(task.prompt, files), sample=sample)
    try:
        edits: dict[str, str | None] = dict(extract_edits(out.text))
    except RepoError as e:
        bad = GraderScore(grader="tests", grader_version="1", score=0.0, evidence=f"rejected: {e}"[:500])
        return _result(task, "", out.model, sample, bad, False, out.cost_usd, None)
    score, hard_fail = repo_tests(task, files, edits, timeout_s=timeout_s)
    return _result(task, render_edits(edits), out.model, sample, score, hard_fail, out.cost_usd, None)


def read_export(tar_bytes: bytes) -> Files:
    """Files from an agent's workspace export. Lenient where the agent can influence the shape: links and odd members are skipped."""
    files: Files = {}
    total = 0
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as tar:
        for m in tar:
            name = m.name.removeprefix("./")
            if not m.isreg() or not name:
                continue
            try:
                safe_relpath(name)
            except ValueError:
                continue
            total += m.size
            if len(files) >= MAX_FILES or total > MAX_UNPACKED_BYTES:
                raise RepoError("The agent's workspace is over the file or size limit.")
            f = tar.extractfile(m)
            assert f is not None
            files[name] = f.read(m.size)
    return files


def run_agent_sample(
    task: Task, agent: AgentModel, sample: int, files: Files, *, timeout_s: float = 300
) -> SampleResult:
    """Tier 4: the agent works in a sandbox; its diff is then graded in a fresh container with the hidden tests."""
    from evalry.repo_tasks import build_workspace
    from evalry.schemas import GraderScore

    assert task.repo and task.agent
    with AgentContainer(build_workspace(files), task.repo.image) as ws:
        run = run_agent(task, agent, ws, task.agent)
        edits = workspace_edits(files, read_export(ws.export())) if not run.workspace_lost else {}
    metrics = run.metrics
    if run.workspace_lost:
        score, hard_fail = (
            GraderScore(
                grader="tests",
                grader_version="1",
                score=0.0,
                evidence="sandbox was reset by a timed-out command",
            ),
            False,
        )
    else:
        score, hard_fail = repo_tests(task, files, edits, timeout_s=timeout_s)
    if score.score < 1.0:
        metrics = metrics.model_copy(
            update={"failure_categories": sorted({*metrics.failure_categories, "tests_failed"})}
        )
    return _result(
        task, render_edits(edits), run.model or "agent", sample, score, hard_fail, run.cost_usd, metrics
    )
