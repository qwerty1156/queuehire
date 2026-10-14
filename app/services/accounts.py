from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import LedgerEntry, Tenant
from app.errors import BusinessError


def lock_account(session: Session, tenant_id: str) -> None:
    """Serialize account transactions in the database, including on SQLite.

    This no-op write acquires a row lock on PostgreSQL (a write lock on SQLite)
    before the idempotency lookup. Do not commit until the whole operation ends.
    """
    session.execute(
        update(Tenant).where(Tenant.id == tenant_id).values(balance=Tenant.balance)
        .execution_options(synchronize_session=False)
    )


def change_balance(
    session: Session,
    tenant_id: str,
    amount: int,
    kind: str,
    job_id: str | None,
    billing_cycle: int | None = None,
) -> LedgerEntry:
    statement = update(Tenant).where(Tenant.id == tenant_id)
    if amount < 0:
        statement = statement.where(Tenant.balance >= -amount)
    changed = session.scalar(
        statement.values(balance=Tenant.balance + amount).returning(Tenant.id)
        .execution_options(synchronize_session=False)
    )
    if changed is None:
        raise BusinessError(402, "insufficient_credits")
    entry = LedgerEntry(
        tenant_id=tenant_id, amount=amount, kind=kind, job_id=job_id,
        billing_cycle=billing_cycle,
    )
    session.add(entry)
    return entry


def account_ledger(session: Session, tenant_id: str) -> list[LedgerEntry]:
    return list(session.scalars(
        select(LedgerEntry)
        .where(LedgerEntry.tenant_id == tenant_id)
        .order_by(LedgerEntry.created_at, LedgerEntry.id)
    ))
