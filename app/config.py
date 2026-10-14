from dataclasses import dataclass
import os


@dataclass(frozen=True)
class Settings:
    database_url: str
    job_cost: int
    max_attempts: int


def load_settings() -> Settings:
    return Settings(
        database_url=os.getenv("DATABASE_URL", "sqlite:///./queuehire.db"),
        job_cost=int(os.getenv("JOB_COST", "3")),
        max_attempts=int(os.getenv("MAX_ATTEMPTS", "3")),
    )
