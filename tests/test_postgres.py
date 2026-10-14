from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest
from sqlalchemy import func, select

from app.models import Job, LedgerEntry, Tenant
from app.worker import process_job
from tests.helpers import PAYLOAD, balance, create, set_balance

pytestmark = [pytest.mark.acceptance, pytest.mark.postgres]


def concurrent_requests(client, headers, keys):
    barrier = threading.Barrier(len(keys))

    def submit(key):
        barrier.wait(timeout=10)
        return create(client, headers, key=key)

    with ThreadPoolExecutor(max_workers=len(keys)) as pool:
        return list(pool.map(submit, keys))


def test_parallel_same_key_charges_exactly_once(pgclient, pgdb, headers):
    responses = concurrent_requests(pgclient, headers["alpha"], ["same"] * 8)
    statuses = [response.status_code for response in responses]
    assert statuses.count(201) == 1, statuses
    assert statuses.count(200) == 7, statuses
    assert len({response.json()["id"] for response in responses}) == 1
    assert balance(pgclient, headers["alpha"]) == 17
    with pgdb["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 1
        assert session.scalar(
            select(func.count()).select_from(LedgerEntry).where(LedgerEntry.kind == "charge")
        ) == 1


def test_parallel_different_keys_cannot_spend_same_credits(pgclient, pgdb, headers):
    set_balance(pgdb, 3)
    responses = concurrent_requests(pgclient, headers["alpha"], [f"unique-{n}" for n in range(8)])
    statuses = [response.status_code for response in responses]
    assert statuses.count(201) == 1, statuses
    assert statuses.count(402) == 7, statuses
    assert balance(pgclient, headers["alpha"]) == 0
    with pgdb["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 1
        tenant = session.get(Tenant, pgdb["ids"]["alpha"])
        total = session.scalar(select(func.sum(LedgerEntry.amount)).where(
            LedgerEntry.tenant_id == tenant.id,
        ))
        assert total == tenant.balance


def test_two_workers_only_one_calls_provider(pgclient, pgdb, headers, monkeypatch):
    job_id = create(pgclient, headers["alpha"]).json()["id"]
    barrier = threading.Barrier(2)
    calls = []
    lock = threading.Lock()

    def fake_provider(title, text, attempt):
        with lock:
            calls.append(attempt)
        time.sleep(0.15)
        return {"summary": "ok", "skills": [], "provider": "test"}

    monkeypatch.setattr("app.worker.analyze", fake_provider)

    def run(_):
        with pgdb["factory"]() as session:
            barrier.wait(timeout=10)
            process_job(session, job_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(run, range(2)))
    assert len(calls) == 1
    job = pgclient.get(f"/v1/jobs/{job_id}", headers=headers["alpha"]).json()
    assert job["status"] == "succeeded"
    assert job["attempts"] == 1


def test_create_is_atomic_if_ledger_write_fails(pgclient, pgdb, headers):
    # Inject a DB-side failure, leaving Python code unchanged.
    from sqlalchemy import text
    with pgdb["engine"].begin() as connection:
        connection.execute(text("""
            CREATE FUNCTION block_charge() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.kind = 'charge' THEN
                    RAISE EXCEPTION 'test charge failure';
                END IF;
                RETURN NEW;
            END;
            $$
        """))
        connection.execute(text("""
            CREATE TRIGGER reject_charge BEFORE INSERT ON ledger
            FOR EACH ROW EXECUTE FUNCTION block_charge()
        """))
    response = create(pgclient, headers["alpha"])
    assert response.status_code >= 400
    assert balance(pgclient, headers["alpha"]) == 20
    with pgdb["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 0
        assert session.scalar(select(func.count()).select_from(LedgerEntry).where(
            LedgerEntry.kind == "charge",
        )) == 0


def test_parallel_manual_retry_charges_once(pgclient, pgdb, headers):
    job_id = create(
        pgclient, headers["alpha"], payload={**PAYLOAD, "text": "[TEMPORARY]"},
    ).json()["id"]
    for _ in range(3):
        with pgdb["factory"]() as session:
            process_job(session, job_id)
    assert balance(pgclient, headers["alpha"]) == 20
    barrier = threading.Barrier(2)

    def retry(_):
        barrier.wait(timeout=10)
        return pgclient.post(f"/v1/jobs/{job_id}/retry", headers=headers["alpha"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(retry, range(2)))
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert balance(pgclient, headers["alpha"]) == 17
    with pgdb["factory"]() as session:
        count = session.scalar(select(func.count()).select_from(LedgerEntry).where(
            LedgerEntry.kind == "charge", LedgerEntry.job_id == job_id,
        ))
        assert count == 2  # Original charge plus exactly one new cycle.
