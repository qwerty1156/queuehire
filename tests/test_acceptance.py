from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.models import Job, Note
from app.worker import process_job
from tests.helpers import PAYLOAD, balance, create, entries, mark_failed, set_balance

pytestmark = pytest.mark.acceptance


@pytest.mark.parametrize("action", ["read", "note", "retry"])
def test_foreign_job_is_not_found(client, headers, action):
    job_id = create(client, headers["alpha"]).json()["id"]
    path = f"/v1/jobs/{job_id}"
    if action == "read":
        response = client.get(path, headers=headers["beta"])
    elif action == "note":
        response = client.post(path + "/notes", headers=headers["beta"], json={"text": "oops"})
    else:
        response = client.post(path + "/retry", headers=headers["beta"])
    assert response.status_code == 404
    assert response.json()["detail"] == "job_not_found"
    assert client.get(path, headers=headers["alpha"]).json()["notes"] == []


def test_repeat_create_charges_once(client, db, headers):
    first = create(client, headers["alpha"])
    second = create(client, headers["alpha"])
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert balance(client, headers["alpha"]) == 17
    assert len(entries(db, kind="charge")) == 1


def test_repeat_works_with_zero_remaining_credits(client, db, headers):
    set_balance(db, 3)
    first = create(client, headers["alpha"])
    repeated = create(client, headers["alpha"])
    assert repeated.status_code == 200
    assert repeated.json()["id"] == first.json()["id"]
    assert balance(client, headers["alpha"]) == 0


def test_same_key_different_payload_conflicts(client, db, headers):
    create(client, headers["alpha"])
    response = create(client, headers["alpha"], payload={**PAYLOAD, "title": "Different role"})
    assert response.status_code == 409
    assert response.json()["detail"] == "idempotency_conflict"
    assert balance(client, headers["alpha"]) == 17
    assert len(entries(db, kind="charge")) == 1


def test_same_key_is_independent_between_tenants(client, headers):
    first = create(client, headers["alpha"])
    second = create(client, headers["beta"])
    assert second.status_code == 201
    assert second.json()["id"] != first.json()["id"]
    assert client.get(f"/v1/jobs/{second.json()['id']}", headers=headers["alpha"]).status_code == 404


def test_filtered_pagination_and_total(client, db, headers):
    first = create(client, headers["alpha"], "first").json()["id"]
    create(client, headers["alpha"], "second")
    mark_failed(db, first)
    response = client.get("/v1/jobs?status=failed&limit=1", headers=headers["alpha"])
    page = response.json()
    assert page["total"] == 1
    assert [item["id"] for item in page["items"]] == [first]


def test_permanent_failure_does_not_retry(client, db, headers):
    job_id = create(client, headers["alpha"], payload={**PAYLOAD, "text": "[PERMANENT]"}).json()["id"]
    with db["factory"]() as session:
        assert process_job(session, job_id) == "failed"
    assert balance(client, headers["alpha"]) == 20
    assert len(entries(db, kind="refund")) == 1


def test_duplicate_delivery_does_not_refund_twice(client, db, headers):
    job_id = create(client, headers["alpha"], payload={**PAYLOAD, "text": "[TEMPORARY]"}).json()["id"]
    for _ in range(3):
        with db["factory"]() as session:
            process_job(session, job_id)
    before = client.get(f"/v1/jobs/{job_id}", headers=headers["alpha"]).json()
    assert before["status"] == "failed"
    with db["factory"]() as session:
        process_job(session, job_id)
    after = client.get(f"/v1/jobs/{job_id}", headers=headers["alpha"]).json()
    assert after["attempts"] == before["attempts"]
    assert balance(client, headers["alpha"]) == 20
    assert len(entries(db, kind="refund")) == 1


def test_succeeded_delivery_is_noop(client, db, headers):
    job_id = create(client, headers["alpha"]).json()["id"]
    with db["factory"]() as session:
        process_job(session, job_id)
    before = client.get(f"/v1/jobs/{job_id}", headers=headers["alpha"]).json()
    with db["factory"]() as session:
        process_job(session, job_id)
    after = client.get(f"/v1/jobs/{job_id}", headers=headers["alpha"]).json()
    assert after["attempts"] == before["attempts"]
    assert after["result"] == before["result"]


