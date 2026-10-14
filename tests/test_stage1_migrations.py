"""Upgrade existing 0001 data without rewriting identities or ledger history."""
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def migration_database(backend, tmp_path):
    if backend == "sqlite":
        url = f"sqlite:///{tmp_path / 'existing.db'}"
        engine = sa.create_engine(url)
        try:
            yield url, engine
        finally:
            engine.dispose()
        return
    raw = os.getenv("TEST_DATABASE_URL")
    if not raw:
        pytest.skip("Set TEST_DATABASE_URL to verify the PostgreSQL upgrade with historical data")
    base = make_url(raw)
    assert base.get_backend_name() == "postgresql"
    admin = sa.create_engine(base)
    schema = "qh_upgrade_" + uuid.uuid4().hex
    with admin.begin() as connection:
        connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
    url = base.update_query_dict({"options": f"-csearch_path={schema}"}).render_as_string(hide_password=False)
    engine = sa.create_engine(url)
    try:
        yield url, engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def migrate(url, target):
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", target],
        cwd=ROOT, env=dict(os.environ, DATABASE_URL=url), capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("backend", ["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)])
def test_upgrade_preserves_all_historical_fields_and_allows_tenant_key_reuse(backend, tmp_path):
    from datetime import datetime, timezone
    from app.auth import digest_key
    from app.db import build_engine
    from app.models import Job, LedgerEntry, Note, Tenant
    from sqlalchemy.orm import sessionmaker
    from tests.conftest import bind_client
    from app.main import app
    from tests.helpers import create
    with migration_database(backend, tmp_path) as (url, engine):
        migrate(url, "0001")
        metadata = sa.MetaData()
        metadata.reflect(engine)
        tables = {name: metadata.tables[name] for name in ["tenants", "api_keys", "jobs", "notes", "ledger"]}
        now = datetime.now(timezone.utc)
        with engine.begin() as connection:
            connection.execute(tables["tenants"].insert(), [
                {"id": "alpha", "name": "alpha", "balance": 20, "created_at": now},
                # Historical corrupt balance is retained, never silently repaired.
                {"id": "corrupt", "name": "corrupt", "balance": -2, "created_at": now},
                {"id": "beta", "name": "beta", "balance": 20, "created_at": now},
            ])
            connection.execute(tables["api_keys"].insert(), [
                {"id": "key-a", "tenant_id": "alpha", "digest": digest_key("test-alpha"), "active": True},
                {"id": "key-b", "tenant_id": "beta", "digest": digest_key("test-beta"), "active": True},
            ])
            connection.execute(tables["jobs"].insert(), {
                "id": "original-job", "tenant_id": "alpha", "idempotency_key": "shared-key",
                "title": "Original role", "text": "  Python\nUnicode: вакансія  ",
                "status": "failed", "attempts": 3, "error_code": "provider_unavailable",
                "result": {"original": [1, "do not rewrite"]}, "cost": 3,
                "created_at": now, "updated_at": now,
            })
            connection.execute(tables["notes"].insert(), {"id": "original-note", "job_id": "original-job", "text": "Private note", "created_at": now})
            connection.execute(tables["ledger"].insert(), [
                {"id": "opening", "tenant_id": "alpha", "job_id": None, "kind": "opening", "amount": 17, "created_at": now},
                {"id": "charge", "tenant_id": "alpha", "job_id": "original-job", "kind": "charge", "amount": -3, "created_at": now},
                {"id": "refund", "tenant_id": "alpha", "job_id": "original-job", "kind": "refund", "amount": 3, "created_at": now},
                # Duplicate legacy refund survives; cycle boundaries cannot be guessed.
                {"id": "duplicate-refund", "tenant_id": "alpha", "job_id": "original-job", "kind": "refund", "amount": 3, "created_at": now},
                {"id": "beta-opening", "tenant_id": "beta", "job_id": None, "kind": "opening", "amount": 20, "created_at": now},
                {"id": "corrupt-opening", "tenant_id": "corrupt", "job_id": None, "kind": "opening", "amount": -2, "created_at": now},
            ])
        def snapshot():
            with engine.connect() as connection:
                return {name: list(connection.execute(sa.select(table).order_by(table.c.id))) for name, table in tables.items()}
        before = snapshot()
        migrate(url, "head")
        assert snapshot() == before
        migrate(url, "head")
        assert snapshot() == before
        if backend == "sqlite":
            with engine.connect() as connection:
                assert connection.execute(sa.text("PRAGMA foreign_key_check")).all() == []
        new_engine = build_engine(url)
        factory = sessionmaker(bind=new_engine, expire_on_commit=False)
        try:
            with factory() as session:
                assert session.get(Job, "original-job").billing_cycle == 1
                assert session.get(Note, "original-note").text == "Private note"
                assert session.get(Tenant, "corrupt").balance == -2
                assert all(entry.billing_cycle is None for entry in session.scalars(sa.select(LedgerEntry)))
            with bind_client(factory) as client:
                replay = create(client, {"X-API-Key": "test-alpha"}, key="shared-key", payload={"title": "Original role", "text": "  Python\nUnicode: вакансія  "})
                assert replay.status_code == 200 and replay.json()["id"] == "original-job"
                beta = create(client, {"X-API-Key": "test-beta"}, key="shared-key")
                assert beta.status_code == 201 and beta.json()["id"] != "original-job"
                assert client.get("/v1/jobs/original-job", headers={"X-API-Key": "test-beta"}).status_code == 404
        finally:
            app.dependency_overrides.clear()
            new_engine.dispose()
