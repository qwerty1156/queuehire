from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth import current_tenant
from app.db import get_session
from app.models import Tenant
from app.schemas import AccountOut, LedgerOut
from app.services.accounts import account_ledger

router = APIRouter(prefix="/v1/account", tags=["account"])


@router.get("", response_model=AccountOut)
def account(tenant: Annotated[Tenant, Depends(current_tenant)]):
    return AccountOut(id=tenant.id, name=tenant.name, balance=tenant.balance)


@router.get("/ledger", response_model=list[LedgerOut])
def ledger(
    tenant: Annotated[Tenant, Depends(current_tenant)],
    session: Annotated[Session, Depends(get_session)],
):
    return account_ledger(session, tenant.id)
