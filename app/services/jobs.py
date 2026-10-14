from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.config import load_settings
from app.errors import BusinessError
from app.models import Job, Note, Tenant, utcnow
from app.schemas import CreateJob, JobOut, JobPage, JobSummary, NoteOut
from app.services.accounts import change_balance, lock_account


def create_job(
    session: Session,
    tenant: Tenant,
    payload: CreateJob,
    key: str,
) -> tuple[Job, bool]:
    tenant_id = tenant.id
    try:
        lock_account(session, tenant_id)
        existing = session.scalar(
            select(Job).where(Job.tenant_id == tenant_id, Job.idempotency_key == key)
            .execution_options(populate_existing=True)
        )
        if existing is not None:
            if (existing.title, existing.text) != (payload.title, payload.text):
                raise BusinessError(409, "idempotency_conflict")
            session.commit()
            return existing, False

        cost = load_settings().job_cost
        job = Job(
            tenant_id=tenant_id, title=payload.title, text=payload.text,
            idempotency_key=key, cost=cost,
        )
        session.add(job)
        session.flush()
        change_balance(session, tenant_id, -cost, "charge", job.id, job.billing_cycle)
        session.commit()
        return job, True
    except Exception:
        session.rollback()
        raise


def get_job(session: Session, tenant_id: str, job_id: str) -> Job:
    job = session.scalar(
        select(Job).where(Job.id == job_id, Job.tenant_id == tenant_id)
        .execution_options(populate_existing=True)
    )
    if job is None:
        raise BusinessError(404, "job_not_found")
    return job


def render_job(job: Job) -> JobOut:
    return JobOut(
        id=job.id,
        title=job.title,
        text=job.text,
        status=job.status,
        attempts=job.attempts,
        error_code=job.error_code,
        result=job.result,
        cost=job.cost,
        created_at=job.created_at,
        updated_at=job.updated_at,
        notes=[NoteOut.model_validate(note) for note in job.notes],
    )


def list_statements(
    tenant_id: str,
    status: str | None,
    limit: int,
    offset: int,
):
    filters = [Job.tenant_id == tenant_id]
    if status is not None:
        filters.append(Job.status == status)
    note_count = (
        select(func.count()).select_from(Note).where(Note.job_id == Job.id)
        .correlate(Job).scalar_subquery().label("note_count")
    )
    # Project summaries only: no full Job or Note objects, text, or result.
    page = (
        select(Job.id, Job.title, Job.status, Job.attempts, Job.cost, Job.created_at, note_count)
        .where(*filters).order_by(Job.created_at.desc(), Job.id.desc())
        .limit(limit).offset(offset)
    )
    count = select(func.count()).select_from(Job).where(*filters)
    return count, page


def list_jobs(
    session: Session,
    tenant: Tenant,
    status: str | None,
    limit: int,
    offset: int,
) -> JobPage:
    count, page = list_statements(tenant.id, status, limit, offset)
    total = session.scalar(count)
    rows = session.execute(page).mappings()
    return JobPage(
        items=[JobSummary(**row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


def add_note(session: Session, tenant_id: str, job_id: str, text: str) -> Note:
    job = get_job(session, tenant_id, job_id)
    note = Note(job_id=job.id, text=text)
    session.add(note)
    session.commit()
    return note


def retry_job(session: Session, tenant_id: str, job_id: str) -> Job:
    try:
        # Ownership must be checked before state and available credits.
        get_job(session, tenant_id, job_id)
        # The conditional UPDATE serializes competing retries, including on
        # SQLite. Its uncommitted queued state cannot be claimed by a worker.
        job = session.scalar(
            update(Job).where(
                Job.id == job_id, Job.tenant_id == tenant_id, Job.status == "failed",
            ).values(
                status="queued", attempts=0, error_code=None, result=None,
                billing_cycle=Job.billing_cycle + 1, updated_at=utcnow(),
            ).returning(Job)
            .execution_options(synchronize_session=False, populate_existing=True)
        )
        if job is None:
            raise BusinessError(409, "job_not_retryable")
        change_balance(session, tenant_id, -job.cost, "charge", job.id, job.billing_cycle)
        session.commit()
        return job
    except Exception:
        session.rollback()
        raise
