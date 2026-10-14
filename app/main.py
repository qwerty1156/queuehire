import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.db import SessionLocal
from app.errors import BusinessError
from app.routers import account, jobs

app = FastAPI(title="QueueHire", version="0.1.0")
app.include_router(jobs.router)
app.include_router(account.router)


@app.middleware("http")
async def request_id(request: Request, call_next):
    request.state.request_id = str(uuid.uuid4())
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    return response


@app.exception_handler(BusinessError)
async def business_error(request: Request, exc: BusinessError):
    return JSONResponse(status_code=exc.status, content={"detail": exc.code})


@app.get("/health/live")
def live():
    return {"status": "ok"}


@app.get("/health/ready")
def ready():
    with SessionLocal() as session:
        session.execute(text("SELECT 1"))
    return {"status": "ok"}
