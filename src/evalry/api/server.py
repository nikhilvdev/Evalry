"""Server entry point: build the API from environment variables and run it.

Configuration is validated up front and every problem is reported at once, never echoing a secret value. staging and
production require PostgreSQL; the in-memory store and the local audit file exist for ``dev`` only. Secrets (database passwords
inside the DSNs, the gate signing key) come from the environment, so they are not in a file or on a command line. The server
binds to loopback unless told otherwise; put TLS termination and a body-size limit in front of it.
"""

import os
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from evalry.access.store import Store
from evalry.api.app import create_app
from evalry.api.auth import JwksKeys, KeyResolver, StaticKeys, TokenVerifier
from evalry.audit.chain import AuditLog
from evalry.gates.hard import Environment
from evalry.scheduler.jobs import JobQueue, JobStore
from evalry.storage.backend import Backend, MemoryBackend
from evalry.storage.objects import FileObjectStore, ObjectStore, S3ObjectStore
from evalry.storage.pgjobs import PostgresJobQueue
from evalry.storage.pgstore import PostgresAuditSink, PostgresBackend

_ENVIRONMENTS: tuple[str, ...] = ("dev", "staging", "production")
_TENANT_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")


class ConfigError(Exception):
    """One or more settings are missing or unsafe. The message lists all of them."""


@dataclass(frozen=True)
class Settings:
    environment: Environment
    issuer: str
    audience: str
    tenant_claim: str
    jwks_url: str | None
    public_key_file: Path | None
    db_app_dsn: str | None
    db_admin_dsn: str | None
    audit_file: Path
    gate_key: bytes | None
    dev_tenants: tuple[tuple[str, str], ...]
    host: str
    port: int
    object_dir: Path | None = None
    s3_bucket: str | None = None
    s3_endpoint: str | None = None
    s3_keys: tuple[str, str] | None = None


def load_settings(env: Mapping[str, str]) -> Settings:
    problems: list[str] = []

    def get(name: str) -> str | None:
        return env.get(name) or None

    environment = get("EVALRY_ENV")
    if environment not in _ENVIRONMENTS:
        problems.append("EVALRY_ENV must be one of dev, staging, production.")
    is_dev = environment == "dev"

    issuer, audience = get("EVALRY_OIDC_ISSUER"), get("EVALRY_OIDC_AUDIENCE")
    if not issuer:
        problems.append(
            "EVALRY_OIDC_ISSUER is required (the token issuer, for example https://login.example.com)."
        )
    if not audience:
        problems.append("EVALRY_OIDC_AUDIENCE is required (the audience tokens must be issued for).")

    jwks, key_file = get("EVALRY_OIDC_JWKS_URL"), get("EVALRY_OIDC_PUBLIC_KEY_FILE")
    if jwks and not jwks.startswith("https://"):
        problems.append("EVALRY_OIDC_JWKS_URL must be an https:// URL.")
    if key_file and not is_dev:
        problems.append(
            "EVALRY_OIDC_PUBLIC_KEY_FILE is only allowed when EVALRY_ENV=dev; use EVALRY_OIDC_JWKS_URL."
        )
    if not jwks and not (key_file and is_dev):
        problems.append(
            "Set EVALRY_OIDC_JWKS_URL (or, in dev only, EVALRY_OIDC_PUBLIC_KEY_FILE) so tokens can be verified."
        )

    app_dsn, admin_dsn = get("EVALRY_DB_APP_DSN"), get("EVALRY_DB_ADMIN_DSN")
    if environment in ("staging", "production") and not app_dsn:
        problems.append(
            f"EVALRY_DB_APP_DSN is required in {environment}: the in-memory store is for dev only."
        )

    object_dir, bucket = get("EVALRY_OBJECT_DIR"), get("EVALRY_S3_BUCKET")
    access_key, secret_key = get("EVALRY_S3_ACCESS_KEY"), get("EVALRY_S3_SECRET_KEY")
    if object_dir and not is_dev:
        problems.append("EVALRY_OBJECT_DIR is only allowed when EVALRY_ENV=dev; use EVALRY_S3_BUCKET.")
    if object_dir and bucket:
        problems.append("Set EVALRY_OBJECT_DIR or EVALRY_S3_BUCKET, not both.")
    if environment in ("staging", "production") and not bucket:
        problems.append(f"EVALRY_S3_BUCKET is required in {environment}: run code is kept in object storage.")
    if bucket and not (access_key and secret_key):
        problems.append("EVALRY_S3_ACCESS_KEY and EVALRY_S3_SECRET_KEY are required with EVALRY_S3_BUCKET.")

    gate_key = get("EVALRY_GATE_KEY")
    if environment == "production" and not gate_key:
        problems.append("EVALRY_GATE_KEY is required in production: gate decisions must be signed.")

    dev_tenants: list[tuple[str, str]] = []
    if raw := get("EVALRY_DEV_TENANTS"):
        if not is_dev:
            problems.append("EVALRY_DEV_TENANTS is only allowed when EVALRY_ENV=dev.")
        else:
            for part in raw.split(","):
                tenant, _, admin = part.partition(":")
                if not _TENANT_ID.fullmatch(tenant) or not admin:
                    problems.append("EVALRY_DEV_TENANTS must look like 'acme:alice,globex:bob'.")
                    break
                dev_tenants.append((tenant, admin))

    port_text = get("EVALRY_PORT") or "8000"
    if not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
        problems.append("EVALRY_PORT must be a number from 1 to 65535.")
        port_text = "8000"

    if problems:
        raise ConfigError("Invalid configuration:\n- " + "\n- ".join(problems))
    assert environment and issuer and audience  # narrowed by the checks above
    return Settings(
        environment=environment,  # type: ignore[arg-type]
        issuer=issuer,
        audience=audience,
        tenant_claim=get("EVALRY_TENANT_CLAIM") or "tenant_id",
        jwks_url=jwks,
        public_key_file=Path(key_file) if key_file else None,
        db_app_dsn=app_dsn,
        db_admin_dsn=admin_dsn,
        audit_file=Path(get("EVALRY_AUDIT_FILE") or "evalry-audit.jsonl"),
        gate_key=gate_key.encode() if gate_key else None,
        dev_tenants=tuple(dev_tenants),
        host=get("EVALRY_HOST") or "127.0.0.1",
        port=int(port_text),
        object_dir=Path(object_dir) if object_dir else None,
        s3_bucket=bucket,
        s3_endpoint=get("EVALRY_S3_ENDPOINT"),
        s3_keys=(access_key, secret_key) if bucket and access_key and secret_key else None,
    )


