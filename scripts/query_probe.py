"""Count SQL and loaded ORM objects for one authenticated list request."""
import argparse
from time import perf_counter

from fastapi.testclient import TestClient
from sqlalchemy import event

from app.db import engine
from app.main import app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--key", default="demo-alpha-key")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--status", choices=["queued", "running", "succeeded", "failed"])
    args = parser.parse_args()
    statements = []
    loaded = []

    def before_execute(conn, cursor, statement, params, context, many):
        statements.append(statement)

    from sqlalchemy.orm import Session

    def object_loaded(session, instance):
        loaded.append(type(instance).__name__)

    event.listen(engine, "before_cursor_execute", before_execute)
    event.listen(Session, "loaded_as_persistent", object_loaded)
    try:
        started = perf_counter()
        with TestClient(app) as client:
            params = {"limit": args.limit, "offset": args.offset}
            if args.status:
                params["status"] = args.status
            response = client.get("/v1/jobs", params=params, headers={"X-API-Key": args.key})
        elapsed = perf_counter() - started
    finally:
        event.remove(engine, "before_cursor_execute", before_execute)
        event.remove(Session, "loaded_as_persistent", object_loaded)
    print(f"status={response.status_code} seconds={elapsed:.4f}")
    print(f"sql_count={len(statements)} loaded_objects={len(loaded)}")
    print(f"loaded_jobs={loaded.count('Job')} loaded_notes={loaded.count('Note')}")
    if response.status_code == 200:
        page = response.json()
        print(f"items={len(page['items'])} total={page['total']}")
    for n, sql in enumerate(statements, start=1):
        print(f"[{n}] {' '.join(sql.split())}")


if __name__ == "__main__":
    main()
