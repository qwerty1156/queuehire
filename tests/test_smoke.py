import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import inspect, select

from app.db import build_engine
from app.models import ApiKey, Job
from app.worker import process_job
from tests.helpers import PAYLOAD, balance, create, entries, set_balance


def test_live(client):
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["X-Request-ID"]


@pytest.mark.parametrize("path", ["/v1/jobs", "/v1/account", "/v1/account/ledger"])
def test_auth_required(client, path):
    assert client.get(path).status_code == 401


def test_invalid_key(client):
    assert client.get("/v1/account", headers={"X-API-Key": "wrong"}).status_code == 401


def test_revoked_key(client, db, headers):
    with db["factory"]() as session:
        key = session.scalar(select(ApiKey).where(ApiKey.tenant_id == db["ids"]["alpha"]))
        key.active = False
        session.commit()
    assert client.get("/v1/account", headers=headers["alpha"]).status_code == 401


def test_create_and_charge(client, db, headers):
    response = create(client, headers["alpha"])
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "queued"
    assert body["attempts"] == 0
    assert body["cost"] == 3
    assert body["result"] is None
    assert balance(client, headers["alpha"]) == 17
    assert len(entries(db, kind="charge")) == 1
    assert entries(db, kind="charge")[0].amount == -3


def test_read_own_job(client, headers):
    job_id = create(client, headers["alpha"]).json()["id"]
    response = client.get(f"/v1/jobs/{job_id}", headers=headers["alpha"])
    assert response.status_code == 200
    assert response.json()["title"] == PAYLOAD["title"]


def test_own_note(client, headers):
    job_id = create(client, headers["alpha"]).json()["id"]
    response = client.post(
        f"/v1/jobs/{job_id}/notes", headers=headers["alpha"], json={"text": "Review later"},
    )
    assert response.status_code == 201
    detail = client.get(f"/v1/jobs/{job_id}", headers=headers["alpha"]).json()
    assert detail["notes"][0]["text"] == "Review later"


def test_list_is_scoped(client, headers):
    alpha = create(client, headers["alpha"], "a").json()["id"]
    create(client, headers["beta"], "b")
    page = client.get("/v1/jobs", headers=headers["alpha"]).json()
    assert [item["id"] for item in page["items"]] == [alpha]
    assert page["total"] == 1


def test_account_ledger_scoped(client, headers):
    create(client, headers["beta"], "b")
    ledger = client.get("/v1/account/ledger", headers=headers["alpha"]).json()
    assert len(ledger) == 1
    assert ledger[0]["kind"] == "opening"


def test_insufficient_credits(client, db, headers):
    set_balance(db, 2)
    assert create(client, headers["alpha"]).status_code == 402
    assert balance(client, headers["alpha"]) == 2
    with db["factory"]() as session:
        assert session.scalar(select(Job.id)) is None


def test_missing_idempotency_key(client, headers):
    assert client.post("/v1/jobs", headers=headers["alpha"], json=PAYLOAD).status_code == 422


@pytest.mark.parametrize("payload", [
    {"title": "", "text": "hello"},
    {"title": "hello", "text": ""},
    {"title": "hello", "text": "x" * 20_001},
    {**PAYLOAD, "tenant_id": "attacker"},
])
def test_payload_validation(client, headers, payload):
    assert create(client, headers["alpha"], payload=payload).status_code == 422


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1", "status=invalid"])
def test_query_validation(client, headers, query):
    assert client.get(f"/v1/jobs?{query}", headers=headers["alpha"]).status_code == 422


def test_provider_success(client, db, headers):
    job_id = create(client, headers["alpha"]).json()["id"]
    with db["factory"]() as session:
        assert process_job(session, job_id) == "succeeded"
    job = client.get(f"/v1/jobs/{job_id}", headers=headers["alpha"]).json()
    assert job["attempts"] == 1
    assert "Python" in job["result"]["skills"]
    assert balance(client, headers["alpha"]) == 17


def test_temporary_error_then_success(client, db, headers):
    job_id = create(client, headers["alpha"], payload={**PAYLOAD, "text": "[FAIL_ONCE] Python"}).json()["id"]
    with db["factory"]() as session:
        assert process_job(session, job_id) == "queued"
    with db["factory"]() as session:
        assert process_job(session, job_id) == "succeeded"
    assert balance(client, headers["alpha"]) == 17


def test_sqlite_migration_roundtrip(tmp_path):
    root = Path(__file__).resolve().parents[1]
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    env = dict(os.environ, DATABASE_URL=url)
    for target in ["head", "base", "head"]:
        command = "downgrade" if target == "base" else "upgrade"
        result = subprocess.run(
            [sys.executable, "-m", "alembic", command, target],
            cwd=root, env=env, text=True, capture_output=True,
        )
        assert result.returncode == 0, result.stderr
    engine = build_engine(url)
    assert {"jobs", "tenants", "notes", "ledger", "api_keys"} <= set(inspect(engine).get_table_names())
    engine.dispose()
