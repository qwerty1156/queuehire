"""SQLite concurrency checks are useful but do not replace PostgreSQL tests."""
from concurrent.futures import ThreadPoolExecutor
import threading

from sqlalchemy import func, select, text

from app.models import Job, LedgerEntry, Tenant
from tests.helpers import balance, create, entries, set_balance
from tests.stage1_process_helpers import run_process_workers


def simultaneous(client, headers, keys):
    barrier = threading.Barrier(len(keys))
    def submit(key):
        barrier.wait(timeout=10)
        return create(client, headers, key=key)
    with ThreadPoolExecutor(max_workers=len(keys)) as pool:
        return list(pool.map(submit, keys))


def test_sqlite_parallel_creation_with_one_key_and_last_credits(client, db, headers):
    set_balance(db, 3)
    responses = simultaneous(client, headers["alpha"], ["same"] * 8)
    statuses = [r.status_code for r in responses]
    assert statuses.count(201) == 1 and statuses.count(200) == 7, statuses
    assert len({r.json()["id"] for r in responses}) == 1
    assert balance(client, headers["alpha"]) == 0
    assert len(entries(db, kind="charge")) == 1


def test_sqlite_parallel_different_keys_cannot_overspend(client, db, headers):
    set_balance(db, 3)
    responses = simultaneous(client, headers["alpha"], [f"unique-{n}" for n in range(8)])
    statuses = [r.status_code for r in responses]
    assert statuses.count(201) == 1 and statuses.count(402) == 7, statuses
    assert balance(client, headers["alpha"]) == 0
    with db["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 1
        assert session.scalar(select(func.sum(LedgerEntry.amount)).where(LedgerEntry.tenant_id == db["ids"]["alpha"])) == session.get(Tenant, db["ids"]["alpha"]).balance


def test_sqlite_workers_in_separate_processes_call_provider_once(client, db, headers, tmp_path):
    job_id = create(client, headers["alpha"]).json()["id"]
    with db["engine"].begin() as connection:
        connection.execute(text("CREATE TABLE provider_calls (attempt integer NOT NULL)"))
    run_process_workers(db, job_id, tmp_path)
    with db["engine"].connect() as connection:
        assert connection.execute(text("SELECT attempt FROM provider_calls")).scalars().all() == [1]
    job = client.get(f"/v1/jobs/{job_id}", headers=headers["alpha"]).json()
    assert job["status"] == "succeeded" and job["attempts"] == 1
    assert balance(client, headers["alpha"]) == 17
    assert entries(db, kind="refund") == []
