import base64
import hashlib
import hmac
import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from evalry.access.store import Store
from evalry.api.app import create_app
from evalry.api.auth import StaticKeys, TokenVerifier
from evalry.audit.chain import AuditLog
from evalry.scheduler.jobs import Job, JobQueue
from evalry.scheduler.worker import work
from evalry.schemas import GraderScore, RunRecord, SampleResult, Task

ISS, AUD = "https://issuer.test", "evalry-api"
NOW = datetime(2026, 11, 10, tzinfo=UTC)
RULE = {"name": "smoke", "suite_version": "s1", "grader_version": "2"}


@pytest.fixture(scope="module")
def keypair() -> tuple[Any, Any]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private, private.public_key()


def make_token(private: Any, sub: str = "u", tenant: str | None = "acme", **over: Any) -> str:
    claims: dict[str, Any] = {"sub": sub, "iss": ISS, "aud": AUD, "exp": int(time.time()) + 300}
    if tenant is not None:
        claims["tenant_id"] = tenant
    claims.update(over)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, private, algorithm="RS256")


class Env:
    def __init__(self, tmp_path: Path, keys: tuple[Any, Any]) -> None:
        self.private, public = keys
        self.audit = AuditLog(tmp_path / "audit.jsonl")
        self.store = Store(self.audit)
        self.queue = JobQueue()
        verifier = TokenVerifier(issuer=ISS, audience=AUD, keys=StaticKeys(public))
        self.app = create_app(
            store=self.store,
            audit=self.audit,
            verifier=verifier,
            environment="staging",
            gate_key=b"k",
            queue_for=lambda tenant: self.queue,
        )
        self.client = TestClient(self.app)
        for t in ("acme", "globex"):
            self.store.bootstrap_tenant(t, f"{t}-admin", operator="ops")

    def h(self, sub: str, tenant: str = "acme") -> dict[str, str]:
        return {"Authorization": f"Bearer {make_token(self.private, sub, tenant)}"}

    def setup_project(self, tenant: str = "acme") -> None:
        assert (
            self.client.post(
                "/v1/projects", json={"project_id": "app"}, headers=self.h(f"{tenant}-admin", tenant)
            ).status_code
            == 201
        )
        for role in ("viewer", "author", "approver"):
            r = self.client.put(
                f"/v1/projects/app/members/{tenant}-{role}",
                json={"role": role},
                headers=self.h(f"{tenant}-admin", tenant),
            )
            assert r.status_code == 204


@pytest.fixture
def env(tmp_path: Path, keypair: tuple[Any, Any]) -> Iterator[Env]:
    yield Env(tmp_path, keypair)


def run_json(run_id: str = "r1", isolation: str = "rpc", score: float = 1.0) -> dict[str, Any]:
    rec = RunRecord(
        run_id=run_id, suite_version="s1", started_at=NOW,
        results=[SampleResult(
            task_id="fizzbuzz", task_hash="h", sample=0, generator_model="m", code="c", composite=score,
            scores=[GraderScore(grader="tests", grader_version="2", score=score)], isolation=isolation,  # type: ignore[arg-type]
        )],
    )  # fmt: skip
    return rec.model_dump(mode="json")


# -- authentication ------------------------------------------------------------------------------------------------------


def test_health_is_open_and_everything_else_needs_a_token(env: Env) -> None:
    assert env.client.get("/healthz").json() == {"status": "ok"}
    for method, path in [
        ("get", "/v1/projects/app/runs"),
        ("post", "/v1/projects"),
        ("get", "/v1/projects/app/runs/r1"),
    ]:
        r = getattr(env.client, method)(path)
        assert r.status_code == 401 and r.headers["WWW-Authenticate"] == "Bearer"


