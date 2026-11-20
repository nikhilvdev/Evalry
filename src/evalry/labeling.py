"""Human labeling (plan §20): turns reviewer scores into the golden labels judge calibration needs.

Items are blind by construction: a batch holds only the task prompt and the code, never the generator's name or the judge's
score. Some items are scored by two reviewers; when they differ, an approver adjudicates (the guideline is fixed when
agreement is low, scores are not averaged). Everything is append-only: state is derived here from the batch and its labels.
"""

from collections import Counter
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from evalry.judge.calibration import Labeled

AGREEMENT_TARGET = 0.75  # within one point, same bar as judge-vs-human


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LabelItemIn(_Strict):
    prompt: str = Field(min_length=1, max_length=200_000)
    code: str = Field(max_length=200_000)


class LabelItem(LabelItemIn):
    item_id: int = Field(ge=0)
    reviews_needed: Literal[1, 2]


class LabelBatch(_Strict):
    batch_id: str
    guideline: str = Field(
        min_length=1, max_length=50_000, description="Scoring guidance with examples of a 1, 3 and 5."
    )
    items: list[LabelItem]
    created_by: str
    created_at: datetime


class Label(_Strict):
    batch_id: str
    item_id: int
    reviewer: str
    score: int = Field(ge=1, le=5)
    kind: Literal["review", "adjudication"] = "review"
    created_at: datetime


def make_items(items: list[LabelItemIn], double_share: float) -> list[LabelItem]:
    """Spread the doubly-reviewed share evenly through the batch (deterministic, so it can be re-derived)."""
    if not items:
        raise ValueError("A labeling batch needs at least one item.")
    if not 0 <= double_share <= 1:
        raise ValueError("double_share must be between 0 and 1.")
    return [
        LabelItem(item_id=i, **it.model_dump(), reviews_needed=2 if int((i + 1) * double_share) > int(i * double_share) else 1)
        for i, it in enumerate(items)
    ]  # fmt: skip


def _reviews(labels: list[Label], item: LabelItem) -> list[Label]:
    """The first ``reviews_needed`` reviews in time order; a late extra (two reviewers racing) is kept on record but ignored."""
    mine = sorted(
        (x for x in labels if x.item_id == item.item_id and x.kind == "review"), key=lambda x: x.created_at
    )
    return mine[: item.reviews_needed]


def state(item: LabelItem, labels: list[Label]) -> Literal["open", "disagreement", "final"]:
    if any(x.item_id == item.item_id and x.kind == "adjudication" for x in labels):
        return "final"
    got = _reviews(labels, item)
    if len(got) < item.reviews_needed:
        return "open"
    return "final" if len({x.score for x in got}) == 1 else "disagreement"


def final_score(item: LabelItem, labels: list[Label]) -> int | None:
    if state(item, labels) != "final":
        return None
    adj = [x for x in labels if x.item_id == item.item_id and x.kind == "adjudication"]
    return adj[0].score if adj else _reviews(labels, item)[0].score


def next_item(batch: LabelBatch, labels: list[Label], reviewer: str) -> LabelItem | None:
    """First item still needing a review that this reviewer has not scored."""
    for item in batch.items:
        if state(item, labels) == "open" and not any(
            x.item_id == item.item_id and x.reviewer == reviewer for x in labels
        ):
            return item
    return None


class Status(_Strict):
    batch_id: str
    items: int
    by_state: dict[str, int]
    double_reviewed: int
    exact_agreement: float | None
    within_one_agreement: float | None
    guideline_needs_work: bool = Field(
        description="Reviewers agree within one point less often than the target: fix the guideline."
    )
    disagreeing_items: list[int]


def status(batch: LabelBatch, labels: list[Label]) -> Status:
    states = {i.item_id: state(i, labels) for i in batch.items}
    pairs = [
        [x.score for x in _reviews(labels, i)]
        for i in batch.items
        if i.reviews_needed == 2 and len(_reviews(labels, i)) == 2
    ]
    exact = sum(a == b for a, b in pairs) / len(pairs) if pairs else None
    near = sum(abs(a - b) <= 1 for a, b in pairs) / len(pairs) if pairs else None
    return Status(
        batch_id=batch.batch_id, items=len(batch.items), by_state=dict(Counter(states.values())),
        double_reviewed=len(pairs), exact_agreement=exact, within_one_agreement=near,
        guideline_needs_work=near is not None and near < AGREEMENT_TARGET,
        disagreeing_items=[i for i, s in states.items() if s == "disagreement"],
    )  # fmt: skip


def export(batch: LabelBatch, labels: list[Label]) -> list[Labeled]:
    """Golden labels for ``evalry judge-calibrate``; refuses until every item has a final score."""
    scores = {i.item_id: final_score(i, labels) for i in batch.items}
    if missing := sorted(k for k, v in scores.items() if v is None):
        raise ValueError(
            f"{len(missing)} of {len(batch.items)} items have no final label yet (first: {missing[:5]}). "
            "Finish the reviews and adjudicate disagreements first."
        )
    return [Labeled(prompt=i.prompt, code=i.code, human_score=scores[i.item_id]) for i in batch.items]  # type: ignore[arg-type]
