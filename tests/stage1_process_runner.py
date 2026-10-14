"""Subprocess-only test provider; never imported by production code."""
import json
from pathlib import Path
import sys
import time

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker
from app.db import build_engine
import app.worker as worker


def main():
    url, job_id, ready_path, gate_path = sys.argv[1:]
    engine = build_engine(url)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    def provider(title, body, attempt):
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO provider_calls (attempt) VALUES (:attempt)"), {"attempt": attempt})
            assert connection.scalar(text("SELECT status FROM jobs WHERE id = :id"), {"id": job_id}) == "running"
        return {"summary": "cross-process result", "skills": []}
    worker.analyze = provider
    Path(ready_path).touch()
    deadline = time.monotonic() + 15
    while not Path(gate_path).exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("Parent did not release worker gate")
        time.sleep(0.01)
    try:
        with factory() as session:
            print(json.dumps({"status": worker.process_job(session, job_id)}))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
