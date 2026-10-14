"""Reproduce before/after probes and verify a seeded 0001 upgrade.

The original challenge ZIP is used only inside a new temporary directory.
PostgreSQL runs in a uniquely named temporary schema in the supplied test DB.
No existing database, schema, or source directory is cleared or replaced.
"""
import argparse
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid
import zipfile

from sqlalchemy import create_engine, MetaData, select, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def benchmark_database(raw_url, directory):
    if raw_url is None:
        url = f"sqlite:///{directory / 'benchmark.db'}"
        engine = create_engine(url)
        try:
            yield "sqlite", url, engine
        finally:
            engine.dispose()
        return
    base = make_url(raw_url)
    if base.get_backend_name() != "postgresql":
        raise ValueError("--postgres-url must point to a disposable PostgreSQL test database")
    admin = create_engine(base)
    schema = "qh_bench_" + uuid.uuid4().hex
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    url = base.update_query_dict({"options": f"-csearch_path={schema}"}).render_as_string(hide_password=False)
    engine = create_engine(url)
    try:
        yield "postgres", url, engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def run(module, args, cwd, url):
    result = subprocess.run(
        [sys.executable, "-m", module, *args], cwd=cwd,
        env=dict(os.environ, DATABASE_URL=url), capture_output=True, text=True,
    )
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    return result.stdout + result.stderr


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("original_zip", type=Path)
    parser.add_argument("--postgres-url", default=None)
    parser.add_argument("--output", type=Path, default=ROOT / "docs/final-validation")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="qh_compare_") as name:
        directory = Path(name)
        with zipfile.ZipFile(args.original_zip) as archive:
            for member in archive.infolist():
                destination = (directory / member.filename).resolve()
                if not destination.is_relative_to(directory.resolve()):
                    raise ValueError("Archive member escapes the benchmark directory")
            archive.extractall(directory)
        original = directory / "backend-challenge"
        if not (original / "app/models.py").is_file():
            raise ValueError("Expected the original backend-challenge/ source ZIP")
        # Identical instrumentation, different application code and schema.
        shutil.copyfile(ROOT / "scripts/query_probe.py", original / "scripts/query_probe.py")
        with benchmark_database(args.postgres_url, directory) as (backend, url, engine):
            run("alembic", ["upgrade", "head"], original, url)
            run("scripts.seed", ["--large"], original, url)
            metadata = MetaData()
            metadata.reflect(engine)
            tables = {key: metadata.tables[key] for key in ["tenants", "api_keys", "jobs", "notes", "ledger"]}
            def snapshot():
                with engine.connect() as connection:
                    return {key: list(connection.execute(select(table).order_by(table.c.id))) for key, table in tables.items()}
            before = snapshot()
            alpha_id = next(row.id for row in before["tenants"] if row.name == "alpha")
            alpha_rows = [row for row in before["jobs"] if row.tenant_id == alpha_id]
            def probes(prefix, cwd):
                for case, flags in [("all", []), ("failed_offset40", ["--status", "failed", "--offset", "40"])]:
                    output = run("scripts.query_probe", ["--limit", "20", *flags], cwd, url)
                    (args.output / f"{prefix}_{backend}_{case}.txt").write_text(output)
                    print(f"{prefix}_{backend}_{case}: " + " | ".join(output.splitlines()[:4]))
                    if prefix == "after":
                        expected = len(alpha_rows) if case == "all" else sum(row.status == "failed" for row in alpha_rows)
                        actual = int(output.split(" total=", 1)[1].splitlines()[0])
                        if actual != expected:
                            raise AssertionError(f"List total {actual} differs from preserved row oracle {expected}")
            if backend == "postgres":
                with engine.begin() as connection:
                    connection.execute(text("ANALYZE jobs"))
                    connection.execute(text("ANALYZE notes"))
            probes("before", original)
            upgrade = run("alembic", ["upgrade", "head"], ROOT, url)
            if snapshot() != before:
                raise AssertionError("Upgrade changed historical fields or rows")
            counts = {key: len(rows) for key, rows in before.items()}
            if backend == "sqlite":
                with engine.connect() as connection:
                    assert not connection.execute(text("PRAGMA foreign_key_check")).all()
            else:
                with engine.begin() as connection:
                    connection.execute(text("ANALYZE jobs"))
                    connection.execute(text("ANALYZE notes"))
            check = run("alembic", ["check"], ROOT, url)
            (args.output / f"upgrade_{backend}.txt").write_text(
                upgrade + f"All historical fields preserved: {counts}\n" + check,
            )
            probes("after", ROOT)
            if backend == "postgres":
                for case, flags in [("all", []), ("failed", ["--status", "failed"])]:
                    output = run("scripts.explain_list", flags, ROOT, url)
                    (args.output / f"explain_{case}_postgres.txt").write_text(output)
            print(f"Upgrade {backend} preserved {counts}; Alembic check succeeded")


if __name__ == "__main__":
    main()
