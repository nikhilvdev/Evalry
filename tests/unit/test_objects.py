import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from evalry.access.store import Principal, Store
from evalry.audit.chain import AuditLog
from evalry.schemas import RunRecord, SampleResult
from evalry.storage.backend import MemoryBackend
from evalry.storage.objects import FileObjectStore, ObjectError, ObjectStore, S3ObjectStore


class NoSuchKey(Exception):
    pass


class FakeS3:
    """Just enough of the boto3 client surface that S3ObjectStore uses."""

    class exceptions:
        NoSuchKey = NoSuchKey

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> None:
        self.objects[(Bucket, Key)] = Body

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        if (Bucket, Key) not in self.objects:
            raise NoSuchKey
        data = self.objects[(Bucket, Key)]
        return {"Body": type("B", (), {"read": lambda self: data})()}


@pytest.fixture(params=["file", "s3"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> ObjectStore:
    return FileObjectStore(tmp_path) if request.param == "file" else S3ObjectStore(FakeS3(), "b")


def test_round_trip_is_content_addressed_and_idempotent(store: ObjectStore) -> None:
    d = store.put("t1", "p1", b"code")
    assert d == hashlib.sha256(b"code").hexdigest() == store.put("t1", "p1", b"code")
    assert store.get("t1", "p1", d) == b"code"
    assert store.put("t1", "p1", b"") and store.get("t1", "p1", "0" * 64) is None


def test_tenants_and_projects_cannot_read_each_other(store: ObjectStore) -> None:
    d = store.put("t1", "p1", b"secret")
    assert store.get("t2", "p1", d) is None and store.get("t1", "p2", d) is None


@pytest.mark.parametrize("tenant", ["../t2", "T1", "", "a/b", "a" * 64])
def test_ids_that_could_escape_the_prefix_are_rejected(store: ObjectStore, tenant: str) -> None:
    with pytest.raises(ObjectError, match="invalid tenant_id"):
        store.put(tenant, "p1", b"x")
    with pytest.raises(ObjectError, match="invalid digest"):
        store.get("t1", "p1", "../../etc/passwd")


def test_a_tampered_object_is_rejected(tmp_path: Path) -> None:
    fs = FileObjectStore(tmp_path)
    d = fs.put("t1", "p1", b"good")
    (tmp_path / "t1" / "p1" / d[:2] / d).write_bytes(b"evil")
    with pytest.raises(ObjectError, match="tampered"):
        fs.get("t1", "p1", d)
    s3 = FakeS3()
    S3ObjectStore(s3, "b").put("t1", "p1", b"good")
    ((k,),) = [(k,) for k in s3.objects]
    s3.objects[k] = b"evil"
    with pytest.raises(ObjectError, match="tampered"):
        S3ObjectStore(s3, "b").get("t1", "p1", d)


# -- wired into runs ----------------------------------------------------------------------------------------------------


def _store_with_objects(tmp_path: Path) -> tuple[Store, MemoryBackend, FileObjectStore]:
    backend, objects = MemoryBackend(), FileObjectStore(tmp_path / "obj")
    store = Store(AuditLog(tmp_path / "audit.jsonl"), backend, objects)
    for t in ("acme", "globex"):
        store.bootstrap_tenant(t, f"{t}-admin", operator="ops")
        store.view(Principal(f"{t}-admin", t)).create_project("app")
    return store, backend, objects


def _record(code: str = "def f(): return 1") -> RunRecord:
    r = SampleResult(
        task_id="t", task_hash="h", sample=0, generator_model="m", code=code, scores=[], composite=1.0
    )
    return RunRecord(
        run_id="r1", suite_version="s1", started_at=datetime(2026, 11, 11, tzinfo=UTC), results=[r]
    )


def test_run_code_goes_to_object_storage_and_reads_back(tmp_path: Path) -> None:
    store, backend, objects = _store_with_objects(tmp_path)
    alice = store.view(Principal("acme-admin", "acme"))
    alice.put_run("app", _record())
    row = backend.get_run("acme", "app", "r1")
    assert row is not None
    only = row.results[0]
    assert only.code == "" and only.code_digest == hashlib.sha256(b"def f(): return 1").hexdigest()
    assert alice.get_run("app", "r1").results[0].code == "def f(): return 1"
    assert objects.get("globex", "app", only.code_digest) is None  # other tenants have no such object


def test_missing_object_is_an_error_not_empty_code(tmp_path: Path) -> None:
    store, _, _ = _store_with_objects(tmp_path)
    alice = store.view(Principal("acme-admin", "acme"))
    alice.put_run("app", _record())
    for f in (tmp_path / "obj").rglob("*"):
        if f.is_file():
            f.unlink()
    with pytest.raises(ObjectError, match="missing from object storage"):
        alice.get_run("app", "r1")


def test_without_an_object_store_code_stays_inline(tmp_path: Path) -> None:
    backend = MemoryBackend()
    store = Store(AuditLog(tmp_path / "a.jsonl"), backend)
    store.bootstrap_tenant("acme", "root", operator="ops")
    root = store.view(Principal("root", "acme"))
    root.create_project("app")
    root.put_run("app", _record())
    row = backend.get_run("acme", "app", "r1")
    assert (
        row is not None and row.results[0].code == "def f(): return 1" and row.results[0].code_digest is None
    )