def _keys(settings: Settings) -> KeyResolver:
    if settings.jwks_url:
        return JwksKeys(settings.jwks_url)
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    assert settings.public_key_file is not None  # load_settings guarantees one of the two
    return StaticKeys(load_pem_public_key(settings.public_key_file.read_bytes()))


def build_stores(settings: Settings) -> tuple[Backend, AuditLog]:
    """PostgreSQL backend with the audit chain in the database, or (dev only) memory with a local audit file."""
    if settings.db_app_dsn:
        pg = PostgresBackend(settings.db_app_dsn, admin_dsn=settings.db_admin_dsn)
        return pg, AuditLog(PostgresAuditSink(pg))
    return MemoryBackend(), AuditLog(settings.audit_file)


def build_objects(settings: Settings) -> ObjectStore | None:
    """S3-compatible bucket, or (dev only) a local directory. None keeps run code inline in the database."""
    if settings.s3_bucket:
        import boto3  # type: ignore[import-untyped]  # shortcut: no stubs, upgrade to boto3-stubs if typing matters

        assert settings.s3_keys  # load_settings requires keys with a bucket
        client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint,
            aws_access_key_id=settings.s3_keys[0],
            aws_secret_access_key=settings.s3_keys[1],
        )
        return S3ObjectStore(client, settings.s3_bucket)
    return FileObjectStore(settings.object_dir) if settings.object_dir else None


def build_app(settings: Settings) -> FastAPI:
    backend, audit = build_stores(settings)
    store = Store(audit, backend, build_objects(settings))
    for (
        tenant,
        admin,
    ) in settings.dev_tenants:  # dev convenience: the in-memory store starts empty on every run
        store.bootstrap_tenant(tenant, admin, operator="dev-startup")
    verifier = TokenVerifier(
        issuer=settings.issuer,
        audience=settings.audience,
        keys=_keys(settings),
        tenant_claim=settings.tenant_claim,
    )
    queue_for: Callable[[str], JobStore]
    if settings.db_app_dsn:
        pg_queue = PostgresJobQueue(
            settings.db_app_dsn, tenant_id="-"
        )  # "-" is no tenant: every view rebinds it
        queue_for = pg_queue.for_tenant
    else:
        local = JobQueue()  # dev only: nothing outside this process can work it
        queue_for = lambda tenant: local
    return create_app(
        queue_for=queue_for,
        store=store,
        audit=audit,
        verifier=verifier,
        environment=settings.environment,
        gate_key=settings.gate_key,
    )


def bootstrap_tenant(settings: Settings, tenant_id: str, admin_subject: str, operator: str) -> None:
    """Operator action: create a tenant and its first admin. Needs the admin connection; never reachable over HTTP."""
    if not settings.db_app_dsn or not settings.db_admin_dsn:
        raise ConfigError(
            "Creating a tenant needs EVALRY_DB_APP_DSN and EVALRY_DB_ADMIN_DSN (a database, not dev memory)."
        )
    if not _TENANT_ID.fullmatch(tenant_id):
        raise ConfigError("Tenant ids use lowercase letters, digits and hyphens, up to 63 characters.")
    backend, audit = build_stores(settings)
    try:
        Store(audit, backend).bootstrap_tenant(tenant_id, admin_subject, operator=operator)
    finally:
        if isinstance(backend, PostgresBackend):
            backend.close()


def main() -> None:
    try:
        settings = load_settings(os.environ)
        app = build_app(settings)
    except ConfigError as e:
        print(e, file=sys.stderr)
        raise SystemExit(2) from e
    uvicorn.run(
        app, host=settings.host, port=settings.port, access_log=False
    )  # requests are logged by the app
