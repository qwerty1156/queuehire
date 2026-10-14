from sqlalchemy import select

from app.models import Job, LedgerEntry, Tenant

PAYLOAD = {"title": "Python Backend Developer", "text": "Python FastAPI PostgreSQL Docker"}


def create(client, headers, key="one", payload=None):
    return client.post(
        "/v1/jobs",
        headers={**headers, "Idempotency-Key": key},
        json=PAYLOAD if payload is None else payload,
    )


def balance(client, headers):
    return client.get("/v1/account", headers=headers).json()["balance"]


def entries(db, tenant="alpha", kind=None):
    with db["factory"]() as session:
        query = select(LedgerEntry).where(LedgerEntry.tenant_id == db["ids"][tenant])
        if kind:
            query = query.where(LedgerEntry.kind == kind)
        return list(session.scalars(query))


def set_balance(db, amount, tenant="alpha"):
    with db["factory"]() as session:
        account = session.get(Tenant, db["ids"][tenant])
        old = account.balance
        account.balance = amount
        session.add(LedgerEntry(
            tenant_id=account.id, kind="adjustment", amount=amount-old, job_id=None,
        ))
        session.commit()


def mark_failed(db, job_id):
    # Useful for list tests only, not a substitute for billing lifecycle.
    with db["factory"]() as session:
        job = session.get(Job, job_id)
        job.status = "failed"
        job.error_code = "provider_unavailable"
        session.commit()
