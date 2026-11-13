"""Shared fixtures. Postgres tests start one throwaway container for the session and a fresh migrated database per test.

The container is named pgtest-* (not evalry-*) so the sandbox tests' "no containers left behind" checks ignore it, binds only
127.0.0.1, and is removed at the end. Tests skip, rather than fail, when Docker is not available.
"""

import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import psycopg
import pytest

from evalry.storage.backend import Backend, MemoryBackend
from evalry.storage.db import migrate
from evalry.storage.pgstore import PostgresBackend

PG_IMAGE = "postgres:16-alpine"
ADMIN_PASSWORD, APP_PASSWORD, WORKER_PASSWORD = (
    "admin-test-pw",
    "app-test-pw",
    "worker-test-pw",
)  # throwaway container, loopback only


@dataclass(frozen=True)
class PgServer:
    host: str
    port: int

    def dsn(self, user: str, password: str, db: str = "postgres") -> str:
        return f"host={self.host} port={self.port} user={user} password={password} dbname={db}"


def _docker_ok() -> bool:
    return (
        shutil.which("docker") is not None
        and subprocess.run(["docker", "info"], check=False, capture_output=True).returncode == 0
    )


@pytest.fixture(scope="session")
def pg_server() -> Iterator[PgServer]:
    if not _docker_ok():
        pytest.skip("Docker is not available")
    name = f"pgtest-{uuid.uuid4().hex[:8]}"
    run = subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name, "-e", f"POSTGRES_PASSWORD={ADMIN_PASSWORD}",
         "-p", "127.0.0.1::5432", PG_IMAGE],
        check=False, capture_output=True, text=True,
    )  # fmt: skip
    if run.returncode != 0:
        pytest.skip(f"cannot start {PG_IMAGE}: {run.stderr.strip()[:200]}")
    try:
        port_line = subprocess.run(
            ["docker", "port", name, "5432/tcp"], check=True, capture_output=True, text=True
        ).stdout
        server = PgServer("127.0.0.1", int(port_line.splitlines()[0].rsplit(":", 1)[1]))
        deadline = time.monotonic() + 40
        while True:
            try:
                with psycopg.connect(
                    server.dsn("postgres", ADMIN_PASSWORD), connect_timeout=2, autocommit=True
                ) as c:
                    c.execute("SELECT 1")
                    # the application role is cluster-level, so provisioning (not the migration) creates it
                    c.execute(
                        f"CREATE ROLE evalry_app LOGIN PASSWORD '{APP_PASSWORD}' NOSUPERUSER NOBYPASSRLS"
                    )
                    c.execute(
                        f"CREATE ROLE evalry_worker LOGIN PASSWORD '{WORKER_PASSWORD}' NOSUPERUSER NOBYPASSRLS"
                    )
                break
            except psycopg.OperationalError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.3)
        yield server
    finally:
        subprocess.run(["docker", "rm", "-f", name], check=False, capture_output=True)


@dataclass(frozen=True)
class PgDb:
    server: PgServer
    name: str

    @property
    def admin_dsn(self) -> str:
        return self.server.dsn("postgres", ADMIN_PASSWORD, self.name)

    @property
    def app_dsn(self) -> str:
        return self.server.dsn("evalry_app", APP_PASSWORD, self.name)

    @property
    def worker_dsn(self) -> str:
        return self.server.dsn("evalry_worker", WORKER_PASSWORD, self.name)


@pytest.fixture
def pg_db(pg_server: PgServer) -> Iterator[PgDb]:
    name = f"t_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(pg_server.dsn("postgres", ADMIN_PASSWORD), autocommit=True) as c:
        c.execute(f"CREATE DATABASE {name}")
    db = PgDb(pg_server, name)
    migrate(db.admin_dsn)
    try:
        yield db
    finally:
        with psycopg.connect(pg_server.dsn("postgres", ADMIN_PASSWORD), autocommit=True) as c:
            c.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


@pytest.fixture(params=["memory", "postgres"])
def backend(request: pytest.FixtureRequest) -> Iterator[Backend]:
    """The same isolation tests run against both storage backends."""
    if request.param == "memory":
        yield MemoryBackend()
        return
    db: PgDb = request.getfixturevalue("pg_db")
    pg = PostgresBackend(db.app_dsn, admin_dsn=db.admin_dsn)
    try:
        yield pg
    finally:
        pg.close()
