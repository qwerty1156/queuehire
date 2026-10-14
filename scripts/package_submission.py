"""Package source code and the report without local databases or credentials."""
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DIRECTORIES = ["app", "tests", "migrations", "scripts", "docs", ".github"]
FILES = [
    "README.md", "TASK.md", "REPORT.md", "requirements.txt", "requirements-lock.txt",
    "pyproject.toml", "alembic.ini", "Dockerfile", "compose.yaml", ".dockerignore",
    ".gitignore", ".env.example", "BASELINE.md",
]


def main():
    report = ROOT / "REPORT.md"
    if not report.is_file():
        raise SystemExit("Create REPORT.md from docs/REPORT_TEMPLATE.md first.")
    paths = []
    for directory in DIRECTORIES:
        for path in sorted((ROOT / directory).rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            if "__pycache__" in path.parts or ".pytest_cache" in path.parts:
                continue
            if path.suffix in {".pyc", ".db", ".sqlite", ".sqlite3"} or path.name == ".env":
                continue
            paths.append(path)
    paths.extend(ROOT / name for name in FILES if (ROOT / name).is_file())
    output = ROOT / "submission.zip"
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            archive.write(path, path.relative_to(ROOT))
    print(f"Created {output.name}: {len(paths)} files")
    print("Check the archive before sharing; source files must not contain real credentials.")


if __name__ == "__main__":
    main()
