"""Append-only, hash-chained audit log (plan §8, §9).

Every run, job and decision is recorded with who did it, what, to what, and when. Each event carries the hash of the one
before it, so editing or deleting a line breaks every hash after it. shortcut: a local JSONL file stands in for the
append-only Postgres/object store (system of record, phase 2 storage work). Events are also mirrored to LogQuill with
``TamperEvidentPlugin`` as a second, independent check.
"""

import hashlib
import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path

from logquill import TamperEvidentPlugin
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from evalry.log import get_logger

GENESIS = "0" * 64
_MAX_DETAIL = 256

log = get_logger("audit").use(TamperEvidentPlugin())


class AuditEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int = Field(ge=0)
    ts: datetime
    actor: str = Field(min_length=1)
    action: str = Field(min_length=1, description="Dotted verb, e.g. run.submitted, gate.approved.")
    target: str = Field(min_length=1)
    source: str = Field(min_length=1, description="Where the action came from: cli, api, worker.")
    tenant_id: str | None = None
    project_id: str | None = None
    details: dict[str, str] = {}
    prev_hash: str
    hash: str = ""


def _digest(event: AuditEvent) -> str:
    body = event.model_dump(mode="json", exclude={"hash"})
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
        last = self._read()[-1:] or []
        self._seq = last[0].seq + 1 if last else 0
        self._head = last[0].hash if last else GENESIS

    def _read(self) -> list[AuditEvent]:
        try:
            return [
                AuditEvent.model_validate_json(line) for line in self.path.read_text().splitlines() if line
            ]
        except ValidationError as e:
            raise ValueError(
                f"Audit log {self.path} has an unreadable line; do not append until it is investigated."
            ) from e

    def append(
        self,
        actor: str,
        action: str,
        target: str,
        *,
        source: str,
        tenant_id: str | None = None,
        project_id: str | None = None,
        details: dict[str, str] | None = None,
    ) -> AuditEvent:
        """``details`` holds ids and hashes, never content: values over 256 characters are rejected."""
        details = details or {}
        if too_long := [k for k, v in details.items() if len(v) > _MAX_DETAIL]:
            raise ValueError(
                f"Audit detail {too_long} is over {_MAX_DETAIL} characters. Store ids or hashes, not content."
            )
        with self._lock:
            event = AuditEvent(
                seq=self._seq, ts=datetime.now(UTC), actor=actor, action=action, target=target, source=source,
                tenant_id=tenant_id, project_id=project_id, details=details, prev_hash=self._head,
            )  # fmt: skip
            event = event.model_copy(update={"hash": _digest(event)})
            with self.path.open("a") as f:
                f.write(event.model_dump_json() + "\n")
                f.flush()
                os.fsync(f.fileno())
            self._seq, self._head = event.seq + 1, event.hash
        log.info(action, actor=actor, target=target, source=source, project_id=project_id, seq=event.seq)
        log.flush()  # the mirror is written before append returns, so a crash cannot lose the last event's copy
        return event

    def head(self) -> str:
        """Latest hash. Store it somewhere the log's writers cannot reach to detect truncation."""
        return self._head

    def events(self) -> list[AuditEvent]:
        return self._read()

    def verify(self, expected_head: str | None = None) -> list[str]:
        """Problems found, empty when the chain is intact. ``expected_head`` also catches deleted trailing events."""
        problems: list[str] = []
        prev = GENESIS
        events = self._read()
        for i, e in enumerate(events):
            if e.seq != i:
                problems.append(f"event {i}: sequence is {e.seq}, expected {i} (event removed or reordered)")
            if e.prev_hash != prev:
                problems.append(f"event {i}: prev_hash does not match the previous event")
            if e.hash != _digest(e):
                problems.append(f"event {i}: content does not match its hash (edited)")
            prev = e.hash
        if expected_head is not None and prev != expected_head:
            problems.append("head does not match the expected head (events removed from the end, or added)")
        return problems
