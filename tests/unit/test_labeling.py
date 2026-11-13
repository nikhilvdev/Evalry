from datetime import UTC, datetime, timedelta

import pytest

from evalry import labeling
from evalry.labeling import Label, LabelItemIn

T0 = datetime(2026, 11, 13, tzinfo=UTC)


def batch(n: int = 10, share: float = 0.2) -> labeling.LabelBatch:
    items = labeling.make_items([LabelItemIn(prompt=f"p{i}", code=f"c{i}") for i in range(n)], share)
    return labeling.LabelBatch(batch_id="b", guideline="g", items=items, created_by="x", created_at=T0)


def lab(item: int, who: str, score: int, kind: str = "review", at: int = 0) -> Label:
    return Label(
        batch_id="b",
        item_id=item,
        reviewer=who,
        score=score,
        kind=kind,
        created_at=T0 + timedelta(seconds=at),
    )  # type: ignore[arg-type]


def test_double_review_share_is_spread_evenly_and_deterministic() -> None:
    needs = [i.reviews_needed for i in batch(10, 0.2).items]
    assert needs.count(2) == 2 and needs == [i.reviews_needed for i in batch(10, 0.2).items]
    assert {i.reviews_needed for i in batch(5, 0).items} == {1} and {
        i.reviews_needed for i in batch(5, 1).items
    } == {2}
    with pytest.raises(ValueError):
        labeling.make_items([], 0.2)


def test_items_are_blind_the_schema_has_no_room_for_generator_or_judge_score() -> None:
    with pytest.raises(ValueError):
        LabelItemIn(prompt="p", code="c", generator="claude", judge_score=5)  # type: ignore[call-arg]
    assert set(batch(1).items[0].model_dump()) == {"prompt", "code", "item_id", "reviews_needed"}


def test_queue_hands_each_reviewer_unscored_open_items_only() -> None:
    b = batch(3, 1.0)  # every item needs two reviews
    labels = [lab(0, "ann", 3)]
    assert labeling.next_item(b, labels, "ann").item_id == 1  # type: ignore[union-attr]
    assert labeling.next_item(b, labels, "bob").item_id == 0  # type: ignore[union-attr]
    labels += [lab(0, "bob", 3, at=1), lab(1, "ann", 2, at=2), lab(2, "ann", 2, at=3)]
    assert labeling.next_item(b, labels, "ann") is None  # ann has scored everything
    assert labeling.next_item(b, labels, "bob").item_id == 1  # type: ignore[union-attr]


def test_states_disagreement_needs_adjudication_and_scores_are_never_averaged() -> None:
    b = batch(4, 1.0)
    labels = [lab(0, "a", 3), lab(0, "b", 3, at=1), lab(1, "a", 2), lab(1, "b", 4, at=1), lab(2, "a", 5)]
    st = {i.item_id: labeling.state(i, labels) for i in b.items}
    assert st == {0: "final", 1: "disagreement", 2: "open", 3: "open"}
    assert labeling.final_score(b.items[0], labels) == 3 and labeling.final_score(b.items[1], labels) is None
    labels.append(lab(1, "boss", 5, "adjudication", at=2))
    assert labeling.final_score(b.items[1], labels) == 5  # the adjudicator's score, not 3
    assert labeling.state(b.items[1], labels) == "final"


def test_a_third_racing_review_is_ignored() -> None:
    b = batch(1, 0)  # one review needed
    labels = [lab(0, "late", 1, at=5), lab(0, "first", 4, at=1)]
    assert labeling.final_score(b.items[0], labels) == 4


def test_agreement_flags_a_guideline_that_reviewers_cannot_apply() -> None:
    b = batch(4, 1.0)
    split = [lab(i, "a", 1) for i in range(4)] + [lab(i, "b", 5, at=1) for i in range(4)]
    s = labeling.status(b, split)
    assert (s.double_reviewed, s.exact_agreement, s.within_one_agreement) == (4, 0.0, 0.0)
    assert s.guideline_needs_work and s.disagreeing_items == [0, 1, 2, 3]
    close = [lab(i, "a", 3) for i in range(4)] + [lab(i, "b", 4, at=1) for i in range(4)]
    s = labeling.status(b, close)
    assert s.exact_agreement == 0.0 and s.within_one_agreement == 1.0 and not s.guideline_needs_work
    assert labeling.status(b, []).within_one_agreement is None


def test_export_refuses_until_every_item_is_final() -> None:
    b = batch(2, 0)
    with pytest.raises(ValueError, match="2 of 2"):
        labeling.export(b, [])
    out = labeling.export(b, [lab(0, "a", 5), lab(1, "a", 1)])
    assert [(x.code, x.human_score) for x in out] == [("c0", 5), ("c1", 1)]
