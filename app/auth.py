import hashlib
from typing import Annotated

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import ApiKey, Tenant


def digest_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def current_tenant(
    session: Annotated[Session, Depends(get_session)],
    x_api_key: Annotated[str | None, Header()] = None,
) -> Tenant:
    if not x_api_key:
        raise HTTPException(status_code=401, detail="invalid_api_key")
    key = session.scalar(
        select(ApiKey).where(ApiKey.digest == digest_key(x_api_key), ApiKey.active.is_(True))
    )
    if key is None:
        raise HTTPException(status_code=401, detail="invalid_api_key")
    tenant = session.get(Tenant, key.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=401, detail="invalid_api_key")
    return tenant
