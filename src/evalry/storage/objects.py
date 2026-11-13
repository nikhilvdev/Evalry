"""Content-addressed object storage for code, logs, raw responses and reports (plan §8).

An object's key is the SHA-256 of its bytes, so it can be written twice harmlessly and a read that does not hash back to its
key is rejected as tampered or corrupt. Objects live under ``tenant/project/`` and both ids are validated before they reach a
path or key, so no caller-supplied id can escape its prefix. Stores hold bytes only; who may read them is decided by the
access layer, which must check the project role before calling ``get``. Blobs are not logged. ``FileObjectStore`` is for dev
and tests; ``S3ObjectStore`` talks to any S3-compatible service (MinIO locally, S3 in production).
"""

import hashlib
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from evalry.log import get_logger

log = get_logger("objects")
_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_DIGEST = re.compile(r"[0-9a-f]{64}")


class ObjectError(Exception):
    """The id or digest is malformed, or a stored object does not match its digest."""


class ObjectStore(Protocol):
    def put(self, tenant_id: str, project_id: str, data: bytes) -> str:
        """Store the bytes and return their digest. Storing the same bytes again is a no-op."""
        ...

    def get(self, tenant_id: str, project_id: str, digest: str) -> bytes | None:
        """The bytes, or None if absent. Raises ObjectError if what is stored does not hash to ``digest``."""
        ...


def _key(tenant_id: str, project_id: str, digest: str | None = None) -> str:
    for name, value in (("tenant_id", tenant_id), ("project_id", project_id)):
        if not _ID.fullmatch(value):
            raise ObjectError(f"invalid {name} {value!r}: use lowercase letters, digits and '-'.")
    if digest is None:
        return f"{tenant_id}/{project_id}"
    if not _DIGEST.fullmatch(digest):
        raise ObjectError("invalid digest: expected 64 lowercase hex characters (SHA-256).")
    return f"{tenant_id}/{project_id}/{digest[:2]}/{digest}"


def _verified(data: bytes, digest: str) -> bytes:
    if hashlib.sha256(data).hexdigest() != digest:
        log.error("object does not match its digest", digest=digest)
        raise ObjectError(f"object {digest} does not match its digest; treat it as corrupt or tampered with.")
    return data


class FileObjectStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def put(self, tenant_id: str, project_id: str, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        path = self.root / _key(tenant_id, project_id, digest)
        if path.exists():
            return digest
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent)  # atomic: a crash never leaves a half-written object
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        return digest

    def get(self, tenant_id: str, project_id: str, digest: str) -> bytes | None:
        try:
            return _verified((self.root / _key(tenant_id, project_id, digest)).read_bytes(), digest)
        except FileNotFoundError:
            return None


class S3ObjectStore:
    """``client`` is a boto3 S3 client; the bucket must exist with versioning and server-side encryption set by the operator."""

    def __init__(self, client: Any, bucket: str) -> None:
        self.client = client
        self.bucket = bucket

    def put(self, tenant_id: str, project_id: str, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        self.client.put_object(Bucket=self.bucket, Key=_key(tenant_id, project_id, digest), Body=data)
        return digest

    def get(self, tenant_id: str, project_id: str, digest: str) -> bytes | None:
        try:
            body = self.client.get_object(Bucket=self.bucket, Key=_key(tenant_id, project_id, digest))["Body"]
        except self.client.exceptions.NoSuchKey:
            return None
        return _verified(bytes(body.read()), digest)


def objects_from_env(env: Mapping[str, str]) -> ObjectStore | None:
    """The object store a worker or operator tool reads: ``EVALRY_OBJECT_DIR`` (dev) or the ``EVALRY_S3_*`` settings, else None.

    shortcut: mirrors the API server's own setup; share one builder when the worker gets its own service account.
    """
    if directory := env.get("EVALRY_OBJECT_DIR"):
        return FileObjectStore(Path(directory))
    if bucket := env.get("EVALRY_S3_BUCKET"):
        import boto3  # type: ignore[import-untyped]

        if not (env.get("EVALRY_S3_ACCESS_KEY") and env.get("EVALRY_S3_SECRET_KEY")):
            raise ValueError(
                "EVALRY_S3_ACCESS_KEY and EVALRY_S3_SECRET_KEY are required with EVALRY_S3_BUCKET."
            )
        client = boto3.client(
            "s3", endpoint_url=env.get("EVALRY_S3_ENDPOINT") or None,
            aws_access_key_id=env["EVALRY_S3_ACCESS_KEY"], aws_secret_access_key=env["EVALRY_S3_SECRET_KEY"],
        )  # fmt: skip
        return S3ObjectStore(client, bucket)
    return None
