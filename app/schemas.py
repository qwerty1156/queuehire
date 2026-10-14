from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


JobStatus = Literal["queued", "running", "succeeded", "failed"]


class CreateJob(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=20_000)


class CreateNote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=1000)


class NoteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    text: str
    created_at: datetime


class JobOut(BaseModel):
    id: str
    title: str
    text: str
    status: JobStatus
    attempts: int
    error_code: str | None
    result: dict | None
    cost: int
    created_at: datetime
    updated_at: datetime
    notes: list[NoteOut]


class JobSummary(BaseModel):
    id: str
    title: str
    status: JobStatus
    attempts: int
    cost: int
    note_count: int
    created_at: datetime


class JobPage(BaseModel):
    items: list[JobSummary]
    total: int
    limit: int
    offset: int


class AccountOut(BaseModel):
    id: str
    name: str
    balance: int


class LedgerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    job_id: str | None
    kind: str
    amount: int
    created_at: datetime
