"""Engine construction and migrations (ADR-0005).

The engine is synchronous: FastAPI runs the sync endpoints in its threadpool, Alembic is
synchronous anyway, and one driver (psycopg 3) serves both. `migrate()` is what
`slas-api migrate` runs: Alembic to head under a Postgres advisory lock so two api replicas
starting together never race; SQLite (tests) has no such lock and needs none.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from alembic import command
from alembic.config import Config
from sqlalchemy import URL, Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from slas_observability.events import EventLog

#: `pg_advisory_lock` key: the bytes of "SLAS" as a 32-bit number; any fixed key works.
MIGRATION_LOCK_KEY: Final = 0x534C4153
MIGRATIONS_DIR: Final = Path(__file__).parent / "migrations"


def make_engine(url: URL) -> Engine:
    if url.get_backend_name() == "sqlite":
        # The threadpool hands one request to any thread; file-backed SQLite copes with that.
        return create_engine(url, connect_args={"check_same_thread": False})
    return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5)


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    """One transaction: commit on success, roll back on any error."""
    session = Session(engine, expire_on_commit=False)
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def alembic_config() -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    # Alembic's file template is only used by `revision`; kept explicit for future revisions.
    config.set_main_option("file_template", "r%%(rev)s_%%(slug)s")
    return config


def migrate(engine: Engine, log: EventLog | None = None) -> str:
    """Upgrade to head. Returns the dialect name for the caller's sentence."""
    config = alembic_config()
    dialect = engine.dialect.name
    with engine.connect() as connection:
        locked = False
        if dialect == "postgresql":
            connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": MIGRATION_LOCK_KEY})
            locked = True
        try:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            connection.commit()
        finally:
            if locked:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": MIGRATION_LOCK_KEY}
                )
                connection.commit()
    if log is not None:
        log.info("migrations.applied", dialect=dialect)
    return dialect


def postgres_ok(engine: Engine) -> bool:
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception:  # any driver error means "not ok"
        return False
    return True