def test_success_clears_previous_error(client, db, headers):
    job_id = create(client, headers["alpha"], payload={**PAYLOAD, "text": "[FAIL_ONCE]"}).json()["id"]
    for _ in range(2):
        with db["factory"]() as session:
            process_job(session, job_id)
    job = client.get(f"/v1/jobs/{job_id}", headers=headers["alpha"]).json()
    assert job["status"] == "succeeded"
    assert job["error_code"] is None


def test_manual_retry_is_a_new_billing_cycle(client, db, headers):
    job_id = create(client, headers["alpha"], payload={**PAYLOAD, "text": "[TEMPORARY]"}).json()["id"]
    for _ in range(3):
        with db["factory"]() as session:
            process_job(session, job_id)
    assert balance(client, headers["alpha"]) == 20
    response = client.post(f"/v1/jobs/{job_id}/retry", headers=headers["alpha"])
    assert response.status_code == 200
    assert response.json()["id"] == job_id
    assert response.json()["status"] == "queued"
    assert response.json()["attempts"] == 0
    assert response.json()["error_code"] is None
    assert balance(client, headers["alpha"]) == 17
    repeated = client.post(f"/v1/jobs/{job_id}/retry", headers=headers["alpha"])
    assert repeated.status_code == 409
    assert balance(client, headers["alpha"]) == 17
    for _ in range(3):
        with db["factory"]() as session:
            process_job(session, job_id)
    assert balance(client, headers["alpha"]) == 20
    assert len(entries(db, kind="charge")) == 2
    assert len(entries(db, kind="refund")) == 2


def test_retry_requires_credits(client, db, headers):
    job_id = create(client, headers["alpha"], payload={**PAYLOAD, "text": "[TEMPORARY]"}).json()["id"]
    for _ in range(3):
        with db["factory"]() as session:
            process_job(session, job_id)
    set_balance(db, 2)
    response = client.post(f"/v1/jobs/{job_id}/retry", headers=headers["alpha"])
    assert response.status_code == 402
    assert balance(client, headers["alpha"]) == 2
    assert client.get(f"/v1/jobs/{job_id}", headers=headers["alpha"]).json()["status"] == "failed"


@pytest.mark.parametrize("status", ["queued", "running", "succeeded"])
def test_retry_rejects_non_failed_state(client, db, headers, status):
    job_id = create(client, headers["alpha"]).json()["id"]
    with db["factory"]() as session:
        job = session.get(Job, job_id)
        job.status = status
        session.commit()
    response = client.post(f"/v1/jobs/{job_id}/retry", headers=headers["alpha"])
    assert response.status_code == 409
    assert response.json()["detail"] == "job_not_retryable"
    assert balance(client, headers["alpha"]) == 17


def test_unknown_job_retry_returns_not_found(client, headers):
    response = client.post("/v1/jobs/nonexistent/retry", headers=headers["alpha"])
    assert response.status_code == 404
    assert response.json()["detail"] == "job_not_found"


def test_list_has_bounded_queries_and_loaded_rows(client, db, headers):
    now = datetime.now(timezone.utc)
    with db["factory"]() as session:
        for index in range(150):
            job = Job(
                tenant_id=db["ids"]["alpha"], idempotency_key=f"import-{index}",
                title=f"Job {index}", text="Python", cost=0, status="succeeded",
                created_at=now-timedelta(seconds=index),
            )
            session.add(job)
            session.flush()
            session.add(Note(job_id=job.id, text="A note"))
        session.commit()
    statements = []
    loaded_jobs = []

    def executed(conn, cursor, statement, parameters, context, many):
        statements.append(statement)

    def loaded(session, instance):
        if isinstance(instance, Job):
            loaded_jobs.append(instance.id)

    event.listen(db["engine"], "before_cursor_execute", executed)
    event.listen(Session, "loaded_as_persistent", loaded)
    try:
        response = client.get("/v1/jobs?limit=20", headers=headers["alpha"])
    finally:
        event.remove(db["engine"], "before_cursor_execute", executed)
        event.remove(Session, "loaded_as_persistent", loaded)
    assert response.status_code == 200
    assert response.json()["total"] == 150
    assert len(response.json()["items"]) == 20
    assert all(item["note_count"] == 1 for item in response.json()["items"])
    assert len(statements) <= 5, f"SQL statements including auth: {len(statements)}"
    assert len(loaded_jobs) <= 20, f"Loaded Job objects: {len(loaded_jobs)}"
