"""Invariant checks for the first three repair goals (no list/retry feature tests)."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.models import Job, LedgerEntry, Note, Tenant
from app.worker import process_job
from tests.helpers import PAYLOAD, balance, create, entries, set_balance


def snapshot(db):
    with db["factory"]() as session:
        return {
            "jobs": list(session.execute(select(
                Job.id, Job.tenant_id, Job.title, Job.text, Job.status, Job.attempts,
                Job.error_code, Job.result, Job.cost, Job.billing_cycle,
                Job.created_at, Job.updated_at,
            ).order_by(Job.id))),
            "notes": list(session.execute(select(Note.id, Note.job_id, Note.text).order_by(Note.id))),
            "balances": list(session.execute(select(Tenant.id, Tenant.balance).order_by(Tenant.id))),
            "ledger": list(session.execute(select(
                LedgerEntry.id, LedgerEntry.tenant_id, LedgerEntry.job_id, LedgerEntry.kind,
                LedgerEntry.amount, LedgerEntry.billing_cycle,
            ).order_by(LedgerEntry.id))),
        }


@pytest.mark.parametrize("status", ["queued", "running", "succeeded", "failed"])
@pytest.mark.parametrize("action", ["read", "note", "retry"])
def test_foreign_and_missing_ids_are_indistinguishable_and_do_not_write(client, db, headers, status, action):
    job_id = create(client, headers["alpha"]).json()["id"]
    client.post(f"/v1/jobs/{job_id}/notes", headers=headers["alpha"], json={"text": "private"})
    with db["factory"]() as session:
        job = session.get(Job, job_id)
        job.status = status
        job.result = {"private": "tenant alpha result"}
        session.commit()
    set_balance(db, 0, "beta")
    before = snapshot(db)
    for candidate in [job_id, "missing-job"]:
        path = f"/v1/jobs/{candidate}"
        if action == "read":
            response = client.get(path, headers=headers["beta"])
        elif action == "note":
            response = client.post(path + "/notes", headers=headers["beta"], json={"text": "attack"})
        else:
            response = client.post(path + "/retry", headers=headers["beta"])
        assert response.status_code == 404
        assert response.json() == {"detail": "job_not_found"}
        assert response.headers["X-Request-ID"]
    assert snapshot(db) == before


@pytest.mark.parametrize("change", [
    {"title": PAYLOAD["title"].lower()},
    {"title": PAYLOAD["title"] + " "},
    {"text": PAYLOAD["text"] + " "},
    {"text": PAYLOAD["text"].lower()},
])
def test_payload_comparison_is_exact_and_precedes_credit_check(client, db, headers, change):
    set_balance(db, 3)
    create(client, headers["alpha"])
    before = snapshot(db)
    response = create(client, headers["alpha"], payload={**PAYLOAD, **change})
    assert response.status_code == 409
    assert response.json() == {"detail": "idempotency_conflict"}
    assert snapshot(db) == before


@pytest.mark.parametrize("marker,status", [("Python", "succeeded"), ("[PERMANENT]", "failed")])
def test_creation_replay_returns_current_job_with_notes_without_repricing(client, db, headers, monkeypatch, marker, status):
    payload = {**PAYLOAD, "text": marker}
    job_id = create(client, headers["alpha"], payload=payload).json()["id"]
    client.post(f"/v1/jobs/{job_id}/notes", headers=headers["alpha"], json={"text": "review"})
    with db["factory"]() as session:
        process_job(session, job_id)
    monkeypatch.setenv("JOB_COST", "1000")
    before = snapshot(db)
    response = create(client, headers["alpha"], payload={"text": marker, "title": PAYLOAD["title"]})
    assert response.status_code == 200
    job = response.json()
    assert job["id"] == job_id and job["status"] == status and job["cost"] == 3
    assert job["notes"][0]["text"] == "review"
    assert snapshot(db) == before


@pytest.mark.parametrize("kind", ["job", "charge"])
def test_create_rolls_back_every_effect_and_key_can_be_reused_after_db_failure(client, db, headers, kind):
    table, condition = ("jobs", "1") if kind == "job" else ("ledger", "NEW.kind = 'charge'")
    with db["engine"].begin() as connection:
        connection.execute(text(f"""
            CREATE TRIGGER reject_insert BEFORE INSERT ON {table}
            WHEN {condition} BEGIN SELECT RAISE(ABORT, 'injected write failure'); END
        """))
    before = snapshot(db)
    assert create(client, headers["alpha"]).status_code == 500
    assert snapshot(db) == before
    with db["engine"].begin() as connection:
        connection.execute(text("DROP TRIGGER reject_insert"))
    assert create(client, headers["alpha"]).status_code == 201
    assert balance(client, headers["alpha"]) == 17
    assert len(entries(db, kind="charge")) == 1


@pytest.mark.parametrize("marker", ["[PERMANENT]", "[TEMPORARY]"])
def test_refund_write_failure_cannot_persist_terminal_state_or_credit(client, db, headers, marker):
    job_id = create(client, headers["alpha"], payload={**PAYLOAD, "text": marker}).json()["id"]
    if marker == "[TEMPORARY]":
        for _ in range(2):
            with db["factory"]() as session:
                assert process_job(session, job_id) == "queued"
    with db["engine"].begin() as connection:
        connection.execute(text("""
            CREATE TRIGGER reject_refund BEFORE INSERT ON ledger WHEN NEW.kind = 'refund'
            BEGIN SELECT RAISE(ABORT, 'injected refund failure'); END
        """))
    with db["factory"]() as session:
        with pytest.raises(DBAPIError):
            process_job(session, job_id)
    job = client.get(f"/v1/jobs/{job_id}", headers=headers["alpha"]).json()
    assert job["status"] == "running"
    assert job["attempts"] == (1 if marker == "[PERMANENT]" else 3)
    assert job["result"] is None
    assert balance(client, headers["alpha"]) == 17
    assert entries(db, kind="refund") == []


@pytest.mark.parametrize("marker", ["Python", "[PERMANENT]", "[TEMPORARY]"])
def test_terminal_delivery_is_noop_even_with_a_stale_session(client, db, headers, monkeypatch, marker):
    job_id = create(client, headers["alpha"], payload={**PAYLOAD, "text": marker}).json()["id"]
    with db["factory"]() as stale:
        cached = stale.get(Job, job_id)
        for _ in range(3 if marker == "[TEMPORARY]" else 1):
            with db["factory"]() as session:
                process_job(session, job_id)
        before = snapshot(db)
        def unexpected(*args):
            pytest.fail("Terminal or missing delivery called provider")
        monkeypatch.setattr("app.worker.analyze", unexpected)
        process_job(stale, job_id)
        process_job(stale, "missing-job")
        assert cached is not None
    assert snapshot(db) == before


def test_active_running_job_is_visible_and_reentrant_delivery_cannot_call_provider(client, db, headers, monkeypatch):
    job_id = create(client, headers["alpha"]).json()["id"]
    started, finish = threading.Event(), threading.Event()
    calls = []
    def provider(title, text, attempt):
        calls.append(attempt)
        started.set()
        assert finish.wait(10)
        return {"summary": "one provider call"}
    monkeypatch.setattr("app.worker.analyze", provider)
    def run():
        with db["factory"]() as session:
            return process_job(session, job_id)
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(run)
        try:
            assert started.wait(10)
            before = snapshot(db)
            body = client.get(f"/v1/jobs/{job_id}", headers=headers["alpha"]).json()
            assert body["status"] == "running" and body["attempts"] == 1
            assert run() == "running"
            assert snapshot(db) == before
        finally:
            finish.set()
        assert running.result(timeout=10) == "succeeded"
    assert calls == [1]


def test_each_temporary_delivery_is_one_attempt_and_refund_uses_saved_cost(client, db, headers, monkeypatch):
    monkeypatch.setenv("JOB_COST", "7")
    job_id = create(client, headers["alpha"], payload={**PAYLOAD, "text": "[TEMPORARY]"}).json()["id"]
    monkeypatch.setenv("JOB_COST", "1")
    for attempt in range(1, 4):
        with db["factory"]() as session:
            process_job(session, job_id)
        job = client.get(f"/v1/jobs/{job_id}", headers=headers["alpha"]).json()
        assert job["attempts"] == attempt
        assert job["status"] == ("queued" if attempt < 3 else "failed")
        assert job["error_code"] == "provider_unavailable" and job["result"] is None
        assert balance(client, headers["alpha"]) == (13 if attempt < 3 else 20)
    assert [entry.amount for entry in entries(db, kind="refund")] == [7]
    with db["factory"]() as session:
        total = session.scalar(select(func.sum(LedgerEntry.amount)).where(LedgerEntry.tenant_id == db["ids"]["alpha"]))
        assert total == session.get(Tenant, db["ids"]["alpha"]).balance
