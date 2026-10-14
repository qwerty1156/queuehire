from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Response
from sqlalchemy.orm import Session

from app.auth import current_tenant
from app.db import get_session
from app.models import Tenant
from app.schemas import CreateJob, CreateNote, JobOut, JobPage, JobStatus, NoteOut
from app.services import jobs

router = APIRouter(prefix="/v1/jobs", tags=["jobs"])
SessionDep = Annotated[Session, Depends(get_session)]
TenantDep = Annotated[Tenant, Depends(current_tenant)]


@router.post("", response_model=JobOut, status_code=201)
def create(
    payload: CreateJob,
    session: SessionDep,
    tenant: TenantDep,
    response: Response,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=100)],
):
    job, created = jobs.create_job(session, tenant, payload, idempotency_key)
    response.status_code = 201 if created else 200
    return jobs.render_job(job)


@router.get("", response_model=JobPage)
def list_all(
    session: SessionDep,
    tenant: TenantDep,
    status: JobStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    return jobs.list_jobs(session, tenant, status, limit, offset)


@router.get("/{job_id}", response_model=JobOut)
def detail(job_id: str, session: SessionDep, tenant: TenantDep):
    return jobs.render_job(jobs.get_job(session, tenant.id, job_id))


@router.post("/{job_id}/notes", response_model=NoteOut, status_code=201)
def note(job_id: str, payload: CreateNote, session: SessionDep, tenant: TenantDep):
    return NoteOut.model_validate(jobs.add_note(session, tenant.id, job_id, payload.text))


@router.post("/{job_id}/retry", response_model=JobOut)
def retry(job_id: str, session: SessionDep, tenant: TenantDep):
    return jobs.render_job(jobs.retry_job(session, tenant.id, job_id))
