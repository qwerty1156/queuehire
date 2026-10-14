"""Explain the actual production list/count statements on PostgreSQL."""
import argparse

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.auth import digest_key
from app.db import engine
from app.models import ApiKey
from app.services.jobs import list_statements


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--key", default="demo-alpha-key")
    parser.add_argument("--status", choices=["queued", "running", "succeeded", "failed"])
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--offset", type=int, default=0)
    args = parser.parse_args()
    if engine.dialect.name != "postgresql":
        raise SystemExit("Use a PostgreSQL DATABASE_URL for EXPLAIN; query_probe also supports SQLite.")
    with Session(engine) as session:
        tenant_id = session.scalar(select(ApiKey.tenant_id).where(
            ApiKey.digest == digest_key(args.key), ApiKey.active.is_(True),
        ))
        if tenant_id is None:
            raise SystemExit("The selected database has no active matching demo key.")
        count, page = list_statements(tenant_id, args.status, args.limit, args.offset)
        for name, statement in [("count", count), ("page", page)]:
            sql = str(statement.compile(dialect=engine.dialect, compile_kwargs={"literal_binds": True}))
            print(f"{name}:\n{sql}")
            for row in session.execute(text("EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) " + sql)):
                print(row[0])


if __name__ == "__main__":
    main()
