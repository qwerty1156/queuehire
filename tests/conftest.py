import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.auth import digest_key
from app.db import build_engine, get_session
from app.main import app
from app.models import ApiKey, Base, LedgerEntry, Tenant

ROOT = Path(__file__).resolve().parents[1]


def provision(factory, balance=20):
    with factory() as session:
        ids = {}
        for name in ["alpha", "beta"]:
            tenant = Tenant(name=name, balance=balance)
            session.add(tenant)
            session.flush()
            session.add(ApiKey(tenant_id=tenant.id, digest=digest_key(f"test-{name}")))
            session.add(LedgerEntry(
                tenant_id=tenant.id, job_id=None, kind="opening", amount=balance,
            ))
            ids[name] = tenant.id
        session.commit()
        return ids


def bind_client(factory):
    def override():
        with factory() as session:
            yield session
    app.dependency_overrides[get_session] = override
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_COST", "3")
    monkeypatch.setenv("MAX_ATTEMPTS", "3")
    engine = build_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    ids = provision(factory)
    yield {"engine": engine, "factory": factory, "ids": ids}
    engine.dispose()


@pytest.fixture
def client(db):
    with bind_client(db["factory"]) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def headers():
    return {"alpha": {"X-API-Key": "test-alpha"}, "beta": {"X-API-Key": "test-beta"}}


@pytest.fixture
def pgdb(monkeypatch):
    raw_url = os.getenv("TEST_DATABASE_URL")
    if not raw_url:
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL checks")
    monkeypatch.setenv("JOB_COST", "3")
    monkeypatch.setenv("MAX_ATTEMPTS", "3")
    base_url = make_url(raw_url)
    if base_url.get_backend_name() != "postgresql":
        pytest.fail("TEST_DATABASE_URL must point to PostgreSQL")
    # Each test gets a unique disposable schema, never drops a database.
    schema = "qh_test_" + uuid.uuid4().hex
    admin = create_engine(base_url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    url = base_url.update_query_dict({"options": f"-csearch_path={schema}"})
    engine = build_engine(url.render_as_string(hide_password=False))
    try:
        env = dict(os.environ, DATABASE_URL=url.render_as_string(hide_password=False))
        migrated = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT, env=env, capture_output=True, text=True,
        )
        assert migrated.returncode == 0, "Alembic upgrade failed in disposable schema"
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        ids = provision(factory)
        yield {"engine": engine, "factory": factory, "ids": ids}
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture
def pgclient(pgdb):
    with bind_client(pgdb["factory"]) as client:
        yield client
    app.dependency_overrides.clear()
