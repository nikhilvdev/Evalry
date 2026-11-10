import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jwt
import psycopg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from evalry.api.server import ConfigError, bootstrap_tenant, build_app, build_stores, load_settings
from evalry.api.server import main as server_main
from evalry.cli import main as cli_main
from evalry.pgstore import PostgresBackend
from evalry.schemas import GraderScore, RunRecord, SampleResult
from tests.conftest import PgDb

ISS, AUD = "https://issuer.test", "evalry-api"
SECRET_DSN_PASSWORD, SECRET_KEY = "s3cr3t-db-password", "s3cr3t-gate-key"


@pytest.fixture(scope="module")
def private_key() -> Any:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def key_file(tmp_path: Path, private_key: Any) -> Path:
    path = tmp_path / "issuer.pem"
    path.write_bytes(
        private_key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    return path


def dev_env(key_file: Path, **extra: str) -> dict[str, str]:
    return {
        "EVALRY_ENV": "dev", "EVALRY_OIDC_ISSUER": ISS, "EVALRY_OIDC_AUDIENCE": AUD,
        "EVALRY_OIDC_PUBLIC_KEY_FILE": str(key_file), **extra,
    }  # fmt: skip


def token(private_key: Any, sub: str, tenant: str) -> dict[str, str]:
    claims = {"sub": sub, "iss": ISS, "aud": AUD, "exp": int(time.time()) + 300, "tenant_id": tenant}
    return {"Authorization": "Bearer " + jwt.encode(claims, private_key, algorithm="RS256")}


def run_json(run_id: str = "r1") -> dict[str, Any]:
    rec = RunRecord(
        run_id=run_id, suite_version="s1", started_at=datetime(2026, 11, 10, tzinfo=UTC),
        results=[SampleResult(
            task_id="t", task_hash="h", sample=0, generator_model="m", code="c", composite=1.0,
            scores=[GraderScore(grader="tests", grader_version="2", score=1.0)], isolation="rpc",
        )],
    )  # fmt: skip
    return rec.model_dump(mode="json")


# -- configuration ------------------------------------------------------------------------------------------------------


def test_every_problem_is_reported_at_once() -> None:
    with pytest.raises(ConfigError) as e:
        load_settings({})
    msg = str(e.value)
    for needle in ("EVALRY_ENV", "EVALRY_OIDC_ISSUER", "EVALRY_OIDC_AUDIENCE", "EVALRY_OIDC_JWKS_URL"):
        assert needle in msg


def test_production_and_staging_need_a_database_and_production_needs_a_key(key_file: Path) -> None:
    base = {
        "EVALRY_OIDC_ISSUER": ISS,
        "EVALRY_OIDC_AUDIENCE": AUD,
        "EVALRY_OIDC_JWKS_URL": "https://issuer.test/jwks.json",
    }
    with pytest.raises(ConfigError, match="EVALRY_DB_APP_DSN is required in staging"):
        load_settings({**base, "EVALRY_ENV": "staging"})
    with pytest.raises(ConfigError) as e:
        load_settings({**base, "EVALRY_ENV": "production"})
    assert "EVALRY_DB_APP_DSN" in str(e.value) and "EVALRY_GATE_KEY" in str(e.value)
    ok = load_settings(
        {**base, "EVALRY_ENV": "production", "EVALRY_DB_APP_DSN": "host=h", "EVALRY_GATE_KEY": "k"}
    )
    assert (ok.host, ok.port, ok.environment) == (
        "127.0.0.1",
        8000,
        "production",
    )  # loopback unless told otherwise


def test_dev_only_features_are_refused_elsewhere(key_file: Path) -> None:
    base = {
        "EVALRY_ENV": "staging",
        "EVALRY_OIDC_ISSUER": ISS,
        "EVALRY_OIDC_AUDIENCE": AUD,
        "EVALRY_DB_APP_DSN": "host=h",
    }
    with pytest.raises(ConfigError) as e:
        load_settings(
            {**base, "EVALRY_OIDC_PUBLIC_KEY_FILE": str(key_file), "EVALRY_DEV_TENANTS": "acme:alice"}
        )
    msg = str(e.value)
    assert "only allowed when EVALRY_ENV=dev" in msg and "EVALRY_DEV_TENANTS is only allowed" in msg


@pytest.mark.parametrize(
    ("name", "value"),
    [("EVALRY_OIDC_JWKS_URL", "http://issuer.test/jwks"), ("EVALRY_PORT", "99999"), ("EVALRY_PORT", "abc"),
     ("EVALRY_DEV_TENANTS", "Bad Tenant:alice"), ("EVALRY_DEV_TENANTS", "acme")],
)  # fmt: skip
def test_malformed_values_are_rejected(key_file: Path, name: str, value: str) -> None:
    with pytest.raises(ConfigError):
        load_settings(dev_env(key_file, **{name: value}))


def test_error_messages_never_contain_secret_values(key_file: Path) -> None:
    env: Mapping[str, str] = {
        "EVALRY_ENV": "production", "EVALRY_DB_APP_DSN": f"host=h password={SECRET_DSN_PASSWORD}",
        "EVALRY_DB_ADMIN_DSN": f"host=h password={SECRET_DSN_PASSWORD}", "EVALRY_GATE_KEY": SECRET_KEY,
        "EVALRY_OIDC_JWKS_URL": "http://insecure", "EVALRY_PORT": "x",
    }  # fmt: skip
    with pytest.raises(ConfigError) as e:
        load_settings(env)
    assert SECRET_DSN_PASSWORD not in str(e.value) and SECRET_KEY not in str(e.value)


def test_main_exits_2_with_the_problems_and_starts_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in [k for k in list(__import__("os").environ) if k.startswith("EVALRY_")]:
        monkeypatch.delenv(name)
    with pytest.raises(SystemExit) as e:
        server_main()
    assert e.value.code == 2 and "EVALRY_ENV" in capsys.readouterr().err


# -- dev mode (memory store, local audit file) --------------------------------------------------------------------------


def test_dev_app_runs_end_to_end_with_dev_tenants(tmp_path: Path, key_file: Path, private_key: Any) -> None:
    settings = load_settings(
        dev_env(
            key_file,
            EVALRY_DEV_TENANTS="acme:alice,globex:bob",
            EVALRY_AUDIT_FILE=str(tmp_path / "audit.jsonl"),
        )
    )
    client = TestClient(build_app(settings))
    alice = token(private_key, "alice", "acme")
    assert client.get("/healthz").status_code == 200
    assert client.post("/v1/projects", json={"project_id": "app"}, headers=alice).status_code == 201
    assert client.get("/v1/projects/app/runs", headers=token(private_key, "bob", "globex")).status_code == 404
    assert (tmp_path / "audit.jsonl").read_text().count(
        "\n"
    ) >= 3  # tenants created, project created, access denied


# -- PostgreSQL: bootstrap, the API, the audit chain in the database, and an outage -------------------------------------


def pg_env(db: PgDb, key_file: Path, **extra: str) -> dict[str, str]:
    return dev_env(key_file, EVALRY_DB_APP_DSN=db.app_dsn, EVALRY_DB_ADMIN_DSN=db.admin_dsn, **extra)


def test_bootstrap_command_and_a_full_flow_on_postgres(
    pg_db: PgDb,
    key_file: Path,
    private_key: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for k, v in pg_env(pg_db, key_file).items():
        monkeypatch.setenv(k, v)
    assert cli_main(["bootstrap-tenant", "acme", "alice", "--operator", "ops"]) == 0
    assert cli_main(["bootstrap-tenant", "globex", "bob", "--operator", "ops"]) == 0
    assert (
        cli_main(["bootstrap-tenant", "acme", "carol"]) == 2 and "already exists" in capsys.readouterr().err
    )
    assert cli_main(["bootstrap-tenant", "Bad_Id", "x"]) == 2

    app = build_app(load_settings(pg_env(pg_db, key_file)))
    client = TestClient(app)
    alice, bob = token(private_key, "alice", "acme"), token(private_key, "bob", "globex")
    assert client.post("/v1/projects", json={"project_id": "app"}, headers=alice).status_code == 201
    assert (
        client.put("/v1/projects/app/members/dana", json={"role": "author"}, headers=alice).status_code == 204
    )
    assert (
        client.post(
            "/v1/projects/app/runs", json=run_json("r1"), headers=token(private_key, "dana", "acme")
        ).status_code
        == 201
    )
    assert client.get("/v1/projects/app/runs/r1", headers=alice).json()["run_id"] == "r1"
    assert (
        client.get("/v1/projects/app/runs/r1", headers=bob).status_code == 404
    )  # another tenant, same database
    rule = {"name": "g", "suite_version": "s1", "grader_version": "2"}
    assert client.post("/v1/projects/app/runs/r1/gate", json={"rule": rule}, headers=alice).status_code == 200

    with psycopg.connect(pg_db.admin_dsn) as c:
        chains: dict[str, int] = dict(
            c.execute("SELECT chain_id, count(*) FROM audit_events GROUP BY chain_id").fetchall()
        )
        actions = [
            r[0]
            for r in c.execute(
                "SELECT action FROM audit_events WHERE chain_id = 'acme' ORDER BY seq"
            ).fetchall()
        ]
    assert set(chains) == {"acme", "globex"}
    assert actions[:2] == ["tenant.created", "project.created"] and {
        "member.granted",
        "run.recorded",
        "gate.rehearsed",
        "decision.recorded",
    } <= set(actions)
    _, audit = build_stores(load_settings(pg_env(pg_db, key_file)))
    assert audit.verify(tenant_id="acme") == [] and audit.verify(tenant_id="globex") == []


def test_bootstrap_needs_a_database(key_file: Path) -> None:
    with pytest.raises(ConfigError, match="needs EVALRY_DB_APP_DSN and EVALRY_DB_ADMIN_DSN"):
        bootstrap_tenant(load_settings(dev_env(key_file)), "acme", "alice", "ops")


def test_a_database_outage_is_a_503_that_does_not_leak_the_dsn(
    pg_db: PgDb, key_file: Path, private_key: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = load_settings(pg_env(pg_db, key_file))
    bootstrap_tenant(settings, "acme", "alice", "ops")
    client = TestClient(build_app(settings), raise_server_exceptions=False)

    def down(self: PostgresBackend, *a: object, **k: object) -> bool:
        raise psycopg.OperationalError(f"connection failed: {pg_db.app_dsn}")

    monkeypatch.setattr(PostgresBackend, "project_exists", down)
    r = client.get("/v1/projects/app/runs", headers=token(private_key, "alice", "acme"))
    assert r.status_code == 503 and r.headers["Retry-After"] == "5"
    assert r.json() == {"error": "The database is unavailable. Try again shortly."}
    assert "app-test-pw" not in r.text