def forge(header: dict[str, str], secret: bytes | None) -> str:
    """Hand-build a JWT, because PyJWT refuses to sign with a public key or with alg=none (which is exactly the attack)."""

    def b64(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

    payload = {"sub": "u", "iss": ISS, "aud": AUD, "exp": int(time.time()) + 300, "tenant_id": "acme"}
    head = b64(json.dumps(header).encode()) + "." + b64(json.dumps(payload).encode())
    sig = b"" if secret is None else hmac.new(secret, head.encode(), hashlib.sha256).digest()
    return head + "." + b64(sig)


def bad_tokens(private: Any, public: Any) -> dict[str, str]:
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = public.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    return {
        "expired": make_token(private, exp=int(time.time()) - 3600),
        "wrong audience": make_token(private, aud="someone-else"),
        "wrong issuer": make_token(private, iss="https://evil.test"),
        "signed by another key": make_token(other),
        "no tenant claim": make_token(private, tenant=None),
        "empty tenant claim": make_token(private, tenant=""),
        "no subject": make_token(private, sub=None),  # type: ignore[arg-type]
        "no expiry": make_token(private, exp=None),
        "alg none": forge({"alg": "none", "typ": "JWT"}, None),
        "hs256 with the public key as secret": forge({"alg": "HS256", "typ": "JWT"}, pem),
        "garbage": "not.a.jwt",
    }  # fmt: skip


def test_every_forged_or_invalid_token_is_rejected(env: Env, keypair: tuple[Any, Any]) -> None:
    for name, token in bad_tokens(*keypair).items():
        r = env.client.get("/v1/projects/app/runs", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401, name
        assert "invalid or expired" in r.json()["error"] or "no usable" in r.json()["error"], name
    for header in ("Basic abc", "Bearer", "bearer ", "Token abc"):
        assert env.client.get("/v1/projects/app/runs", headers={"Authorization": header}).status_code == 401


def test_tenant_cannot_be_chosen_by_the_caller(env: Env) -> None:
    env.setup_project("acme")
    # a body or query field claiming another tenant is rejected or ignored; only the token's claim counts
    r = env.client.post(
        "/v1/projects", json={"project_id": "x", "tenant_id": "acme"}, headers=env.h("globex-admin", "globex")
    )
    assert r.status_code == 422
    assert (
        env.client.get(
            "/v1/projects/app/runs?tenant_id=acme", headers=env.h("globex-admin", "globex")
        ).status_code
        == 404
    )


# -- authorisation and tenant isolation over HTTP -----------------------------------------------------------------------


def test_project_and_member_management(env: Env) -> None:
    env.setup_project()
    assert (
        env.client.post("/v1/projects", json={"project_id": "app"}, headers=env.h("acme-admin")).status_code
        == 409
    )
    assert (
        env.client.post("/v1/projects", json={"project_id": "y"}, headers=env.h("acme-approver")).status_code
        == 403
    )
    assert (
        env.client.post(
            "/v1/projects", json={"project_id": "Bad_Name"}, headers=env.h("acme-admin")
        ).status_code
        == 400
    )
    assert (
        env.client.put(
            "/v1/projects/app/members/z", json={"role": "viewer"}, headers=env.h("acme-approver")
        ).status_code
        == 403
    )
    assert (
        env.client.put(
            "/v1/projects/app/members/z", json={"role": "root"}, headers=env.h("acme-admin")
        ).status_code
        == 422
    )
    assert (
        env.client.delete("/v1/projects/app/members/acme-author", headers=env.h("acme-admin")).status_code
        == 204
    )
    assert (
        env.client.post("/v1/projects/app/runs", json=run_json(), headers=env.h("acme-author")).status_code
        == 404
    )  # no role left


def test_roles_over_http_and_immutable_runs(env: Env) -> None:
    env.setup_project()
    assert (
        env.client.post("/v1/projects/app/runs", json=run_json(), headers=env.h("acme-viewer")).status_code
        == 403
    )
    r = env.client.post("/v1/projects/app/runs", json=run_json("r1"), headers=env.h("acme-author"))
    assert r.status_code == 201 and r.json() == {"run_id": "r1"}
    assert (
        env.client.post(
            "/v1/projects/app/runs", json=run_json("r1"), headers=env.h("acme-author")
        ).status_code
        == 409
    )
    assert env.client.get("/v1/projects/app/runs", headers=env.h("acme-viewer")).json() == {"run_ids": ["r1"]}
    assert env.client.get("/v1/projects/app/runs/r1", headers=env.h("acme-viewer")).json()["run_id"] == "r1"
    assert env.client.get("/v1/projects/app/runs/nope", headers=env.h("acme-viewer")).status_code == 404
    bad = {**run_json("r2"), "surprise": 1}
    assert env.client.post("/v1/projects/app/runs", json=bad, headers=env.h("acme-author")).status_code == 422


def test_cross_tenant_requests_see_nothing_and_cannot_tell_the_difference(env: Env) -> None:
    env.setup_project("acme")
    env.client.post("/v1/projects/app/runs", json=run_json("secret"), headers=env.h("acme-author"))
    for sub in ("globex-admin", "someone"):
        for path in (
            "/v1/projects/app/runs/secret",
            "/v1/projects/app/runs",
            "/v1/projects/app/decisions/d1",
        ):
            assert env.client.get(path, headers=env.h(sub, "globex")).status_code == 404
        assert (
            env.client.post(
                "/v1/projects/app/runs", json=run_json("evil"), headers=env.h(sub, "globex")
            ).status_code
            == 404
        )
        assert (
            env.client.post(
                "/v1/projects/app/runs/secret/gate", json={"rule": RULE}, headers=env.h(sub, "globex")
            ).status_code
            == 404
        )
    foreign = env.client.get("/v1/projects/app/runs", headers=env.h("globex-admin", "globex"))
    missing = env.client.get("/v1/projects/never-existed/runs", headers=env.h("globex-admin", "globex"))
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["error"].replace("app", "X") == missing.json()["error"].replace(
        "never-existed", "X"
    )
    assert env.client.get("/v1/projects/app/runs", headers=env.h("acme-viewer")).json() == {
        "run_ids": ["secret"]
    }


def test_gate_flow_signed_decision_and_server_side_environment(env: Env) -> None:
    env.setup_project()
    env.client.post("/v1/projects/app/runs", json=run_json("good"), headers=env.h("acme-author"))
    env.client.post(
        "/v1/projects/app/runs", json=run_json("inproc", isolation="inprocess"), headers=env.h("acme-author")
    )
    assert (
        env.client.post(
            "/v1/projects/app/runs/good/gate", json={"rule": RULE}, headers=env.h("acme-author")
        ).status_code
        == 403
    )
    r = env.client.post(
        "/v1/projects/app/runs/good/gate", json={"rule": RULE}, headers=env.h("acme-approver")
    )
    d = r.json()
    assert (
        r.status_code == 200 and d["outcome"] == "pass" and d["environment"] == "staging" and d["signature"]
    )
    got = env.client.get(f"/v1/projects/app/decisions/{d['decision_id']}", headers=env.h("acme-viewer"))
    assert got.json() == d
    blocked = env.client.post(
        "/v1/projects/app/runs/inproc/gate", json={"rule": RULE}, headers=env.h("acme-approver")
    ).json()
    assert blocked["outcome"] == "block" and "rpc" in blocked["reasons"][0]
    forged = env.client.post(
        "/v1/projects/app/runs/good/gate",
        json={"rule": RULE, "environment": "production"},
        headers=env.h("acme-approver"),
    )
    assert forged.status_code == 422  # the caller cannot pick the environment or bring a key
    assert [e.action for e in env.audit.events() if e.action.startswith("gate.")] == [
        "gate.rehearsed",
        "gate.rehearsed",
    ]


def test_gate_exception_needs_an_approver(env: Env) -> None:
    env.setup_project()
    env.client.post(
        "/v1/projects/app/runs", json=run_json("failing", score=0.0), headers=env.h("acme-author")
    )
    exc = {
        "task_id": "fizzbuzz",
        "approver": "acme-viewer",
        "reason": "flake",
        "expires_at": "2099-01-01T00:00:00Z",
    }
    r = env.client.post(
        "/v1/projects/app/runs/failing/gate",
        json={"rule": RULE, "exceptions": [exc]},
        headers=env.h("acme-approver"),
    )
    assert r.status_code == 403 and "approver role" in r.json()["error"]


# -- operations ---------------------------------------------------------------------------------------------------------


def test_production_without_a_key_refuses_to_start(tmp_path: Path, keypair: tuple[Any, Any]) -> None:
    audit = AuditLog(tmp_path / "a.jsonl")
    with pytest.raises(ValueError, match="signing key"):
        create_app(
            store=Store(audit),
            audit=audit,
            verifier=TokenVerifier(issuer=ISS, audience=AUD, keys=StaticKeys(keypair[1])),
            environment="production",
            gate_key=None,
        )


def test_oversized_body_is_refused_and_request_id_is_returned(env: Env) -> None:
    env.setup_project()
    r = env.client.post(
        "/v1/projects/app/runs",
        content=b"x" * 10,
        headers={
            **env.h("acme-author"),
            "Content-Length": str(6 * 1024 * 1024),
            "Content-Type": "application/json",
        },
    )
    assert r.status_code == 413
    assert len(env.client.get("/healthz").headers["X-Request-ID"]) == 32


def test_logs_have_route_tenant_actor_and_never_the_token_or_ids(
    env: Env, capfd: pytest.CaptureFixture[str]
) -> None:
    env.setup_project()
    token_header = env.h("acme-viewer")
    env.client.get("/v1/projects/app/runs/some-run-id", headers=token_header)
    env.client.get("/v1/projects/app/runs", headers={"Authorization": "Bearer forged.token.value"})
    out = capfd.readouterr().out
    assert token_header["Authorization"].split()[1] not in out and "forged.token.value" not in out
    assert '"route":"/v1/projects/{project_id}/runs/{run_id}"' in out and "some-run-id" not in out
    assert (
        '"tenant_id":"acme"' in out and '"actor":"acme-viewer"' in out and '"message":"token rejected"' in out
    )


def test_openapi_lists_versioned_routes_and_docs_are_off(env: Env) -> None:
    paths = env.app.openapi()["paths"]
    assert "/v1/projects/{project_id}/runs/{run_id}/gate" in paths and "/healthz" in paths
    assert env.client.get("/docs").status_code == 404


def test_suites_over_http(env: Env) -> None:
    env.setup_project()
    body = {"tasks": [{"id": "a", "language": "python", "prompt": "p", "tests": "assert True"}]}
    url = "/v1/projects/app/suites/v1"
    assert env.client.put(url, json=body, headers=env.h("acme-viewer")).status_code == 403
    r = env.client.put(url, json=body, headers=env.h("acme-author"))
    assert r.status_code == 201 and len(r.json()["content_hash"]) == 64
    assert env.client.put(url, json=body, headers=env.h("acme-author")).status_code == 409
    assert env.client.get("/v1/projects/app/suites", headers=env.h("acme-viewer")).json() == {
        "versions": ["v1"]
    }
    assert env.client.get(url, headers=env.h("acme-viewer")).status_code == 403  # hidden tests
    assert env.client.get(url, headers=env.h("acme-author")).json() == {
        "tasks": [{**body["tasks"][0], "schema_version": "1", "tier": 1, "reference": None, "tags": [],
                   "sensitivity": "internal", "owner": None}]
    }  # fmt: skip
    assert env.client.get(url, headers=env.h("globex-author", "globex")).status_code == 404


# -- API to worker --------------------------------------------------------------------------------------------------------


def fake_result(job: Job, task: Task) -> SampleResult:
    return SampleResult(
        task_id=task.id,
        task_hash="h",
        sample=job.sample,
        generator_model="m",
        code="c",
        scores=[],
        composite=1.0,
    )


def test_run_request_flows_from_api_through_a_worker_to_a_stored_run(env: Env) -> None:
    env.setup_project()
    body = {"tasks": [{"id": "a", "language": "python", "prompt": "p", "tests": "assert True"}]}
    author, base = env.h("acme-author"), "/v1/projects/app"
    assert env.client.put(f"{base}/suites/v1", json=body, headers=author).status_code == 201
    req = {"suite_version": "v1", "samples": 2, "priority": "smoke"}
    assert env.client.post(f"{base}/run-requests", json=req, headers=env.h("acme-viewer")).status_code == 403
    r = env.client.post(f"{base}/run-requests", json=req, headers=author)
    assert r.status_code == 202 and r.json()["jobs"] == 2
    run_id = r.json()["run_id"]
    again = env.client.post(f"{base}/run-requests", json=req, headers=author).json()
    assert again["run_id"] == run_id and len(env.queue.jobs()) == 2  # idempotent: nothing duplicated

    status = env.client.get(f"{base}/run-requests/{run_id}", headers=author).json()
    assert status["state"] == "running" and not status["recorded"]
    assert env.client.get(f"{base}/runs/{run_id}", headers=author).status_code == 404

    def task_for(job: Job) -> Task:
        assert (job.suite_version, job.run_id) == (
            "v1",
            run_id,
        )  # the worker finds the suite from the job alone
        return Task(**body["tasks"][0])

    assert work(env.queue, task_for, fake_result) == 2
    status = env.client.get(f"{base}/run-requests/{run_id}", headers=author).json()
    assert status["state"] == "done" and status["recorded"]
    run = env.client.get(f"{base}/runs/{run_id}", headers=env.h("acme-viewer")).json()
    assert run["suite_version"] == "v1" and [x["sample"] for x in run["results"]] == [0, 1]
    assert (
        env.client.get(f"{base}/run-requests/{run_id}", headers=author).json()["state"] == "done"
    )  # polling again is harmless
    assert (
        env.client.post(f"{base}/run-requests", json=req, headers=author).status_code == 409
    )  # recorded runs are immutable
    assert (
        env.client.get(f"{base}/run-requests/{run_id}", headers=env.h("globex-author", "globex")).status_code
        == 404
    )


def test_run_request_reports_a_failed_job_and_unknown_suite(env: Env) -> None:
    env.setup_project()
    author, base = env.h("acme-author"), "/v1/projects/app"
    req = {"suite_version": "v1"}
    assert env.client.post(f"{base}/run-requests", json=req, headers=author).status_code == 404
    body = {"tasks": [{"id": "a", "language": "python", "prompt": "p", "tests": "assert True"}]}
    env.client.put(f"{base}/suites/v1", json=body, headers=author)
    run_id = env.client.post(f"{base}/run-requests", json=req, headers=author).json()["run_id"]

    def boom(job: Job, task: Task) -> SampleResult:
        raise RuntimeError("provider down")

    work(env.queue, lambda job: Task(**body["tasks"][0]), boom)
    status = env.client.get(f"{base}/run-requests/{run_id}", headers=author).json()
    assert status["state"] == "failed" and "provider down" in status["error"]
    assert (
        env.client.get(f"{base}/runs/{run_id}", headers=author).status_code == 404
    )  # a failed run records nothing


# -- judge calibration workflow -------------------------------------------------------------------------------------------

CAL = {"judge_version": "jv1", "set_hash": "a" * 64, "items": 24, "agreement_within_one": 0.8, "stability": 0.95,
       "injection_resistant": True}  # fmt: skip
JUDGE_RULE = {"name": "j", "suite_version": "s1", "grader": "judge", "grader_version": "jv1"}


def judged_run(env: Env) -> str:
    rec = RunRecord(
        run_id="rj", suite_version="s1", started_at=NOW,
        results=[SampleResult(
            task_id="t", task_hash="h", sample=0, generator_model="m", code="c", composite=1.0, isolation="rpc",
            scores=[GraderScore(grader="judge", grader_version="jv1", score=1.0)],
        )],
    )  # fmt: skip
    assert (
        env.client.post(
            "/v1/projects/app/runs", json=rec.model_dump(mode="json"), headers=env.h("acme-author")
        ).status_code
        == 201
    )
    return rec.run_id


def test_a_judge_gates_only_after_an_approver_records_a_passing_calibration(env: Env) -> None:
    env.setup_project()
    run_id = judged_run(env)
    gate = lambda: env.client.post(
        f"/v1/projects/app/runs/{run_id}/gate", json={"rule": JUDGE_RULE}, headers=env.h("acme-approver")
    )
    refused = gate()
    assert refused.status_code == 403 and "no current passing calibration" in refused.json()["error"]

    url = "/v1/projects/app/judge-calibrations"
    assert env.client.post(url, json=CAL, headers=env.h("acme-author")).status_code == 403  # not their call
    assert env.client.post(url, json={**CAL, "items": 5}, headers=env.h("acme-approver")).status_code == 400
    failing = env.client.post(
        url, json={**CAL, "agreement_within_one": 0.5, "passed": True}, headers=env.h("acme-approver")
    )
    assert failing.status_code == 201 and failing.json()["passed"] is False  # the caller's claim is ignored
    assert gate().status_code == 403

    ok = env.client.post(url, json=CAL, headers=env.h("acme-approver"))
    assert ok.json()["passed"] is True and ok.json()["created_by"] == "acme-approver"
    decision = gate()
    assert decision.status_code == 200 and decision.json()["outcome"] == "pass"

    env.client.post(
        url, json={**CAL, "injection_resistant": False}, headers=env.h("acme-approver")
    )  # recalibration fails
    assert gate().status_code == 403  # the newest record wins: a failing recalibration revokes the pass
    listed = env.client.get(url, headers=env.h("acme-viewer")).json()
    assert [r["passed"] for r in listed] == [False, True, False]
    assert env.client.get(url, headers=env.h("globex-admin", "globex")).status_code == 404


# -- human labeling workflow ----------------------------------------------------------------------------------------------


def test_labeling_workflow_from_blind_batch_to_calibration_ready_labels(env: Env) -> None:
    env.setup_project()
    env.client.put("/v1/projects/app/members/acme-rev2", json={"role": "author"}, headers=env.h("acme-admin"))
    env.client.put(
        "/v1/projects/app/members/acme-boss", json={"role": "approver"}, headers=env.h("acme-admin")
    )
    base, items = "/v1/projects/app/label-batches", [{"prompt": f"p{i}", "code": f"c{i}"} for i in range(20)]
    body = {
        "guideline": "1 = broken, 3 = works but messy, 5 = correct and clear",
        "items": items,
        "double_share": 0.1,
    }
    assert (
        env.client.post(base, json=body, headers=env.h("acme-author")).status_code == 403
    )  # approvers pick what is labeled
    bad = {**body, "items": [{**items[0], "generator": "claude"}]}
    assert (
        env.client.post(base, json=bad, headers=env.h("acme-approver")).status_code == 422
    )  # blind by schema
    r = env.client.post(base, json=body, headers=env.h("acme-approver"))
    assert r.status_code == 201 and r.json()["items"] == 20
    b = f"{base}/{r.json()['batch_id']}"

    def label(who: str, score_for: dict[int, int]) -> None:
        while (nxt := env.client.get(f"{b}/next", headers=env.h(who)).json())["item"]:
            assert (
                set(nxt["item"]) == {"prompt", "code", "item_id", "reviews_needed"}
                and "1 = broken" in nxt["guideline"]
            )
            i = nxt["item"]["item_id"]
            assert (
                env.client.put(
                    f"{b}/items/{i}/label", json={"score": score_for.get(i, 4)}, headers=env.h(who)
                ).status_code
                == 204
            )

    assert env.client.get(f"{b}/next", headers=env.h("acme-viewer")).status_code == 403
    assert env.client.get(f"{b}/export", headers=env.h("acme-boss")).status_code == 409  # nothing labeled yet
    label("acme-author", {})
    again = env.client.put(f"{b}/items/0/label", json={"score": 1}, headers=env.h("acme-author"))
    assert again.status_code == 409  # immutable, and the item already has its review
    label(
        "acme-rev2", {9: 1, 19: 4}
    )  # reviewer 2 gets only the doubly-reviewed items; item 9 disagrees with 4
    st = env.client.get(b, headers=env.h("acme-viewer")).json()
    assert (
        st["double_reviewed"] == 2 and st["disagreeing_items"] == [9] and st["by_state"]["disagreement"] == 1
    )
    assert (
        env.client.get(f"{b}/export", headers=env.h("acme-boss")).status_code == 409
    )  # a disagreement blocks export

    adj = f"{b}/items/9/adjudication"
    assert env.client.put(adj, json={"score": 2}, headers=env.h("acme-author")).status_code == 403
    assert env.client.put(adj, json={"score": 2}, headers=env.h("acme-boss")).status_code == 204
    assert (
        env.client.put(adj, json={"score": 2}, headers=env.h("acme-boss")).status_code == 409
    )  # nothing left to settle
    exported = env.client.get(f"{b}/export", headers=env.h("acme-boss")).json()
    assert len(exported) == 20 and exported[9]["human_score"] == 2 and exported[0]["human_score"] == 4
    assert env.client.get(f"{b}/export", headers=env.h("acme-author")).status_code == 403
    assert env.client.get(b, headers=env.h("globex-admin", "globex")).status_code == 404


# -- disputes over gate failures ------------------------------------------------------------------------------------------


def two_task_run(run_id: str) -> dict[str, Any]:
    """One task fails the gate, one passes, so a waiver on the failing task leaves something actually gated."""
    rec = RunRecord(
        run_id=run_id, suite_version="s1", started_at=NOW,
        results=[SampleResult(
            task_id=t, task_hash="h", sample=0, generator_model="m", code="c", composite=score, isolation="rpc",
            scores=[GraderScore(grader="tests", grader_version="2", score=score)],
        ) for t, score in (("fizzbuzz", 0.0), ("other", 1.0))],
    )  # fmt: skip
    return rec.model_dump(mode="json")


def test_dispute_workflow_from_blocked_decision_to_exception_and_regate(env: Env) -> None:
    from datetime import timedelta

    env.setup_project()
    env.client.put(
        "/v1/projects/app/members/acme-boss", json={"role": "approver"}, headers=env.h("acme-admin")
    )
    base = "/v1/projects/app"
    env.client.post(f"{base}/runs", json=two_task_run("r2"), headers=env.h("acme-author"))
    env.client.post(f"{base}/runs", json=run_json("good"), headers=env.h("acme-author"))
    gate = lambda run, **extra: env.client.post(
        f"{base}/runs/{run}/gate", json={"rule": RULE, **extra}, headers=env.h("acme-approver")
    ).json()
    blocked, passed = gate("r2"), gate("good")
    assert blocked["outcome"] == "block" and passed["outcome"] == "pass"
    d = blocked["decision_id"]
    url = f"{base}/decisions/{d}/dispute"
    reason = {"reason": "the reference solution is wrong for n=0", "task_ids": ["fizzbuzz"]}

    assert env.client.post(url, json=reason, headers=env.h("acme-viewer")).status_code == 403
    assert (
        env.client.post(url, json={**reason, "task_ids": ["nope"]}, headers=env.h("acme-author")).status_code
        == 400
    )
    assert (
        env.client.post(
            f"{base}/decisions/{passed['decision_id']}/dispute", json=reason, headers=env.h("acme-author")
        ).status_code
        == 409
    )
    assert env.client.post(url, json=reason, headers=env.h("acme-approver")).status_code == 201
    assert (
        env.client.post(url, json=reason, headers=env.h("acme-author")).status_code == 409
    )  # one dispute per decision
    assert env.client.get(url, headers=env.h("acme-viewer")).json()["state"] == "open"
    assert gate("r2", dispute_decision_id=d) == {
        "error": "The dispute on decision " + repr(d) + " has no approved exception to apply."
    }

    res = f"{url}/resolution"
    soon = (datetime.now(UTC) + timedelta(days=7)).isoformat()
    waive = {
        "outcome": "exception_approved",
        "rationale": "task bug, fix pending",
        "exceptions": [{"task_id": "fizzbuzz", "reason": "bug", "expires_at": soon}],
    }
    assert env.client.post(res, json=waive, headers=env.h("acme-author")).status_code == 403
    assert (
        env.client.post(res, json=waive, headers=env.h("acme-approver")).status_code == 403
    )  # opened it themselves
    bad = [
        {**waive, "exceptions": [{"task_id": "other", "reason": "x", "expires_at": soon}]},  # not contested
        {
            **waive,
            "exceptions": [
                {
                    "task_id": "fizzbuzz",
                    "reason": "x",
                    "expires_at": (datetime.now(UTC) + timedelta(days=90)).isoformat(),
                }
            ],
        },
        {**waive, "exceptions": []},
        {"outcome": "task_corrected", "rationale": "r"},  # no reference
        {"outcome": "task_corrected", "rationale": "r", "correction_ref": "s9"},  # no such suite
        {"outcome": "rejected", "rationale": "r", "correction_ref": "s2"},
    ]
    assert [env.client.post(res, json=b, headers=env.h("acme-boss")).status_code for b in bad] == [400] * 6

    ok = env.client.post(res, json=waive, headers=env.h("acme-boss"))
    assert (
        ok.status_code == 201 and ok.json()["exceptions"][0]["approver"] == "acme-boss"
    )  # the resolver is the named approver
    assert (
        env.client.post(res, json=waive, headers=env.h("acme-boss")).status_code == 409
    )  # resolutions are immutable
    assert env.client.get(url, headers=env.h("acme-viewer")).json()["state"] == "resolved"

    regated = gate("r2", dispute_decision_id=d)
    assert (
        regated["outcome"] == "pass"
        and regated["decision_id"] != d
        and regated["waived"][0]["task_id"] == "fizzbuzz"
    )
    assert (
        env.client.get(f"{base}/decisions/{d}", headers=env.h("acme-viewer")).json() == blocked
    )  # the contested decision is untouched
    assert gate("good", dispute_decision_id=d).get("error", "").startswith("That dispute concerns run")
    assert [v["state"] for v in env.client.get(f"{base}/disputes", headers=env.h("acme-viewer")).json()] == [
        "resolved"
    ]
    assert env.client.get(f"{base}/disputes", headers=env.h("globex-admin", "globex")).status_code == 404
    actions = [e.action for e in env.audit.events() if e.action.startswith("dispute.")]
    assert actions == ["dispute.opened", "dispute.resolved"]


def test_a_dispute_can_be_rejected_or_resolved_by_pointing_at_a_corrected_suite(env: Env) -> None:
    env.setup_project()
    env.client.put(
        "/v1/projects/app/members/acme-boss", json={"role": "approver"}, headers=env.h("acme-admin")
    )
    base = "/v1/projects/app"
    env.client.post(f"{base}/runs", json=two_task_run("r2"), headers=env.h("acme-author"))
    suite = {"tasks": [{"id": "fizzbuzz", "language": "python", "prompt": "p", "tests": "assert True"}]}
    env.client.put(f"{base}/suites/s2", json=suite, headers=env.h("acme-author"))

    def blocked() -> str:
        r = env.client.post(f"{base}/runs/r2/gate", json={"rule": RULE}, headers=env.h("acme-approver"))
        return str(r.json()["decision_id"])

    one, two = blocked(), blocked()
    for d in (one, two):
        env.client.post(
            f"{base}/decisions/{d}/dispute",
            json={"reason": "r", "task_ids": ["fizzbuzz"]},
            headers=env.h("acme-author"),
        )
    rejected = env.client.post(
        f"{base}/decisions/{one}/dispute/resolution",
        json={"outcome": "rejected", "rationale": "the task is right"},
        headers=env.h("acme-boss"),
    )
    fixed = env.client.post(
        f"{base}/decisions/{two}/dispute/resolution",
        json={"outcome": "task_corrected", "rationale": "tests fixed", "correction_ref": "s2"},
        headers=env.h("acme-boss"),
    )
    assert (rejected.status_code, rejected.json()["exceptions"]) == (201, []) and fixed.json()[
        "correction_ref"
    ] == "s2"
    assert (
        env.client.post(
            f"{base}/runs/r2/gate",
            json={"rule": RULE, "dispute_decision_id": one},
            headers=env.h("acme-approver"),
        ).status_code
        == 409
    )
