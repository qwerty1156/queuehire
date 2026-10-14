import argparse
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.auth import digest_key
from app.db import SessionLocal
from app.models import ApiKey, Job, LedgerEntry, Note, Tenant

DEMO_KEYS = {"alpha": "demo-alpha-key", "beta": "demo-beta-key"}


def seed(large: bool):
    with SessionLocal() as session:
        if session.scalar(select(Tenant.id)) is not None:
            print("Database already has tenants; seed skipped.")
            return
        for name in ["alpha", "beta"]:
            tenant = Tenant(name=name, balance=100)
            session.add(tenant)
            session.flush()
            session.add(ApiKey(tenant_id=tenant.id, digest=digest_key(DEMO_KEYS[name])))
            session.add(LedgerEntry(
                tenant_id=tenant.id, amount=100, kind="opening", job_id=None,
            ))
        session.commit()
        if large:
            tenant = session.scalar(select(Tenant).where(Tenant.name == "alpha"))
            now = datetime.now(timezone.utc)
            # Imported historical jobs are free: cost=0, no billing effect.
            for index in range(1000):
                job = Job(
                    tenant_id=tenant.id,
                    idempotency_key=f"import-{index}",
                    title=f"Historical Python job {index}",
                    text="Python FastAPI PostgreSQL",
                    status="succeeded" if index % 3 else "failed",
                    cost=0,
                    attempts=1,
                    created_at=now - timedelta(seconds=index),
                )
                session.add(job)
                session.flush()
                for n in range(index % 4):
                    session.add(Note(job_id=job.id, text=f"Imported note {n}"))
            session.commit()
    print("Created alpha and beta demo accounts, 100 credits each.")
    print("Keys: demo-alpha-key / demo-beta-key (local demo only).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--large", action="store_true")
    seed(parser.parse_args().large)
