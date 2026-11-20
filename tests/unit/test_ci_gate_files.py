"""The CI gate's checked-in inputs must stay consistent with the suite; the workflow itself runs them with Docker."""

import json
from pathlib import Path

import yaml

from evalry.gates.hard import HardGateRule
from evalry.scheduler.suite import load_suite

GATE = Path(__file__).parents[1] / "golden" / "gate"
SUITE = Path(__file__).parents[1] / "golden" / "suite"


def test_rule_and_replies_cover_the_suite() -> None:
    rule = HardGateRule.model_validate(yaml.safe_load((GATE / "rule.yaml").read_text()))
    replies = json.loads((GATE / "replies.json").read_text())
    ids = {t.id for t in load_suite(SUITE)}
    assert ids == set(rule.required_tasks) == set(replies)
