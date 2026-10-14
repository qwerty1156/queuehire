"""Contract combinations checked on both SQLite and native PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import threading

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.orm import Session

from app.main import app
from app.models import Job, LedgerEntry, Note, Tenant
from app.worker import process_job
from tests.conftest import bind_client
from tests.helpers import PAYLOAD, balance, create, entries, set_balance
from tests.test_stage1_regressions import snapshot


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)])
def store(request):
    db = request.getfixturevalue("db" if request.param == "sqlite" else "pgdb")
    with bind_client(db["factory"]) as client:
        yield db, client
    app.dependency_overrides.clear()


def failed_job(db, client, headers, key="original"):
    payload = {**PAYLOAD, "text": "[PERMANENT]"}
    response = create(client, headers, key=key, payload=payload)
    assert response.status_code == 201
    job_id = response.json()["id"]
    with db["factory"]() as session:
        assert process_job(session, job_id) == "failed"
    return job_id, payload


@pytest.mark.parametrize("status,limit,offset,total,expected", [
    (None, 3, 0, 12, [1, 0, 3]),
    ("failed", 2, 1, 3, [7, 11]),
    ("failed", 20, 99, 3, []),
    ("running", 1, 0, 3, [1]),
    ("queued", 1, 1, 3, [4]),
    ("succeeded", 100, 0, 3, [2, 6, 10]),
])
def test_list_filters_ties_empty_offsets_and_zero_notes_in_database(store, headers, status, limit, offset, total, expected):
    db, client = store
    now = datetime.now(timezone.utc)
    with db["factory"]() as session:
        for tenant in ["alpha", "beta"]:
            for index in range(12):
                job = Job(
                    id=f"{tenant}-{index:02}", tenant_id=db["ids"][tenant],
                    idempotency_key=f"import-{index}", title=f"Role {index}",
                    text="Private vacancy " * 100, result={"private": "large result"},
                    status=["queued", "running", "succeeded", "failed"][index % 4], cost=0,
                    created_at=now - timedelta(seconds=index // 2),
                )
                session.add(job)
                session.flush()
                for note in range(index % 3):
                    session.add(Note(job_id=job.id, text=f"Private {tenant} note {note}"))
        session.commit()
    statements, objects = [], []
    def executed(conn, cursor, statement, parameters, context, many):
        statements.append(statement)
    def loaded(session, instance):
        objects.append(instance)
    event.listen(db["engine"], "before_cursor_execute", executed)
    event.listen(Session, "loaded_as_persistent", loaded)
    try:
        params = {"limit": limit, "offset": offset}
        if status is not None:
            params["status"] = status
        response = client.get("/v1/jobs", params=params, headers=headers["alpha"])
    finally:
        event.remove(db["engine"], "before_cursor_execute", executed)
        event.remove(Session, "loaded_as_persistent", loaded)
    assert response.status_code == 200
    page = response.json()
    assert page["total"] == total and page["limit"] == limit and page["offset"] == offset
    assert [item["id"] for item in page["items"]] == [f"alpha-{index:02}" for index in expected]
    assert [item["note_count"] for item in page["items"]] == [index % 3 for index in expected]
    assert all(set(item) == {"id", "title", "status", "attempts", "cost", "note_count", "created_at"} for item in page["items"])
    assert len(statements) <= 5
    assert not any(isinstance(obj, (Job, Note)) for obj in objects)
    assert not any("jobs.text" in sql or "jobs.result" in sql for sql in statements)


def test_retry_uses_saved_cost_keeps_notes_and_creation_key_and_clears_old_result(store, headers, monkeypatch):
    db, client = store
    job_id, payload = failed_job(db, client, headers["alpha"])
    assert client.post(f"/v1/jobs/{job_id}/notes", headers=headers["alpha"], json={"text": "keep this note"}).status_code == 201
    with db["factory"]() as session:
        job = session.get(Job, job_id)
        job.result = {"historical": "stale failure result"}
        session.commit()
    before = client.get(f"/v1/jobs/{job_id}", headers=headers["alpha"]).json()
    monkeypatch.setenv("JOB_COST", "100")
    response = client.post(f"/v1/jobs/{job_id}/retry", headers=headers["alpha"])
    assert response.status_code == 200
    retried = response.json()
    for key in ["id", "title", "text", "cost", "created_at", "notes"]:
        assert retried[key] == before[key]
    assert retried["status"] == "queued" and retried["attempts"] == 0
    assert retried["result"] is None and retried["error_code"] is None
    assert balance(client, headers["alpha"]) == 17
    after = snapshot(db)
    replay = create(client, headers["alpha"], key="original", payload=payload)
    assert replay.status_code == 200 and replay.json() == retried
    assert snapshot(db) == after
    assert [entry.amount for entry in entries(db, kind="charge")] == [-3, -3]


def test_three_paid_cycles_and_repeated_terminal_deliveries_preserve_ledger_invariant(store, headers):
    db, client = store
    job_id = create(client, headers["alpha"], payload={**PAYLOAD, "text": "[PERMANENT]"}).json()["id"]
    for cycle in range(1, 4):
        with db["factory"]() as session:
            assert process_job(session, job_id) == "failed"
        assert balance(client, headers["alpha"]) == 20
        before = snapshot(db)
        with db["factory"]() as session:
            process_job(session, job_id)
            process_job(session, job_id)
        assert snapshot(db) == before
        with db["factory"]() as session:
            assert session.get(Job, job_id).billing_cycle == cycle
            rows = list(session.scalars(select(LedgerEntry).where(LedgerEntry.job_id == job_id)))
            assert {(entry.billing_cycle, entry.kind, entry.amount) for entry in rows} == {
                (c, kind, amount) for c in range(1, cycle + 1) for kind, amount in [("charge", -3), ("refund", 3)]
            }
            assert len(rows) == cycle * 2
            assert session.scalar(select(func.sum(LedgerEntry.amount)).where(LedgerEntry.tenant_id == db["ids"]["alpha"])) == session.get(Tenant, db["ids"]["alpha"]).balance
        if cycle < 3:
            response = client.post(f"/v1/jobs/{job_id}/retry", headers=headers["alpha"])
            assert response.status_code == 200 and response.json()["attempts"] == 0
            assert balance(client, headers["alpha"]) == 17


@pytest.mark.parametrize("status", ["queued", "running", "succeeded"])
def test_retry_state_conflict_precedes_zero_credit_check(store, headers, status):
    db, client = store
    job_id = create(client, headers["alpha"]).json()["id"]
    with db["factory"]() as session:
        session.get(Job, job_id).status = status
        session.commit()
    set_balance(db, 0)
    before = snapshot(db)
    response = client.post(f"/v1/jobs/{job_id}/retry", headers=headers["alpha"])
    assert response.status_code == 409 and response.json() == {"detail": "job_not_retryable"}
    assert snapshot(db) == before


def test_retry_insufficient_funds_leaves_full_failed_state_unchanged(store, headers):
    db, client = store
    job_id, _ = failed_job(db, client, headers["alpha"])
    set_balance(db, 2)
    before = snapshot(db)
    response = client.post(f"/v1/jobs/{job_id}/retry", headers=headers["alpha"])
    assert response.status_code == 402 and response.json() == {"detail": "insufficient_credits"}
    assert snapshot(db) == before


def test_retry_ledger_write_failure_cannot_reset_state_cycle_or_balance(store, headers):
    db, client = store
    job_id, _ = failed_job(db, client, headers["alpha"])
    with db["engine"].begin() as connection:
        if db["engine"].dialect.name == "sqlite":
            connection.execute(text("""
                CREATE TRIGGER reject_retry_charge BEFORE INSERT ON ledger WHEN NEW.kind = 'charge'
                BEGIN SELECT RAISE(ABORT, 'injected retry charge failure'); END
            """))
        else:
            connection.execute(text("""
                CREATE FUNCTION block_retry_charge() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                    IF NEW.kind = 'charge' THEN RAISE EXCEPTION 'injected retry charge failure'; END IF;
                    RETURN NEW;
                END; $$
            """))
            connection.execute(text("""
                CREATE TRIGGER reject_retry_charge BEFORE INSERT ON ledger
                FOR EACH ROW EXECUTE FUNCTION block_retry_charge()
            """))
    before = snapshot(db)
    assert client.post(f"/v1/jobs/{job_id}/retry", headers=headers["alpha"]).status_code == 500
    assert snapshot(db) == before


def test_eight_parallel_retries_of_one_failed_job_charge_one_new_cycle(store, headers):
    db, client = store
    job_id, _ = failed_job(db, client, headers["alpha"])
    barrier = threading.Barrier(8)
    def retry(_):
        barrier.wait(timeout=10)
        return client.post(f"/v1/jobs/{job_id}/retry", headers=headers["alpha"])
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(retry, range(8)))
    statuses = [response.status_code for response in responses]
    assert statuses.count(200) == 1 and statuses.count(409) == 7, statuses
    assert balance(client, headers["alpha"]) == 17
    assert len(entries(db, kind="charge")) == 2
    with db["factory"]() as session:
        job = session.get(Job, job_id)
        assert job.billing_cycle == 2 and job.status == "queued" and job.attempts == 0


def test_retries_of_different_jobs_compete_for_last_credits_atomically(store, headers):
    db, client = store
    ids = [failed_job(db, client, headers["alpha"], key=f"original-{n}")[0] for n in range(2)]
    set_balance(db, 3)
    barrier = threading.Barrier(2)
    def retry(job_id):
        barrier.wait(timeout=10)
        return client.post(f"/v1/jobs/{job_id}/retry", headers=headers["alpha"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(retry, ids))
    assert sorted(r.status_code for r in responses) == [200, 402]
    assert balance(client, headers["alpha"]) == 0
    with db["factory"]() as session:
        jobs = list(session.scalars(select(Job)))
        assert sorted((job.status, job.billing_cycle) for job in jobs) == [("failed", 1), ("queued", 2)]
        assert session.scalar(select(func.sum(LedgerEntry.amount)).where(LedgerEntry.tenant_id == db["ids"]["alpha"])) == session.get(Tenant, db["ids"]["alpha"]).balance
