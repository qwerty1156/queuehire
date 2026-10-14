"""Real PostgreSQL transactions and cross-process worker ownership."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from app.models import Job, LedgerEntry, Tenant
from app.provider import ProviderPermanentError
from app.worker import process_job
from tests.helpers import PAYLOAD, balance, create, entries, set_balance
from tests.stage1_process_helpers import run_process_workers

pytestmark = pytest.mark.postgres


def test_competing_payloads_at_zero_balance_produce_conflict_not_an_extra_charge(pgclient, pgdb, headers):
    set_balance(pgdb, 3)
    barrier = threading.Barrier(6)
    def submit(index):
        payload = {**PAYLOAD, "text": "payload A" if index % 2 else "payload B"}
        barrier.wait(timeout=10)
        return create(pgclient, headers["alpha"], key="shared-key", payload=payload)
    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(submit, range(6)))
    statuses = [response.status_code for response in responses]
    assert statuses.count(201) == 1
    assert statuses.count(200) == 2
    assert statuses.count(409) == 3
    assert balance(pgclient, headers["alpha"]) == 0
    assert len(entries(pgdb, kind="charge")) == 1


def test_simultaneous_same_key_for_two_tenants_is_independent(pgclient, pgdb, headers):
    barrier = threading.Barrier(2)
    def submit(tenant):
        barrier.wait(timeout=10)
        return create(pgclient, headers[tenant], key="daily-import")
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(submit, ["alpha", "beta"]))
    assert [r.status_code for r in responses] == [201, 201]
    assert responses[0].json()["id"] != responses[1].json()["id"]
    assert balance(pgclient, headers["alpha"]) == balance(pgclient, headers["beta"]) == 17


def test_workers_in_different_processes_call_provider_once(pgclient, pgdb, headers, tmp_path):
    job_id = create(pgclient, headers["alpha"]).json()["id"]
    with pgdb["engine"].begin() as connection:
        connection.execute(text("CREATE TABLE provider_calls (attempt integer NOT NULL)"))
    run_process_workers(pgdb, job_id, tmp_path)
    with pgdb["engine"].connect() as connection:
        assert connection.execute(text("SELECT attempt FROM provider_calls")).scalars().all() == [1]
    job = pgclient.get(f"/v1/jobs/{job_id}", headers=headers["alpha"]).json()
    assert job["status"] == "succeeded" and job["attempts"] == 1
    assert balance(pgclient, headers["alpha"]) == 17
    assert entries(pgdb, kind="refund") == []


def test_parallel_refunds_for_different_jobs_cannot_lose_balance_updates(pgclient, pgdb, headers, monkeypatch):
    ids = [create(pgclient, headers["alpha"], key=f"job-{n}").json()["id"] for n in range(4)]
    barrier = threading.Barrier(4)
    def provider(*args):
        barrier.wait(timeout=10)
        raise ProviderPermanentError("provider_rejected")
    monkeypatch.setattr("app.worker.analyze", provider)
    def run(job_id):
        with pgdb["factory"]() as session:
            return process_job(session, job_id)
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(run, ids)) == ["failed"] * 4
    assert balance(pgclient, headers["alpha"]) == 20
    assert len(entries(pgdb, kind="refund")) == 4
    with pgdb["factory"]() as session:
        assert session.scalar(select(func.sum(LedgerEntry.amount)).where(
            LedgerEntry.tenant_id == pgdb["ids"]["alpha"],
        )) == session.get(Tenant, pgdb["ids"]["alpha"]).balance


def test_postgres_refund_failure_rolls_back_terminal_state_and_balance(pgclient, pgdb, headers):
    job_id = create(pgclient, headers["alpha"], payload={**PAYLOAD, "text": "[PERMANENT]"}).json()["id"]
    with pgdb["engine"].begin() as connection:
        connection.execute(text("""
            CREATE FUNCTION block_refund() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.kind = 'refund' THEN RAISE EXCEPTION 'injected refund failure'; END IF;
                RETURN NEW;
            END; $$
        """))
        connection.execute(text("""
            CREATE TRIGGER reject_refund BEFORE INSERT ON ledger
            FOR EACH ROW EXECUTE FUNCTION block_refund()
        """))
    with pgdb["factory"]() as session:
        with pytest.raises(DBAPIError):
            process_job(session, job_id)
        # The public entry point must leave its session usable after rollback.
        assert session.scalar(select(Job.status).where(Job.id == job_id)) == "running"
    assert balance(pgclient, headers["alpha"]) == 17
    assert entries(pgdb, kind="refund") == []


def test_parallel_duplicate_failed_deliveries_cannot_refund_twice(pgclient, pgdb, headers, monkeypatch):
    job_id = create(pgclient, headers["alpha"], payload={**PAYLOAD, "text": "[PERMANENT]"}).json()["id"]
    with pgdb["factory"]() as session:
        assert process_job(session, job_id) == "failed"
    def unexpected(*args):
        pytest.fail("Failed delivery called provider")
    monkeypatch.setattr("app.worker.analyze", unexpected)
    barrier = threading.Barrier(8)
    def run(_):
        with pgdb["factory"]() as session:
            barrier.wait(timeout=10)
            process_job(session, job_id)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(run, range(8)))
    assert balance(pgclient, headers["alpha"]) == 20
    assert len(entries(pgdb, kind="refund")) == 1
    job = pgclient.get(f"/v1/jobs/{job_id}", headers=headers["alpha"]).json()
    assert job["status"] == "failed" and job["attempts"] == 1
