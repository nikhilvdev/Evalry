"""Append-only, hash-chained audit log (plan §8, §9).

Every run, job and decision is recorded with who did it, what, to what, and when. Each event carries the hash of the one
before it, so editing or deleting a line breaks every hash after it. Events live in a sink: a local JSONL file (one chain,
for local mode and tests) or PostgreSQL (one chain per tenant, see ``evalry.pgstore``). Events are also mirrored to LogQuill
with ``TamperEvidentPlugin`` as a second, independent check.
"""

import hashlib
import json
import os
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

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


class AuditSink(Protocol):
    """Where sealed events are stored. The sink owns sequencing, so several writers cannot fork a chain."""

    def append(self, tenant_id: str | None, make: Callable[[int, str], AuditEvent]) -> AuditEvent:
        """Call ``make(seq, prev_hash)`` while holding the chain, store the event it returns, and return it."""
        ...

    def events(self, tenant_id: str | None = None) -> list[AuditEvent]: ...


class FileSink:
    """One chain in a local JSONL file. ``tenant_id`` is ignored: local mode has a single chain."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
        last = self.events()[-1:]
        self._seq = last[0].seq + 1 if last else 0
        self._head = last[0].hash if last else GENESIS

    def events(self, tenant_id: str | None = None) -> list[AuditEvent]:
        try:
            return [
                AuditEvent.model_validate_json(line) for line in self.path.read_text().splitlines() if line
            ]
        except ValidationError as e:
            raise ValueError(
                f"Audit log {self.path} has an unreadable line; do not append until it is investigated."
            ) from e

    def append(self, tenant_id: str | None, make: Callable[[int, str], AuditEvent]) -> AuditEvent:
        with self._lock:
            event = make(self._seq, self._head)
            with self.path.open("a") as f:
                f.write(event.model_dump_json() + "\n")
                f.flush()
                os.fsync(f.fileno())
            self._seq, self._head = event.seq + 1, event.hash
            return event


class AuditLog:
    def __init__(self, target: Path | AuditSink) -> None:
        self._sink: AuditSink = FileSink(target) if isinstance(target, Path) else target
        self.path: Path | None = target if isinstance(target, Path) else None

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

        def seal(seq: int, prev_hash: str) -> AuditEvent:
            event = AuditEvent(
                seq=seq, ts=datetime.now(UTC), actor=actor, action=action, target=target, source=source,
                tenant_id=tenant_id, project_id=project_id, details=details, prev_hash=prev_hash,
            )  # fmt: skip
            return event.model_copy(update={"hash": _digest(event)})

        event = self._sink.append(tenant_id, seal)
        log.info(action, actor=actor, target=target, source=source, project_id=project_id, seq=event.seq)
        log.flush()  # the mirror is written before append returns, so a crash cannot lose the last event's copy
        return event

    def events(self, tenant_id: str | None = None) -> list[AuditEvent]:
        return self._sink.events(tenant_id)

    def head(self, tenant_id: str | None = None) -> str:
        """Latest hash of the chain. Store it somewhere the log's writers cannot reach to detect truncation."""
        last = self._sink.events(tenant_id)[-1:]
        return last[0].hash if last else GENESIS

    def verify(self, expected_head: str | None = None, tenant_id: str | None = None) -> list[str]:
        """Problems found, empty when the chain is intact. ``expected_head`` also catches deleted trailing events."""
        problems: list[str] = []
        prev = GENESIS
        for i, e in enumerate(self._sink.events(tenant_id)):
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
