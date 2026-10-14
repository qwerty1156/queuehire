import argparse
import logging
import time

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import load_settings
from app.db import SessionLocal
from app.models import Job, utcnow
from app.provider import analyze, ProviderPermanentError, ProviderTemporaryError
from app.services.accounts import change_balance

logger = logging.getLogger("queuehire.worker")


def process_job(session: Session, job_id: str) -> str:
    try:
        # The conditional UPDATE arbitrates ownership between processes. A
        # second worker must observe running/terminal and cannot call analyze.
        job = session.scalar(
            update(Job).where(Job.id == job_id, Job.status == "queued")
            .values(status="running", attempts=Job.attempts + 1, updated_at=utcnow())
            .returning(Job)
            .execution_options(synchronize_session=False, populate_existing=True)
        )
        if job is None:
            status = session.scalar(select(Job.status).where(Job.id == job_id))
            session.rollback()
            return status or "missing"
        # Copy claim data before commit so expire_on_commit=True also works.
        title, text, attempt = job.title, job.text, job.attempts
        tenant_id, cost, cycle = job.tenant_id, job.cost, job.billing_cycle
        session.commit()
    except Exception:
        session.rollback()
        raise

    error_code = None
    result = None
    try:
        result = analyze(title, text, attempt)
    except (ProviderTemporaryError, ProviderPermanentError) as exc:
        error_code = str(exc)
        terminal = isinstance(exc, ProviderPermanentError) or attempt >= load_settings().max_attempts
        status = "failed" if terminal else "queued"
    else:
        status = "succeeded"

    try:
        finalized = session.scalar(
            update(Job).where(
                Job.id == job_id, Job.status == "running",
                Job.attempts == attempt, Job.billing_cycle == cycle,
            ).values(status=status, result=result, error_code=error_code, updated_at=utcnow())
            .returning(Job.id).execution_options(synchronize_session=False)
        )
        if finalized is None:
            session.rollback()
            return "not_owned"
        if status == "failed":
            change_balance(session, tenant_id, cost, "refund", job_id, cycle)
        session.commit()
    except Exception:
        # A failure of the refund write also rolls back the terminal state.
        session.rollback()
        raise
    # Keep an already-loaded instance consistent without using it as a lock.
    session.expire(job)
    logger.log(logging.INFO if status == "succeeded" else logging.WARNING,
               "job_processed job_id=%s attempt=%s status=%s", job_id, attempt, status)
    return status


def run_once() -> int:
    with SessionLocal() as session:
        job_ids = list(session.scalars(
            select(Job.id).where(Job.status == "queued")
            .order_by(Job.created_at, Job.id).limit(25)
        ))
        for job_id in job_ids:
            process_job(session, job_id)
        return len(job_ids)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    while True:
        count = run_once()
        if args.once:
            print(f"processed={count}")
            return
        time.sleep(1)


if __name__ == "__main__":
    main()
